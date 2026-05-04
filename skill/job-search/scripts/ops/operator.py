"""Operator gate.

Single-int env-var check. The operator role is *monitoring* (alerts, /health,
/stats, /runlog). It's intentionally separate from `ADMIN_CHAT_ID`, which the
bot already uses for product-side admin actions — see plan §9 for rationale.

`is_operator` is silent on missing/unparseable env var: returns False, no
log spam. Non-operators ghosting on unknown commands is the standard pattern
used elsewhere in the bot (see `_is_admin` in bot.py around line 887).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

OPERATOR_CHAT_ID_ENV = "OPERATOR_CHAT_ID"


def _operator_chat_id() -> Optional[int]:
    """Return the configured operator chat id, or None if not configured.

    Returns None on missing, empty, or unparseable env var. Single int — the
    monitoring role is one-person-only on purpose; if it grows past that
    we'll move to a list and revisit the alert fan-out semantics.
    """
    raw = os.environ.get(OPERATOR_CHAT_ID_ENV, "")
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_operator(chat_id: int) -> bool:
    """True iff `chat_id` matches `OPERATOR_CHAT_ID`. Silent on missing env."""
    op = _operator_chat_id()
    if op is None:
        return False
    try:
        return int(chat_id) == op
    except (TypeError, ValueError):
        return False
