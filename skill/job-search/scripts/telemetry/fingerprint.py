"""Stateless helpers for error fingerprinting and time-bucketing.

Used by `instrumentation.error_capture` (slice B) to decide whether an
exception is a duplicate of one we've already alerted on this hour.
Lives here in slice A so the fingerprint algorithm and the UNIQUE
constraint on `error_events` are co-located — change one, change the
other in the same review.
"""
from __future__ import annotations

import hashlib
import time
import traceback
from types import TracebackType
from typing import Optional


def hour_bucket(ts: Optional[float] = None) -> int:
    """Return floor(ts / 3600) for the rate-limit UNIQUE key.

    Falls back to `time.time()` when ts is None so callers don't have to
    reach for the clock themselves.
    """
    if ts is None:
        ts = time.time()
    return int(ts // 3600)


def error_fingerprint(exc: BaseException) -> str:
    """sha1(error_class + last-frame "file:line" + first 80 chars of msg).

    The last frame is the deepest one we have a traceback for — usually
    the actual raise site. We deliberately exclude the full stack so
    cosmetic frame shifts (line moves on a refactor) don't churn the
    fingerprint.
    """
    cls = type(exc).__name__
    msg = (str(exc) or "")[:80]

    tb: Optional[TracebackType] = getattr(exc, "__traceback__", None)
    last_frame_str = ""
    if tb is not None:
        # Walk to the deepest frame.
        deepest = tb
        while deepest.tb_next is not None:
            deepest = deepest.tb_next
        last_frame_str = f"{deepest.tb_frame.f_code.co_filename}:{deepest.tb_lineno}"

    payload = f"{cls}|{last_frame_str}|{msg}".encode("utf-8", errors="replace")
    return hashlib.sha1(payload).hexdigest()


def format_stack_tail(tb: Optional[TracebackType], n: int = 8) -> str:
    """Return the last n frames as `<file>:<line> in <fn>` lines.

    Empty string when tb is None (defensive — shouldn't happen on a
    real raised exception, but `error_capture` may construct envelopes
    in edge cases).
    """
    if tb is None:
        return ""
    frames = traceback.extract_tb(tb)
    tail = frames[-n:] if n > 0 else frames
    return "\n".join(f"{f.filename}:{f.lineno} in {f.name}" for f in tail)
