"""M2 — closed-loop LLM query optimiser (multi-armed bandit over seeds).

The skip-rebuild loop in `search_jobs._maybe_auto_rebuild_profile` optimises
PRECISION and needs SENT jobs to fire — a user with zero sends never auto-
tunes. This module closes the OTHER loop: it reads the per-query FUNNEL yield
recorded in the `query_runs` table (fetched → scored → matched_ge4 → queued →
sent, see telemetry/schema.py) over a recent, time-decayed window and asks an
Opus call to:

  * KEEP queries that are productive (high matched_ge4 / sent),
  * PRUNE queries that are dead (0 matched_ge4 over >= K runs), and
  * PROPOSE new / mutated variants (synonyms, adjacent titles, OTHER
    LANGUAGES, ATS `site:` targets) under an EXPLORE QUOTA so the arm set
    never collapses to the current local optimum.

The LLM is the MUTATION OPERATOR (it decides synonyms / translations /
adjacencies — never a Python token table, per CLAUDE.md's AI-first
principle); the TELEMETRY decides what survives (productive arms are fed back
as "keep", dead arms as "prune candidates"). The optimiser then persists the
updated `search_seeds` back into the user's profile WITHOUT clobbering any
unrelated profile field.

COLD START (explicit, tested): the reward signal is the `matched_ge4` /
`scored` funnel stage, NOT only `sent`. A 0-send user still produces
scored/matched counts, so a query that surfaces RELEVANT-but-unsent postings
is rewarded (kept / mutated) while one that surfaces nothing scorable is
flagged for pruning. The cold-start path is what makes this useful for the
exact users the skip loop cannot reach.

Safety / integration:
  * Gated behind `defaults.query_optimizer_enabled` (default False). The
    telemetry + reward layers record unconditionally; only this mutation
    step is gated.
  * Fires on its OWN cadence (`query_optimizer_min_interval_s`), independent
    of the skip counter, tracked via an `ops_toggles` marker per user.
  * Never raises into the search loop — every public entry point returns an
    `OptimizeResult` and logs on failure.
  * Tests inject `_run_p=` (a stub) so the real `claude -p` is never called.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import forensic
from claude_cli import extract_assistant_text, parse_json_block
from instrumentation.wrappers import wrapped_run_p

log = logging.getLogger(__name__)

# Opus is the mutation operator: low-volume, high-leverage, must reason about
# synonyms / translations / adjacent role titles in the candidate's context.
DEFAULT_MODEL = "opus"

# Hard cap on the LinkedIn query set the optimiser may emit, mirroring
# `profile_builder._MAX_LINKEDIN_QUERIES` so the merged profile still passes
# `profile_builder.validate_linkedin_seeds`. Imported lazily to avoid a hard
# dependency cycle at module import time.
def _max_linkedin_queries() -> int:
    try:
        from profile_builder import _MAX_LINKEDIN_QUERIES
        return int(_MAX_LINKEDIN_QUERIES)
    except Exception:
        return 10


# ops_toggles key for the per-user last-optimisation timestamp marker. One
# row per user keyed by chat_id keeps the cadence gate durable across
# process restarts without a new table.
def _marker_key(chat_id: int) -> str:
    return f"query_optimizer:last_run:{int(chat_id)}"


@dataclass
class OptimizeResult:
    """Outcome of one optimisation pass. Never raised — always returned."""
    status: str                       # ok | disabled | cadence_skip | no_reward |
                                      # no_profile | cli_error | parse_error | no_change | error
    kept: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    web_seed_phrases: Optional[list[str]] = None
    reason: str = ""


def _instrumented_run_p(prompt: str, **kwargs) -> Optional[str]:
    """Default LLM caller — records the call under caller='query_optimizer'."""
    return wrapped_run_p(None, "query_optimizer", prompt, **kwargs)


# ---------- reward → prompt ----------

def _split_linkedin_queries(li_seeds: dict | None) -> tuple[list[str], dict]:
    """Return (current_query_strings, carry) from a search_seeds.linkedin block.

    `carry` preserves the non-query scaffolding (geos / f_TPR for the
    SEPARATED shape) so we can re-emit it untouched. Handles BOTH the
    SEPARATED (list-of-str) and PAIRED (list-of-dict) shapes that
    `profile_builder.validate_linkedin_seeds` accepts; PAIRED is normalised
    to the SEPARATED query-string list (its per-entry geo/f_TPR is collapsed
    into the carry's geos set so nothing is lost).
    """
    li = li_seeds if isinstance(li_seeds, dict) else {}
    queries = li.get("queries")
    carry: dict = {}
    if not isinstance(queries, list) or not queries:
        # No queries yet — carry whatever geos/f_TPR exist for re-emit.
        if isinstance(li.get("geos"), list):
            carry["geos"] = list(li["geos"])
        if isinstance(li.get("f_TPR"), str):
            carry["f_TPR"] = li["f_TPR"]
        return [], carry

    first = queries[0]
    if isinstance(first, str):
        q_strs = [q for q in queries if isinstance(q, str)]
        if isinstance(li.get("geos"), list):
            carry["geos"] = list(li["geos"])
        if isinstance(li.get("f_TPR"), str):
            carry["f_TPR"] = li["f_TPR"]
        return q_strs, carry

    # PAIRED legacy shape: [{"q":..., "geo":..., "f_TPR":...}, ...]
    q_strs = []
    geos: list[str] = []
    f_tpr = None
    for entry in queries:
        if not isinstance(entry, dict):
            continue
        qq = entry.get("q")
        if isinstance(qq, str) and qq:
            q_strs.append(qq)
        g = entry.get("geo")
        if isinstance(g, str) and g and g not in geos:
            geos.append(g)
        if f_tpr is None and isinstance(entry.get("f_TPR"), str):
            f_tpr = entry["f_TPR"]
    if geos:
        carry["geos"] = geos
    if f_tpr:
        carry["f_TPR"] = f_tpr
    return q_strs, carry


def _build_prompt(
    *,
    profile: dict,
    reward_rows: list[dict],
    prune_candidates: set[str],
    explore_quota: float,
    max_queries: int,
) -> str:
    """Render the optimiser prompt.

    The prompt hands the model the candidate's role + current queries + the
    per-query funnel reward (decayed AND raw, so the cold-start "0 sent but N
    scored" case is visible) and asks for a strict-JSON decision. ALL fit /
    synonym / translation reasoning is delegated to the model — there are no
    Python token lists here.
    """
    role = str(profile.get("primary_role") or "").strip()
    stack = profile.get("stack_primary") or profile.get("stack") or ""
    if isinstance(stack, list):
        stack = ", ".join(str(s) for s in stack)
    focus = ""
    ws = ((profile.get("search_seeds") or {}).get("web_search") or {})
    if isinstance(ws, dict):
        focus = str(ws.get("focus_notes") or "")

    lines = []
    for r in reward_rows:
        flag = " [PRUNE-CANDIDATE: dead]" if r["query"] in prune_candidates else ""
        lines.append(
            f"  - source={r['source_key']} query={r['query_raw']!r} "
            f"runs={r['runs']} "
            f"fetched~{r['fetched']:.1f} scored~{r['scored']:.1f} "
            f"matched>=floor~{r['matched_ge4']:.1f} sent~{r['sent']:.1f} "
            f"(raw: scored={r['raw_scored']} matched={r['raw_matched_ge4']} "
            f"sent={r['raw_sent']}){flag}"
        )
    reward_block = "\n".join(lines) if lines else "  (no per-query telemetry yet)"

    explore_pct = int(round(max(0.0, min(1.0, explore_quota)) * 100))

    return f"""You are the SEARCH-QUERY OPTIMISER for a job-alert bot. You tune the
LinkedIn search queries (and the web_search seed phrases) for one candidate
to MAXIMISE the number of RELEVANT job postings surfaced.

You are a multi-armed bandit's mutation operator. Each query is an "arm".
Telemetry below tells you how each arm performed across recent search runs,
through a funnel that narrows left to right:

  fetched      -> raw postings the query returned
  scored       -> of those, how many the relevance scorer evaluated
  matched>=floor-> of those, how many scored at/above the user's match bar
  sent         -> of those, how many were actually delivered

COLD-START RULE (critical): a query is GOOD if it produces postings that
SCORE and MATCH, EVEN IF NONE WERE SENT YET. Sends can be zero for reasons
unrelated to the query (quiet buffer, night mute, dedupe). Judge an arm
PRIMARILY by `matched>=floor` and `scored`, and only secondarily by `sent`.
NEVER prune an arm that has matched>=floor > 0.

CANDIDATE
  primary_role: {role!r}
  stack: {str(stack)[:300]!r}
  web_search focus_notes: {focus[:300]!r}

CURRENT QUERY PERFORMANCE (decayed over the recent window; ~ marks decayed
floats, raw integers in parens):
{reward_block}

YOUR JOB
1. KEEP every arm with matched>=floor > 0 (productive). List them verbatim.
2. PRUNE arms flagged [PRUNE-CANDIDATE] (dead: enough runs, zero matches).
   You MAY also keep a dead arm if you believe it is strategically important
   and just needs time, but justify it by keeping — do not invent new flags.
3. PROPOSE NEW or MUTATED query variants. This is where your judgement
   matters: synonyms, adjacent role titles, OTHER LANGUAGES the candidate's
   target market uses (translate the role into the local language when the
   geo warrants it), seniority variants, and ATS `site:` targets for
   web_search. At least {explore_pct}% of your FINAL linkedin query list must
   be NEW arms not present above (the EXPLORE QUOTA) so the search never gets
   stuck on a local optimum.

CONSTRAINTS
  - Emit AT MOST {max_queries} linkedin queries total (keep + new).
  - linkedin queries are plain search strings (e.g. "react developer remote",
    "desarrollador frontend"); do NOT include geo or date filters in them.
  - web_search seed_phrases: 3-8 short phrases; you may translate / localise.

OUTPUT — STRICT JSON, no prose, no markdown fences:
{{
  "linkedin_queries": ["<kept and new query strings>"],
  "web_search_seed_phrases": ["<phrase>", ...],
  "kept": ["<subset of linkedin_queries that existed before>"],
  "pruned": ["<old queries you dropped>"],
  "added": ["<new linkedin queries you introduced>"],
  "rationale": "<one sentence>"
}}
"""


# ---------- merge + persist ----------

def _merge_into_profile(
    profile: dict,
    *,
    linkedin_queries: list[str],
    web_seed_phrases: Optional[list[str]],
    carry: dict,
    max_queries: int,
) -> dict:
    """Return a NEW profile dict with the optimised search_seeds spliced in.

    Only `search_seeds.linkedin.queries` (+ carried geos/f_TPR) and
    `search_seeds.web_search.seed_phrases` are touched. Every other profile
    field (primary_role, min_match_score, enabled_sources, bookkeeping, …) is
    copied through unchanged so the optimiser never clobbers unrelated state.
    """
    new = dict(profile)  # shallow copy of top level
    seeds = dict(new.get("search_seeds") or {})

    # LinkedIn block: SEPARATED shape (list of query strings) + carried
    # geos / f_TPR. Clamp to the validator's cap.
    li = dict(seeds.get("linkedin") or {})
    clean_q: list[str] = []
    seen: set[str] = set()
    for q in linkedin_queries:
        if not isinstance(q, str):
            continue
        qn = " ".join(q.split())
        if not qn or qn.lower() in seen:
            continue
        seen.add(qn.lower())
        clean_q.append(qn)
        if len(clean_q) >= max_queries:
            break
    li["queries"] = clean_q
    if "geos" in carry:
        li["geos"] = carry["geos"]
    if "f_TPR" in carry:
        li["f_TPR"] = carry["f_TPR"]
    seeds["linkedin"] = li

    if web_seed_phrases is not None:
        ws = dict(seeds.get("web_search") or {})
        clean_phrases = [
            " ".join(str(p).split())
            for p in web_seed_phrases
            if isinstance(p, str) and str(p).strip()
        ]
        if clean_phrases:
            ws["seed_phrases"] = clean_phrases[:8]
            seeds["web_search"] = ws

    new["search_seeds"] = seeds
    new["last_query_optimized_at"] = time.time()
    return new


# ---------- public entry point ----------

def optimize_queries(
    db: Any,
    store: Any,
    chat_id: int,
    filters: dict,
    *,
    now: Optional[float] = None,
    _run_p: Callable = _instrumented_run_p,
) -> OptimizeResult:
    """Run one query-optimisation pass for `chat_id`. Never raises.

    Returns an `OptimizeResult` describing what was kept / pruned / added.
    Persists the updated `search_seeds` via `db.set_user_profile` on success.

    Tests inject `_run_p=` to avoid the real CLI; pass a stub returning a
    JSON string (the function parses it the same way the live path parses
    the CLI envelope's assistant text).
    """
    if now is None:
        now = time.time()
    try:
        return _optimize_queries_inner(
            db, store, chat_id, filters, now=now, _run_p=_run_p,
        )
    except Exception as e:  # absolute belt-and-braces — never break a run
        log.exception("optimize_queries: unexpected failure for chat=%s", chat_id)
        return OptimizeResult(status="error", reason=repr(e)[:200])


def _optimize_queries_inner(
    db: Any,
    store: Any,
    chat_id: int,
    filters: dict,
    *,
    now: float,
    _run_p: Callable,
) -> OptimizeResult:
    if not bool(filters.get("query_optimizer_enabled", False)):
        return OptimizeResult(status="disabled", reason="flag off")

    # Cadence gate — at most once per interval per user, durable via
    # ops_toggles. A None/zero interval disables the gate (test convenience).
    interval = float(filters.get("query_optimizer_min_interval_s") or 0)
    if interval > 0 and store is not None:
        try:
            last = float(store.get_toggle(_marker_key(chat_id), "0") or 0)
        except Exception:
            last = 0.0
        if last and (now - last) < interval:
            return OptimizeResult(
                status="cadence_skip",
                reason=f"{int(now - last)}s since last < {int(interval)}s",
            )

    raw_profile = db.get_user_profile(chat_id)
    if not raw_profile:
        return OptimizeResult(status="no_profile", reason="empty profile")
    try:
        profile = json.loads(raw_profile)
    except (TypeError, ValueError):
        return OptimizeResult(status="no_profile", reason="profile not JSON")
    if not isinstance(profile, dict):
        return OptimizeResult(status="no_profile", reason="profile not an object")

    window_s = float(filters.get("query_optimizer_window_s") or (14 * 86400))
    half_life_s = float(filters.get("query_optimizer_half_life_s") or (7 * 86400))
    reward_rows = store.query_yield_window(
        chat_id,
        since_ts=now - window_s,
        half_life_s=half_life_s,
        now=now,
    )
    if not reward_rows:
        # Nothing to learn from yet — leave the profile untouched but stamp
        # the marker so we don't re-check every iteration.
        _stamp_marker(store, chat_id, now)
        return OptimizeResult(status="no_reward", reason="no query_runs yet")

    # Flag dead arms: enough runs AND zero raw matches across the window.
    try:
        min_runs = int(filters.get("query_optimizer_min_runs_before_prune") or 3)
    except (TypeError, ValueError):
        min_runs = 3
    prune_candidates = {
        r["query"]
        for r in reward_rows
        if r["runs"] >= min_runs and r["raw_matched_ge4"] == 0
    }

    explore_quota = float(filters.get("query_optimizer_explore_quota") or 0.3)
    max_queries = _max_linkedin_queries()
    timeout_s = int(filters.get("query_optimizer_timeout_s") or 180)

    # Current linkedin query strings — used to classify kept-vs-added when
    # the LLM omits or miscategorises those fields.
    li_seeds = (profile.get("search_seeds") or {}).get("linkedin")
    current_queries, carry = _split_linkedin_queries(li_seeds)
    current_norm = {" ".join(q.split()).lower() for q in current_queries}

    with forensic.step(
        "query_optimizer.optimize_queries",
        input={
            "chat_id": chat_id,
            "reward_arms": len(reward_rows),
            "prune_candidates": sorted(prune_candidates)[:10],
            "current_queries": current_queries,
            "explore_quota": explore_quota,
        },
        chat_id=chat_id,
    ) as fctx:
        prompt = _build_prompt(
            profile=profile,
            reward_rows=reward_rows,
            prune_candidates=prune_candidates,
            explore_quota=explore_quota,
            max_queries=max_queries,
        )
        try:
            stdout = _run_p(prompt, timeout_s=timeout_s, model=DEFAULT_MODEL)
        except Exception as e:
            fctx.set_output({"status": "cli_error", "reason": repr(e)[:200]})
            return OptimizeResult(status="cli_error", reason=repr(e)[:200])
        if not stdout:
            fctx.set_output({"status": "cli_error", "reason": "run_p None/empty"})
            return OptimizeResult(status="cli_error", reason="run_p returned None/empty")

        body = extract_assistant_text(stdout)
        parsed = parse_json_block(body)
        if not isinstance(parsed, dict):
            fctx.set_output({"status": "parse_error", "body_head": (body or "")[:200]})
            return OptimizeResult(
                status="parse_error", reason=f"unparseable: {(body or '')[:120]!r}",
            )

        new_queries = parsed.get("linkedin_queries")
        if not isinstance(new_queries, list):
            new_queries = []
        new_queries = [q for q in new_queries if isinstance(q, str) and q.strip()]

        web_phrases = parsed.get("web_search_seed_phrases")
        if not isinstance(web_phrases, list):
            web_phrases = None

        # Derive kept / added / pruned from the actual emitted set vs the
        # current set — we trust the telemetry-vs-emitted diff over the LLM's
        # self-reported buckets (it may mislabel).
        emitted_norm = {" ".join(q.split()).lower(): q for q in new_queries}
        kept = [q for n, q in emitted_norm.items() if n in current_norm]
        added = [q for n, q in emitted_norm.items() if n not in current_norm]
        pruned = [q for q in current_queries
                  if " ".join(q.split()).lower() not in emitted_norm]

        if not new_queries:
            # Defensive: a non-empty current set but an empty emitted set is
            # almost always an LLM error — refuse to wipe the user's queries.
            _stamp_marker(store, chat_id, now)
            fctx.set_output({"status": "no_change", "reason": "empty emitted set"})
            return OptimizeResult(
                status="no_change", reason="LLM emitted no queries; profile untouched",
            )

        merged = _merge_into_profile(
            profile,
            linkedin_queries=new_queries,
            web_seed_phrases=web_phrases,
            carry=carry,
            max_queries=max_queries,
        )

        try:
            db.set_user_profile(chat_id, json.dumps(merged, ensure_ascii=False))
        except Exception as e:
            log.exception("optimize_queries: set_user_profile failed chat=%s", chat_id)
            fctx.set_output({"status": "error", "reason": f"persist failed: {e!r}"[:200]})
            return OptimizeResult(status="error", reason=f"persist failed: {e!r}"[:200])

        _stamp_marker(store, chat_id, now)
        result = OptimizeResult(
            status="ok",
            kept=kept,
            pruned=pruned,
            added=added,
            web_seed_phrases=(web_phrases if isinstance(web_phrases, list) else None),
            reason=str(parsed.get("rationale") or "")[:200],
        )
        fctx.set_output({
            "status": "ok",
            "kept": kept,
            "pruned": pruned,
            "added": added,
            "final_query_count": len((merged.get("search_seeds") or {}).get("linkedin", {}).get("queries") or []),
        })
        log.info(
            "query_optimizer: chat=%s kept=%d pruned=%d added=%d (%s)",
            chat_id, len(kept), len(pruned), len(added), result.reason,
        )
        return result


def _stamp_marker(store: Any, chat_id: int, now: float) -> None:
    """Best-effort: record the last-optimisation timestamp for the cadence gate."""
    if store is None:
        return
    try:
        store.set_toggle(_marker_key(chat_id), str(float(now)))
    except Exception:
        log.debug("query_optimizer: marker stamp failed for chat=%s", chat_id,
                  exc_info=True)
