#!/usr/bin/env python3
"""Tests for the Phase-3 continuous-searcher loop.

Style matches the project's other smoke tests (`smoke_*.py`): each test
is a top-level function, asserts via the local `_assert` helper, and a
`main()` at the bottom runs them all. We don't depend on pytest-asyncio
— `asyncio.run` is enough for these five cases.

Covered:
  1. `run_forever` invokes the search callable each interval.
  2. A failing iteration is logged + swallowed; the loop keeps firing.
  3. When an iteration overruns `interval_seconds`, the loop still
     sleeps `min_sleep_seconds` (no busy-loop on a degraded source).
  4. Cancellation mid-iteration propagates as `CancelledError`.
  5. The fake callable receives the right `only_chat=` kwarg.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from continuous_searcher import ContinuousSearcher  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


class _Recorder:
    """Records every call to the fake search_run_callable.

    Stores wall-clock timestamp + kwargs so tests can assert on cadence
    and call shape. Default behavior is to return 0 (success). Set
    `raise_on` to a set of iteration indices (1-based) where the callable
    should raise; useful for the "swallow failures" test.
    """

    def __init__(
        self,
        *,
        sleep_for: float = 0.0,
        raise_on: set[int] | None = None,
    ) -> None:
        self.calls: list[tuple[float, dict]] = []
        self._sleep_for = float(sleep_for)
        self._raise_on = set(raise_on or ())

    def __call__(self, **kwargs):
        idx = len(self.calls) + 1
        self.calls.append((time.time(), dict(kwargs)))
        if self._sleep_for > 0:
            time.sleep(self._sleep_for)
        if idx in self._raise_on:
            raise RuntimeError(f"forced failure on iter={idx}")
        return 0


async def _drive_until(
    searcher: ContinuousSearcher,
    *,
    until_iterations: int,
    timeout_s: float = 10.0,
) -> None:
    """Schedule `run_forever`, wait until the searcher has completed at
    least `until_iterations` iterations, then cancel it.

    Cancellation propagates as `CancelledError`; we catch it here so the
    test bodies can stay focused on the assertions about side-effects.
    """
    task = asyncio.create_task(searcher.run_forever(), name="cs-test")
    deadline = time.time() + timeout_s
    try:
        while searcher.iterations < until_iterations:
            if time.time() > deadline:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise AssertionError(
                    f"searcher did not reach {until_iterations} iterations "
                    f"within {timeout_s}s (got {searcher.iterations})"
                )
            await asyncio.sleep(0.05)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_forever_calls_search_each_interval() -> None:
    section("1. run_forever calls the search callable per iteration")
    rec = _Recorder()
    searcher = ContinuousSearcher(
        db=None,
        chat_id=12345,
        interval_seconds=1,
        search_run_callable=rec,
        # Floor=0 lets the loop sleep as little as the interval allows —
        # otherwise the default 60s floor would dominate and the test
        # would hang. The "min-sleep is honoured" property is covered in
        # the dedicated test below.
        min_sleep_seconds=0,
    )

    asyncio.run(_drive_until(searcher, until_iterations=3, timeout_s=8.0))

    _assert(
        len(rec.calls) >= 3,
        f"search callable invoked at least 3 times (got {len(rec.calls)})",
    )
    # Inter-iteration gap should be close to interval_seconds=1.
    if len(rec.calls) >= 2:
        gaps = [
            rec.calls[i + 1][0] - rec.calls[i][0]
            for i in range(len(rec.calls) - 1)
        ]
        avg = sum(gaps) / len(gaps)
        _assert(
            0.8 <= avg <= 2.5,
            f"average inter-iteration gap roughly equals interval (got {avg:.2f}s)",
        )


def test_iteration_failure_does_not_kill_loop() -> None:
    section("2. one failing iteration does not kill the loop")
    # Iter 2 raises. We want at least 3 iterations to prove iter 3 fired
    # AFTER the failure.
    rec = _Recorder(raise_on={2})
    searcher = ContinuousSearcher(
        db=None,
        chat_id=999,
        interval_seconds=1,
        search_run_callable=rec,
        min_sleep_seconds=0,
    )

    asyncio.run(_drive_until(searcher, until_iterations=3, timeout_s=8.0))

    _assert(
        len(rec.calls) >= 3,
        f"loop ran at least 3 times even though iter 2 raised (got {len(rec.calls)})",
    )


def test_minimum_sleep_floor_respected() -> None:
    section("3. min_sleep_seconds floor honoured when iteration overruns")
    # Each iteration sleeps 0.3s of wall time but interval is 0.1s — so
    # remaining = -0.2s and we MUST fall through to the floor. We set the
    # floor to 0.4s so the gap between calls 1 and 2 is ~0.4s, NOT zero.
    rec = _Recorder(sleep_for=0.3)
    searcher = ContinuousSearcher(
        db=None,
        chat_id=42,
        interval_seconds=1,   # interval is short; iteration overruns it.
        search_run_callable=rec,
        min_sleep_seconds=1,  # floor = 1s
    )
    # Patch the interval to be SHORTER than the iteration so we exercise
    # the overrun branch. Direct attribute assignment is fine — this is
    # private state and the test owns the instance.
    searcher._interval = 0  # iteration "elapsed" will always exceed this

    async def _go():
        task = asyncio.create_task(searcher.run_forever())
        # Wait for two iterations to complete, then cancel.
        deadline = time.time() + 8.0
        while searcher.iterations < 2 and time.time() < deadline:
            await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_go())

    _assert(
        len(rec.calls) >= 2,
        f"at least 2 iterations recorded (got {len(rec.calls)})",
    )
    gap = rec.calls[1][0] - rec.calls[0][0]
    # gap = iteration_sleep (0.3) + min_sleep (~1.0) ≈ 1.3s. We tolerate
    # generous timing slop so this isn't flaky on a loaded CI box.
    _assert(
        gap >= 1.0,
        f"gap between iter 1 and iter 2 honours the 1s floor (got {gap:.2f}s)",
    )


def test_cancellation_propagates() -> None:
    section("4. cancellation propagates as CancelledError")

    cancelled_propagated = {"value": False}

    async def _go():
        rec = _Recorder(sleep_for=0.2)
        searcher = ContinuousSearcher(
            db=None,
            chat_id=7,
            interval_seconds=1,
            search_run_callable=rec,
            min_sleep_seconds=0,
        )
        task = asyncio.create_task(searcher.run_forever())
        # Let the first iteration kick off, then cancel.
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            cancelled_propagated["value"] = True

    asyncio.run(_go())

    _assert(
        cancelled_propagated["value"] is True,
        "awaiting the cancelled task surfaced CancelledError to the caller",
    )


def test_chat_id_passed_to_search_callable() -> None:
    section("5. chat_id is forwarded to the search callable")
    rec = _Recorder()
    expected_chat_id = 433775883
    searcher = ContinuousSearcher(
        db=None,
        chat_id=expected_chat_id,
        interval_seconds=1,
        search_run_callable=rec,
        min_sleep_seconds=0,
    )

    asyncio.run(_drive_until(searcher, until_iterations=1, timeout_s=5.0))

    _assert(
        len(rec.calls) >= 1,
        f"search callable invoked at least once (got {len(rec.calls)})",
    )
    _, kwargs = rec.calls[0]
    _assert(
        kwargs.get("only_chat") == expected_chat_id,
        f"callable received only_chat={expected_chat_id!r} (got {kwargs!r})",
    )


def test_ctor_rejects_invalid_args() -> None:
    section("6. constructor rejects non-positive chat_id / interval")
    for bad_chat in (0, -1):
        try:
            ContinuousSearcher(db=None, chat_id=bad_chat, interval_seconds=10)
        except ValueError:
            _assert(True, f"chat_id={bad_chat} → ValueError")
        else:
            _assert(False, f"chat_id={bad_chat} should have raised ValueError")

    for bad_interval in (0, -5):
        try:
            ContinuousSearcher(db=None, chat_id=1, interval_seconds=bad_interval)
        except ValueError:
            _assert(True, f"interval_seconds={bad_interval} → ValueError")
        else:
            _assert(
                False,
                f"interval_seconds={bad_interval} should have raised ValueError",
            )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    test_run_forever_calls_search_each_interval()
    test_iteration_failure_does_not_kill_loop()
    test_minimum_sleep_floor_respected()
    test_cancellation_propagates()
    test_chat_id_passed_to_search_callable()
    test_ctor_rejects_invalid_args()
    print("\nAll continuous-searcher tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
