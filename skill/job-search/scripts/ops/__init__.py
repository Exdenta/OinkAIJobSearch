"""Operator-facing delivery surface — slice C of the monitoring plan.

This package owns three responsibilities:

  * Alert envelope rendering + delivery (alerts.py)
  * Daily summary digest building + delivery (summary.py)
  * Operator-only Telegram commands (commands.py)

Imports are intentionally narrow: only `telemetry` (slice A) and
`telegram_client`. Slice B (`instrumentation`) is NOT imported here — the only
contract between slices is the `MonitorStore` API surface defined by slice A.
"""
from __future__ import annotations

from .operator import OPERATOR_CHAT_ID_ENV, is_operator
from .alerts import deliver_alert, render_alert
from .summary import build_daily_summary, deliver_daily_summary
from .commands import handle_operator_command

__all__ = [
    "OPERATOR_CHAT_ID_ENV",
    "is_operator",
    "deliver_alert",
    "render_alert",
    "build_daily_summary",
    "deliver_daily_summary",
    "handle_operator_command",
]
