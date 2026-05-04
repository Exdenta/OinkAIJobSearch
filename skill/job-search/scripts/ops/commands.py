"""Operator-only Telegram commands.

Four commands per plan §5: `/health`, `/stats [24h|7d]`, `/alerts […]`,
`/runlog [N]`. All gated by `is_operator(chat_id)` — non-operators silently
ghost (mirrors the existing `_is_admin` pattern in bot.py around line 887).

The dispatcher returns False for:
  * Non-operator (so the bot's normal /command handler can take over)
  * Unrecognized commands
  * Anything that doesn't start with '/'
This is critical — the integration point in bot.py is `if
handle_operator_command(...): return`, so a False return MUST mean "I didn't
do anything; someone else should handle this."

All output is rendered inside MarkdownV2 triple-backtick code blocks (see
plan §5 examples). Code blocks need only backtick + backslash escaping;
everything else passes through verbatim, which is exactly what we want for
fixed-column tables.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable

from telegram_client import TelegramClient, mdv2_escape
from .alerts import _code_block
from .operator import is_operator

log = logging.getLogger(__name__)


# ---------- small formatting helpers ----------

def _fmt_age(now_ts: float, then_ts: float | None) -> str:
    """Human-friendly age: '4s ago', '22h ago', '3d ago'."""
    if not then_ts:
        return "—"
    delta = max(0, int(now_ts - then_ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


def _fmt_dollars(micro_usd: int) -> str:
    """Render micro-USD as $X.XX (or $X.XXX for sub-cent values)."""
    if not micro_usd:
        return "$0.00"
    dollars = micro_usd / 1_000_000.0
    if dollars >= 0.01:
        return f"${dollars:,.2f}"
    return f"${dollars:.4f}"


def _fmt_chars(n: int) -> str:
    """Compact char count: '1.2M', '8.9M', '342K', '512'."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Tolerant accessor — see notes in summary.py."""
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


def _pad(s: str, width: int) -> str:
    """Left-justify and clip — for fixed-column tables inside code blocks.

    Code blocks render in monospace, so simple character-count alignment
    works. We DO NOT escape here because the escaping happens once, at
    code-block construction time.
    """
    s = str(s)
    if len(s) > width:
        return s[: max(0, width - 1)] + "…"
    return s.ljust(width)


# ---------- /health ----------

def cmd_health(tg: TelegramClient, store: Any, chat_id: int, args: list[str]) -> None:
    """`/health` — last source-run snapshot + recent error count + toggles."""
    now = time.time()

    try:
        rows = list(store.last_source_run_per_source() or [])
    except Exception:
        log.exception("/health: last_source_run_per_source failed")
        rows = []

    try:
        errs = list(store.recent_errors(now - 86400) or [])
    except Exception:
        log.exception("/health: recent_errors failed")
        errs = []

    alerts_on = str(_safe_toggle(store, "alerts_enabled", "1")) == "1"
    quiet = str(_safe_toggle(store, "quiet_alerts", "0")) == "1"

    # Build the table inside a code block. Columns sized for ≤60-char rows
    # per plan §5: source(15) status(12) count(4) age(10) note(rest).
    title = f"🩺 Health  · {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now))}"
    table_lines = ["", "Sources (last run per source)"]
    if not rows:
        table_lines.append("  (no source runs recorded yet)")
    else:
        # Column widths: source(15) status(16, fits 'suspicious_zero'=15)
        # count(4) age(8) note(rest). Each row stays under ~70 chars.
        for r in rows:
            key = _row_get(r, "source_key", "?")
            status = _row_get(r, "status", "?")
            raw = _row_get(r, "raw_count", 0)
            finished = _row_get(r, "finished_at")
            age = _fmt_age(now, finished)
            err_class = _row_get(r, "error_class") or ""

            count_str = "" if status in ("failed", "exception") else str(int(raw or 0))
            note = ""
            if status == "suspicious_zero":
                note = "⚠ 3rd zero-run"
            elif status in ("failed", "exception") and err_class:
                note = err_class
            table_lines.append(
                "  " + _pad(key, 15) + " " + _pad(status, 16) + " "
                + _pad(count_str, 4) + " · " + _pad(age, 8) + ("  " + note if note else "")
            )

    table_lines.append("")
    table_lines.append(f"Errors (last 24h): {len(errs)}")
    table_lines.append(
        "Toggles: alerts="
        + ("on" if alerts_on else "off")
        + "  quiet_alerts="
        + ("on" if quiet else "off")
    )

    body = title + "\n" + _code_block("\n".join(table_lines))
    tg.send_message(chat_id, body, parse_mode="MarkdownV2")


