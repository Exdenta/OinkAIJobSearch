#!/usr/bin/env python3
"""Smoke test for telegram_client.py rate-limit protection.

Covers:
  1. Per-chat burst — 6 sends to one chat with capacity=3 must block ≥3 times.
  2. Global bucket — 12 sends to 12 distinct chats with global capacity=10 must
     block ≥2 times at the global tier.
  3. 429 retry-after handling — monkey-patched requests.post returns 429 then
     200; assert ~retry_after seconds slept and the second response wins.
  4. Opt-out — TG_RATE_LIMIT_OFF=1 skips the bucket entirely.

Run from worktree root:
    python3 skill/job-search/scripts/tests/smoke_telegram_rate_limit.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _assert(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


def _fresh_import(env: dict) -> types.ModuleType:
    """Reload telegram_client with a specific env. Module-level constants
    (TG_*_RPS, TG_BURST_*, TG_RATE_LIMIT_OFF, the singleton _RATE_LIMITER)
    are read at import time, so each test that varies them needs a clean
    import.
    """
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # Force fresh module so the singleton picks up new env values.
    for mod in ("telegram_client",):
        if mod in sys.modules:
            del sys.modules[mod]
    import telegram_client  # noqa: WPS433
    return telegram_client


class _FakeResp:
    def __init__(self, status: int, body: dict, text: str = "") -> None:
        self.status_code = status
        self._body = body
        self.text = text

    def json(self) -> dict:
        return self._body


def _ok_post(*args, **kwargs):
    return _FakeResp(200, {"ok": True, "result": {"message_id": 1}})


def test_per_chat_burst() -> None:
    section("1. per-chat bucket — 6 sends, capacity=3, must block ≥3 times")
    tc = _fresh_import({
        "TG_RATE_LIMIT_OFF": None,
        "TG_GLOBAL_RPS": "1000",
        "TG_BURST_GLOBAL": "1000",
        "TG_PER_CHAT_RPS": "10",
        "TG_BURST_PER_CHAT": "3",
        # forensic OFF so we don't pollute /tmp; rate limiter still runs
        "FORENSIC_OFF": "1",
    })
    # Patch HTTP — every send returns 200/ok immediately so the only real
    # latency is the limiter itself.
    import requests as _r
    _r.post = _ok_post  # type: ignore[assignment]

    blocks = 0
    for _ in range(6):
        slept_g, slept_c = tc._RATE_LIMITER.acquire(chat_id=12345)
        if slept_c > 0.0:
            blocks += 1
    _assert(blocks >= 3, f"expected ≥3 per-chat blocks, got {blocks}")


def test_global_bucket() -> None:
    section("2. global bucket — 12 chats, capacity=10, must block ≥2 times")
    tc = _fresh_import({
        "TG_RATE_LIMIT_OFF": None,
        "TG_GLOBAL_RPS": "5",
        "TG_BURST_GLOBAL": "10",
        "TG_PER_CHAT_RPS": "1000",
        "TG_BURST_PER_CHAT": "1000",
        "FORENSIC_OFF": "1",
    })
    blocks = 0
    for i in range(12):
        slept_g, _ = tc._RATE_LIMITER.acquire(chat_id=1000 + i)
        if slept_g > 0.0:
            blocks += 1
    _assert(blocks >= 2, f"expected ≥2 global blocks, got {blocks}")


def test_429_retry() -> None:
    section("3. 429 retry-after — first call 429 (retry_after=1), second 200")
    tc = _fresh_import({
        "TG_RATE_LIMIT_OFF": "1",  # turn limiter off so we ONLY measure 429 sleep
        "FORENSIC_OFF": "1",
    })
    state = {"calls": 0}

    def flaky_post(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            return _FakeResp(
                429,
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests: retry after 1",
                    "parameters": {"retry_after": 1},
                },
            )
        return _FakeResp(200, {"ok": True, "result": {"message_id": 99}})

    import requests as _r
    _r.post = flaky_post  # type: ignore[assignment]

    client = tc.TelegramClient(token="x" * 8, timeout=5)
    t0 = time.monotonic()
    res = client._call("sendMessage", {"chat_id": 5, "text": "hi"})
    dur = time.monotonic() - t0

    _assert(state["calls"] == 2, f"expected 2 HTTP calls, got {state['calls']}")
    # retry_after=1 + jitter (0.5..0.75); allow [1.0, 2.5] window for OS jitter.
    _assert(1.0 <= dur <= 2.5, f"expected ~1s sleep, got {dur:.2f}s")
    _assert(res.get("message_id") == 99, f"expected 200 payload, got {res!r}")


def test_opt_out() -> None:
    section("4. opt-out — TG_RATE_LIMIT_OFF=1 skips bucket entirely")
    tc = _fresh_import({
        "TG_RATE_LIMIT_OFF": "1",
        "TG_GLOBAL_RPS": "1",
        "TG_BURST_GLOBAL": "1",
        "TG_PER_CHAT_RPS": "1",
        "TG_BURST_PER_CHAT": "1",
        "FORENSIC_OFF": "1",
    })
    import requests as _r
    _r.post = _ok_post  # type: ignore[assignment]

    client = tc.TelegramClient(token="x" * 8, timeout=5)
    t0 = time.monotonic()
    for _ in range(5):
        client._call("sendMessage", {"chat_id": 9, "text": "hi"})
    dur = time.monotonic() - t0
    # With the limiter ON and these settings, 5 calls would take ~4s
    # (1 token capacity, 1 rps refill). With it OFF, all 5 should fly
    # through in well under a second.
    _assert(dur < 0.5, f"expected fast path with opt-out, got {dur:.2f}s")
    _assert(tc.TG_RATE_LIMIT_OFF is True, "TG_RATE_LIMIT_OFF flag should be True")


def main() -> None:
    # Pin a tempdir for STATE_DIR so any forensic files we accidentally
    # produce don't end up in the repo root.
    os.environ.setdefault("STATE_DIR", tempfile.mkdtemp())
    test_per_chat_burst()
    test_global_bucket()
    test_429_retry()
    test_opt_out()
    print("\nAll telegram rate-limit smoke tests passed.")


if __name__ == "__main__":
    main()
