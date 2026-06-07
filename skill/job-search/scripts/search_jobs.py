#!/usr/bin/env python3
"""Daily job-alert orchestrator (multi-user, DB-backed, inline buttons).

Flow:
  1. Load operational defaults (defaults.DEFAULTS) and .env
  2. Fan out to enabled source adapters (global pass: HN + remote boards +
     curated boards). LinkedIn + web_search run PER-USER inside the recipient
     loop because they need profile.search_seeds to shape queries.
  3. Upsert each posting into the jobs table
  4. For every registered user (who has uploaded a resume):
       a. Skip postings they've already been sent OR already actioned
       b. AI-score each remaining posting against the user's resume + profile
       c. Drop postings below the user's ⭐ floor (or default ai_min_match_score)
       d. Send each remaining posting as its own Telegram message with buttons
       e. Log sent_messages(chat_id, message_id → job_id)

Matching is per-user. There is no global keyword/title/location filter — the
AI score is the sole matching gate.

Run from the project root:
    python skill/job-search/scripts/search_jobs.py
    python skill/job-search/scripts/search_jobs.py --dry-run
    python skill/job-search/scripts/search_jobs.py --chat-id 123456789  # single user

Exit codes:
    0 success · 1 bad config · 2 partial (some source errors) · 3 hard fail
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dedupe import Job, JobStore, dedupe_cross_source      # noqa: E402
from db import DB, profile_hash as _profile_hash           # noqa: E402
from defaults import DEFAULTS                              # noqa: E402
from telegram_client import TelegramClient, send_per_job_digest  # noqa: E402
from sources import (                                      # noqa: E402
    hackernews, remote_boards, linkedin, curated_boards, web_search,
    reliefweb, euraxess, un_careers, math_ku_phd, ub_doctoral,
    # Wave 2 sources (added 2026-05-01). 10 live + 3 blocked-stubs.
    # Toggle each independently in defaults.py.
    eures, infojobs, tecnoempleo, ai_jobs_net, jobs_ac_uk,
    academicpositions, ikerbasque, wellfound, ycombinator_was, wttj,
    builtin, impactpool, devex,
    # EU frontend-focused sources (added 2026-05-06).
    justjoinit, nofluffjobs,
)
from telemetry import MonitorStore                         # noqa: E402
from instrumentation import pipeline_run, source_run, error_capture  # noqa: E402
from ops.alerts import deliver_alert                       # noqa: E402
from ops.summary import deliver_daily_summary              # noqa: E402
import forensic                                             # noqa: E402
from log_ttl import cleanup_logs                            # noqa: E402
# `linkedin` and `web_search` are imported but NOT in the global SOURCES
# dispatch below — they only run per-user inside the recipient loop, using
# each profile's stored seeds. See run() below.
from job_enrich import (                                   # noqa: E402
    enrich_jobs_ai,
    by_job_id,
    reanalyze_scoring_ai,
)
from user_profile import (                                 # noqa: E402
    profile_from_json,
    is_empty_profile,
    effective_filters,
    project_to_prefs,
)
import user_files                                          # noqa: E402
import pig_stickers as _pigs                               # noqa: E402

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

log = logging.getLogger("job-search")


SOURCES = {
    "hackernews":      hackernews,
    "remote_boards":   remote_boards,
    "curated_boards":  curated_boards,
    "reliefweb":       reliefweb,
    "euraxess":        euraxess,
    "un_careers":      un_careers,
    "math_ku_phd":     math_ku_phd,
    "ub_doctoral":     ub_doctoral,
    # Wave 2 sources. Each adapter ships default-OFF in defaults.py — the
    # operator opts each in independently after weighing yield vs cost
    # (HTML-scrape adapters have higher fragility / Cloudflare risk than
    # RSS adapters).
    "eures":            eures,            # blocked: EU Login required, stub
    "infojobs":         infojobs,         # Spain HTML scrape
    "tecnoempleo":      tecnoempleo,      # Spain tech RSS
    "ai_jobs_net":      ai_jobs_net,      # Curated AI/ML HTML
    "jobs_ac_uk":       jobs_ac_uk,       # UK/EU academic RSS
    "academicpositions": academicpositions,  # blocked: Cloudflare BFM, stub
    "ikerbasque":       ikerbasque,       # Basque research HTML
    "wellfound":        wellfound,        # blocked: DataDome, stub
    "ycombinator_was":  ycombinator_was,  # YC startups JSON
    "wttj":             wttj,             # Welcome to the Jungle Algolia
    "builtin":          builtin,          # US tech HTML
    "impactpool":       impactpool,       # UN/NGO HTML (researcher-friendly)
    "devex":            devex,            # International dev (Claude CLI)
    "justjoinit":       justjoinit,       # Polish/CEE tech JSON API
    "nofluffjobs":      nofluffjobs,      # Polish/CEE tech JSON API
    # `linkedin` and `web_search` are PER-USER only (run inside the recipient
    # loop with profile.search_seeds), so they're deliberately absent here.
}


def project_root() -> Path:
    return HERE.parent.parent.parent


def load_env() -> None:
    if load_dotenv:
        load_dotenv(project_root() / ".env")
    else:
        env_path = project_root() / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# ---------- helpers ----------

def _is_in_night_mute_window(
    now: datetime.datetime | None = None,
    *,
    tz_name: str = "Europe/Madrid",
    start_hour: int = 23,
    end_hour: int = 9,
) -> bool:
    """Return True iff current local time in ``tz_name`` is inside the
    [``start_hour``, ``end_hour``) night-mute window.

    The window wraps midnight when ``start_hour > end_hour`` (the common
    Madrid 23 → 09 case = mute from 23:00 through 08:59:59, resume at
    09:00). When ``start_hour < end_hour`` it is a same-day window. When
    ``start_hour == end_hour`` the window is empty and this returns False
    for any ``now`` (feature disabled).

    ``end_hour`` is exclusive, ``start_hour`` is inclusive — i.e. exactly
    09:00 in the default config is NOT muted; exactly 23:00 IS muted.

    ``now`` defaults to ``datetime.datetime.now(tz=UTC)``; tests inject a
    frozen value. The function converts to the target tz before comparing
    hours, so tz-naive callers and tz-aware callers behave identically.

    Fails open on bad tz: if ``zoneinfo`` can't load ``tz_name`` we log a
    WARNING and return False (never mute) — a typo in defaults.py must
    not muzzle the user forever.
    """
    if start_hour == end_hour:
        return False
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        log.warning(
            "_is_in_night_mute_window: unknown timezone %r; "
            "failing open (no mute)", tz_name,
        )
        return False

    if now is None:
        now = datetime.datetime.now(tz=datetime.timezone.utc)
    elif now.tzinfo is None:
        # Treat a naive datetime as UTC (matches the default branch above)
        # rather than silently interpreting it as wall-clock in tz_name.
        now = now.replace(tzinfo=datetime.timezone.utc)
    local_hour = now.astimezone(tz).hour

    if start_hour < end_hour:
        # Same-day window: [start_hour, end_hour).
        return start_hour <= local_hour < end_hour
    # Wraps midnight: hour is in the window if it's >= start OR < end.
    return local_hour >= start_hour or local_hour < end_hour


def _decide_buffer_flush(
    db,
    chat_id: int,
    alive_floor: list[Job],
    enrichments_by_job_id: dict[str, dict],
    profile_hash: str,
    filters: dict,
) -> tuple[list[Job], bool, int, float]:
    """Quality-buffer enqueue + flush decision (algorithm v2.6, P1).

    1. Purge stale-profile queued rows (silently invalidated by a resume
       or prefs edit since they were enqueued).
    2. Enqueue every (job_id, score) in `alive_floor` — these already
       cleared the score floor AND the send-time prefilter, so they're
       fit to deliver. `enqueue_match` is INSERT OR IGNORE: re-runs on
       the same posting don't reset its `queued_at` clock.
    3. Compute depth + oldest_age under the current profile_hash.
    4. Flush when EITHER depth >= ``quality_send_threshold`` OR
       oldest_age >= ``max_queue_latency_hours`` (the latency cap).
       The night-mute window (``night_mute_*`` knobs, P7) overrides
       this back to hold during the operator's quiet hours — applies
       to BOTH the threshold-flush AND the age-flush.
    5. Whether or not a flush fires, scan the current queue for rows
       that the rehydration step would silently drop — already-handled
       jobs (in `applications`), already-sent jobs (in `sent_messages`,
       e.g. delivered by a parallel run), or rows whose backing `jobs`
       entry has been pruned. Purge those leak ids from `queued_matches`
       immediately so they don't inflate depth or get reported as
       "ancient" by `queue_oldest_age_seconds` forever (empty-flush
       loop). This MUST run on the hold path too — a stale queue gets
       cleaned the moment it's noticed.
    6. On flush: rehydrate `Job` dataclasses from the surviving rows,
       carrying the queue ordering (match_score DESC, queued_at ASC).
       On hold: return [], flush=False.

    Returns ``(jobs_to_send, flush, depth, oldest_age)``. When
    ``flush=False``, ``jobs_to_send`` is always ``[]``. ``depth`` is
    reported PRE-leak-purge (the value the flush decision was made on)
    so observability stays consistent with the decision branch taken.
    """
    db.purge_stale_queue(chat_id, profile_hash)
    for j in alive_floor:
        score = int(
            (enrichments_by_job_id.get(j.job_id) or {}).get("match_score") or 0
        )
        db.enqueue_match(chat_id, j.job_id, profile_hash, score)

    depth = db.queue_depth(chat_id, profile_hash)
    oldest_age = db.queue_oldest_age_seconds(chat_id, profile_hash) or 0.0

    try:
        threshold = int(filters.get("quality_send_threshold", 5))
    except (TypeError, ValueError):
        threshold = 5
    try:
        max_latency_s = float(filters.get("max_queue_latency_hours", 48)) * 3600
    except (TypeError, ValueError):
        max_latency_s = 48 * 3600.0

    flush = depth >= threshold or (depth > 0 and oldest_age >= max_latency_s)

    # Night-mute override (P7). If the operator's quiet-hours window is
    # active in the configured tz, hold this iteration regardless of
    # depth/age — the next iteration after the window ends will flush.
    # This covers BOTH branches of the flush decision above: a single
    # `flush = False` here cancels the threshold-met flush AND the age
    # latency-cap flush. The leak-purge sweep below still runs (we hand
    # back to it via the normal `flush` variable), and `purge_stale_queue`
    # / `enqueue_match` above already ran — night-mute only flips the
    # "do we send now?" verdict.
    if flush:
        tz_name = filters.get("night_mute_tz", "Europe/Madrid")
        try:
            start_h = int(filters.get("night_mute_start_hour", 23))
            end_h = int(filters.get("night_mute_end_hour", 9))
        except (TypeError, ValueError):
            start_h, end_h = 23, 9
        if start_h != end_h and _is_in_night_mute_window(
            tz_name=tz_name, start_hour=start_h, end_hour=end_h,
        ):
            log.info(
                "User %s: quality-buffer flush would fire (depth=%d, "
                "oldest_age=%.1fh) but night-mute window active "
                "(%02d:00-%02d:00 %s) — holding until morning",
                chat_id, depth, (oldest_age or 0) / 3600.0,
                start_h, end_h, tz_name,
            )
            flush = False

    # Snapshot the queue so we can both (a) decide what to ship on flush
    # and (b) detect leak rows that no longer correspond to a deliverable
    # `Job`. We always run the leak sweep — even on hold — so the queue
    # self-heals between iterations.
    queued_rows = db.fetch_queue(chat_id, profile_hash)
    if not queued_rows:
        return [], False, depth, oldest_age

    ids = [r["job_id"] for r in queued_rows]
    row_by_id = db.get_jobs_by_ids(ids)
    handled = db.handled_job_ids(chat_id)
    # Bulk lookup — single SELECT instead of N row probes. Includes any
    # row delivered by a parallel run since this iteration started, so
    # we never re-issue a card that's already in the user's chat.
    already_sent = db.user_seen_jobs(chat_id, ids)

    leak_ids: list[str] = []
    deliverable_rows: list[dict] = []
    for entry in queued_rows:
        jid = entry["job_id"]
        if jid in handled:
            # User already actioned this — applied / skipped / interested.
            leak_ids.append(jid)
            continue
        if jid in already_sent:
            # Delivered already (parallel run or previous flush that
            # confirmed `on_sent` but failed mid-`clear_queue`). Drop so
            # the user never sees the same card twice.
            leak_ids.append(jid)
            continue
        row = row_by_id.get(jid)
        if row is None:
            # `jobs` row pruned underneath us (TTL eviction, manual
            # delete). Nothing to ship; clear the dangling pointer.
            leak_ids.append(jid)
            continue
        deliverable_rows.append({"entry": entry, "row": row})

    if leak_ids:
        try:
            db.clear_queue(chat_id, leak_ids)
        except Exception:
            log.debug("clear_queue leak-purge failed; continuing",
                      exc_info=True)

    if not flush:
        return [], False, depth, oldest_age

    # Carry the queue's ordering (match_score DESC, queued_at ASC) into
    # the Job list so the digest reaches the user in the same priority
    # order the buffer accumulated.
    jobs_to_send: list[Job] = []
    for d in deliverable_rows:
        row = d["row"]
        jobs_to_send.append(
            Job(
                source=row["source"],
                external_id=row["external_id"] or "",
                title=row["title"] or "",
                company=row["company"] or "",
                location=row["location"] or "",
                url=row["url"] or "",
                posted_at=row["posted_at"] or "",
                snippet=row["snippet"] or "",
                salary=row["salary"] or "",
            )
        )
    return jobs_to_send, True, depth, oldest_age


def _score_histogram(enrichments_by_job_id: dict[str, dict]) -> dict[str, int]:
    """Score histogram for forensic logging — { '0': 3, '1': 2, ... '5': 1 }."""
    hist: dict[str, int] = {str(i): 0 for i in range(6)}
    hist["unknown"] = 0
    for v in enrichments_by_job_id.values():
        try:
            s = int((v or {}).get("match_score") or 0)
        except (TypeError, ValueError):
            hist["unknown"] += 1
            continue
        if 0 <= s <= 5:
            hist[str(s)] += 1
        else:
            hist["unknown"] += 1
    return hist


def _record_query_funnel(
    store,
    *,
    pipeline_run_id: int | None,
    chat_id: int,
    attribution: dict[str, str],
    enrichments_by_job_id: dict[str, dict],
    match_floor: int,
    queued_job_ids: set[str],
    sent_job_ids: set[str],
    started_at: float,
    finished_at: float,
) -> None:
    """Roll the per-job query attribution up into `query_runs` funnel rows
    (M2 component 1).

    For each originating query string in `attribution`, count the jobs that
    flowed through each funnel stage:

      fetched     all jobs the adapter attributed to this query
      scored      of those, jobs the AI scorer produced a verdict for
      matched_ge4 of those, jobs whose score >= `match_floor`
      queued      of those, jobs that entered the quality buffer this run
      sent        of those, jobs actually delivered this run

    One `query_runs` row is written per distinct query for this
    (pipeline_run, chat_id). Source key is inferred from the query string
    shape: web_search keys are prefixed ``web_search:`` (see
    `web_search._web_search_query_key`); everything else is a LinkedIn
    query. Best-effort: a failure logs and returns — telemetry must never
    break a search run.
    """
    if store is None or not attribution:
        return
    try:
        per_query: dict[str, dict] = {}
        for job_id, query in attribution.items():
            src = "web_search" if str(query).startswith("web_search:") else "linkedin"
            slot = per_query.setdefault(
                query,
                {"source_key": src, "fetched": 0, "scored": 0,
                 "matched_ge4": 0, "queued": 0, "sent": 0},
            )
            slot["fetched"] += 1
            enr = enrichments_by_job_id.get(job_id)
            if enr is not None:
                slot["scored"] += 1
                try:
                    score = int((enr or {}).get("match_score") or 0)
                except (TypeError, ValueError):
                    score = 0
                if score >= match_floor:
                    slot["matched_ge4"] += 1
            if job_id in queued_job_ids:
                slot["queued"] += 1
            if job_id in sent_job_ids:
                slot["sent"] += 1
        for query, slot in per_query.items():
            store.record_query_run(
                chat_id, slot["source_key"], query,
                pipeline_run_id=pipeline_run_id,
                fetched=slot["fetched"], scored=slot["scored"],
                matched_ge4=slot["matched_ge4"], queued=slot["queued"],
                sent=slot["sent"],
                started_at=started_at, finished_at=finished_at,
            )
    except Exception:
        log.debug("_record_query_funnel failed; continuing", exc_info=True)


# ---------- post-filters ----------

def seniority_matches(job: Job, wanted: str) -> bool:
    wanted = (wanted or "any").lower()
    if wanted == "any":
        return True
    return wanted in f"{job.title} {job.snippet}".lower()


def _digits(text: str):
    buf, cur = [], []
    for ch in text:
        if ch.isdigit():
            cur.append(ch)
        else:
            if cur:
                buf.append("".join(cur))
                cur = []
    if cur:
        buf.append("".join(cur))
    return buf


def post_filter(jobs: list[Job], filters: dict) -> list[Job]:
    """Pass-through.

    Historically this function ran regex/substring gates on title, body,
    company, seniority, and salary. The product decision is now: AI scoring
    (job_enrich.enrich_jobs_ai) is the single matching gate for every job
    across every source. Per-user profile fields (keywords, title gates,
    locations, companies, salary) are IGNORED here — Claude reads the user's
    resume + stated preferences and decides fit holistically.

    Left as an identity function (not deleted) so every historical call site
    keeps compiling and so ops can re-enable a cheap pre-filter later by
    editing this one spot. The `filters` arg is deliberately unused.
    """
    del filters  # intentionally unused — AI is the gate
    return list(jobs)


# ---------- fetch ----------

# Algorithm v2.7 / P2: adapters that accept page-memory cursor kwargs
# (`db=` / `min_revisit_age_s=`). The cursor lives in the `search_fetches`
# table; only these three are threaded via the global-pass dispatcher.
# `linkedin` and `web_search` get the same treatment at their per-user
# call sites further down. Other adapters (HN, RemoteOK, etc.) don't
# paginate natively and stay on the cursor-less `mod.fetch(filters)`
# code path.
_CURSOR_AWARE_SOURCES = frozenset({"justjoinit", "nofluffjobs", "builtin"})

# The FULL set of adapters that write novelty to `search_fetches` (via
# `record_fetch`). `_CURSOR_AWARE_SOURCES` is a strict subset — it's the
# global-pass cursor adapters. `linkedin` and `web_search` ALSO instrument
# `search_fetches`, but through per-user dispatch (their `fetch_per_user`
# entry points), so they don't appear in the cursor-aware set above.
#
# Tier 4 adds the three Claude-CLI delegation adapters that never paginate
# (`devex`, `un_careers`, and the curated_boards module). The curated module
# records under PER-SUB-BOARD keys (`remocate` / `wantapply` /
# `remoterocketship`) — not under `curated_boards` — because the dispatcher
# gates each sub-board's cooldown independently (see `_curated_subboards_to_run`
# / `fetch_all`). So the instrumented set lists the three sub-board keys, not
# the module name. `devex` / `un_careers` are single-key modules and use
# their own name as the source key.
#
# This list must stay COMPLETE: the P6-T1 migration
# (`reset_uninstrumented_source_cooldowns`) uses it to tell which cooldown
# rows are legitimately FSM-driven versus stuck due to the original
# "no signal == 0% novelty" coercion bug. A key that records novelty but is
# missing here would be wrongly reset to 'normal' on every run.
_P2_INSTRUMENTED_SOURCES = frozenset({
    "linkedin", "justjoinit", "nofluffjobs", "builtin", "web_search",
    # Tier 4 — Claude-CLI delegation adapters (no native pagination).
    "devex", "un_careers",
    # curated_boards sub-boards — each records + cools down on its own key.
    "remocate", "wantapply", "remoterocketship",
})

# The curated_boards sub-board source keys, in dispatch order. The module is
# registered in SOURCES under the single key `curated_boards`, but each board
# is toggled (and now cooled down) independently. Keep this in sync with
# `sources.curated_boards.BOARDS`.
_CURATED_SUBBOARDS = ("remocate", "wantapply", "remoterocketship")

# Adapters whose `fetch(filters)` accepts a `db=` kwarg for Tier-4
# novelty recording but DON'T take the cursor (`min_revisit_age_s=`) kwarg
# that `_CURSOR_AWARE_SOURCES` adapters do. These are the Claude-CLI
# delegation sources: they record one fixed-cell `search_fetches` row per
# fetch so the cooldown FSM gets a novelty signal, without any page cursor.
_DB_ONLY_SOURCES = frozenset({"devex", "un_careers", "curated_boards"})


# Adaptive source cooldown (algorithm v2.8 / P4 pipeline overhaul).
# Demotion fires only after this many consecutive checks in which the
# source's 24h novelty ratio sits below `low_novelty_threshold`. One
# quiet iteration is not enough — sources naturally have lulls.
_COOLDOWN_LOW_CYCLES_BEFORE_DEMOTE = 3

# Window the cooldown decision reads novelty over. 24h matches the
# operator's expectation of "is this source still useful TODAY?" while
# still being long enough that a short outage doesn't immediately
# demote a productive source.
_COOLDOWN_NOVELTY_WINDOW_S = 24 * 3600


def _maybe_auto_rebuild_profile(
    db,
    chat_id: int,
    *,
    threshold: int,
    _trigger_rebuild=None,    # injected in tests
) -> bool:
    """Kick off a profile rebuild iff the user has accumulated ≥ threshold
    skip-feedback events since the last rebuild.

    Algorithm v2.8 / P4 pipeline overhaul.

    Reads `db.get_skip_events_since_rebuild(chat_id)`. When the counter
    is at or above the threshold, calls `profile_builder.rebuild_profile`
    synchronously (it's already ~30-60s of Opus time; spawning a thread
    just to fire-and-forget would race against the next iteration's
    enrichment, which reads the freshly-written profile). On a
    SUCCESSFUL rebuild (BuildResult.status == 'ok'), zeroes the counter
    so the NEXT K events trigger the NEXT rebuild — not a re-run of
    the same one.

    Critical detail: on FAILURE the counter is NOT reset, so a transient
    CLI / timeout / schema-validation hiccup doesn't silently consume
    K events. The next iteration retries the rebuild against the same
    counter value.

    Returns True iff a rebuild was triggered and succeeded.
    """
    if db is None or threshold <= 0:
        return False
    try:
        count = int(db.get_skip_events_since_rebuild(chat_id) or 0)
    except Exception:
        log.debug(
            "_maybe_auto_rebuild_profile: get_skip_events_since_rebuild "
            "failed for chat=%s", chat_id, exc_info=True,
        )
        return False
    if count < threshold:
        return False

    if _trigger_rebuild is None:
        from profile_builder import rebuild_profile as _trigger_rebuild  # noqa: WPS433

    try:
        result = _trigger_rebuild(db, chat_id)
        status = getattr(result, "status", "exception")
    except Exception as e:
        log.exception(
            "auto-rebuild: rebuild_profile raised for chat=%s: %s",
            chat_id, e,
        )
        log.info(
            "auto-rebuild: chat_id=%d triggered after %d skip events; "
            "status=exception",
            chat_id, count,
        )
        return False

    log.info(
        "auto-rebuild: chat_id=%d triggered after %d skip events; status=%s",
        chat_id, count, status,
    )
    if status == "ok":
        try:
            db.reset_skip_events_since_rebuild(chat_id)
        except Exception:
            log.exception(
                "auto-rebuild: counter reset failed for chat=%s", chat_id,
            )
        return True
    return False


def _maybe_optimize_queries(
    db,
    store,
    chat_id: int,
    filters: dict,
    *,
    _optimize=None,    # injected in tests
) -> bool:
    """Fire the M2 closed-loop query optimiser on its own cadence.

    COMPLEMENTS `_maybe_auto_rebuild_profile`: the skip-rebuild path needs
    SENT jobs and rewrites the whole profile; this path reads the per-query
    funnel yield (`query_runs`) and mutates only the search queries, so a
    0-send user whose skip counter never fires still gets tuned from the
    scored/matched signal.

    Gated behind `filters['query_optimizer_enabled']` (default OFF) — the
    optimiser itself re-checks the flag, but we short-circuit here to avoid
    importing the module and building a reward window when disabled.

    Best-effort: returns True iff an optimisation actually mutated the
    profile (status == 'ok'); any failure logs and returns False. Never
    raises into the search loop.
    """
    if not bool(filters.get("query_optimizer_enabled", False)):
        return False
    if _optimize is None:
        from query_optimizer import optimize_queries as _optimize  # noqa: WPS433
    try:
        result = _optimize(db, store, chat_id, filters)
    except Exception:
        log.exception(
            "_maybe_optimize_queries: optimiser raised for chat=%s", chat_id,
        )
        return False
    status = getattr(result, "status", "error")
    if status == "ok":
        log.info(
            "query-optimiser: chat=%d kept=%d pruned=%d added=%d",
            chat_id, len(getattr(result, "kept", []) or []),
            len(getattr(result, "pruned", []) or []),
            len(getattr(result, "added", []) or []),
        )
        return True
    log.debug("query-optimiser: chat=%d status=%s (%s)",
              chat_id, status, getattr(result, "reason", ""))
    return False


def should_run_source(
    db,
    source: str,
    *,
    cycle_index: int,
    low_novelty_threshold: float = 0.05,
) -> bool:
    """Decide if `source` should fire this iteration.

    Reads novelty over the last 24h via `db.source_novelty_ratio`. The
    finite-state machine:

      * If novelty is ``None`` (no instrumentation data — see P6-T1)
        → treat as "no signal": do NOT increment the counter, do NOT
        demote, and let the source's existing cooldown state decide.
        State 'normal' runs; state 'half_freq' obeys the parity gate.
      * If novelty >= ``low_novelty_threshold`` → state becomes 'normal',
        ``consecutive_low_novelty_cycles`` resets to 0, return True.
      * If novelty < threshold:
          - increment ``consecutive_low_novelty_cycles``.
          - If the counter reaches ``_COOLDOWN_LOW_CYCLES_BEFORE_DEMOTE``,
            flip ``state`` to 'half_freq'.
          - In 'half_freq' state, return True only on ODD `cycle_index`
            (so the source fires every other cycle).
          - In 'normal' state during the warm-up to demotion, the source
            still runs every cycle.
      * No prior row → treat as state='normal' with counter=0.

    The DB row is updated on every call so the FSM is durable across
    process restarts.

    P6-T1 bug-fix note
    ------------------
    Only 5 source adapters (linkedin, justjoinit, nofluffjobs, builtin,
    web_search) write to ``search_fetches`` via ``record_fetch``. The
    other ~18 sources never produce novelty data, so
    ``source_novelty_ratio`` returns ``None`` for them. Pre-fix the FSM
    coerced the prior ``0.0`` return to "below threshold" and demoted
    every uninstrumented source after 3 iterations — halving the live
    search volume. The ``None``-handling branch above preserves
    cooldown state for uninstrumented sources so they keep running on
    the default schedule until they're either instrumented or
    explicitly disabled.

    `cycle_index` strategy
    ----------------------
    The CALLER passes in a monotonic counter — for `fetch_all` that's
    a per-process counter passed down from the continuous searcher,
    not a clock-derived value. We chose this over `time.time() //
    interval_seconds` so:

      1. Unit tests can drive the FSM deterministically without
         monkeypatching the clock.
      2. The cycle parity stays consistent across a process restart
         within the same run (no "missed" half_freq iterations because
         the wall clock crossed an interval boundary mid-fetch).
      3. The continuous searcher already owns iteration bookkeeping;
         threading the counter through one more function call is a
         smaller surface area than baking time-math into both ends.

    The continuous-searcher hook increments its own counter (starting
    at 1) on every iteration; half_freq sources fire on odd cycles
    (1, 3, 5, ...). Callers passing the default cycle_index=0 (e.g. a
    one-shot manual run) hit the even branch and SKIP half_freq
    sources — if you want a one-shot run to bypass cooldown, pass an
    odd value explicitly (e.g. cycle_index=1).
    """
    if db is None or not source:
        return True
    try:
        novelty = db.source_novelty_ratio(source, _COOLDOWN_NOVELTY_WINDOW_S)
    except Exception:
        log.debug(
            "should_run_source: novelty lookup failed for %s; running",
            source, exc_info=True,
        )
        return True

    try:
        row = db.get_source_cooldown(source) or {}
    except Exception:
        log.debug(
            "should_run_source: cooldown lookup failed for %s; running",
            source, exc_info=True,
        )
        return True
    cur_state = str(row.get("state") or "normal")
    consec = int(row.get("consecutive_low_novelty_cycles") or 0)

    if novelty is None:
        # P6-T1: no instrumentation data — do NOT touch the FSM. The
        # source's prior cooldown state still applies (a half_freq row
        # left over from a real demotion years ago still gates parity),
        # but uninstrumented sources never accumulate low-novelty
        # cycles, so they never get newly demoted by this branch.
        if cur_state == "half_freq":
            return (int(cycle_index) % 2) == 1
        return True

    novelty_f = float(novelty)
    if novelty_f >= float(low_novelty_threshold):
        # Recovery — clear the demotion immediately.
        if cur_state != "normal" or consec != 0:
            try:
                db.upsert_source_cooldown(source, "normal", 0)
            except Exception:
                log.debug(
                    "should_run_source: cooldown upsert failed (recovery)",
                    exc_info=True,
                )
        return True

    # Below threshold — bump the counter, possibly demote.
    consec += 1
    new_state = cur_state
    if consec >= _COOLDOWN_LOW_CYCLES_BEFORE_DEMOTE:
        new_state = "half_freq"
    try:
        db.upsert_source_cooldown(source, new_state, consec)
    except Exception:
        log.debug(
            "should_run_source: cooldown upsert failed (demote)",
            exc_info=True,
        )

    if new_state == "half_freq":
        # Every other cycle. Odd cycle runs, even cycle skips.
        return (int(cycle_index) % 2) == 1

    # Still in 'normal' during the warm-up — keep running.
    return True


def _fetch_one_source(
    key: str,
    mod,
    filters: dict,
    *,
    store: MonitorStore | None = None,
    pipeline_run_id: int | None = None,
    db=None,
) -> tuple[str, list[Job], str | None]:
    """Run ONE source adapter inside its telemetry contexts.

    Returns (key, jobs, err_str). `err_str=None` on success; string
    summary on failure (adapter exception) — the parallel orchestrator
    collects errors and surfaces them via the legacy `errors` list.

    `db` is forwarded to cursor-aware adapters (`_CURSOR_AWARE_SOURCES`)
    so they can advance their `search_fetches` cursor, and to the Tier-4
    `_DB_ONLY_SOURCES` (devex / un_careers / curated_boards) so they can
    record fetch novelty (no page cursor). All other adapters keep their
    legacy `fetch(filters)` signature untouched.
    """
    min_revisit_age_s = int(filters.get("source_min_revisit_age_s") or 21600)

    def _call_adapter() -> list[Job]:
        if db is not None and key in _CURSOR_AWARE_SOURCES:
            return mod.fetch(
                filters,
                db=db,
                min_revisit_age_s=min_revisit_age_s,
            ) or []
        if db is not None and key in _DB_ONLY_SOURCES:
            # Tier 4: novelty recording only, no cursor kwarg.
            return mod.fetch(filters, db=db) or []
        return mod.fetch(filters) or []

    try:
        with forensic.step(
            f"sources.{key}.fetch",
            input={
                "max_age_hours": filters.get("max_age_hours"),
                "max_per_source": filters.get("max_per_source"),
                "sources_enabled": list((filters.get("sources") or {}).keys()),
                "cursor_enabled": db is not None and key in _CURSOR_AWARE_SOURCES,
            },
            run_id=pipeline_run_id,
        ) as fctx:
            if store is not None and pipeline_run_id is not None:
                with source_run(store, pipeline_run_id, key) as sctx:
                    fetched = _call_adapter()
                    sctx.set_count(len(fetched))
            else:
                fetched = _call_adapter()
            fctx.set_output({
                "count": len(fetched),
                "sample_titles": [getattr(j, "title", "")[:80] for j in fetched[:5]],
            })
        log.info("  %s → %d raw postings", key, len(fetched))
        return key, fetched, None
    except Exception as e:
        log.exception("%s fetch raised: %s", key, e)
        return key, [], f"{type(e).__name__}: {str(e)[:200]}"


def _curated_subboards_to_run(
    db,
    enabled: dict,
    *,
    cycle_index: int,
    low_novelty_threshold: float,
) -> tuple[list[str], list[str]]:
    """Split the ENABLED curated sub-boards into (run-this-cycle, cooled-off).

    The curated_boards module is one entry in ``SOURCES``, but its three
    sub-boards (remocate / wantapply / remoterocketship) each record their
    own ``search_fetches`` novelty (Tier 4) and so each gets its OWN
    adaptive-cooldown decision — a chronically quiet board can be demoted
    to half-frequency without dragging its siblings down.

    Returns ``(to_run, cooled)`` where both are sub-board source keys drawn
    from the enabled set. ``db is None`` (dry-run preview) bypasses the gate
    entirely: every enabled board runs and ``cooled`` is empty, matching the
    rest of `fetch_all`'s "preview reflects the full source set" contract.
    """
    enabled_boards = [k for k in _CURATED_SUBBOARDS if enabled.get(k, False)]
    if db is None:
        return enabled_boards, []
    to_run: list[str] = []
    cooled: list[str] = []
    for board in enabled_boards:
        if should_run_source(
            db, board, cycle_index=cycle_index,
            low_novelty_threshold=low_novelty_threshold,
        ):
            to_run.append(board)
        else:
            cooled.append(board)
    return to_run, cooled


def fetch_all(
    filters: dict,
    *,
    store: MonitorStore | None = None,
    pipeline_run_id: int | None = None,
    db=None,
    cycle_index: int = 0,
) -> tuple[list[Job], list[str]]:
    """Fan out to every enabled global source adapter, IN PARALLEL.

    Algorithm v2.2: switched from a serial for-loop to a thread pool
    (configurable via `defaults.ai_source_workers`, default 6). Source
    adapters are network-IO bound (HTTP/RSS/JSON gets, occasional
    Claude CLI subprocess for curated_boards/devex/un_careers/
    ub_doctoral) so threading scales them well — wall time drops from
    ~11-23 min serial to ~2-4 min for 23 adapters.

    Thread-safety notes:
      * `forensic.log_step` is explicitly thread-safe (per-line append).
      * `MonitorStore.source_run` opens a fresh sqlite3 connection per
        call via `DB._conn`, so concurrent inserts don't collide.
      * Adapters keep their own internal concurrency (LinkedIn paces
        1.5s between requests, impactpool fans out 8 detail-page
        workers). Running multiple adapters in parallel multiplies
        outbound traffic — keep `ai_source_workers` modest.

    When `store` and `pipeline_run_id` are supplied, each adapter call
    is still wrapped in a `source_run` telemetry context. The dry-run
    path passes neither and skips telemetry.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    enabled = filters.get("sources") or {}
    try:
        low_thresh = float(filters.get("source_low_novelty_threshold", 0.05))
    except (TypeError, ValueError):
        low_thresh = 0.05

    # Periodic P2 cursor reset (see `cursor_reset_every_n_cycles` in
    # defaults). Fires on cycle 4, 8, 12, … of each chat_id's continuous
    # loop. Strong matches concentrate on pages 1-4; without this the
    # cursor walks 10+ pages deep before the natural staleness window
    # lets it return to page 1.
    try:
        reset_every = int(filters.get("cursor_reset_every_n_cycles", 4))
    except (TypeError, ValueError):
        reset_every = 4
    if (
        db is not None
        and reset_every > 0
        and cycle_index > 0
        and cycle_index % reset_every == 0
    ):
        try:
            deleted = db.reset_search_cursors(_P2_INSTRUMENTED_SOURCES)
            log.info(
                "fetch_all: cycle=%d hit cursor-reset boundary (every %d) — "
                "cleared %d search_fetches rows for %d sources",
                cycle_index, reset_every, deleted, len(_P2_INSTRUMENTED_SOURCES),
            )
        except Exception as e:  # noqa: BLE001 — defensive
            log.exception(
                "fetch_all: cursor reset failed at cycle=%d: %s",
                cycle_index, type(e).__name__,
            )
    # Each task is (source_key, module, filters_override). The override is
    # None for the normal case (use the shared `filters`); curated_boards
    # supplies a narrowed `sources` map so only the sub-boards that survived
    # their per-board cooldown get scraped this cycle.
    tasks: list[tuple[str, object, dict | None]] = []
    cooldown_skipped: list[str] = []
    for key, mod in SOURCES.items():
        if key == "remote_boards":
            if not any(enabled.get(k, True) for k in ("remoteok", "remotive", "weworkremotely")):
                continue
        elif key == "curated_boards":
            # Per-sub-board adaptive cooldown (Tier 4). Each enabled board
            # gets its own `should_run_source` decision; the module runs
            # only the survivors. If every enabled board is on the OFF half
            # of its alternation this cycle, the whole module is skipped.
            if not any(enabled.get(k, False) for k in _CURATED_SUBBOARDS):
                continue
            to_run, cooled = _curated_subboards_to_run(
                db, enabled,
                cycle_index=cycle_index,
                low_novelty_threshold=low_thresh,
            )
            cooldown_skipped.extend(cooled)
            if not to_run:
                continue
            # Narrow the sources map to the surviving sub-boards so demoted
            # boards aren't scraped. Other filter keys are shared by
            # reference (read-only in the adapters), only `sources` is
            # replaced — and only the curated keys are toggled, so a board
            # the FSM left alone keeps its original enabled flag.
            override = None
            if db is not None and set(to_run) != set(
                k for k in _CURATED_SUBBOARDS if enabled.get(k, False)
            ):
                new_sources = dict(enabled)
                for board in _CURATED_SUBBOARDS:
                    new_sources[board] = board in to_run
                override = {**filters, "sources": new_sources}
            tasks.append((key, mod, override))
            continue
        elif not enabled.get(key, True):
            continue
        # Adaptive source cooldown (P4): skip when a source's 24h
        # novelty ratio has been below threshold for 3 consecutive
        # checks AND this cycle is the OFF half of the alternation.
        # `db is None` (dry-run path) bypasses the gate so a preview
        # always reflects the full source set.
        if db is not None and not should_run_source(
            db, key, cycle_index=cycle_index,
            low_novelty_threshold=low_thresh,
        ):
            cooldown_skipped.append(key)
            continue
        tasks.append((key, mod, None))

    if cooldown_skipped:
        log.info(
            "fetch_all: cooldown-skipped %d sources this cycle: %s",
            len(cooldown_skipped), cooldown_skipped,
        )

    workers = int(filters.get("ai_source_workers") or 6)
    log.info("fetch_all: dispatching %d sources across %d workers",
             len(tasks), workers)

    all_jobs: list[Job] = []
    errors: list[str] = []
    if workers <= 1 or len(tasks) <= 1:
        for key, mod, override in tasks:
            _k, fetched, err = _fetch_one_source(
                key, mod, override or filters,
                store=store, pipeline_run_id=pipeline_run_id, db=db,
            )
            all_jobs.extend(fetched)
            if err is not None:
                errors.append(_k)
        return all_jobs, errors

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _fetch_one_source, key, mod, override or filters,
                store=store, pipeline_run_id=pipeline_run_id, db=db,
            )
            for key, mod, override in tasks
        ]
        for fut in as_completed(futures):
            try:
                key, fetched, err = fut.result()
            except Exception as e:
                # _fetch_one_source already catches & returns errs;
                # this is the belt-and-braces guard.
                log.exception("fetch_all worker raised: %s", e)
                errors.append("worker_exception")
                continue
            all_jobs.extend(fetched)
            if err is not None:
                errors.append(key)
    return all_jobs, errors