# ---------- /stats ----------

def cmd_stats(tg: TelegramClient, store: Any, chat_id: int, args: list[str]) -> None:
    """`/stats [24h|7d]` — pipeline / user / Claude-cost rollup.

    Default: two columns (24h | 7d). With explicit `24h` arg: collapse to one.
    """
    mode = (args[0].lower() if args else "").strip()
    now = time.time()
    if mode == "24h":
        windows = [("last 24h", now - 86400)]
    elif mode == "7d":
        windows = [("last 7d", now - 7 * 86400)]
    else:
        windows = [("last 24h", now - 86400), ("last 7d", now - 7 * 86400)]

    # Pull rollups per window. We deliberately call the same store API
    # `claude_call_window_summary(since_ts) -> dict` documented in slice A §3.
    summaries = []
    for label, since in windows:
        s = _safe_call(store, "claude_call_window_summary", since)
        if not isinstance(s, dict):
            s = {}
        s["_label"] = label
        s["_since"] = since
        summaries.append(s)

    # Pipeline counts per kind, derived from `recent_pipeline_runs` filtered
    # by finished_at. We pull a generous limit and bucket locally; the store's
    # `recent_pipeline_runs(limit)` returns DESC by finished_at so we can stop
    # iterating once we leave the oldest window.
    earliest_since = min(s["_since"] for s in summaries)
    runs = _safe_call(store, "recent_pipeline_runs", 1000) or []
    pipeline_buckets: dict[str, dict[str, dict[str, int]]] = {}
    for run in runs:
        finished = float(_row_get(run, "finished_at", 0) or 0)
        if finished < earliest_since:
            continue
        kind = str(_row_get(run, "kind", "?"))
        status = str(_row_get(run, "status", "?"))
        for s in summaries:
            if finished >= s["_since"]:
                bk = pipeline_buckets.setdefault(s["_label"], {})
                ks = bk.setdefault(kind, {})
                ks["total"] = ks.get("total", 0) + 1
                ks[status] = ks.get(status, 0) + 1

    # Render. Header line names the windows; rows align via _pad.
    title = "📊 *Stats*  · " + " \\| ".join(mdv2_escape(s["_label"]) for s in summaries)

    lines: list[str] = []
    lines.append("Pipelines")
    pipeline_kinds = sorted({k for bk in pipeline_buckets.values() for k in bk.keys()})
    if not pipeline_kinds:
        lines.append("  (none)")
    for kind in pipeline_kinds:
        cells = []
        breakdown_extra = ""
        for s in summaries:
            entry = pipeline_buckets.get(s["_label"], {}).get(kind, {})
            cells.append(str(entry.get("total", 0)))
            if s is summaries[0] and entry:
                # Show ok/partial breakdown ONLY for the first (smallest) window
                ok = entry.get("ok", 0)
                partial = entry.get("partial", 0)
                if ok or partial:
                    breakdown_extra = f"     (ok {ok} · partial {partial})"
        col = "  | ".join(_pad(c, 4) for c in cells)
        lines.append("  " + _pad(kind, 18) + " " + col + breakdown_extra)

    # Users — derived from claude window summary if it carries it; otherwise omit.
    lines.append("")
    lines.append("Users")
    has_user = any("active_users" in s or "digests_sent" in s for s in summaries)
    if has_user:
        active_cells = "  | ".join(_pad(str(s.get("active_users", "—")), 4) for s in summaries)
        digest_cells = "  | ".join(_pad(str(s.get("digests_sent", "—")), 4) for s in summaries)
        lines.append("  " + _pad("active (sent any)", 18) + " " + active_cells)
        lines.append("  " + _pad("digests sent", 18) + " " + digest_cells)
    else:
        lines.append("  (no user-level rollup available)")

    # Claude CLI block.
    lines.append("")
    lines.append("Claude CLI")
    call_cells = "  | ".join(_pad(str(s.get("calls", 0)), 4) for s in summaries)
    lines.append("  " + _pad("calls", 18) + " " + call_cells)

    # by_model breakdown — first window only (don't double-stuff the page).
    bm = summaries[0].get("by_model") or {}
    if isinstance(bm, dict) and bm:
        bm_str = " / ".join(f"{k} {v}" for k, v in sorted(bm.items()))
        lines.append("  by model: " + bm_str)
    bc = summaries[0].get("by_caller") or {}
    if isinstance(bc, dict) and bc:
        # top 5 only, descending
        top = sorted(bc.items(), key=lambda kv: -int(kv[1]))[:5]
        bc_str = " · ".join(f"{k} {v}" for k, v in top)
        lines.append("  by caller: " + bc_str)

    char_cells = "  | ".join(
        _pad(_fmt_chars(s.get("prompt_chars", 0)), 4) for s in summaries
    )
    lines.append("  " + _pad("prompt chars", 18) + " " + char_cells)

    cost_cells = "  | ".join(
        _pad(_fmt_dollars(s.get("cost_estimate_us", 0)), 6) for s in summaries
    )
    lines.append(
        "  " + _pad("est. cost (surr.)", 18) + " " + cost_cells + "    surrogate · ±25%"
    )

    # Errors (delivered) — count from recent_errors per window. delivered_at != NULL
    # means the alert went out; NULL means rate-limit suppression.
    lines.append("")
    err_delivered = []
    err_total = []
    for s in summaries:
        evs = list(_safe_call(store, "recent_errors", s["_since"]) or [])
        delivered = sum(1 for e in evs if _row_get(e, "delivered_at"))
        err_delivered.append(delivered)
        err_total.append(len(evs))
    err_cells = "  | ".join(
        _pad(f"{d} / {t - d}", 6) for d, t in zip(err_delivered, err_total)
    )
    lines.append("  " + _pad("Errors (delivered/supr)", 24) + " " + err_cells)

    body = title + "\n" + _code_block("\n".join(lines))
    tg.send_message(chat_id, body, parse_mode="MarkdownV2")


