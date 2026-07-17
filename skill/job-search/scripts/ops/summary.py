"""Per-run operator message — bare facts only, no table formatting.

Restyled 2026-05-29 per the operator's request. One message per pipeline
run (the continuous searcher delivers one per finished iteration, and a
run is always for a single user). Layout:

  👤 <user_id> — <N> positions sent
  💵 $<total_cost_usd>
  🔢 <in> in · <out> out · <cache-read> · <cache-write> (<total>)

  📊 By source (seen · cleared ≥<min_score> · sent):
  • <source>: <seen_count> · <cleared_count> · <sent_count>
  • ...

  📤 Positions:
  • [<score>] <Title> — <Company>
  • ...

Per-source retrieved/cleared counts were reinstated 2026-07-02 per operator
request (they were dropped in the 2026-05-29 restyle along with the
recent-runs table and queue contents — those stay dropped, only the
per-source breakdown came back). No recent-runs table, no queue contents,
no anomalies/footer.

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

def _user_chats_for_run(sources: Iterable[Any]) -> list[int]:
    """Distinct chat_ids this run's per-user source rows touched, in first-seen
    order. Continuous-searcher cycles yield one; the scheduled multi-user
    digest (the 10:00 flush) yields all processed users. Empty for
    global-only runs."""
    out: list[int] = []
    for s in sources or []:
        chat = _row_get(s, "user_chat_id")
        if chat is None:
            continue
        try:
            chat = int(chat)
        except (TypeError, ValueError):
            continue
        if chat not in out:
            out.append(chat)
    return out


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

    Score comes from the latest `job_scores` row at send time. Historical
    profile_hash rows for the same job must not duplicate the sent position.
    A job with no persisted score row yields score=None. Empty when nothing
    shipped or `db` is unavailable.
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
                       COALESCE(
                           (
                               SELECT sc.match_score
                                 FROM job_scores sc
                                WHERE sc.chat_id = s.chat_id
                                  AND sc.job_id = s.job_id
                                  AND sc.scored_at <= s.sent_at + 60
                                ORDER BY sc.scored_at DESC
                                LIMIT 1
                           ),
                           (
                               SELECT sc.match_score
                                 FROM job_scores sc
                                WHERE sc.chat_id = s.chat_id
                                  AND sc.job_id = s.job_id
                                ORDER BY sc.scored_at DESC
                                LIMIT 1
                           )
                       ) AS match_score,
                       hc.status AS hc_status, hc.contact_json
                  FROM sent_messages s
                  JOIN jobs j ON j.job_id = s.job_id
             LEFT JOIN hiring_contacts hc
                    ON hc.job_id = s.job_id
                 WHERE s.chat_id = ? AND s.sent_at BETWEEN ? AND ?
                 ORDER BY match_score IS NULL, match_score DESC, s.sent_at
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


def _default_min_score() -> int:
    """Fallback ⭐ floor when a user hasn't set their own — mirrors
    `defaults.DEFAULTS["ai_min_match_score"]`. Lazily imported (same
    reasoning as `_detail_fetch._browser_fallback_config`: keep this
    module importable in isolation) with a hardcoded fallback matching
    today's shipped default if the import itself fails."""
    try:
        from defaults import DEFAULTS
        return max(0, min(5, int(DEFAULTS.get("ai_min_match_score", 4) or 0)))
    except Exception:
        return 4


def _effective_min_score(db: Any, chat_id: int | None) -> int:
    """The ⭐ floor a job's `match_score` had to clear to count as
    "cleared" below — same rule `search_jobs.py` applies at send time
    (per-user override if set, else the global default). Best-effort:
    any lookup failure falls back to the global default."""
    default = _default_min_score()
    if db is None or chat_id is None:
        return default
    try:
        user_min = int(db.get_min_match_score(int(chat_id)) or 0)
    except Exception:
        return default
    return user_min if user_min > 0 else default


