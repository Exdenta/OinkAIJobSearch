"""Feedback-digest loop: periodically summarize accumulated user feedback
into short LLM-written notes injected into the job-scoring prompt.

WHY AN LLM SUMMARY INSTEAD OF HARDCODED RULES
----------------------------------------------
Per this repo's CLAUDE.md design principle ("prefer AI, avoid hardcoded
heuristics"), this module does NOT try to decide what a burst of 👎s
means, whether a skip-without-reason is a real signal, or how long a
preference stays valid. That is exactly the kind of judgment call the
project deliberately routes to a model instead of a regex / score-cap /
allow-list. This module's own responsibility is narrow and mechanical:
gather the raw feedback rows + implicit COUNTS (plain counting, zero
interpretation), hand them to the model inside `prompts/feedback_digest.txt`,
and persist whatever short notes it writes back. All weighing — explicit
vs. implicit, recency, burst-vs-pattern, veto-vs-soft-preference — is the
model's job, guided by rules written into the prompt (see that file).

Sibling module / template
--------------------------
This mirrors `skip_feedback.py`'s shape closely: same prompt-file-on-disk
loading convention, same `claude_cli` / `instrumentation.wrappers.
wrapped_run_p` invocation pattern, same defensive envelope-unwrap +
`parse_json_block` parsing, same "never raises, degrades to None on any
failure, never touches stored state on failure" contract. Where
skip_feedback.py does a narrow SURGICAL edit (append a few exclusion
tokens) after every single skip, this module does a PERIODIC BULK
resummarization (every `feedback_digest_threshold` new feedback events)
that rewrites the whole notes blob from scratch — appropriate because the
output here is a prose paragraph the model should keep coherent and
non-redundant, not a list of independent tokens to merge.

Public API
----------
    maybe_run_feedback_digest(db, chat_id, *, force=False) -> str | None

Returns the fresh notes string on success, or None if the digest was
skipped (below threshold, kill-switched) or failed for any reason. On
failure the previously stored notes (`users.feedback_notes_text`) are
left untouched — a bad digest run must never blank out a good one.

Feedback reason free text was already safety-screened at CAPTURE time
(`safety_check.check_user_input` in bot.py, before `db.set_job_feedback_
reason` is ever called) — see bot.py's skip-feedback text handler. We
still treat every reason string as opaque, clearly-delimited DATA inside
the prompt (never as instructions) as defense in depth; a screen at
capture time doesn't make later prompt construction exempt from the same
discipline every other AI-backed module in this codebase follows.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from claude_cli import SMALLEST_MODEL, extract_assistant_text, parse_json_block
from instrumentation.wrappers import wrapped_run_p
import forensic


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Cheap/fast tier — same rationale as skip_feedback.py: this is a
# high-volume-ish, low-stakes-per-call summarization pass, not a
# from-scratch profile rebuild. Override via CLAUDE_SMALLEST_MODEL.
DEFAULT_MODEL = SMALLEST_MODEL
DEFAULT_TIMEOUT_S = 60

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "feedback_digest.txt"

# Bounded context window fed to the model, most-recent-last. Even a user
# with a long feedback history only gets their latest N rows — this is
# what makes "favor recency" true by construction, not just by prompt
# instruction. The prompt is also told the total so it can judge whether
# it's looking at "all of it" or a truncated tail.
_CONTEXT_WINDOW_TOTAL = 100

# `db.get_job_feedback_since` is ASC-ordered (oldest first) with
# LIMIT-from-the-start semantics — the DB layer is intentionally frozen
# (see repo instructions), so to recover the MOST RECENT rows for a user
# with more history than our context window, we fetch up to this generous
# cap (comfortably above any real user's accumulated feedback — this is a
# personal job bot, not a high-traffic product) and slice the tail in
# Python. See `_gather_explicit`.
_FETCH_CAP = 5000

# Implicit-signal list caps — keep the prompt bounded even for a power
# user with hundreds of applications. These are display caps only; the
# underlying counts (e.g. sent_unactioned_count) are NOT capped.
_MAX_APPLIED_LISTED = 20
_MAX_SKIPPED_LISTED = 20

# Model output caps, enforced server-side (never trust the model to keep
# its own promises) — mirrors skip_feedback.py's "second line of
# defense" philosophy.
_MAX_NOTE_LINES = 12
_MAX_NOTE_LINE_CHARS = 200
_MAX_NOTES_CHARS = 3000


def _feedback_digest_threshold() -> int:
    """Trigger threshold — how many NEW feedback rows since the last
    digest are required before we spend a model call. Config knob so
    operators can tune cadence without a code change."""
    try:
        from defaults import DEFAULTS
        return max(1, int(DEFAULTS.get("feedback_digest_threshold", 5)))
    except Exception:
        return 5


def _feedback_digest_enabled() -> bool:
    """Kill switch, checked here too (not just at the continuous_searcher
    call site) so any other future caller gets the same off-switch for
    free. Default ON — "0" disables. Same convention as
    SKIP_DELETES_MESSAGE / other boolean env knobs in this codebase."""
    import os
    return os.environ.get("FEEDBACK_DIGEST_ENABLED", "1") not in (
        "0", "false", "False", "",
    )


# ---------------------------------------------------------------------------
# Data gathering — plain counting, zero interpretation
# ---------------------------------------------------------------------------

def _gather_explicit(db: Any, chat_id: int) -> list[Any]:
    """Return up to `_CONTEXT_WINDOW_TOTAL` most-recent job_feedback rows
    (oldest-first within the window, matching the DB's own ordering) for
    this user. Empty list on any DB error — a digest failure here must
    never propagate into a broken search iteration."""
    try:
        all_rows = list(
            db.get_job_feedback_since(chat_id, since=0.0, limit=_FETCH_CAP)
        )
    except Exception:
        log.exception("feedback_digest: get_job_feedback_since failed chat=%s", chat_id)
        return []
    return all_rows[-_CONTEXT_WINDOW_TOTAL:]


def _gather_implicit(db: Any, chat_id: int) -> dict[str, Any]:
    """Implicit aggregates: applied (strong positive), skipped-without-any-
    explicit-feedback (medium negative), sent-but-never-actioned (weak
    negative, count only). Pure SQL/Python counting — no scoring, no
    judgment; the model in the prompt decides what these counts mean.

    `applied_jobs` is a public db.py accessor; the other two aggregates
    have no dedicated method (each is used by exactly this one caller),
    so we hand-roll SQL against `db._conn()` — same pattern already used
    by sources/web_search.py's `_recent_web_search_titles` for a
    single-caller query that didn't earn its own db.py method.
    """
    out: dict[str, Any] = {
        "applied": [],
        "skipped_no_reason": [],
        "sent_unactioned_count": 0,
    }

    try:
        applied_rows = db.applied_jobs(chat_id)  # DESC by updated_at
    except Exception:
        log.exception("feedback_digest: applied_jobs failed chat=%s", chat_id)
        applied_rows = []
    for r in list(applied_rows)[:_MAX_APPLIED_LISTED]:
        out["applied"].append({
            "title": (r["title"] or "")[:120],
            "company": (r["company"] or "")[:80],
        })

    try:
        with db._conn() as c:  # noqa: SLF001 — intentional internal use, see docstring
            skipped_rows = c.execute(
                """
                SELECT j.title, j.company
                FROM applications a
                JOIN jobs j ON j.job_id = a.job_id
                LEFT JOIN job_feedback f
                       ON f.chat_id = a.chat_id AND f.job_id = a.job_id
                WHERE a.chat_id = ? AND a.status = 'skipped' AND f.job_id IS NULL
                ORDER BY a.updated_at DESC
                LIMIT ?
                """,
                (chat_id, _MAX_SKIPPED_LISTED),
            ).fetchall()
            sent_unactioned_row = c.execute(
                """
                SELECT COUNT(*) AS n
                FROM sent_messages s
                LEFT JOIN applications a
                       ON a.chat_id = s.chat_id AND a.job_id = s.job_id
                LEFT JOIN job_feedback f
                       ON f.chat_id = s.chat_id AND f.job_id = s.job_id
                WHERE s.chat_id = ? AND a.job_id IS NULL AND f.job_id IS NULL
                """,
                (chat_id,),
            ).fetchone()
    except Exception:
        log.exception("feedback_digest: implicit-signal query failed chat=%s", chat_id)
        skipped_rows = []
        sent_unactioned_row = None

    for r in skipped_rows:
        out["skipped_no_reason"].append({
            "title": (r["title"] or "")[:120],
            "company": (r["company"] or "")[:80],
        })
    out["sent_unactioned_count"] = int(sent_unactioned_row["n"]) if sent_unactioned_row else 0
    return out


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _load_prompt_template() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log.error("feedback_digest: can't read prompt at %s: %s", _PROMPT_PATH, e)
        return ""


def _age_str(created_at: Any, now: float) -> str:
    """Human-relative age ('today' / '3d ago' / '2mo ago') so the model
    can judge recency without doing date arithmetic itself."""
    try:
        ts = float(created_at or 0)
    except (TypeError, ValueError):
        return "unknown"
    if ts <= 0:
        return "unknown"
    days = max(0.0, now - ts) / 86400.0
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 14:
        return f"{int(days)}d ago"
    if days < 60:
        return f"{int(days / 7)}w ago"
    return f"{int(days / 30)}mo ago"


def _render_feedback_line(row: Any, since: float, now: float) -> str:
    verdict = "UP" if (row["verdict"] or "") == "up" else "DOWN"
    tag = "NEW" if float(row["created_at"] or 0) > float(since or 0) else "seen"
    title = str(row["title"] or "?")[:100]
    company = str(row["company"] or "?")[:80]
    reason = str(row["reason"] or "").strip()
    reason_part = f' reason="{reason[:200]}"' if reason else " reason=(none given)"
    age = _age_str(row["created_at"], now)
    return f'- [{tag}] {verdict} "{title}" @ {company} ({age}){reason_part}'


def _render_job_list(jobs: list[dict[str, str]]) -> str:
    if not jobs:
        return "(none)"
    return "\n".join(f'- "{j["title"]}" @ {j["company"]}' for j in jobs)


def _render_prompt(
    window_rows: list[Any],
    implicit: dict[str, Any],
    previous_notes: str | None,
    since: float,
) -> str:
    """Substitute the rendered sections into the template. Plain
    `.replace()`, not `.format()` — the template's JSON schema block
    contains literal `{` / `}` that would break str.format()."""
    tmpl = _load_prompt_template()
    if not tmpl:
        return ""

    now = time.time()
    if window_rows:
        feedback_block = "\n".join(
            _render_feedback_line(r, since, now) for r in window_rows
        )
    else:
        feedback_block = "(no explicit feedback yet)"

    prev = (previous_notes or "").strip()
    prev_block = prev if prev else "(none yet — first digest for this user)"

    return (
        tmpl
        .replace("{previous_notes}", prev_block)
        .replace("{feedback_rows}", feedback_block)
        .replace("{context_window_total}", str(_CONTEXT_WINDOW_TOTAL))
        .replace("{applied_jobs}", _render_job_list(implicit.get("applied") or []))
        .replace("{max_applied}", str(_MAX_APPLIED_LISTED))
        .replace(
            "{skipped_no_reason_jobs}",
            _render_job_list(implicit.get("skipped_no_reason") or []),
        )
        .replace("{max_skipped}", str(_MAX_SKIPPED_LISTED))
        .replace(
            "{sent_unactioned_count}",
            str(int(implicit.get("sent_unactioned_count") or 0)),
        )
        .replace("{max_notes}", str(_MAX_NOTE_LINES))
    )


# ---------------------------------------------------------------------------
# Output parsing / sanitization
# ---------------------------------------------------------------------------

def _parse_model_output(stdout: str | None) -> str | None:
    """Decode the CLI envelope → JSON object → sanitized notes string.

    Returns None on any STRUCTURAL failure (missing stdout, unparseable
    JSON, wrong shape) — these are the only cases where the caller should
    leave the previously stored notes untouched. A well-formed but EMPTY
    `notes` array is a legitimate model decision ("nothing durable to say
    yet"), not a failure, and is passed through as an empty string.
    """
    if stdout is None:
        return None
    body = extract_assistant_text(stdout)
    parsed = parse_json_block(body)
    if not isinstance(parsed, dict):
        return None
    raw_notes = parsed.get("notes")
    if not isinstance(raw_notes, list):
        return None

    lines: list[str] = []
    for item in raw_notes:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s:
            continue
        lines.append(s[:_MAX_NOTE_LINE_CHARS])
        if len(lines) >= _MAX_NOTE_LINES:
            break

    if not lines:
        return ""
    notes = "\n".join(f"- {line}" for line in lines)
    return notes[:_MAX_NOTES_CHARS]


# ---------------------------------------------------------------------------
# Scoring-prompt injection
# ---------------------------------------------------------------------------

# Header the notes section is appended under. Kept as a module constant so
# tests and any future reader (e.g. a "/feedback" bot command that wants to
# strip this section back out) can match on it exactly.
NOTES_SECTION_HEADER = "## Feedback notes (AI-distilled from this user's \U0001F44D/\U0001F44E history)"


def augment_prefs_text(db: Any, chat_id: int, prefs_text: str | None) -> str:
    """Append the stored feedback-digest notes (if any) to `prefs_text` as
    a clearly delimited section.

    Call this ONCE at the point `prefs_text` is read (search_jobs.py,
    where `user_files.read_prefs(chat_id)` is assigned) so every
    downstream consumer of that variable — scoring (`enrich_jobs_ai`), the
    scoring-audit pass (`reanalyze_scoring_ai`), and `_profile_hash` —
    sees the identical augmented text. The profile_hash consequence is
    intentional and desired: newly summarized feedback notes are a real
    scoring input, not cosmetic metadata, so a fresh digest correctly
    invalidates the per-user `job_scores` cache the same way a resume or
    /prefs edit would.

    Never raises; on any DB error (or no notes yet) returns `prefs_text`
    unchanged so a feedback-digest hiccup never blanks out scoring input.
    """
    try:
        notes, _updated_at = db.get_feedback_notes(chat_id)
    except Exception:
        log.exception("feedback_digest: get_feedback_notes failed (augment) chat=%s", chat_id)
        return prefs_text or ""
    notes = (notes or "").strip()
    if not notes:
        return prefs_text or ""
    base = prefs_text or ""
    return f"{base}\n\n{NOTES_SECTION_HEADER}\n{notes}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def maybe_run_feedback_digest(
    db: Any,
    chat_id: int,
    *,
    force: bool = False,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    model: str = DEFAULT_MODEL,
    _run_p=None,    # injected in tests
) -> str | None:
    """Run the feedback digest for `chat_id` if the trigger threshold is
    met (or `force=True`), and persist fresh notes on success.

    Always returns quietly. Never raises — every internal step is wrapped
    so a digest problem can never break the caller's search iteration.
    Returns the fresh notes string on success, else None (below
    threshold, kill-switched, or any failure — in every None case the
    previously stored notes are left exactly as they were).
    """
    if db is None or not chat_id:
        return None

    if not _feedback_digest_enabled():
        return None

    try:
        prev_notes, prev_updated_at = db.get_feedback_notes(chat_id)
    except Exception:
        log.exception("feedback_digest: get_feedback_notes failed chat=%s", chat_id)
        return None
    since = float(prev_updated_at or 0.0)

    if not force:
        try:
            new_count = db.count_job_feedback_since(chat_id, since)
        except Exception:
            log.exception("feedback_digest: count_job_feedback_since failed chat=%s", chat_id)
            return None
        if new_count < _feedback_digest_threshold():
            return None

    window_rows = _gather_explicit(db, chat_id)
    implicit = _gather_implicit(db, chat_id)

    # Nothing new to summarize AND no previous notes worth re-stating —
    # skip the model call entirely rather than asking it to write about
    # nothing. (force=True with a genuinely empty account still reaches
    # here and short-circuits, which is correct: there's nothing to say.)
    has_signal = bool(
        window_rows
        or implicit["applied"]
        or implicit["skipped_no_reason"]
        or implicit["sent_unactioned_count"]
        or (prev_notes or "").strip()
    )
    if not has_signal:
        return None

    prompt = _render_prompt(window_rows, implicit, prev_notes, since)
    if not prompt:
        return None

    forensic_input = {
        "chat_id": chat_id,
        "since": since,
        "window_rows": len(window_rows),
        "new_rows_since_last": sum(
            1 for r in window_rows if float(r["created_at"] or 0) > since
        ),
        "applied_count": len(implicit["applied"]),
        "skipped_no_reason_count": len(implicit["skipped_no_reason"]),
        "sent_unactioned_count": implicit["sent_unactioned_count"],
        "had_previous_notes": bool((prev_notes or "").strip()),
        "force": force,
    }

    runner = _run_p if _run_p is not None else wrapped_run_p
    try:
        if _run_p is not None:
            stdout = runner(prompt, timeout_s=timeout_s, model=model)
        else:
            stdout = runner(
                None, "feedback_digest", prompt,
                timeout_s=timeout_s, model=model, chat_id=chat_id,
            )
    except Exception as e:
        log.exception("feedback_digest: runner raised for chat=%s: %s", chat_id, e)
        forensic.log_step(
            "feedback_digest.run",
            input=forensic_input,
            output={"error": f"runner: {e!r}"},
            chat_id=chat_id,
        )
        return None

    notes = _parse_model_output(stdout)
    if notes is None:
        forensic.log_step(
            "feedback_digest.run",
            input=forensic_input,
            output={"skipped": "parse_error"},
            chat_id=chat_id,
        )
        return None

    try:
        db.set_feedback_notes(chat_id, notes)
    except Exception:
        log.exception("feedback_digest: set_feedback_notes failed chat=%s", chat_id)
        forensic.log_step(
            "feedback_digest.run",
            input=forensic_input,
            output={"error": "set_feedback_notes failed"},
            chat_id=chat_id,
        )
        return None

    forensic.log_step(
        "feedback_digest.run",
        input=forensic_input,
        output={"notes_chars": len(notes), "notes_head": notes[:300]},
        chat_id=chat_id,
    )
    return notes
