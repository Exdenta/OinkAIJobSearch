"""Per-run operator message — bare facts only, no table formatting.

Restyled 2026-05-29 per the operator's request. One message per pipeline
run (the continuous searcher delivers one per finished iteration, and a
run is always for a single user). Layout:

  👤 <user_id> — <N> positions sent
  💵 $<total_cost_usd>
  🔢 <in> in · <out> out · <cache-read> · <cache-write> (<total>)

  📤 Positions:
  • [<score>] <Title> — <Company>
     ↳ 👤 <Contact name — their title> · <why this person>
  • ...

The ↳ line mirrors the "who to write to" block on the user's job card
(read back from the `hiring_contacts` cache — no extra LLM call) and is
omitted when the send-time lookup found nobody.

That is ALL the operator sees — no recent-runs table, no per-source
funnel, no queue contents, no anomalies/footer.

Cost and token usage are summed from `claude_calls` over the run's time
window. The table HAS `pipeline_run_id` / `chat_id` columns but the
instrumentation does not populate them, so attribution is by TIME WINDOW.
That is accurate in practice: `continuous_searcher` runs are staggered
hours apart and each lasts ~25 min, so at most one run is ever active —
the window catches exactly that run's calls. `cost_actual_us` (the CLI
envelope's real `total_cost_usd`) is preferred per row, falling back to
the char-count surrogate `cost_estimate_us`.

The positions list + scores come from `sent_messages ⨝ jobs ⨝
job_scores` on (chat_id, job_id). Every db-backed figure degrades to
0 / empty when `db is None` (e.g. smoke tests pass no db).

The caller in search_jobs.py keeps using `deliver_daily_summary(tg,
store, run_id, db)`; the legacy module/function names are preserved so
nothing else has to change.

parse_mode="HTML" — titles render as clickable links; only the four
HTML-significant chars are escaped. Telegram's 4096-char ceiling is
respected by line-boundary chunking when a run ships a long list.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable

from telegram_client import TelegramClient
from .operator import _operator_chat_id

log = logging.getLogger(__name__)


# Telegram per-message ceiling. Stay below to leave headroom for emoji
# UTF-8 expansion and any "(continued)" preamble.
_TELEGRAM_MSG_LIMIT = 3800


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Tolerant accessor: works for sqlite3.Row, dict, or attribute objects."""
    if row is None:
        return default
    try:
        if key in row.keys():  # type: ignore[attr-defined]
            return row[key]
    except (AttributeError, TypeError):
        pass
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        pass
    return getattr(row, key, default)


def _html_escape(s: str) -> str:
    """Minimal HTML escape — only the four chars Telegram cares about
    in visible text and href attributes."""
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt_usd(micros: Any) -> str:
    """Render micro-USD (millionths of a dollar) as `$D.DDDD`."""
    try:
        return f"${(int(micros or 0) / 1_000_000):.4f}"
    except (TypeError, ValueError):
        return "$0.0000"


