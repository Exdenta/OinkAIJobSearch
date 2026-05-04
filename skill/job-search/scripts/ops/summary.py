"""Daily-digest summary builder + delivery.

Plan §6: 4-6 line MarkdownV2 digest fired from `search_jobs.run` after a
successful daily-digest pipeline. Suppressible via the `quiet_alerts` toggle
(distinct from `alerts_enabled`, which gates error alerts).

Read shape (per plan §3, slice A `pipeline_run_with_sources`):
  (run_row, [source_row, ...])

`run_row` keys we use:
  id, kind, status, exit_code, users_total, jobs_raw, jobs_sent,
  error_count, extra_json, started_at, finished_at

`source_row` keys we use:
  source_key, status, raw_count, user_chat_id

The renderer is defensive about missing keys — telemetry rows are sqlite3.Row
or plain dicts depending on whether tests use a fake store. Either works.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from telegram_client import TelegramClient, mdv2_escape
from .operator import _operator_chat_id

log = logging.getLogger(__name__)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Tolerant accessor: works for sqlite3.Row, dict, or attribute objects."""
    if row is None:
        return default
    # Mapping access first (dict / sqlite3.Row both support this).
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


def _fmt_duration(seconds: float) -> str:
    """Compact duration: '4m21s', '12s', '1h03m'."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


def _date_from_ts(ts: float | None) -> str:
    if not ts:
        return time.strftime("%Y-%m-%d", time.gmtime())
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _chunk_chips(chips: list[str], per_row: int = 3) -> list[str]:
    """Pack chips into rows of `per_row` for the bulleted Sources blocks."""
    rows: list[str] = []
    for i in range(0, len(chips), per_row):
        rows.append("  • " + "   • ".join(chips[i:i + per_row]))
    return rows


def build_daily_summary(store: Any, run_id: int) -> str:
    """Render the MarkdownV2 daily-digest summary for the operator.

    Layout:

        🐷 *Daily digest* · 2026-05-01

        📬 *5 jobs* delivered to *1 user*

        📡 *Static sources* — 84 raw
          • hackernews 12   • remote_boards 36   • reliefweb 12
          • euraxess 12   • math_ku_phd 12

        👤 *Per-user sources* — 21 raw
          • linkedin 12   • web_search 9

        ⚠️ *Anomalies:* …

        ⏱ Run #22 · 9m44s · exit 0

    Edge cases handled:
      * zero raw / zero sent → blocks shrink, footer still emitted.
      * all sources failed → synthesized anomaly line.
      * extra_json["anomalies"] from slice B → appended verbatim.
      * jobs_sent (which counts the digest header too) is corrected to a
        per-card count so the operator-facing number matches what users
        actually received.
    """
    run, sources = store.pipeline_run_with_sources(run_id)

    # ---- header ----
    date = _date_from_ts(_row_get(run, "finished_at"))
    header = f"🐷 *Daily digest* · {mdv2_escape(date)}"

    # ---- delivery summary ----
    users_total = int(_row_get(run, "users_total", 0) or 0)
    jobs_raw = int(_row_get(run, "jobs_raw", 0) or 0)
    raw_total_messages = int(_row_get(run, "jobs_sent", 0) or 0)
    # jobs_sent is the raw send_per_job_digest return value, which includes
    # one header message per user that received a digest. Strip it so the
    # operator sees real cards, not "5 jobs + 1 header = 6".
    jobs_delivered = (
        max(0, raw_total_messages - users_total)
        if raw_total_messages > 0 and users_total > 0 else raw_total_messages
    )
    user_word = "user" if users_total == 1 else "users"
    job_word = "job" if jobs_delivered == 1 else "jobs"
    if jobs_delivered == 0:
        line_summary = f"📬 _No jobs delivered to_ *{users_total}* _{user_word}_"
    else:
        line_summary = (
            f"📬 *{jobs_delivered} {job_word}* delivered to *{users_total}* "
            f"_{user_word}_"
        )

    # ---- sources breakdown ----
    global_counts: list[tuple[str, int]] = []
    per_user_counts: dict[str, int] = {}
    error_count = 0
    failed_sources: list[str] = []
    static_raw_total = 0
    per_user_raw_total = 0

    for s in sources or []:
        key = str(_row_get(s, "source_key", "?"))
        status = str(_row_get(s, "status", "ok"))
        raw = int(_row_get(s, "raw_count", 0) or 0)
        user = _row_get(s, "user_chat_id")
        if status in ("failed", "exception"):
            error_count += 1
            if key not in failed_sources:
                failed_sources.append(key)
            continue
        if user is None:
            global_counts.append((key, raw))
            static_raw_total += raw
        else:
            per_user_counts[key] = per_user_counts.get(key, 0) + raw
            per_user_raw_total += raw

    blocks: list[str] = []

    # Single unified Sources block. Static sources first (deterministic
    # order from the run), then per-user sources sorted by count desc so
    # the heaviest contributor reads first. Per-user keys could collide
    # with static names (e.g. linkedin runs in both modes today), so
    # disambiguate the per-user chips with a small suffix.
    chips: list[str] = []
    chips.extend(f"{mdv2_escape(k)} {n}" for k, n in global_counts)
    if per_user_counts:
        ordered = sorted(per_user_counts.items(), key=lambda kv: -kv[1])
        chips.extend(f"{mdv2_escape(k)} {n}" for k, n in ordered)

    raw_total = static_raw_total + per_user_raw_total
    if chips:
        blocks.append(f"📡 *Sources* — {raw_total} raw")
        blocks.extend(_chunk_chips(chips, per_row=3))
    else:
        blocks.append("📡 _no source data_")

    # ---- anomalies ----
    anomalies: list[str] = []
    extra_raw = _row_get(run, "extra_json")
    if extra_raw:
        try:
            extra = json.loads(extra_raw) if isinstance(extra_raw, str) else dict(extra_raw)
            extra_anom = extra.get("anomalies")
            if isinstance(extra_anom, list):
                for a in extra_anom:
                    anomalies.append(str(a))
        except Exception:
            log.debug("summary: extra_json unparseable for run %s", run_id)

    if error_count and failed_sources:
        anomalies.append(
            f"{error_count} source error{'s' if error_count != 1 else ''}: "
            + ", ".join(failed_sources[:5])
        )

    # All-sources-failed degenerate case: synthesize an anomaly so the operator
    # sees something even when slice B didn't write one.
    if sources and len(failed_sources) == len(sources) and len(failed_sources) > 0:
        anomalies.append("all sources failed this run")

    # ---- footer (run id + duration + exit code) ----
    started = float(_row_get(run, "started_at", 0) or 0)
    finished = float(_row_get(run, "finished_at", 0) or 0)
    duration = _fmt_duration(finished - started) if finished and started else "—"
    exit_code = _row_get(run, "exit_code")
    exit_str = str(exit_code) if exit_code is not None else "—"
    line_footer = (
        f"⏱ Run \\#{int(_row_get(run, 'id', run_id))} · "
        f"{mdv2_escape(duration)} · exit {mdv2_escape(exit_str)}"
    )

    # ---- assemble ----
    lines: list[str] = [header, "", line_summary, ""]
    lines.extend(blocks)
    if anomalies:
        # Anomalies are user-derived ("linkedin 0 results × 3 runs"), MDv2-escape.
        lines.append("")
        lines.append(f"⚠️ *Anomalies:* {mdv2_escape(' · '.join(anomalies))}")
    lines.append("")
    lines.append(line_footer)
    return "\n".join(lines)


def deliver_daily_summary(tg: TelegramClient, store: Any, run_id: int) -> None:
    """Send the daily summary to the operator. NEVER raises.

    Suppressed when:
      * `OPERATOR_CHAT_ID` is not set, OR
      * `quiet_alerts` toggle is '1' (operator opted out of digest pings).

    Any failure (toggle read, render, send) is swallowed via log.exception.
    The caller is the digest pipeline's success path; we must not break it
    because of a delivery hiccup.
    """
    try:
        op = _operator_chat_id()
        if op is None:
            return
        try:
            quiet = store.get_toggle("quiet_alerts", "0")
        except Exception:
            log.exception("summary: failed to read quiet_alerts toggle")
            return
        if str(quiet) == "1":
            return  # operator silenced the daily digest

        body = build_daily_summary(store, run_id)
        tg.send_message(op, body, parse_mode="MarkdownV2")
    except Exception:
        log.exception("deliver_daily_summary: unhandled error swallowed (run_id=%s)", run_id)
