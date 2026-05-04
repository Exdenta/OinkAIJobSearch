"""Alert envelope rendering + delivery.

`render_alert` produces a MarkdownV2 body matching plan §7. `deliver_alert` is
the callable that gets injected into `instrumentation.error_capture` as
`alert_sink` — it gates on `alerts_enabled` toggle + operator-env-set, sends
via the Telegram client, and confirms the delivery in the store.

CRITICAL: `deliver_alert` MUST never propagate exceptions back to the
captured path. A bug in alert delivery cannot be allowed to mask or replace
the original exception that we're trying to report. All failures swallow
into `log.exception(...)`.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from telegram_client import TelegramClient, mdv2_escape
from .operator import _operator_chat_id

log = logging.getLogger(__name__)


# ---------- code-block helper ----------

def _code_block(s: str) -> str:
    """Wrap arbitrary text in a triple-backtick MarkdownV2 code block.

    Inside a Telegram MarkdownV2 code block, only backtick and backslash need
    escaping (per the spec). All the other reserved chars pass through
    literally — that's why ops output uses code blocks: stable monospace
    rendering with minimal escaping required.
    """
    if s is None:
        s = ""
    # Escape backslash first so we don't double-escape what we add for backticks.
    s = s.replace("\\", "\\\\").replace("`", "\\`")
    return "```\n" + s + "\n```"


# ---------- chat-id redaction ----------

def _redact_chat_id(chat_id: Any) -> str:
    """Partial chat-id redaction for alert bodies.

    DB has the full id (we may need it to debug). The Telegram body keeps the
    last 4 digits and masks the prefix — this matches the plan §7 example
    (`4567...8901`) and avoids broadcasting full chat ids in case the
    operator forwards the alert to a third-party channel.
    """
    if chat_id is None:
        return "—"
    s = str(chat_id)
    if len(s) <= 8:
        return s
    return s[:4] + "..." + s[-4:]


# ---------- rendering ----------

def render_alert(envelope: Any) -> str:
    """Render an AlertEnvelope as a Telegram MarkdownV2 body.

    `envelope` is duck-typed — any object with attributes `where`,
    `error_class`, `message_head`, `stack_tail`, `chat_id`, `occurred_at`,
    `fingerprint`, `event_id` works. The real dataclass lives in
    `instrumentation.contexts.AlertEnvelope` (slice B); we don't import it
    here to keep slices independent.
    """
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(envelope.occurred_at))
    where = mdv2_escape(envelope.where or "")
    error_class = mdv2_escape(envelope.error_class or "")
    chat = mdv2_escape(_redact_chat_id(envelope.chat_id))

    # Header — escape the date because '-' and ':' and '.' are reserved in MDv2.
    header = f"🚨 *Bot error* · {mdv2_escape(when)}"

    # Message head — quote-prefix each line for visual emphasis. Telegram
    # treats `>` as a blockquote marker, but only at line start; we escape
    # body text so the only structural markdown is the `>` we add.
    msg = (envelope.message_head or "")[:200]
    msg_lines = []
    if msg.strip():
        for line in msg.splitlines():
            msg_lines.append("> " + mdv2_escape(line))
    msg_block = "\n".join(msg_lines) if msg_lines else "> " + mdv2_escape("(no message)")

    # Stack tail — render inside a code block. Per plan §7 the stack tail is
    # the LAST thing trimmed if we overflow Telegram's 4096-char limit.
    stack = envelope.stack_tail or ""
    # Constrain each frame to <=100 chars; the formatter in slice A's
    # fingerprint module is supposed to do this already, but defend in depth.
    trimmed_frames = []
    for frame in stack.splitlines():
        trimmed_frames.append(frame[:100])
    stack_text = "\n".join(trimmed_frames)
    stack_block = "stack tail (last 8 frames)\n" + stack_text if stack_text else "stack tail (empty)"

    # Fingerprint footer — short prefix + suppression hint.
    fp = envelope.fingerprint or ""
    fp_short = fp[:6] if fp else "—"
    footer = mdv2_escape(f"fp: {fp_short}… · suppressing dupes for 1h")

    body_parts = [
        header,
        "",
        f"*Where:*  `{where}`",
        f"*Class:*  `{error_class}`",
        f"*Chat:*   `{chat}`",
        "",
        msg_block,
        "",
        _code_block(stack_block),
        "",
        footer,
    ]
    body = "\n".join(body_parts)

    # Telegram hard cap is 4096; we aim for <=3500 to leave headroom for any
    # platform-side wrapping. Per plan §7: trim from BODY (not stack), so we
    # truncate the message_head section if needed. In practice we already
    # capped message_head at 200 chars so this is mostly defensive.
    if len(body) > 3500:
        # Keep header, where/class/chat, stack, footer; drop the msg_block.
        body_parts_min = [
            header,
            "",
            f"*Where:*  `{where}`",
            f"*Class:*  `{error_class}`",
            f"*Chat:*   `{chat}`",
            "",
            _code_block(stack_block),
            "",
            footer,
        ]
        body = "\n".join(body_parts_min)
        if len(body) > 3500:
            # Last resort: brutally trim the full body.
            body = body[:3500]
    return body


# ---------- delivery ----------

def deliver_alert(tg: TelegramClient, store: Any, envelope: Any) -> None:
    """Send `envelope` to the operator chat, then mark it delivered.

    Gated by:
      * `OPERATOR_CHAT_ID` env var must be set (else: no-op).
      * `alerts_enabled` toggle must be '1' (default '1').

    NEVER raises. Any failure (Telegram outage, store write error, malformed
    envelope, …) is swallowed and `log.exception`'d. The caller is the error
    capture path — propagating from here would either (a) replace the
    original exception in flight or (b) double-handle the same fault. Both
    are worse than a missed alert.
    """
    try:
        op = _operator_chat_id()
        if op is None:
            return  # no operator configured → silently disabled
        try:
            enabled = store.get_toggle("alerts_enabled", "1")
        except Exception:
            log.exception("alerts: failed to read alerts_enabled toggle")
            return
        if str(enabled) != "1":
            return  # operator turned alerts off

        body = render_alert(envelope)
        tg.send_message(op, body, parse_mode="MarkdownV2")

        event_id = getattr(envelope, "event_id", None)
        if event_id is not None:
            try:
                store.mark_alert_delivered(event_id)
            except Exception:
                log.exception("alerts: failed to mark event %s delivered", event_id)
    except Exception:
        # Last-resort guard: a bug in render_alert / attr access etc.
        log.exception("deliver_alert: unhandled error swallowed (envelope=%r)", envelope)
