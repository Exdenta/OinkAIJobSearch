"""Self-check for bot.py's single-instance pidfile guard (_claim_single_instance).

Run directly: python3 skill/job-search/scripts/test_bot_singleton.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import bot


def demo() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bot._PID_FILE = Path(tmp) / "bot.pid"

        assert bot._claim_single_instance() is True, "first claim should succeed"
        assert bot._PID_FILE.read_text().strip() == str(os.getpid())

        assert bot._claim_single_instance() is False, (
            "second claim while our own (live) pid owns the file must be refused"
        )

        # Simulate a stale pidfile left by a crashed/killed process: no real
        # process will ever have this pid, so the liveness check must fail
        # over to ProcessLookupError and let the new instance reclaim it.
        dead_pid = 999999
        bot._PID_FILE.write_text(str(dead_pid))
        assert bot._claim_single_instance() is True, "stale pidfile should be reclaimed"
        assert bot._PID_FILE.read_text().strip() == str(os.getpid())

    print("ok")


if __name__ == "__main__":
    demo()