def _source_breakdown_for_run(
    sources: Iterable[Any],
    db: Any,
    chat_id: int | None,
    run_id: int | None,
    started: float,
    finished: float,
    min_score: int,
    sent: Iterable[dict] | None = None,
) -> list[dict]:
    """Per-source seen / cleared / sent counts for this run.

    "seen" starts with `source_runs.raw_count` per `source_key` — the raw
    count each instrumented adapter returned. Some Apify-cache/global-source
    paths can leave no `source_runs` row for a source that still reached
    scoring, so missing keys fall back to `digest_run_jobs` row counts.

    "cleared" counts DISTINCT `digest_run_jobs` rows for this exact
    (chat_id, run_id) whose `match_score >= min_score`. That cache is the
    run snapshot used by the score-floor buttons, so cached score rows are
    counted correctly even when `job_scores.scored_at` predates this run.
    Older DBs/runs fall back to the old job_scores time-window lookup.
    `None` when `db` is unavailable — distinct from a genuine 0, rendered
    as "?" by the caller.

    "sent" comes from the same `sent_messages` window that renders the
    positions list, so quality-buffer flushes show their true sources too.

    Returns one dict per source key that appeared in seen/cleared/sent,
    sorted by seen count descending (then source), e.g.:
      [{"source": "linkedin", "seen": 42, "cleared": 6, "sent": 3}, ...]
    """
    seen: dict[str, int] = {}
    for s in sources or []:
        key = _row_get(s, "source_key") or "?"
        seen[key] = seen.get(key, 0) + int(_row_get(s, "raw_count", 0) or 0)

    cleared: dict[str, int] | None = None
    if db is not None and chat_id is not None and run_id is not None:
        try:
            with db._conn() as c:
                rows = c.execute(
                    """
                    SELECT j.source AS source_key,
                           COUNT(DISTINCT d.job_id) AS seen_n,
                           COUNT(DISTINCT CASE
                               WHEN d.match_score >= ? THEN d.job_id
                           END) AS cleared_n
                      FROM digest_run_jobs d
                      JOIN jobs j ON j.job_id = d.job_id
                     WHERE d.chat_id = ? AND d.run_id = ?
                     GROUP BY j.source
                    """,
                    (int(min_score), int(chat_id), int(run_id)),
                ).fetchall()
            if rows:
                cleared = {}
                for r in rows:
                    key = r["source_key"] or "?"
                    seen.setdefault(key, int(r["seen_n"] or 0))
                    cleared[key] = int(r["cleared_n"] or 0)
        except Exception:
            log.exception("admin_summary: digest-run source lookup failed")
            cleared = None

    if cleared is None and db is not None and chat_id is not None and started:
        lo = float(started) - 10.0
        hi = max(float(finished or 0.0), time.time()) + 60.0
        try:
            with db._conn() as c:
                rows = c.execute(
                    """
                    SELECT j.source AS source_key, COUNT(DISTINCT j.job_id) AS n
                      FROM job_scores sc
                      JOIN jobs j ON j.job_id = sc.job_id
                     WHERE sc.chat_id = ? AND sc.scored_at BETWEEN ? AND ?
                       AND sc.match_score >= ?
                     GROUP BY j.source
                    """,
                    (int(chat_id), lo, hi, int(min_score)),
                ).fetchall()
            cleared = {(r["source_key"] or "?"): int(r["n"] or 0) for r in rows}
        except Exception:
            log.exception("admin_summary: source cleared-count lookup failed")
            cleared = None

    sent_counts: dict[str, int] = {}
    for j in sent or []:
        key = (j.get("source") or "?") if isinstance(j, dict) else "?"
        sent_counts[key] = sent_counts.get(key, 0) + 1

    keys = set(seen)
    if cleared is not None:
        keys.update(cleared)
    keys.update(sent_counts)

    out = [
        {
            "source": key,
            "seen": seen.get(key, 0),
            # `cleared is not None` means the query ran fine; a source key
            # simply absent from its GROUP BY result genuinely had ZERO
            # postings clear the floor (default 0), not "unknown" (None,
            # rendered "?" by the caller) — those are different facts.
            "cleared": cleared.get(key, 0) if cleared is not None else None,
            "sent": sent_counts.get(key, 0),
        }
        for key in keys
    ]
    out.sort(key=lambda r: (-r["seen"], -int(r["cleared"] or 0), -r["sent"], r["source"]))
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

    user_chats = _user_chats_for_run(sources)
    user_chat = user_chats[0] if user_chats else None
    started = float(_row_get(run, "started_at", 0) or 0)
    finished = float(_row_get(run, "finished_at", 0) or 0)

    ct = _cost_tokens_for_run(db, started, finished)

    if len(user_chats) > 1:
        return _build_multi_user_summary(
            db, sources, _row_get(run, "id"), run, user_chats,
            started, finished, ct,
        )
    sent = _sent_jobs_for_run(db, user_chat, started, finished)
    min_score = _effective_min_score(db, user_chat)
    breakdown = _source_breakdown_for_run(
        sources, db, user_chat, _row_get(run, "id"), started, finished, min_score, sent,
    )

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

    if breakdown:
        lines.append("")
        lines.append(f"📊 By source (seen · cleared ≥{min_score} · sent):")
        for b in breakdown:
            cleared_txt = "?" if b["cleared"] is None else str(b["cleared"])
            lines.append(
                f"• {_html_escape(b['source'])}: "
                f"{b['seen']} · {cleared_txt} · {b['sent']}"
            )

    if sent:
        lines.append("")
        lines.append("📤 Positions:")
        lines.extend(_position_lines(sent))

    return "\n".join(lines)