def _humanize_int(n: Any) -> str:
    """Compact human token count: 812 → '812', 12839 → '12.8K',
    8_570_764 → '8.57M'. Operators scan magnitude, not exact digits."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "0"
    if n < 1_000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1_000:.1f}K"
    return f"{n / 1_000_000:.2f}M"


# ---------------------------------------------------------------------------
# Data helpers — all best-effort. A missing table or unexpected row shape
# returns an empty / zeroed result rather than raising — the operator
# message must never block the digest pipeline.
# ---------------------------------------------------------------------------

def _user_chat_for_run(sources: Iterable[Any]) -> int | None:
    """Pipeline runs are per-user — the first per-user source row tells us
    which chat_id this run was for. Returns None for global-only runs."""
    for s in sources or []:
        chat = _row_get(s, "user_chat_id")
        if chat is not None:
            try:
                return int(chat)
            except (TypeError, ValueError):
                continue
    return None


def _cost_tokens_for_run(
    db: Any,
    started: float,
    finished: float,
) -> dict:
    """Sum cost + token usage from `claude_calls` over this run's window.

    Window is [started, max(finished, now)] on `finished_at`. The upper
    bound floats to `now()` because this is often called before the run's
    `finished_at` is stamped (the summary is built mid-finish), and a call
    can land a hair after the run row updates. Staggered runs mean no other
    run's calls fall inside the window.

    Cost prefers `cost_actual_us` (the CLI envelope's real total_cost_usd)
    and falls back per row to the char-count surrogate `cost_estimate_us`.

    Returns zeros on any failure or when `db is None`.
    """
    zero = {"cost_us": 0, "in_tok": 0, "out_tok": 0, "cr_tok": 0, "cc_tok": 0, "calls": 0}
    if db is None or not started:
        return zero
    upper = max(float(finished or 0.0), time.time())
    try:
        with db._conn() as c:
            r = c.execute(
                """
                SELECT COUNT(*)                                              AS calls,
                       COALESCE(SUM(COALESCE(cost_actual_us, cost_estimate_us)), 0) AS cost_us,
                       COALESCE(SUM(input_tokens), 0)                        AS in_tok,
                       COALESCE(SUM(output_tokens), 0)                       AS out_tok,
                       COALESCE(SUM(cache_read_tokens), 0)                   AS cr_tok,
                       COALESCE(SUM(cache_creation_tokens), 0)               AS cc_tok
                  FROM claude_calls
                 WHERE finished_at BETWEEN ? AND ?
                """,
                (float(started), float(upper)),
            ).fetchone()
    except Exception:
        log.exception("admin_summary: cost/token window query failed")
        return zero
    if r is None:
        return zero
    return {
        "cost_us": int(r["cost_us"] or 0),
        "in_tok":  int(r["in_tok"] or 0),
        "out_tok": int(r["out_tok"] or 0),
        "cr_tok":  int(r["cr_tok"] or 0),
        "cc_tok":  int(r["cc_tok"] or 0),
        "calls":   int(r["calls"] or 0),
    }


def _sent_jobs_for_run(
    db: Any,
    chat_id: int | None,
    started: float,
    finished: float,
) -> list[dict]:
    """Return jobs shipped during this run's window, with their match score.

    List shape, ordered best-match-first (NULL scores last):
      [{"title", "url", "company", "source", "score": int | None,
        "contact": dict | None}, ...]

    Score comes from `job_scores` via LEFT JOIN on (chat_id, job_id) — a
    job with no persisted score row yields score=None. `contact` is the
    hiring contact the user's card carried, read back from the
    `hiring_contacts` cache that the send-time lookup populated (job-keyed,
    so no extra LLM call here); None when the lookup found nobody or the
    feature is off. Empty when nothing shipped or `db` is unavailable.
    """
    if db is None or chat_id is None or not started:
        return []
    # Pad the window slightly: a flush can write `sent_at` a hair before
    # the run's `finished_at` is stamped (concurrent context exit).
    lo = float(started) - 10.0
    hi = max(float(finished or 0.0), time.time()) + 60.0
    try:
        with db._conn() as c:
            rows = c.execute(
                """
                SELECT j.title, j.url, j.company, j.source, s.sent_at,
                       sc.match_score,
                       hc.status AS hc_status, hc.contact_json
                  FROM sent_messages s
                  JOIN jobs j ON j.job_id = s.job_id
             LEFT JOIN job_scores sc
                    ON sc.chat_id = s.chat_id AND sc.job_id = s.job_id
             LEFT JOIN hiring_contacts hc
                    ON hc.job_id = s.job_id
                 WHERE s.chat_id = ? AND s.sent_at BETWEEN ? AND ?
                 ORDER BY sc.match_score DESC, s.sent_at
                """,
                (int(chat_id), lo, hi),
            ).fetchall()
    except Exception:
        log.exception("admin_summary: sent_jobs lookup failed")
        return []
    out: list[dict] = []
    for r in rows:
        score = r["match_score"]
        contact = None
        if r["hc_status"] == "found" and r["contact_json"]:
            try:
                parsed = json.loads(r["contact_json"])
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("name"):
                contact = parsed
        out.append({
            "title":   (r["title"] or "(no title)"),
            "url":     (r["url"] or ""),
            "company": (r["company"] or ""),
            "source":  (r["source"] or "?"),
            "score":   int(score) if score is not None else None,
            "contact": contact,
        })
    return out


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def build_daily_summary(store: Any, run_id: int, db: Any | None = None) -> str:
    """Render the per-run operator message body (see module docstring).

    Only the run identified by `run_id` is described. Returns a plain
    placeholder string when the run id is unknown.
    """
    run = None
    sources: list = []
    try:
        run, sources = store.pipeline_run_with_sources(int(run_id))
    except Exception:
        log.exception("admin_summary: pipeline_run lookup failed (run_id=%s)", run_id)

    if run is None:
        return "(no run data for #%d)" % int(run_id or 0)

    user_chat = _user_chat_for_run(sources)
    started = float(_row_get(run, "started_at", 0) or 0)
    finished = float(_row_get(run, "finished_at", 0) or 0)

    ct = _cost_tokens_for_run(db, started, finished)
    sent = _sent_jobs_for_run(db, user_chat, started, finished)

    # Headline count: when we can enumerate the shipped messages (db
    # present) use that, so the number matches the list shown directly
    # below it. Otherwise fall back to the run's own tally.
    if db is not None:
        n_sent = len(sent)
    else:
        n_sent = int(_row_get(run, "jobs_sent", 0) or 0)

    user_label = str(user_chat) if user_chat is not None else "—"
    total_tok = ct["in_tok"] + ct["out_tok"] + ct["cr_tok"] + ct["cc_tok"]

    lines = [
        f"👤 {user_label} — {n_sent} position{'' if n_sent == 1 else 's'} sent",
        f"💵 {_fmt_usd(ct['cost_us'])}",
        (
            f"🔢 {_humanize_int(ct['in_tok'])} in · "
            f"{_humanize_int(ct['out_tok'])} out · "
            f"{_humanize_int(ct['cr_tok'])} cache-read · "
            f"{_humanize_int(ct['cc_tok'])} cache-write "
            f"({_humanize_int(total_tok)} total)"
        ),
    ]

    if sent:
        lines.append("")
        lines.append("📤 Positions:")
        for j in sent:
            score = j.get("score")
            score_txt = f"[{score}]" if score is not None else "[?]"
            title = _html_escape(j["title"])
            comp = _html_escape(j["company"])
            url = _html_escape(j["url"])
            comp_suffix = f" — {comp}" if comp else ""
            if url:
                lines.append(f"• {score_txt} <a href=\"{url}\">{title}</a>{comp_suffix}")
            else:
                lines.append(f"• {score_txt} {title}{comp_suffix}")
            # Mirror of the user card's "who to write to" block, indented
            # under its position so the operator sees exactly what the
            # user was told to do next.
            contact = j.get("contact")
            if contact:
                c_name = _html_escape(str(contact.get("name") or ""))
                c_title = _html_escape(str(contact.get("title") or ""))
                c_url = _html_escape(str(contact.get("profile_url") or ""))
                c_reason = _html_escape(str(contact.get("reason") or ""))
                label = f"{c_name} — {c_title}" if c_title else c_name
                line = (
                    f"   ↳ 👤 <a href=\"{c_url}\">{label}</a>"
                    if c_url else f"   ↳ 👤 {label}"
                )
                if c_reason:
                    line += f" · {c_reason}"
                lines.append(line)

    return "\n".join(lines)


def _chunk_by_size(body: str, limit: int = _TELEGRAM_MSG_LIMIT) -> list[str]:
    """Split the body into Telegram-safe chunks at line boundaries.
    Preserves indentation and never splits a single line."""
    if len(body) <= limit:
        return [body]
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for line in body.splitlines():
        ln = len(line) + 1  # +1 for the joining newline
        if size + ln > limit and buf:
            out.append("\n".join(buf))
            buf = []
            size = 0
        buf.append(line)
        size += ln
    if buf:
        out.append("\n".join(buf))
    return out


def deliver_daily_summary(
    tg: TelegramClient,
    store: Any,
    run_id: int,
    db: Any | None = None,
) -> None:
    """Send the operator message. NEVER raises.

    Suppressed when:
      * `OPERATOR_CHAT_ID` is not set, OR
      * `quiet_alerts` toggle is '1'.

    Chunks the body across multiple Telegram messages if it exceeds the
    per-message limit (a run that ships many positions can run long).
    """
    try:
        op = _operator_chat_id()
        if op is None:
            return
        try:
            quiet = store.get_toggle("quiet_alerts", "0")
        except Exception:
            log.exception("admin_summary: failed to read quiet_alerts toggle")
            return
        if str(quiet) == "1":
            return

        body = build_daily_summary(store, run_id, db=db)
        chunks = _chunk_by_size(body)
        # SIGTERM trace (2026-05-26): wrap each chunk send in a try-block
        # that catches BaseException so SystemExit / KeyboardInterrupt
        # caused by a `kill` during the Telegram POST gets logged. The
        # "delivered N/M chunks" line at start/end lets operators spot
        # interrupted deliveries from logs alone (no more silent loss).
        log.info(
            "admin_summary: delivering run_id=%s — %d chunk(s) to chat %s",
            run_id, len(chunks), op,
        )
        delivered = 0
        for i, chunk in enumerate(chunks):
            try:
                # HTML mode: position titles render as clickable <a> links
                # and we sidestep MarkdownV2's escape-everything tax.
                tg.send_message(op, chunk, parse_mode="HTML")
                delivered += 1
            except BaseException:  # noqa: BLE001 — SIGTERM-aware
                log.exception(
                    "admin_summary: chunk %d/%d send aborted "
                    "(run_id=%s, delivered=%d before abort)",
                    i + 1, len(chunks), run_id, delivered,
                )
                raise
        log.info(
            "admin_summary: run_id=%s delivered %d/%d chunk(s)",
            run_id, delivered, len(chunks),
        )
    except Exception:
        log.exception(
            "deliver_daily_summary: unhandled error swallowed (run_id=%s)",
            run_id,
        )
