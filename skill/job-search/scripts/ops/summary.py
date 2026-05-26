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

def build_daily_summary(store: Any, run_id: int, db: Any | None = None) -> str:
    """Render the per-run admin message body. See module docstring.

    Signature keeps the legacy (store, run_id) prefix so older test
    harnesses that don't pass `db` get a slightly thinner message (the
    queue + queue-contents sections are skipped when `db` is None, since
    they need direct access to the main DB; the funnel section
    degrades to fetched-only).
    """
    try:
        run, sources = store.pipeline_run_with_sources(run_id)
    except Exception:
        log.exception("admin_summary: pipeline_run_with_sources failed")
        run, sources = None, []

    started = float(_row_get(run, "started_at", 0) or 0)
    finished = float(_row_get(run, "finished_at", 0) or 0)
    user_chat = _user_chat_for_run(sources)

    lines: list[str] = []

    # ---- 1. Queue depths (all users) ----
    lines.append("Queue")
    qrows = _queue_per_user(db)
    if not qrows:
        lines.append("  (empty)")
    else:
        for chat, depth, age in qrows:
            if age is None:
                lines.append(f"  chat {chat}: {depth}")
            else:
                lines.append(f"  chat {chat}: {depth}  (oldest {age:.1f}h)")
    lines.append("")

    # ---- 2. Per-source funnel (this run) ----
    funnel = _funnel_for_run(db, sources, user_chat, started, finished)
    chat_label = f"chat {user_chat}" if user_chat is not None else "global"
    lines.append(f"Sources this run ({chat_label})")
    if not funnel:
        lines.append("  (no source data)")
    else:
        lines.append("  source           fetched  scored  matched  queued")
        for src, f, sc, m, q in funnel:
            lines.append(
                f"  {src:<16} {f:>7}  {sc:>6}  {m:>7}  {q:>6}"
            )
    lines.append("")

    # ---- 3. Queue contents (all users) ----
    lines.append("Queue contents")
    by_chat = _queue_entries_per_user(db)
    if not by_chat:
        lines.append("  (empty)")
    else:
        for chat in sorted(by_chat):
            lines.append(f"  chat {chat}:")
            for e in by_chat[chat]:
                src = e["source"]
                comp = f" — {e['company']}" if e["company"] else ""
                lines.append(
                    f"    [{e['match_score']}] {e['title']}{comp}  ({src})"
                )
                if e["url"]:
                    lines.append(f"      {e['url']}")

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
                # send_plain bypasses the MarkdownV2 parser; our body contains
                # raw '(', '_', '-' etc. and we don't want to escape every URL.
                tg.send_plain(op, chunk)
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