# ---------- /alerts ----------

def cmd_alerts(tg: TelegramClient, store: Any, chat_id: int, args: list[str]) -> None:
    """`/alerts [on|off|quiet on|off]` — toggle alert and digest channels."""
    args = [a.strip().lower() for a in (args or [])]
    msg = ""

    if not args:
        # Bare /alerts — print current state.
        ae = str(_safe_toggle(store, "alerts_enabled", "1")) == "1"
        qa = str(_safe_toggle(store, "quiet_alerts", "0")) == "1"
        msg = (
            "current: alerts="
            + ("on" if ae else "off")
            + "  quiet_alerts="
            + ("on" if qa else "off")
        )
    elif args[0] == "on":
        _safe_set(store, "alerts_enabled", "1")
        msg = "✅ alerts ENABLED"
    elif args[0] == "off":
        _safe_set(store, "alerts_enabled", "0")
        msg = "🔕 alerts DISABLED"
    elif args[0] == "quiet" and len(args) >= 2 and args[1] in ("on", "off"):
        if args[1] == "on":
            _safe_set(store, "quiet_alerts", "1")
            msg = "🔕 daily summary suppressed"
        else:
            _safe_set(store, "quiet_alerts", "0")
            msg = "✅ daily summary enabled"
    else:
        # Unknown sub-arg — show usage + current state.
        ae = str(_safe_toggle(store, "alerts_enabled", "1")) == "1"
        qa = str(_safe_toggle(store, "quiet_alerts", "0")) == "1"
        msg = (
            "usage: /alerts [on|off|quiet on|off]\n"
            "current: alerts="
            + ("on" if ae else "off")
            + "  quiet_alerts="
            + ("on" if qa else "off")
        )

    body = _code_block(msg)
    tg.send_message(chat_id, body, parse_mode="MarkdownV2")


# ---------- /runlog ----------

