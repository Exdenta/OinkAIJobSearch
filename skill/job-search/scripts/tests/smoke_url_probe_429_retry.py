#!/usr/bin/env python3
"""Smoke test for the 429 retry policy in ``_url_is_alive``.

Today's cron run (2026-05-02) silently dropped Alena's only score-3 candidate
when news.ycombinator.com 429'd our HEAD probe once. The fix is to retry on
429 with exponential backoff before declaring the URL dead. This file pins
that behavior down so it cannot regress.

Covers:
  1. First 429, second 200 → alive=True after one retry.
  2. Three 429s → alive=False with reason ``http_429_after_retries``.
  3. 404 → no retry; alive=False with reason ``404``.
  4. Retry-After header (3s, 11s, malformed) → respected within the cap.
  5. Other 5xx → no retry; single attempt.

We monkey-patch ``_validation_request`` (the single chokepoint inside
telegram_client) and stub ``time.sleep`` so the test runs in zero wall time.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Force a known backoff base so the assertions are deterministic regardless
# of the operator's environment. Set BEFORE importing telegram_client.
os.environ["URL_VALIDATION_429_BACKOFF_BASE_S"] = "1.0"

import telegram_client as tc  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


class _FakeResp:
    def __init__(self, code: int, headers: dict | None = None) -> None:
        self.status_code = code
        self.headers = headers or {}


class _SequenceProbe:
    """Yields a pre-baked sequence of HEAD responses, one per call.

    Beyond the sequence length it raises so an unexpected extra HEAD shows
    up as a test failure rather than a silent pass.
    """

    def __init__(self, responses: list[_FakeResp]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, timeout_s, *, headers=None, verify=True):
        self.calls.append((method, url))
        if not self.responses:
            raise AssertionError(
                f"unexpected extra probe call ({method} {url}); sequence exhausted"
            )
        return self.responses.pop(0)


def _patch(probe_responses, sleeps_recorded):
    """Install monkey-patches; return the restore callback."""
    orig_req = tc._validation_request
    orig_sleep = tc.time.sleep
    seq = _SequenceProbe(probe_responses)
    tc._validation_request = seq

    def fake_sleep(s):
        sleeps_recorded.append(float(s))

    tc.time.sleep = fake_sleep

    def restore():
        tc._validation_request = orig_req
        tc.time.sleep = orig_sleep

    return seq, restore


# ---------------------------------------------------------------------------

def test_one_429_then_200_recovers() -> None:
    section("1. 429 then 200 → alive after one retry")
    sleeps: list[float] = []
    seq, restore = _patch([_FakeResp(429), _FakeResp(200)], sleeps)
    try:
        alive, reason = tc._url_is_alive("https://example.test/post/1", timeout_s=2.0)
    finally:
        restore()
    _assert(alive is True, f"alive=True after one retry (got {alive})")
    _assert(reason == "ok", f"reason='ok' (got {reason!r})")
    _assert(len(seq.calls) == 2, f"exactly 2 HEAD calls (got {len(seq.calls)})")
    _assert(sleeps == [1.0],
            f"one sleep at backoff base (got {sleeps})")


def test_three_429s_gives_up() -> None:
    section("2. 429 x3 → http_429_after_retries")
    sleeps: list[float] = []
    seq, restore = _patch(
        [_FakeResp(429), _FakeResp(429), _FakeResp(429)], sleeps,
    )
    try:
        alive, reason = tc._url_is_alive(
            "https://news.ycombinator.test/item?id=1", timeout_s=2.0,
        )
    finally:
        restore()
    _assert(alive is False, f"alive=False after exhausting retries (got {alive})")
    _assert(reason == "http_429_after_retries",
            f"reason marks exhausted retries (got {reason!r})")
    _assert(len(seq.calls) == 3, f"exactly 3 HEAD attempts (got {len(seq.calls)})")
    # Backoff schedule: base * 4^0, base * 4^1 → 1.0s, 4.0s.
    _assert(sleeps == [1.0, 4.0],
            f"exponential backoff 1.0s then 4.0s (got {sleeps})")


def test_404_no_retry() -> None:
    section("3. 404 → single attempt, no retry")
    sleeps: list[float] = []
    # Sequence length 1: a second call would AssertionError.
    seq, restore = _patch([_FakeResp(404)], sleeps)
    try:
        alive, reason = tc._url_is_alive("https://gone.test/job/9", timeout_s=2.0)
    finally:
        restore()
    _assert(alive is False, f"alive=False on 404 (got {alive})")
    _assert(reason == "404", f"reason='404' (got {reason!r})")
    _assert(len(seq.calls) == 1, f"only 1 HEAD attempt (got {len(seq.calls)})")
    _assert(sleeps == [], f"no sleeps on 404 (got {sleeps})")


def test_retry_after_header_respected_with_cap() -> None:
    section("4. Retry-After header honored within the 10s cap")

    # 4a. Sane Retry-After: 3 → first sleep should be exactly 3.0s.
    sleeps: list[float] = []
    seq, restore = _patch(
        [_FakeResp(429, {"Retry-After": "3"}), _FakeResp(200)], sleeps,
    )
    try:
        alive, _ = tc._url_is_alive("https://slow.test/a", timeout_s=2.0)
    finally:
        restore()
    _assert(alive is True, "Retry-After=3 then 200 → alive")
    _assert(sleeps == [3.0], f"sleep honored Retry-After=3 (got {sleeps})")

    # 4b. Oversized Retry-After: 11 → clamped to the 10s cap.
    sleeps = []
    seq, restore = _patch(
        [_FakeResp(429, {"Retry-After": "11"}), _FakeResp(200)], sleeps,
    )
    try:
        alive, _ = tc._url_is_alive("https://slow.test/b", timeout_s=2.0)
    finally:
        restore()
    _assert(alive is True, "Retry-After=11 then 200 → alive")
    _assert(sleeps == [10.0],
            f"sleep clamped at 10s cap (got {sleeps})")

    # 4c. Malformed Retry-After: should fall back to the exponential
    # schedule (base * 4^0 = 1.0s on the first retry).
    sleeps = []
    seq, restore = _patch(
        [_FakeResp(429, {"Retry-After": "soon"}), _FakeResp(200)], sleeps,
    )
    try:
        alive, _ = tc._url_is_alive("https://slow.test/c", timeout_s=2.0)
    finally:
        restore()
    _assert(alive is True, "malformed Retry-After then 200 → alive")
    _assert(sleeps == [1.0],
            f"malformed header → exponential fallback (got {sleeps})")


def test_other_5xx_no_retry() -> None:
    section("5. 5xx (non-429) → no retry, single attempt")
    for code in (500, 502, 503):
        sleeps: list[float] = []
        seq, restore = _patch([_FakeResp(code)], sleeps)
        try:
            alive, reason = tc._url_is_alive(
                f"https://broken.test/{code}", timeout_s=2.0,
            )
        finally:
            restore()
        _assert(alive is False, f"{code} → alive=False (got {alive})")
        _assert(reason == f"http_{code}",
                f"{code} → reason='http_{code}' (got {reason!r})")
        _assert(len(seq.calls) == 1,
                f"{code} → only 1 attempt (got {len(seq.calls)})")
        _assert(sleeps == [], f"{code} → no sleeps (got {sleeps})")


def main() -> int:
    test_one_429_then_200_recovers()
    test_three_429s_gives_up()
    test_404_no_retry()
    test_retry_after_header_respected_with_cap()
    test_other_5xx_no_retry()
    print("\nAll 429-retry smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