def _position_lines(sent: Iterable[dict]) -> list[str]:
    lines = []
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
    return lines


def _build_multi_user_summary(
    db: Any,
    sources: Iterable[Any],
    run_id: Any,
    run: Any,
    user_chats: list[int],
    started: float,
    finished: float,
    ct: dict,
) -> str:
    """Operator message for a multi-user run (the scheduled 10:00 flush):
    one section per user with their shipped positions, so the operator can
    see who received what — the single-user render only showed the first
    user and hid the rest of the fleet."""
    per_user = [
        (chat, _sent_jobs_for_run(db, chat, started, finished))
        for chat in user_chats
    ]
    all_sent = [j for _, js in per_user for j in js]
    if db is not None:
        n_sent = len(all_sent)
    else:
        n_sent = int(_row_get(run, "jobs_sent", 0) or 0)

    total_tok = ct["in_tok"] + ct["out_tok"] + ct["cr_tok"] + ct["cc_tok"]
    lines = [
        f"🌊 Digest flush — {len(user_chats)} users, "
        f"{n_sent} position{'' if n_sent == 1 else 's'} sent",
        f"💵 {_fmt_usd(ct['cost_us'])}",
        (
            f"🔢 {_humanize_int(ct['in_tok'])} in · "
            f"{_humanize_int(ct['out_tok'])} out · "
            f"{_humanize_int(ct['cr_tok'])} cache-read · "
            f"{_humanize_int(ct['cc_tok'])} cache-write "
            f"({_humanize_int(total_tok)} total)"
        ),
    ]

    # ponytail: chat_id=None → the "cleared" column renders "?" (no single
    # ⭐ floor exists across users); per-user cleared counts would need one
    # digest_run_jobs query per chat — add if operators ever ask.
    min_score = _default_min_score()
    breakdown = _source_breakdown_for_run(
        sources, db, None, run_id, started, finished, min_score, all_sent,
    )
    if breakdown:
        lines.append("")
        lines.append("📊 By source (seen · cleared · sent):")
        for b in breakdown:
            cleared_txt = "?" if b["cleared"] is None else str(b["cleared"])
            lines.append(
                f"• {_html_escape(b['source'])}: "
                f"{b['seen']} · {cleared_txt} · {b['sent']}"
            )

    for chat, js in per_user:
        lines.append("")
        lines.append(
            f"👤 {chat} — {len(js)} position{'' if len(js) == 1 else 's'}"
        )
        lines.extend(_position_lines(js))

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