def cmd_runlog(tg: TelegramClient, store: Any, chat_id: int, args: list[str]) -> None:
    """`/runlog [N]` — last N pipeline runs in a fixed-column table."""
    n = 10
    if args:
        try:
            n = int(args[0])
        except (TypeError, ValueError):
            n = 10
    n = max(1, min(50, n))

    runs = list(_safe_call(store, "recent_pipeline_runs", n) or [])

    title = f"🧾 *Runs* (last {len(runs)})"
    if not runs:
        body = title + "\n" + _code_block("(no pipeline runs recorded yet)")
        tg.send_message(chat_id, body, parse_mode="MarkdownV2")
        return

    # Column widths chosen so each row ≤ 80 chars per plan §5:
    #   id(6) ts(16) kind(15) status(8) summary(rest)
    lines = []
    for run in runs:
        rid = _row_get(run, "id", 0)
        finished = float(_row_get(run, "finished_at", 0) or 0)
        ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(finished)) if finished else "—"
        kind = str(_row_get(run, "kind", "?"))
        status = str(_row_get(run, "status", "?"))
        users = int(_row_get(run, "users_total", 0) or 0)
        raw = int(_row_get(run, "jobs_raw", 0) or 0)
        sent = int(_row_get(run, "jobs_sent", 0) or 0)
        errs = int(_row_get(run, "error_count", 0) or 0)
        started = float(_row_get(run, "started_at", 0) or 0)
        dur = _fmt_duration(finished - started) if started and finished else "—"

        summary = f"u={users} raw={raw} sent={sent} dur={dur}"
        if errs:
            summary += f"  err={errs}"
        lines.append(
            _pad(f"#{rid}", 6) + " " + _pad(ts, 16) + " "
            + _pad(kind, 15) + " " + _pad(status, 8) + " " + summary
        )

    body = title + "\n" + _code_block("\n".join(lines))
    tg.send_message(chat_id, body, parse_mode="MarkdownV2")


# ---------- safe wrappers ----------

def _safe_toggle(store: Any, key: str, default: str) -> str:
    try:
        v = store.get_toggle(key, default)
        return v if v is not None else default
    except Exception:
        log.exception("toggle read failed for %r", key)
        return default


def _safe_set(store: Any, key: str, value: str) -> None:
    try:
        store.set_toggle(key, value)
    except Exception:
        log.exception("toggle write failed for %r", key)


def _safe_call(store: Any, method: str, *args, **kwargs):
    try:
        fn = getattr(store, method, None)
        if fn is None:
            return None
        return fn(*args, **kwargs)
    except Exception:
        log.exception("store.%s failed", method)
        return None


# ---------- top-level dispatcher ----------

_COMMANDS = {
    "/health": cmd_health,
    "/stats": cmd_stats,
    "/alerts": cmd_alerts,
    "/runlog": cmd_runlog,
}


def handle_operator_command(
    tg: TelegramClient,
    store: Any,
    chat_id: int,
    text: str,
) -> bool:
    """Parse a Telegram message and dispatch if it's a recognized op command.

    Returns True iff this function HANDLED the message — meaning the bot's
    normal handler should NOT also process it. Returns False for:

      * Empty / non-/ text
      * Recognized command but non-operator chat (silent ghost — let the
        normal handler emit "Unknown command. Try /help.")
      * Unrecognized command (e.g. /start, /help, /jobs — the bot owns those)

    This mirrors the existing `_is_admin` ghosting pattern in bot.py:887:
    refuse to acknowledge that a command exists if the requester isn't
    privileged. That avoids leaking the surface area of operator-only
    tooling to random users who guess.
    """
    if not text:
        return False
    text = text.strip()
    if not text.startswith("/"):
        return False

    # First whitespace-delimited token is the command; strip any @botname suffix.
    head, _, rest = text.partition(" ")
    cmd = head.split("@", 1)[0].lower()
    if cmd not in _COMMANDS:
        return False

    # Recognized command. Now check operator status.
    if not is_operator(chat_id):
        # Silent ghost — return False so bot's normal /help-style handler
        # responds with its standard "Unknown command" message. This matches
        # the existing admin pattern (see bot.py:_show_admin_stats:897).
        return False

    args = [a for a in rest.split() if a] if rest else []
    handler = _COMMANDS[cmd]
    try:
        handler(tg, store, chat_id, args)
    except Exception:
        # Last-resort guard so a bug in one command doesn't crash the
        # bot's update loop. The error has already been logged by the
        # handler's own try/except blocks; this is just defense in depth.
        log.exception("operator command %s failed", cmd)
        try:
            tg.send_message(
                chat_id,
                _code_block(f"⚠ {cmd} failed; see server logs"),
                parse_mode="MarkdownV2",
            )
        except Exception:
            log.exception("failed to send error fallback for %s", cmd)
    return True
