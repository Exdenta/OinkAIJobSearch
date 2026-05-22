"""Continuous in-process searcher loop (Phase 3).

Replaces the daily cron-fired digest with a long-running coroutine that
wakes up every `interval_seconds` and runs the existing `search_jobs.run`
pipeline for a single user. P1 (quality buffer) and P2 (page memory) do
the heavy lifting downstream — this module is just the heartbeat.

Why a class
-----------
The class owns NOTHING — the DB instance, the search callable, and the
clock are all injected. That makes the loop trivially unit-testable: pass
in a fake callable that records timings, run with a 1s interval, cancel
after three iterations.

Why asyncio (not a thread directly)
-----------------------------------
`search_jobs.run` is blocking sync code (subprocess calls, HTTP, DB).
We run it in the asyncio default executor so it doesn't block the event
loop the caller is using. bot.py is currently fully synchronous, so it
hosts this loop by spinning up a daemon `threading.Thread` that owns its
own `asyncio.new_event_loop` (see `bot.py.main`). When/if bot.py grows
its own event loop, the searcher can be `await`-ed directly with no
changes to the contract here.

Single-user MVP — the loop drives exactly one chat_id, passed in at
construction. The caller decides which user.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Optional

# Lazy import so importing this module doesn't drag the whole pipeline in.
# Tests inject `search_run_callable` and never trigger the import.
_DEFAULT_SEARCH_RUN: Optional[Callable[..., Any]] = None


def _default_search_run(**kwargs: Any) -> Any:
    """Default `search_run_callable`. Resolves `search_jobs.run` on first
    use so unit tests that pass their own callable never touch the real
    pipeline. Marked private — production callers pass nothing and let
    `ContinuousSearcher.__init__` wire this in.
    """
    global _DEFAULT_SEARCH_RUN
    if _DEFAULT_SEARCH_RUN is None:
        from search_jobs import run as _run  # noqa: WPS433 — deferred import
        _DEFAULT_SEARCH_RUN = _run
    return _DEFAULT_SEARCH_RUN(**kwargs)


log = logging.getLogger("continuous_searcher")


class ContinuousSearcher:
    """Run `search_run_callable(only_chat=chat_id)` on a fixed cadence.

    Per iteration:
      1. Log iteration start.
      2. Execute the (blocking) search callable in a thread executor.
      3. Log iteration end + the result.
      4. Sleep `interval_seconds` minus elapsed iteration time, but never
         less than `min_sleep_seconds` — that floor is the only thing
         preventing a hot loop on a degraded source.
      5. On `CancelledError`, log and propagate.
      6. On any other exception, log + sleep `interval_seconds` and continue.
         One bad iteration must not kill the searcher.
    """

    def __init__(
        self,
        *,
        db: Any,
        chat_id: int,
        interval_seconds: int,
        search_run_callable: Optional[Callable[..., Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        min_sleep_seconds: Optional[int] = None,
    ) -> None:
        if chat_id <= 0:
            raise ValueError(f"chat_id must be positive, got {chat_id!r}")
        if interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds must be positive, got {interval_seconds!r}",
            )

        self._db = db
        self._chat_id = int(chat_id)
        self._interval = int(interval_seconds)
        self._search_run = search_run_callable or _default_search_run
        self._clock = clock or time.time

        # Pull the floor from defaults when the caller leaves it unset.
        # Lazy import keeps the module test-friendly (defaults pulls in
        # the whole config tree).
        if min_sleep_seconds is None:
            try:
                from defaults import DEFAULTS as _DEFAULTS  # noqa: WPS433
                floor = int(_DEFAULTS.get("continuous_min_sleep_seconds", 60))
            except Exception:
                floor = 60
        else:
            floor = int(min_sleep_seconds)
        if floor < 0:
            floor = 0
        self._min_sleep = floor

        self._iterations = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def iterations(self) -> int:
        """Iteration count — useful for tests and operator dashboards."""
        return self._iterations

    @property
    def chat_id(self) -> int:
        return self._chat_id

    @property
    def interval_seconds(self) -> int:
        return self._interval

    @property
    def min_sleep_seconds(self) -> int:
        return self._min_sleep

    async def run_forever(self) -> None:
        """Main loop. Runs until cancelled.

        Cancellation: `asyncio.CancelledError` propagates after a single
        log line. Any other exception inside the iteration is caught,
        logged, and the loop continues — one bad iteration must not kill
        the searcher.
        """
        log.info(
            "continuous_searcher started: chat_id=%d interval=%ds min_sleep=%ds",
            self._chat_id, self._interval, self._min_sleep,
        )
        loop = asyncio.get_event_loop()
        try:
            while True:
                started = self._clock()
                self._iterations += 1
                iteration = self._iterations
                log.info(
                    "continuous_searcher iter=%d chat_id=%d: starting search",
                    iteration, self._chat_id,
                )
                exit_code: Any = None
                error: Optional[BaseException] = None
                try:
                    exit_code = await loop.run_in_executor(
                        None, self._invoke_search,
                    )
                except asyncio.CancelledError:
                    log.info(
                        "continuous_searcher iter=%d: cancelled mid-iteration",
                        iteration,
                    )
                    raise
                except BaseException as e:  # noqa: BLE001 — last-resort guard
                    error = e
                    log.exception(
                        "continuous_searcher iter=%d: search raised %s",
                        iteration, type(e).__name__,
                    )

                elapsed = max(0.0, self._clock() - started)
                if error is None:
                    log.info(
                        "continuous_searcher iter=%d: done in %.1fs (exit=%r)",
                        iteration, elapsed, exit_code,
                    )
                else:
                    log.info(
                        "continuous_searcher iter=%d: failed after %.1fs (%s)",
                        iteration, elapsed, type(error).__name__,
                    )

                sleep_for = self._next_sleep(elapsed)
                log.debug(
                    "continuous_searcher iter=%d: sleeping %.1fs",
                    iteration, sleep_for,
                )
                # asyncio.sleep is the cancellation point — if a SIGTERM
                # cancels the task while we're sleeping, this raises
                # CancelledError immediately rather than after the wait.
                await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            log.info(
                "continuous_searcher cancelled after %d iteration(s); exiting",
                self._iterations,
            )
            raise

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _invoke_search(self) -> Any:
        """Run the search callable. Pulled into a method so it can be
        wrapped in an executor and so subclasses can intercept the call.
        Kwargs are kept narrow on purpose — see the report for the
        single-user-MVP rationale.
        """
        return self._search_run(only_chat=self._chat_id)

    def _next_sleep(self, elapsed: float) -> float:
        """Return how long to sleep after an iteration that took `elapsed`s.

        Target: `interval_seconds`. If the iteration overran, fall back to
        the `min_sleep_seconds` floor — never zero, never negative. The
        floor is the only thing between us and a hot loop when a source
        is timing out repeatedly.
        """
        remaining = float(self._interval) - float(elapsed)
        return max(float(self._min_sleep), remaining)
