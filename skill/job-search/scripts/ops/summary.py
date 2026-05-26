"""Per-run admin message — queue depths, per-source funnel, queue contents.

Replaces the earlier "daily digest with AI cost block" format. The
operator asked for the bare facts only, with no other content:

  1. Accumulated jobs per user (queue depth + oldest age).
  2. Per-source funnel for THIS run — fetched → scored → matched (≥4)
     → queued. One line per source that contributed at any step.
  3. Title + URL of every position currently sitting in the quality
     buffer, grouped per user.

No header decoration, no anomalies, no footer, no cost numbers. The
caller in search_jobs.py keeps using `deliver_daily_summary(tg, store,
run_id, db)`; the legacy module name is preserved so nothing else has to
change.

Plain text (no parse_mode) — URLs auto-linkify in Telegram and we
sidestep MDv2 escaping headaches across long bodies. Telegram's 4096
char ceiling is respected by chunking when the queue grows past it.
"""
from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Data helpers — all best-effort. A missing table or unexpected row shape
# returns an empty result rather than raising — the operator message must
# never block the digest pipeline.
# ---------------------------------------------------------------------------

def _queue_per_user(db: Any) -> list[tuple[int, int, float | None]]:
    """Return [(chat_id, depth, oldest_age_hours), ...] for every chat with
    a non-empty queue. Sorted by chat_id for deterministic output."""
    if db is None:
        return []
    try:
        with db._conn() as c:
            rows = c.execute(
                """
                SELECT chat_id, COUNT(*) AS depth, MIN(queued_at) AS oldest
                  FROM queued_matches
                 GROUP BY chat_id
                 ORDER BY chat_id
                """,
            ).fetchall()
    except Exception:
        log.exception("admin_summary: queue_per_user query failed")
        return []
    now = time.time()
    out: list[tuple[int, int, float | None]] = []
    for r in rows:
        try:
            chat = int(r["chat_id"])
            depth = int(r["depth"])
            oldest = float(r["oldest"]) if r["oldest"] else None
        except (KeyError, TypeError, ValueError):
            continue
        age_h = (now - oldest) / 3600.0 if oldest else None
        out.append((chat, depth, age_h))
    return out


def _queue_entries_per_user(db: Any) -> dict[int, list[dict]]:
    """For every chat with queued jobs, return ordered entries enriched
    with title/url/source from the jobs table. Order matches send order:
    match_score DESC, queued_at ASC.
    """
    if db is None:
        return {}
    try:
        with db._conn() as c:
            rows = c.execute(
                """
                SELECT q.chat_id, q.job_id, q.match_score, q.queued_at,
                       j.title, j.url, j.source, j.company
                  FROM queued_matches q
             LEFT JOIN jobs j ON j.job_id = q.job_id
                 ORDER BY q.chat_id, q.match_score DESC, q.queued_at ASC
                """,
            ).fetchall()
    except Exception:
        log.exception("admin_summary: queue_entries query failed")
        return {}
    by_chat: dict[int, list[dict]] = {}
    for r in rows:
        try:
            chat = int(r["chat_id"])
        except (KeyError, TypeError, ValueError):
            continue
        by_chat.setdefault(chat, []).append({
            "job_id":      r["job_id"],
            "match_score": int(r["match_score"] or 0),
            "title":       (r["title"] or "(no title)"),
            "url":         (r["url"] or ""),
            "source":      (r["source"] or "?"),
            "company":     (r["company"] or ""),
        })
    return by_chat


def _funnel_for_run(
    db: Any,
    sources: Iterable[Any],
    user_chat: int | None,
    started: float,
    finished: float,
) -> list[tuple[str, int, int, int, int]]:
    """Build the per-source funnel rows for one pipeline run.

    Each row: (source, fetched, scored, matched_ge4, queued).
      * fetched  — `source_runs.raw_count` for the run.
      * scored   — count of `job_scores` rows for this user whose job's
                   source matches, where `scored_at` falls inside this
                   run's time window.
      * matched  — same query, restricted to match_score >= 4.
      * queued   — count of `queued_matches` rows for this user with the
                   same source, queued during the run window.

    Sources that contributed zero at every step are dropped — the
    operator only sees rows that did SOMETHING.
    """
    # Aggregate fetched per source key. Per-user sources (linkedin /
    # web_search) and global sources both land here; same name → same
    # bucket.
    fetched_by_src: dict[str, int] = {}
    for s in sources or []:
        key = str(_row_get(s, "source_key", "") or "")
        if not key:
            continue
        raw = int(_row_get(s, "raw_count", 0) or 0)
        fetched_by_src[key] = fetched_by_src.get(key, 0) + raw

    if not fetched_by_src and not user_chat:
        return []

    # Per-source counts for scored / matched / queued within the run window.
    scored_by_src: dict[str, int] = {}
    matched_by_src: dict[str, int] = {}
    queued_by_src: dict[str, int] = {}

    if db is not None and user_chat is not None and started > 0:
        # Be generous on the upper bound — flushes/score writes can land
        # slightly after `finished_at`. Use now() as a safe ceiling.
        upper = max(finished, time.time())
        try:
            with db._conn() as c:
                for r in c.execute(
                    """
                    SELECT j.source AS src,
                           COUNT(*)                                        AS scored,
                           SUM(CASE WHEN s.match_score >= 4 THEN 1 ELSE 0 END) AS matched
                      FROM job_scores s
                      JOIN jobs j ON j.job_id = s.job_id
                     WHERE s.chat_id = ?
                       AND s.scored_at BETWEEN ? AND ?
                     GROUP BY j.source
                    """,
                    (int(user_chat), float(started), float(upper)),
                ).fetchall():
                    src = str(r["src"] or "")
                    if not src:
                        continue
                    scored_by_src[src] = int(r["scored"] or 0)
                    matched_by_src[src] = int(r["matched"] or 0)

                for r in c.execute(
                    """
                    SELECT j.source AS src, COUNT(*) AS n
                      FROM queued_matches q
                      JOIN jobs j ON j.job_id = q.job_id
                     WHERE q.chat_id = ?
                       AND q.queued_at BETWEEN ? AND ?
                     GROUP BY j.source
                    """,
                    (int(user_chat), float(started), float(upper)),
                ).fetchall():
                    src = str(r["src"] or "")
                    if not src:
                        continue
                    queued_by_src[src] = int(r["n"] or 0)
        except Exception:
            log.exception("admin_summary: funnel query failed")

    all_srcs = sorted(
        set(fetched_by_src) | set(scored_by_src)
        | set(matched_by_src) | set(queued_by_src),
        key=lambda k: (
            -fetched_by_src.get(k, 0),
            -scored_by_src.get(k, 0),
            k,
        ),
    )
    rows: list[tuple[str, int, int, int, int]] = []
    for src in all_srcs:
        f = fetched_by_src.get(src, 0)
        sc = scored_by_src.get(src, 0)
        m = matched_by_src.get(src, 0)
        q = queued_by_src.get(src, 0)
        if f == 0 and sc == 0 and m == 0 and q == 0:
            continue
        rows.append((src, f, sc, m, q))
    return rows


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


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