# ---------- main ----------

def run(
    dry_run: bool = False,
    only_chat: int | None = None,
    no_send: bool = False,
    *,
    cycle_index: int = 0,
) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    root = project_root()
    state_dir = root / os.environ.get("STATE_DIR", "state")
    db_path = state_dir / "jobs.db"

    # Prune old logs at the start of every run. TTL = LOG_TTL_DAYS (default
    # 2 days). Disable via LOG_TTL_OFF=1. Best-effort: any failure is
    # swallowed and logged. See log_ttl.py for what's pruned and why.
    try:
        cleanup_logs(state_dir=state_dir)
    except Exception:
        log.exception("log_ttl: cleanup raised; continuing")

    # Operational defaults live in code (defaults.DEFAULTS). Matching is
    # per-user via the Opus-built profile — there is no global YAML config.
    filters = dict(DEFAULTS)

    db = DB(db_path)
    # P6-T1 migration: reset any source_cooldowns row that the FSM
    # wrongly demoted while `source_novelty_ratio` coerced "no
    # instrumentation data" to "0% novelty". After this one-shot
    # cleanup, uninstrumented sources start each run in 'normal' and
    # the no-signal branch in `should_run_source` keeps them there.
    # Idempotent: the SQL filter skips rows already at normal/0.
    try:
        reset_count = db.reset_uninstrumented_source_cooldowns(
            set(_P2_INSTRUMENTED_SOURCES),
        )
        if reset_count:
            log.info(
                "P6-T1: reset %d wrongly-demoted source_cooldown rows "
                "(uninstrumented sources stuck at half_freq).",
                reset_count,
            )
    except Exception:
        log.exception("P6-T1 cooldown reset raised; continuing")
    # TTL-prune the per-run digest enrichment cache that backs the ⬇/⬆
    # filter buttons. 7 days is generous: by then any digest the user
    # might still want to retroactively expand has long scrolled out of
    # view and the score gates likely shifted.
    try:
        purged = db.purge_digest_run_jobs_older_than(7 * 86400)
        if purged:
            log.info("digest_run_jobs: purged %d rows older than 7 days", purged)
    except Exception:
        log.debug("digest_run_jobs purge raised; continuing", exc_info=True)
    # Persistent score cache. 30 days is far enough out that a stale
    # verdict's contribution is mostly noise (the user's profile has
    # likely changed; a stale row at the old profile_hash is dead
    # weight anyway). Larger horizon than digest_run_jobs because score
    # rows are cheap and a hit on a 3-week-old re-run is still a
    # meaningful win.
    try:
        purged_scores = db.purge_job_scores_older_than(30 * 86400)
        if purged_scores:
            log.info("job_scores: purged %d rows older than 30 days",
                     purged_scores)
    except Exception:
        log.debug("job_scores purge raised; continuing", exc_info=True)
    job_store = JobStore(db)
    # Telemetry store. Cheap to construct (just wraps db); used in both the
    # dry-run preview path (untelemetered) and the live path (wrapped in
    # error_capture + pipeline_run below).
    store = MonitorStore(db)

    # ----- Dry-run preview (no telemetry, no Telegram) --------------------
    if dry_run:
        # cycle_index isn't passed in dry-run: the cooldown gate is
        # bypassed (db=None would skip it anyway, and passing db=db is
        # safe because dry-run doesn't have a running searcher counter).
        jobs_raw, errors = fetch_all(filters, db=db, cycle_index=cycle_index)
        log.info("Raw fetched across static sources: %d postings", len(jobs_raw))
        new_in_db = job_store.save_all(jobs_raw)
        log.info("DB: %d newly-inserted jobs, %d already known",
                 new_in_db, len(jobs_raw) - new_in_db)
        if only_chat is not None:
            users = [u for u in [db.get_user(only_chat)] if u is not None]
        else:
            users = db.users_with_resume()
        dry_jobs = post_filter(jobs_raw, filters)
        print(f"\n=== DRY RUN — {len(dry_jobs)} postings (default filter) ===\n")
        for j in dry_jobs:
            print(f"  [{j.source}] {j.title} @ {j.company} — {j.url}")
        print()
        if users:
            print(f"Users that would receive: {[u['chat_id'] for u in users]}")
        if errors:
            print(f"Source errors: {', '.join(errors)}")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN missing")
        return 1

    tg = TelegramClient(token=token)

    # ------------------------------------------------------------------
    # Live run wrapped in monitoring contexts:
    #   * error_capture  — uncaught Exception → fingerprint + alert operator
    #   * pipeline_run   — one row in `pipeline_runs` with counters/status
    # `fetch_all` runs per-source telemetry inside (`source_run`) when given
    # store + run_id. After the contexts close we deliver the daily summary
    # to the operator chat (suppressed by `quiet_alerts` toggle).
    # ------------------------------------------------------------------
    def _alert_sink(env):
        deliver_alert(tg, store, env)

    exit_code = 0
    run_id_for_summary: int | None = None
    try:
      with error_capture(store, where="search_jobs.run", alert_sink=_alert_sink):
        with pipeline_run(store, "daily_digest") as pctx:
            run_id_for_summary = pctx.run_id

            jobs_raw, errors = fetch_all(
                filters, store=store, pipeline_run_id=pctx.run_id, db=db,
                cycle_index=cycle_index,
            )
            log.info("Raw fetched across static sources: %d postings", len(jobs_raw))
            new_in_db = job_store.save_all(jobs_raw)
            log.info("DB: %d newly-inserted jobs, %d already known",
                     new_in_db, len(jobs_raw) - new_in_db)

            if only_chat is not None:
                users = [u for u in [db.get_user(only_chat)] if u is not None]
            else:
                users = db.users_with_resume()
            if not users:
                log.warning("No registered users with a resume. Ask them to /start the bot and upload a CV.")

            msg_cfg = filters.get("message") or {}
            quiet = bool(msg_cfg.get("quiet_if_empty"))
            ai_enrich = bool(filters.get("ai_enrich", True))
            enrich_timeout_s = int(filters.get("ai_enrich_timeout_s") or 240)
            # Sonnet rescore-pass timeout (two-pass mode only). Distinct
            # from `ai_enrich_timeout_s` because measured Sonnet p95
            # (408s) is 2.3× Haiku p99 (176s); same cap for both either
            # over-taxes Haiku or under-taxes Sonnet. Falls back to
            # `enrich_timeout_s` when the dedicated knob isn't set.
            sonnet_timeout_s = (
                int(filters["ai_sonnet_timeout_s"])
                if filters.get("ai_sonnet_timeout_s")
                else enrich_timeout_s
            )
            global_cap = int(filters.get("max_total") or 0)
            # Default AI match-score floor applied when a user hasn't set their own
            # via the bot's ⭐ button. Because the keyword pre-filter has been removed
            # (AI is now the sole matching gate), we need a non-zero default so score-0
            # rejects (jobs Claude explicitly flagged as wrong-fit) don't leak through.
            # Operators can raise/lower this in defaults.py:ai_min_match_score.
            # Clamp to [0, 5]; 0 disables the default floor.
            try:
                default_min_score = int(filters.get("ai_min_match_score") or 0)
            except (TypeError, ValueError):
                default_min_score = 0
            default_min_score = max(0, min(5, default_min_score))

            total_sent = 0
            stats = {
                "users_total":        0,
                "jobs_raw_total":     len(jobs_raw),
                "jobs_sent_total":    0,
                "web_search_hits":    0,
                "linkedin_user_hits": 0,
            }
            send_failed_chat: int | None = None

            for u in users:
                chat_id = int(u["chat_id"])
                stats["users_total"] += 1
                user_started_at = time.time()

                # Algorithm v2: profile JSON is now a thin envelope
                # (search_seeds + bookkeeping). Scoring inputs come from the
                # per-user files state/users/<chat_id>/{resume.txt,prefs.txt}.
                # The DB column users.min_match_score holds the ⭐ floor
                # (survives profile rebuilds, unlike the JSON one used to).
                profile = profile_from_json(db.get_user_profile(chat_id))
                resume_text = user_files.read_resume(chat_id)
                prefs_text = user_files.read_prefs(chat_id)
                free_text = prefs_text  # legacy alias for downstream callers

                # M2 per-query telemetry: side-channel map { job_id ->
                # originating query string } populated by the per-user
                # source adapters (linkedin / web_search). Rolled up into
                # the `query_runs` funnel table at the end of this user's
                # block (see `_record_query_funnel`). Best-effort — a
                # failure here never blocks the search run.
                query_attribution: dict[str, str] = {}

                effective = effective_filters(filters, profile)
                log.info(
                    "User %s: resume_chars=%d prefs_chars=%d seeds=%s",
                    chat_id, len(resume_text), len(prefs_text),
                    bool((profile or {}).get("search_seeds")),
                )

                user_pool = post_filter(jobs_raw, effective)
                log.info("User %s: post_filter %d → %d", chat_id, len(jobs_raw), len(user_pool))

                # Algorithm v2.3: per-user source toggles. If the user
                # has an `enabled_sources` list (set by the profile
                # builder at build time, optionally edited via the
                # /sources UI), drop pool entries whose source isn't in
                # the list. Saves Sonnet enrichment tokens on off-family
                # sources (e.g. ai_jobs_net for a researcher, jobs_ac_uk
                # for a frontend dev).
                enabled = db.get_enabled_sources(chat_id)
                if enabled:
                    allowed = set(enabled)
                    before_src = len(user_pool)
                    user_pool = [j for j in user_pool if j.source in allowed]
                    if before_src != len(user_pool):
                        log.info(
                            "User %s: source-toggle filter %d → %d "
                            "(allowed=%d sources)",
                            chat_id, before_src, len(user_pool), len(allowed),
                        )

                # -------- per-user LinkedIn fetch --------
                li_seeds = ((profile or {}).get("search_seeds") or {}).get("linkedin")
                li_enabled = bool((effective.get("sources") or {}).get("linkedin", False))
                has_queries = bool(li_seeds and li_seeds.get("queries"))
                if li_enabled and has_queries:
                    try:
                        with forensic.step(
                            "linkedin.fetch_for_user",
                            input={
                                "queries": (li_seeds or {}).get("queries"),
                                "remote": effective.get("remote"),
                                "max_per_source": effective.get("max_per_source"),
                            },
                            chat_id=chat_id,
                            run_id=pctx.run_id,
                        ) as fctx:
                            with source_run(store, pctx.run_id, "linkedin", user_chat_id=chat_id) as li_sctx:
                                extra_li = linkedin.fetch_for_user(
                                    effective,
                                    li_seeds,
                                    db=db,
                                    min_revisit_age_s=int(
                                        effective.get("source_min_revisit_age_s") or 21600
                                    ),
                                    attribution=query_attribution,
                                ) or []
                                li_sctx.set_count(len(extra_li))
                            fctx.set_output({
                                "raw_count": len(extra_li),
                                "sample_titles": [j.title[:80] for j in extra_li[:5]],
                                "sample_urls": [j.url for j in extra_li[:5]],
                            })
                        log.info("User %s: linkedin.fetch_for_user → %d raw postings",
                                 chat_id, len(extra_li))
                        extra_li_filtered = post_filter(extra_li, effective)
                        if extra_li_filtered:
                            job_store.save_all(extra_li_filtered)
                            user_pool.extend(extra_li_filtered)
                            stats["linkedin_user_hits"] += len(extra_li_filtered)
                            log.info(
                                "User %s: linkedin.fetch_for_user added %d postings after post_filter",
                                chat_id, len(extra_li_filtered),
                            )
                    except Exception as e:
                        log.exception("User %s: per-user LinkedIn fetch failed: %s", chat_id, e)
                        pctx.incr_errors(1)

                # -------- per-user web_search --------
                web_seeds = ((profile or {}).get("search_seeds") or {}).get("web_search")
                web_search_enabled = bool((effective.get("sources") or {}).get("web_search", False))
                trigger_web = web_search_enabled and (bool(web_seeds) or bool(free_text))
                if trigger_web:
                    try:
                        with forensic.step(
                            "web_search.fetch",
                            input={
                                "free_text_head": (free_text or "")[:300],
                                "profile_seeds": web_seeds,
                                "max_per_source": effective.get("max_per_source"),
                                "ai_web_search_timeout_s": effective.get("ai_web_search_timeout_s"),
                            },
                            chat_id=chat_id,
                            run_id=pctx.run_id,
                        ) as fctx:
                            with source_run(store, pctx.run_id, "web_search", user_chat_id=chat_id) as ws_sctx:
                                extra = web_search.fetch(
                                    effective,
                                    user_free_text=free_text or None,
                                    profile_seeds=web_seeds,
                                    db=db,
                                    min_revisit_age_s=int(
                                        effective.get("source_min_revisit_age_s") or 21600
                                    ),
                                    # Per-user cursor scope: each user has
                                    # their own page counter for web_search
                                    # so the exploration rounds don't bleed
                                    # across users.
                                    cursor_key=str(chat_id),
                                    attribution=query_attribution,
                                ) or []
                                ws_sctx.set_count(len(extra))
                            fctx.set_output({
                                "raw_count": len(extra),
                                "sample_titles": [j.title[:80] for j in extra[:5]],
                                "sample_urls": [j.url for j in extra[:5]],
                            })
                        log.info("User %s: web_search returned %d raw postings",
                                 chat_id, len(extra))
                        extra_filtered = post_filter(extra, effective)
                        if extra_filtered:
                            job_store.save_all(extra_filtered)
                            user_pool.extend(extra_filtered)
                            stats["web_search_hits"] += len(extra_filtered)
                            log.info("User %s: web_search added %d postings after post_filter",
                                     chat_id, len(extra_filtered))
                    except Exception as e:
                        log.exception("User %s: per-user web_search failed: %s", chat_id, e)
                        pctx.incr_errors(1)

                if global_cap > 0:
                    user_pool = user_pool[:global_cap]

                user_jobs = job_store.filter_new_for(chat_id, user_pool)
                log.info("User %s: %d new postings (of %d after per-user filter + web_search)",
                         chat_id, len(user_jobs), len(user_pool))

                # Cross-source dedupe (algorithm v2.8, P4 pipeline overhaul).
                # The same posting can show up across multiple feeds
                # (justjoinit + nofluffjobs + LinkedIn-PL all carry the
                # Polish/CEE tech roles, for instance). Without this step
                # we'd call the scorer 3x for one underlying job. Runs at
                # the TRANSPORT layer — see `dedupe_cross_source` docstring
                # for the design-principle justification.
                if user_jobs:
                    before_xs = len(user_jobs)
                    user_jobs = dedupe_cross_source(user_jobs)
                    if before_xs != len(user_jobs):
                        log.info(
                            "dedupe_cross_source: %d → %d (collapsed %d duplicate postings)",
                            before_xs, len(user_jobs), before_xs - len(user_jobs),
                        )

                if not user_jobs and quiet:
                    continue

                # NOTE: liveness gate runs at send-time on the few postings
                # that survive scoring + the ⭐ floor — see telegram_client.
                # send_per_job_digest. Running it pre-enrich on the full
                # 500-1000 job pool wasted ~90 minutes of Haiku WebFetch
                # calls for a handful of dead postings — most candidates
                # are filtered cheaper by score or floor anyway.

                enrichments_by_job_id: dict[str, dict] = {}
                if ai_enrich and user_jobs:
                    try:
                        with forensic.step(
                            "enrich_jobs_ai",
                            input={
                                "job_count": len(user_jobs),
                                "resume_chars": len(resume_text),
                                "prefs_chars": len(prefs_text),
                                "min_match_score": db.get_min_match_score(chat_id),
                                "sample_input_jobs": [
                                    {"job_id": j.job_id, "title": j.title[:80],
                                     "company": j.company[:60], "source": j.source}
                                    for j in user_jobs[:8]
                                ],
                            },
                            chat_id=chat_id,
                            run_id=pctx.run_id,
                        ) as fctx:
                            raw = enrich_jobs_ai(
                                user_jobs,
                                resume_text,
                                prefs_text,
                                timeout_s=enrich_timeout_s,
                                max_jobs_per_call=int(filters.get("ai_max_jobs_per_call") or 10),
                                two_pass=bool(filters.get("ai_two_pass", False)),
                                triage_floor=int(filters.get("ai_triage_floor") or 2),
                                # ai_triage_ceiling=5 (defaults): skip
                                # Sonnet rescore on Haiku=5 verdicts.
                                # ai_sonnet_max_jobs_per_call=5
                                # (defaults): smaller Sonnet batches cap
                                # worst-case per-batch wall time when the
                                # ~9k-token doctrine + briefs combine to
                                # stall a Sonnet call past 5 minutes.
                                triage_ceiling=int(filters.get("ai_triage_ceiling") or 6),
                                sonnet_max_jobs_per_call=(
                                    int(filters["ai_sonnet_max_jobs_per_call"])
                                    if filters.get("ai_sonnet_max_jobs_per_call")
                                    else None
                                ),
                                # ai_sonnet_timeout_s=300 (defaults):
                                # tight Sonnet-only cap. On timeout the
                                # batch is retried at batch_size=1; if
                                # those single-job calls also time out
                                # the jobs are dropped (no Haiku
                                # fallback — verdict provenance stays
                                # honest).
                                sonnet_timeout_s=sonnet_timeout_s,
                                workers=int(filters.get("ai_enrich_workers") or 4),
                                db=db,
                                chat_id=chat_id,
                            )
                            enrichments_by_job_id = by_job_id(raw, user_jobs)
                            # Summary only. Per-verdict detail is emitted as
                            # one forensic line per job below — packing all
                            # verdicts into a single `output.verdicts` list
                            # blew through the 4 KiB field cap and silently
                            # dropped tail entries (a recent run lost 22/34,
                            # including all LinkedIn jobs and the only
                            # score=2 result). One-line-per-verdict keeps
                            # every job present and well under the cap.
                            fctx.set_output({
                                "enriched_count": len(enrichments_by_job_id),
                                "score_distribution": _score_histogram(enrichments_by_job_id),
                            })
                        # Per-verdict forensic lines. Emitted *after* the
                        # `forensic.step` context closes so the summary line
                        # writes first, but ordering doesn't matter for
                        # correctness — `log_step` is thread-safe and each
                        # line is fully self-describing (carries chat_id +
                        # run_id + job_id). `forensic.log_step` cannot
                        # raise; if it does we still want the digest to
                        # ship, hence the broad except.
                        try:
                            for j in user_jobs:
                                enr = enrichments_by_job_id.get(j.job_id) or {}
                                kd = enr.get("key_details") or {}
                                # Compact "k=v; k=v" summary so the verdict
                                # line stays a few hundred bytes even when
                                # `key_details` carries 6+ fields.
                                key_details_summary = "; ".join(
                                    f"{k}={str(v)[:60]}"
                                    for k, v in kd.items()
                                    if v not in (None, "", [], {})
                                )[:400]
                                forensic.log_step(
                                    "enrich_jobs_ai.verdict",
                                    input={
                                        "job_id": j.job_id,
                                        "title": j.title[:120],
                                        "company": (j.company or "")[:80],
                                        "source": j.source,
                                        "url": (j.url or "")[:200],
                                    },
                                    output={
                                        "match_score": enr.get("match_score"),
                                        "why_match": (enr.get("why_match") or "")[:300],
                                        "why_mismatch": (enr.get("why_mismatch") or "")[:300],
                                        "key_details_summary": key_details_summary,
                                    },
                                    chat_id=chat_id,
                                    run_id=pctx.run_id,
                                )
                        except Exception:
                            log.debug("per-verdict forensic emit failed; continuing",
                                      exc_info=True)
                        log.info("User %s: enriched %d/%d jobs",
                                 chat_id, len(enrichments_by_job_id), len(user_jobs))
                    except Exception as e:
                        log.exception("User %s: enrichment failed: %s", chat_id, e)
                        pctx.incr_errors(1)

                # Snapshot all enriched jobs (passed AND below-floor) into
                # `digest_run_jobs` BEFORE the score-gate filter. That lets the
                # digest header's ⬇ button replay dropped postings later, with
                # the same enrichment payload (match_score + why_match +
                # key_details) the live message would have carried. Failure here
                # never blocks the digest — the cache is a UX nicety.
                if enrichments_by_job_id:
                    try:
                        db.record_digest_run_jobs(chat_id, pctx.run_id, enrichments_by_job_id)
                    except Exception:
                        log.debug("record_digest_run_jobs failed; continuing", exc_info=True)

                enriched_count = len(enrichments_by_job_id)
                # Floor lives on the DB column now (algorithm v2). Falls
                # back to the global default when the user hasn't tapped ⭐.
                user_min_score = int(db.get_min_match_score(chat_id) or 0)
                effective_min_score = user_min_score if user_min_score > 0 else default_min_score
                dropped_below_score = 0
                if effective_min_score > 0 and user_jobs:
                    before = len(user_jobs)
                    user_jobs = [
                        j for j in user_jobs
                        if int((enrichments_by_job_id.get(j.job_id) or {}).get("match_score") or 0) >= effective_min_score
                    ]
                    dropped_below_score = before - len(user_jobs)
                    gate_source = "user" if user_min_score > 0 else "default"
                    log.info("User %s: min_score ≥ %d (%s) gate %d → %d",
                             chat_id, effective_min_score, gate_source, before, len(user_jobs))

                # ----- Send-time prefilter (algorithm v2.6) ----------
                # Run the age / dead-URL / forum / LLM-liveness gates on
                # the score-floor survivors. The v2.4 closest-miss
                # fallback was removed in P1 of the pipeline overhaul —
                # nothing scoring below the floor reaches the user;
                # instead, score-and-live matches accumulate in a
                # per-user quality buffer (queued_matches) and ship in
                # one batch when depth or latency crosses the threshold.
                from telegram_client import prefilter_for_send
                alive_floor: list[Job] = []
                if user_jobs:
                    alive_floor, _drop_counts = prefilter_for_send(
                        user_jobs, chat_id, forensic=forensic,
                    )
                    if len(alive_floor) != len(user_jobs):
                        log.info(
                            "User %s: send-time prefilter %d → %d (gate drops)",
                            chat_id, len(user_jobs), len(alive_floor),
                        )

                # ----- Quality buffer (algorithm v2.6, P1) -----------
                # Enqueue every alive-floor match under the user's
                # current profile_hash; flush in one batch when depth
                # or oldest-age crosses the configured thresholds.
                # On hold: skip this user's send block this iteration.
                #
                # The preview path (`--no-send`) deliberately bypasses
                # the buffer so an operator dry-run never mutates the
                # queue. They get the per-run alive_floor printed
                # verbatim instead.
                # M2 funnel: the alive-floor set is exactly what gets
                # enqueued into the quality buffer this run, so its ids are
                # the "queued" stage for query attribution. "sent" is
                # filled later from `_sent_jobs_this_run`.
                queued_ids_this_run: set[str] = {j.job_id for j in alive_floor}
                sent_ids_this_run: set[str] = set()

                buffer_depth = 0
                buffer_oldest_age = 0.0
                if no_send:
                    user_jobs = alive_floor
                else:
                    user_profile_hash = _profile_hash(resume_text, prefs_text)
                    buffer_jobs, flush_now, buffer_depth, buffer_oldest_age = (
                        _decide_buffer_flush(
                            db, chat_id, alive_floor, enrichments_by_job_id,
                            user_profile_hash, filters,
                        )
                    )
                    if not flush_now:
                        log.info(
                            "User %s: quality-buffer holding %d "
                            "(threshold=%d, oldest_age=%.1fh) — no send",
                            chat_id, buffer_depth,
                            int(filters.get("quality_send_threshold", 5)),
                            buffer_oldest_age / 3600.0,
                        )
                        # P5: in continuous mode the buffer holds nearly
                        # every iteration. We unconditionally skip the
                        # per-user send block here so the user never
                        # receives an empty digest (pig sticker + "0 jobs"
                        # header card). The `quiet_if_empty` config knob
                        # is retained in defaults.py for back-compat but
                        # has NO effect on this branch — empty sends are
                        # always suppressed on hold.
                        #
                        # The v2.5 scoring audit (further down the loop)
                        # is intentionally SKIPPED on a hold iteration:
                        # the cards never shipped, so logging
                        # `scoring_audit.review` entries would falsely
                        # suggest a send happened. The audit will run on
                        # the next flush iteration when the enrichments
                        # are paired with actual delivery.
                        #
                        # The auto profile-rebuild check (also further
                        # down) IS independent of digest delivery and
                        # should still fire on hold so the skip-feedback
                        # counter eventually triggers a rebuild even
                        # during long quiet stretches.
                        try:
                            auto_thr = int(
                                filters.get("auto_rebuild_skip_threshold") or 5
                            )
                        except (TypeError, ValueError):
                            auto_thr = 5
                        try:
                            _maybe_auto_rebuild_profile(
                                db, chat_id, threshold=auto_thr,
                            )
                        except Exception:
                            log.exception(
                                "User %s: auto-rebuild check raised on "
                                "buffer-hold; continuing", chat_id,
                            )
                        # M2: record the per-query funnel even on a hold
                        # iteration — the fetched/scored/matched stages are
                        # the cold-start signal the optimiser needs, and a
                        # 0-send user holds nearly every iteration. `sent`
                        # is correctly 0 here (nothing shipped).
                        _record_query_funnel(
                            store,
                            pipeline_run_id=pctx.run_id,
                            chat_id=chat_id,
                            attribution=query_attribution,
                            enrichments_by_job_id=enrichments_by_job_id,
                            match_floor=effective_min_score,
                            queued_job_ids=queued_ids_this_run,
                            sent_job_ids=set(),
                            started_at=user_started_at,
                            finished_at=time.time(),
                        )
                        # M2: the closed-loop query optimiser MUST fire on
                        # the hold path too. Its entire raison d'être is the
                        # quiet, ~0-send user (the skip-rebuild loop cannot
                        # reach them) — and that user spends nearly every
                        # iteration HERE, never crossing a buffer flush. If
                        # we only optimised on the flush/send path, the
                        # cold-start user would rarely or never get tuned,
                        # defeating the feature. This mirrors the auto-
                        # rebuild check above (fired on BOTH paths for the
                        # same reason). The funnel row was just recorded, so
                        # the optimiser reads the freshest reward window. It
                        # is cheap on the hold path: the once-per-cadence
                        # gate plus the disabled/no_reward/cadence_skip
                        # short-circuits prevent redundant Opus calls.
                        try:
                            _maybe_optimize_queries(
                                db, store, chat_id, filters,
                            )
                        except Exception:
                            log.exception(
                                "User %s: query-optimiser check raised on "
                                "buffer-hold; continuing", chat_id,
                            )
                        continue
                    else:
                        # Hand the rehydrated, ordered queue downstream
                        # as the final send list. `user_jobs` was the
                        # per-run alive_floor; replace with the buffer
                        # contents so the digest reflects what actually
                        # ships.
                        user_jobs = buffer_jobs
                        # Re-verify web_search URLs at flush time. The
                        # original verifier call ran at ENQUEUE; a job
                        # that sits in the buffer for hours/days can
                        # have its URL go dead between then and now
                        # (Workable/SmartRecruiters silently redirect to
                        # the company openings index — server still
                        # returns 200, so a stale verdict from enqueue
                        # would let a dead URL ship).
                        try:
                            web_jobs = [j for j in user_jobs if (j.source or "") == "web_search"]
                            if web_jobs:
                                fresh_alive, _drop = prefilter_for_send(
                                    web_jobs, chat_id, forensic=forensic,
                                )
                                dropped_ids = {j.job_id for j in web_jobs} - {j.job_id for j in fresh_alive}
                                if dropped_ids:
                                    log.info(
                                        "User %s: flush-time re-verify dropped "
                                        "%d web_search URLs as dead",
                                        chat_id, len(dropped_ids),
                                    )
                                    # Purge dead URLs from the queue so they
                                    # don't keep coming back next flush.
                                    try:
                                        db.clear_queue(chat_id, dropped_ids)
                                    except Exception:
                                        log.debug(
                                            "clear_queue (re-verify drops) failed",
                                            exc_info=True,
                                        )
                                    user_jobs = [
                                        j for j in user_jobs
                                        if j.job_id not in dropped_ids
                                    ]
                        except Exception:
                            log.exception(
                                "User %s: flush-time re-verify raised; "
                                "shipping buffer as-is", chat_id,
                            )
                        # Hydrate enrichments for any buffered job that
                        # was enqueued in a PRIOR run — that run's
                        # `enrichments_by_job_id` is gone (this iter's
                        # `filter_new_for` excluded those ids from
                        # `user_jobs`, so `enrich_jobs_ai` never ran on
                        # them). Without this fill, the cards ship with
                        # no ⭐ score, no why-match, no key-details.
                        #
                        # `purge_stale_queue` already ran inside
                        # `_decide_buffer_flush`, so every surviving
                        # buffer row shares the user's CURRENT
                        # profile_hash. `job_scores` is keyed by
                        # (chat_id, profile_hash, job_id) → the lookup
                        # below is a clean hit by construction.
                        buffered_ids = [
                            j.job_id for j in user_jobs
                            if j.job_id not in enrichments_by_job_id
                        ]
                        if buffered_ids:
                            try:
                                cached = db.get_cached_scores(
                                    chat_id, buffered_ids, user_profile_hash,
                                )
                            except Exception:
                                log.warning(
                                    "User %s: get_cached_scores failed during "
                                    "buffer hydration; cards may ship without "
                                    "enrichments", chat_id, exc_info=True,
                                )
                                cached = {}
                            enrichments_by_job_id.update(cached)
                            missing = [
                                jid for jid in buffered_ids if jid not in cached
                            ]
                            if missing:
                                # Defensive: shouldn't happen given the
                                # profile_hash invariant above, but log
                                # so we notice if it does. Cards still
                                # ship — the format path tolerates a
                                # missing enrichment entry.
                                log.warning(
                                    "User %s: %d buffered job(s) had no "
                                    "cached score under profile_hash=%s; "
                                    "shipping without enrichment: %s",
                                    chat_id, len(missing),
                                    user_profile_hash[:8],
                                    missing[:5],
                                )
                # Header floor reflects the gate that actually fired (user
                # value if set, otherwise the global default). Surfacing 0
                # would hide the ⬆ button and lie about what was filtered.
                min_score = effective_min_score
                # Count of cached unsent jobs with score ≥ (floor-1) — feeds
                # the ⬇ button label (e.g. "⬇ ≥2 (+5)"). 0 means hide the
                # button. Inclusive-upward so the (+M) matches what a click
                # actually replays.
                lower_count_at_step = 0
                if effective_min_score > 0:
                    try:
                        lower_count_at_step = db.unsent_count_at_score(
                            chat_id, pctx.run_id, effective_min_score - 1,
                        )
                    except Exception:
                        log.debug("unsent_count_at_score failed; continuing", exc_info=True)

                # Pig sticker moved INTO send_per_job_digest in v2.2 so
                # it fires after the age/url/forum/liveness prefilter and
                # reflects the actual count the user will see, not the
                # pre-gate score-floor count.

                if no_send:
                    # Preview path: print what would be sent, skip Telegram
                    # delivery + sent_messages persistence + the post-send
                    # scoring_audit pass.
                    print(
                        f"\n=== NO-SEND PREVIEW · chat={chat_id} · "
                        f"{len(user_jobs)} jobs would be sent "
                        f"(min_score={min_score}) ===\n"
                    )
                    scored = []
                    for j in user_jobs:
                        enr = enrichments_by_job_id.get(j.job_id) or {}
                        score = int(enr.get("match_score") or 0)
                        scored.append((score, j, enr))
                    scored.sort(key=lambda t: -t[0])
                    for score, j, enr in scored:
                        why = (enr.get("why_match") or "")[:120]
                        print(f"  [{score}⭐] [{j.source}] {j.title} @ {j.company}")
                        if why:
                            print(f"        why: {why}")
                        print(f"        {j.url}")
                    print()
                else:
                    try:
                        _sent_jobs_this_run: list[str] = []
                        def _on_sent(mid, j, _cid=chat_id, _sink=_sent_jobs_this_run):
                            db.log_sent(_cid, mid, j.job_id)
                            _sink.append(j.job_id)
                        sent = send_per_job_digest(
                            tg, chat_id, user_jobs, filters,
                            on_sent=_on_sent,
                            enrichments=enrichments_by_job_id,
                            min_score=min_score,
                            run_id=pctx.run_id,
                            enriched_count=enriched_count,
                            dropped_below_score=dropped_below_score,
                            lower_count_at_step=lower_count_at_step,
                            pre_filtered=True,
                        )
                        total_sent += sent
                        sent_ids_this_run = set(_sent_jobs_this_run)
                        if _sent_jobs_this_run:
                            try:
                                db.mark_digest_jobs_sent(
                                    chat_id, pctx.run_id, _sent_jobs_this_run,
                                    floor=min_score,
                                )
                            except Exception:
                                log.debug("mark_digest_jobs_sent failed; continuing", exc_info=True)
                            # Drop the delivered rows from the quality
                            # buffer. Jobs that failed mid-flush stay
                            # queued for the next attempt — the
                            # confirmation is `_on_sent` firing, not
                            # the call returning.
                            try:
                                cleared = db.clear_queue(
                                    chat_id, _sent_jobs_this_run,
                                )
                                log.info(
                                    "User %s: quality-buffer flushed %d/%d "
                                    "(threshold=%d, oldest_age=%.1fh)",
                                    chat_id, cleared, buffer_depth,
                                    int(filters.get("quality_send_threshold", 5)),
                                    buffer_oldest_age / 3600.0,
                                )
                            except Exception:
                                log.debug("clear_queue failed; continuing",
                                          exc_info=True)
                    except Exception as e:
                        log.exception("Failed to send digest to %s: %s", chat_id, e)
                        pctx.incr_errors(1)
                        send_failed_chat = chat_id
                        break

                # --- v2.5 audit stage --------------------------------
                # AFTER the cards have shipped, re-grade the score-≥1
                # verdicts with a second-opinion model (Opus by default)
                # and persist any disagreements. This catches scoring
                # drift (Sonnet over- or under-scoring) without blocking
                # the digest. Disagreements land in forensic as
                # `scoring_audit.review` lines so we can grep for
                # systematic misses post-hoc and decide whether a
                # follow-up manual top-up is warranted.
                if (not no_send) and filters.get("ai_scoring_audit", True) and enrichments_by_job_id:
                    try:
                        ext_to_job = {j.external_id: j for j in user_pool}
                        # Audit score-≥1 only — score-0s are firmly
                        # negative and re-checking them is rarely useful.
                        ext_id_to_enrich = {
                            ext_id: enr for ext_id, enr in enrichments_by_job_id.items()
                            # enrichments_by_job_id is keyed by *job_id*
                            # not external_id; rebuild via the
                            # external_id keys we kept above.
                        }
                        # Actually rebuild a key-by-external_id map
                        # because reanalyze_scoring_ai wants external_ids.
                        ext_enr: dict[str, dict] = {}
                        audit_jobs: list[Job] = []
                        for j in user_pool:
                            enr = enrichments_by_job_id.get(j.job_id) or {}
                            score = int(enr.get("match_score") or 0)
                            if score >= 1:
                                ext_enr[j.external_id] = enr
                                audit_jobs.append(j)
                        if audit_jobs:
                            audit_timeout = int(
                                filters.get("ai_scoring_audit_timeout_s") or enrich_timeout_s
                            )
                            audit_model = str(
                                filters.get("ai_scoring_audit_model") or "sonnet"
                            )
                            audit_batch_size = int(
                                filters.get("ai_scoring_audit_batch_size") or 10
                            )
                            audit_workers = int(
                                filters.get("ai_scoring_audit_workers") or 4
                            )
                            audit_critic_rounds = int(
                                filters.get("ai_scoring_audit_critic_rounds") or 2
                            )
                            audit_critic_model = str(
                                filters.get("ai_scoring_audit_critic_model") or "sonnet"
                            )
                            reviews = reanalyze_scoring_ai(
                                audit_jobs, ext_enr, resume_text, prefs_text,
                                timeout_s=audit_timeout, model=audit_model,
                                batch_size=audit_batch_size,
                                workers=audit_workers,
                                critic_rounds=audit_critic_rounds,
                                critic_model=audit_critic_model,
                            )
                            disagreements = [
                                r for r in reviews
                                if r["verdict"] != "agree"
                            ]
                            log.info(
                                "User %s: scoring audit — %d reviewed, "
                                "%d disagreements (raises=%d lowers=%d)",
                                chat_id, len(reviews), len(disagreements),
                                sum(1 for r in disagreements if r["verdict"] == "raise"),
                                sum(1 for r in disagreements if r["verdict"] == "lower"),
                            )
                            # One forensic line per review entry so ops
                            # can grep specific patterns later.
                            for r in reviews:
                                j = ext_to_job.get(r["id"])
                                forensic.log_step(
                                    "scoring_audit.review",
                                    input={
                                        "external_id": r["id"],
                                        "title": (getattr(j, "title", "") or "")[:120],
                                        "company": (getattr(j, "company", "") or "")[:80],
                                        "source": getattr(j, "source", ""),
                                    },
                                    output={
                                        "original_score": r["original_score"],
                                        "revised_score":  r["revised_score"],
                                        "verdict":        r["verdict"],
                                        "comment":        r["comment"],
                                    },
                                    chat_id=chat_id,
                                    run_id=pctx.run_id,
                                )
                    except Exception:
                        log.exception("User %s: scoring audit raised; "
                                      "continuing", chat_id)
                # -----------------------------------------------------

                # Auto profile rebuild (algorithm v2.8 / P4). If the
                # user has accumulated ≥ K skip-feedback events since
                # the last rebuild, kick a fresh Opus build now. Runs
                # at most once per iteration per user; on a transient
                # failure the counter stays and the next iteration
                # retries. No-send / dry-run paths still fire — the
                # rebuild is independent of digest delivery and writes
                # to the profile, which downstream iterations need.
                try:
                    auto_thr = int(
                        filters.get("auto_rebuild_skip_threshold") or 5
                    )
                except (TypeError, ValueError):
                    auto_thr = 5
                try:
                    _maybe_auto_rebuild_profile(
                        db, chat_id, threshold=auto_thr,
                    )
                except Exception:
                    log.exception(
                        "User %s: auto-rebuild check raised; continuing",
                        chat_id,
                    )

                # M2: roll the per-query attribution up into `query_runs`
                # on the FLUSH/send path (the hold path recorded its own
                # row before `continue`). `sent_ids_this_run` is populated
                # by the send block above (empty on a no-send/dry preview).
                _record_query_funnel(
                    store,
                    pipeline_run_id=pctx.run_id,
                    chat_id=chat_id,
                    attribution=query_attribution,
                    enrichments_by_job_id=enrichments_by_job_id,
                    match_floor=effective_min_score,
                    queued_job_ids=queued_ids_this_run,
                    sent_job_ids=sent_ids_this_run,
                    started_at=user_started_at,
                    finished_at=time.time(),
                )

                # M2: closed-loop query optimiser (config-gated, default
                # OFF in defaults.py). Runs on its OWN cadence — independent
                # of the skip-rebuild loop — so a 0-send user whose skip
                # counter never fires still gets tuned from the funnel's
                # scored/matched signal. Best-effort: never blocks the run.
                try:
                    _maybe_optimize_queries(db, store, chat_id, filters)
                except Exception:
                    log.exception(
                        "User %s: query-optimiser check raised; continuing",
                        chat_id,
                    )

            stats["jobs_sent_total"] = total_sent
            log.info(
                "DIGEST_SUMMARY users=%d raw=%d sent=%d web=%d li_user=%d",
                stats["users_total"],
                stats["jobs_raw_total"],
                stats["jobs_sent_total"],
                stats["web_search_hits"],
                stats["linkedin_user_hits"],
            )

            pctx.set_users_total(stats["users_total"])
            pctx.set_jobs_raw(stats["jobs_raw_total"])
            pctx.set_jobs_sent(stats["jobs_sent_total"])
            pctx.record_extra("web_hits", stats["web_search_hits"])
            pctx.record_extra("linkedin_user_hits", stats["linkedin_user_hits"])
            if errors:
                pctx.incr_errors(len(errors))
                pctx.record_extra("source_errors", errors)

            if send_failed_chat is not None:
                exit_code = 3
            elif errors:
                exit_code = 2
            else:
                exit_code = 0
            pctx.set_exit_code(exit_code)
    except Exception:
        log.exception("search_jobs.run: top-level failure (escaped error_capture)")
        exit_code = 3

    if run_id_for_summary is not None and not no_send:
        try:
            deliver_daily_summary(tg, store, run_id_for_summary, db=db)
        except Exception:
            log.exception("daily summary delivery raised")

    return exit_code


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Print results, don't post or persist sends.")
    ap.add_argument("--chat-id", type=int, default=None, help="Send only to this one chat_id.")
    ap.add_argument(
        "--no-send",
        action="store_true",
        help=(
            "Run full pipeline (fetch + score) but skip Telegram send, sent_messages "
            "persistence, and the post-send scoring_audit pass. Use to validate "
            "what would be sent and to inspect AI spend before delivering."
        ),
    )
    args = ap.parse_args()
    sys.exit(run(dry_run=args.dry_run, only_chat=args.chat_id, no_send=args.no_send))