# How many recent pipeline runs to show in the admin table.
_ADMIN_RUNS_TO_SHOW = 8


def _format_runs_table(rows: list[Any], store: Any) -> str:
    """Render the recent-runs table body as a box-drawing-character table.
    Wrapped in <pre>...</pre> by the caller for Telegram HTML monospace.
    """
    # Column widths tuned to fit ~70-char phone screens.
    header = "│ Run  │ Time  │   User    │ Raw │ Sent │ Web │ LinkedIn │  Dur  │"
    sep    = "├──────┼───────┼───────────┼─────┼──────┼─────┼──────────┼───────┤"
    lines = [header, sep]
    import json
    for r in rows:
        rid = f"#{int(_row_get(r, 'id', 0) or 0)}"
        started = float(_row_get(r, "started_at", 0) or 0)
        finished = float(_row_get(r, "finished_at", 0) or 0)
        ts = time.strftime("%H:%M", time.localtime(started)) if started else "—"
        # Derive user from per-user source rows.
        user_chat = "—"
        try:
            _, sources = store.pipeline_run_with_sources(int(_row_get(r, "id", 0) or 0))
            uc = _user_chat_for_run(sources)
            if uc is not None:
                user_chat = str(uc)
        except Exception:
            pass
        raw = int(_row_get(r, "jobs_raw", 0) or 0)
        sent = int(_row_get(r, "jobs_sent", 0) or 0)
        extra_raw = _row_get(r, "extra_json")
        web = li = 0
        try:
            if extra_raw:
                e = json.loads(extra_raw) if isinstance(extra_raw, str) else dict(extra_raw)
                web = int(e.get("web_hits") or 0)
                li = int(e.get("linkedin_user_hits") or 0)
        except Exception:
            pass
        dur = int(max(0, finished - started)) if (started and finished) else 0
        dur_s = f"{dur}s"
        lines.append(
            f"│ {rid:<4} │ {ts:<5} │ {user_chat:<9} │ {raw:<3} │ {sent:<4} │ {web:<3} │ {li:<8} │ {dur_s:<5} │"
        )
        lines.append(sep)
    # Replace last separator with a closing border (visual polish).
    if len(lines) > 2:
        lines[-1] = sep.replace("├", "└").replace("┼", "┴").replace("┤", "┘")
    return "\n".join(lines)


def build_daily_summary(store: Any, run_id: int, db: Any | None = None) -> str:
    """Render the per-run admin message body.

    2026-05-26: operator asked for the bare facts in a single compact
    table — the previous queue/funnel/contents triplet was too long.
    Body is just the last N pipeline runs in a box-drawing-character
    table, wrapped in <pre>...</pre> so Telegram HTML renders it
    monospace. `run_id` and `db` kept on the signature for back-compat
    with the existing caller in search_jobs.run.
    """
    try:
        rows = store.recent_pipeline_runs(_ADMIN_RUNS_TO_SHOW)
    except Exception:
        log.exception("admin_summary: recent_pipeline_runs failed")
        rows = []

    if not rows:
        return "<pre>(no recent runs)</pre>"
    table = _format_runs_table(list(rows), store)
    return "<pre>" + table + "</pre>"


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
    """Send the admin message to the operator. NEVER raises.

    Suppressed when:
      * `OPERATOR_CHAT_ID` is not set, OR
      * `quiet_alerts` toggle is '1'.

    Chunks the body across multiple Telegram messages if it exceeds
    the per-message limit (the queue can grow long).
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
                # 2026-05-26: body is now an HTML <pre> block so the
                # box-drawing runs table aligns on mobile (Telegram's
                # default text font is variable-width). HTML mode also
                # avoids MarkdownV2's escape-everything tax.
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
