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

# feedback_digest is lightweight (claude_cli + instrumentation.wrappers +
# forensic — none of which import the source adapters or search_jobs
# itself), so unlike `search_jobs.run` below it is safe to import eagerly
# at module scope without dragging the whole pipeline into every test that
# imports this module.
import feedback_digest
from chat_lock import lock_for

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
        fetch_backend: str = "apify",
        error_sink: Optional[Callable[[BaseException, int], None]] = None,
    ) -> None:
        # Positive = Telegram chat, negative = web-only account (the
        # search pipeline skips Telegram delivery for those and serves
        # the web feed instead). Only 0 is meaningless.
        if chat_id == 0:
            raise ValueError(f"chat_id must be non-zero, got {chat_id!r}")
        if interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds must be positive, got {interval_seconds!r}",
            )

        self._db = db
        self._chat_id = int(chat_id)
        self._interval = int(interval_seconds)
        self._search_run = search_run_callable or _default_search_run
        # Best-effort operator alert on an iteration that raised. Injected so
        # this module stays pipeline-free (owns nothing); bot.py wires it to
        # the same error_capture -> deliver_alert path the rest of the bot
        # uses. Called `(exc, iteration)`; must never raise. None disables it
        # (unit tests, daily-cron path).
        self._error_sink = error_sink
        self._clock = clock or time.time
        # Which global-fetch backend this user's loop drives. "apify" (DEFAULT)
        # = published Apify actors; "local" = DEPRECATED legacy in-process
        # scrapers (rollback only). bot.py sets this per-user via
        # `_backend_for_chat`; the default here keeps Apify the standard path.
        self._fetch_backend = str(fetch_backend or "apify")

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
                # Per-user auto-search opt-out: if the user toggled it OFF
                # (bottom-bar button), skip this run entirely and sleep. The
                # thread stays alive and re-checks each cycle, so toggling ON
                # resumes searches without a respawn. Default ON for unknown
                # users / NULL flag, so this never silences a user who never
                # opted out.
                try:
                    auto_on = self._db.get_auto_search_enabled(self._chat_id)
                except Exception:
                    auto_on = True  # never let a flag-read error wedge the loop
                if not auto_on:
                    log.info(
                        "continuous_searcher iter=%d chat_id=%d: auto-search OFF "
                        "— skipping run",
                        iteration, self._chat_id,
                    )
                    await asyncio.sleep(self._next_sleep(0.0))
                    continue

                # Bot-blocked tombstone: if the user blocked the bot / deleted
                # their account, sending is impossible, so skip the whole run
                # (no fetch, no LLM spend) and sleep. Their data is retained;
                # an inbound message clears the flag (bot._maybe_resume_blocked)
                # and searches resume without a respawn.
                try:
                    blocked = self._db.is_blocked(self._chat_id)
                except Exception:
                    blocked = False  # never let a flag-read error wedge the loop
                if blocked:
                    log.info(
                        "continuous_searcher iter=%d chat_id=%d: user blocked bot "
                        "— skipping run (data retained; resumes on return)",
                        iteration, self._chat_id,
                    )
                    await asyncio.sleep(self._next_sleep(0.0))
                    continue

                # Feedback-digest loop: refresh this user's LLM-distilled
                # 👍/👎 preference notes (if enough new feedback has
                # accumulated) BEFORE this iteration's search/scoring runs,
                # so a just-crossed threshold shows up in THIS run's
                # prefs_text rather than the next one. Best-effort — the
                # module itself never raises, but this try/except is a
                # second line of defense: a digest problem must never
                # cancel the user's actual search iteration.
                try:
                    feedback_digest.maybe_run_feedback_digest(self._db, self._chat_id)
                except Exception:
                    log.exception(
                        "continuous_searcher iter=%d chat_id=%d: "
                        "feedback digest raised — continuing search anyway",
                        iteration, self._chat_id,
                    )

                # Shared with `bot.trigger_job_check` — a manual "Check Now"
                # firing mid-iteration used to race this scheduled run, both
                # calling `search_jobs.run` for the same chat concurrently.
                # `sent_messages` dedupe is check-then-act, so both saw
                # "not sent yet" and both delivered — duplicate messages to
                # the user. Skip this iteration rather than block/queue: the
                # next tick picks up whatever the manual check didn't cover.
                lock = lock_for(self._chat_id)
                if not lock.acquire(blocking=False):
                    log.info(
                        "continuous_searcher iter=%d chat_id=%d: manual check "
                        "in progress — skipping this iteration",
                        iteration, self._chat_id,
                    )
                    await asyncio.sleep(self._next_sleep(0.0))
                    continue

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
                finally:
                    lock.release()

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
                    # Alert the operator. Only for `Exception` (KeyboardInterrupt/
                    # SystemExit are not operational faults). If the error already
                    # went through search_jobs.run's own error_capture, the shared
                    # fingerprint+hour dedup suppresses the duplicate here, so this
                    # only fires for faults outside that inner capture.
                    if self._error_sink is not None and isinstance(error, Exception):
                        try:
                            self._error_sink(error, iteration)
                        except Exception:
                            log.exception(
                                "continuous_searcher iter=%d: error_sink raised",
                                iteration,
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

        Threads ``cycle_index=self._iterations`` so the P4 source-cooldown
        FSM in ``search_jobs.fetch_all`` can alternate ``half_freq`` sources
        on even/odd iterations. The continuous searcher is the natural
        owner of this counter; daily-cron callers pass nothing and get the
        default (0).
        """
        return self._search_run(
            only_chat=self._chat_id,
            cycle_index=self._iterations,
            fetch_backend=self._fetch_backend,
        )

    def _next_sleep(self, elapsed: float) -> float:
        """Return how long to sleep after an iteration that took `elapsed`s.

        Target: `interval_seconds`. If the iteration overran, fall back to
        the `min_sleep_seconds` floor — never zero, never negative. The
        floor is the only thing between us and a hot loop when a source
        is timing out repeatedly.

        Night-mute flush fix: if this iteration ran DURING the night-mute
        window (23:00-09:00 Madrid by default), any ≥floor matches it found
        are being HELD by the quality buffer — and the buffer only flushes
        on a search run. Sleeping the full interval can land the next run
        hours after the mute lifts, stranding the matches for most of a day
        (observed: matches found 00:20 wouldn't send until the 16:00 run).
        So we cap the sleep to wake shortly after the window ends, which
        flushes the held buffer right at 09:00 instead. `min()` only ever
        SHORTENS the sleep, so the normal (non-mute) cadence is untouched.
        """
        remaining = float(self._interval) - float(elapsed)
        sleep_for = max(float(self._min_sleep), remaining)
        mute_end = self._seconds_until_mute_end()
        if mute_end is not None:
            sleep_for = max(float(self._min_sleep), min(sleep_for, mute_end))
        return sleep_for

    def _seconds_until_mute_end(self) -> Optional[float]:
        """If the night-mute window is active right now, return seconds
        until it ends (plus a small per-chat jitter so N searchers don't
        all wake at the same instant). Otherwise return None.

        Reads the same `night_mute_*` knobs `search_jobs._decide_buffer_flush`
        uses, so the wake boundary matches the hold boundary exactly.
        Fails safe to None (no cap) on any config/tz problem.
        """
        try:
            from defaults import DEFAULTS as _D  # noqa: WPS433
            tz_name = _D.get("night_mute_tz", "Europe/Madrid")
            start_h = int(_D.get("night_mute_start_hour", 23))
            end_h = int(_D.get("night_mute_end_hour", 9))
        except Exception:
            return None
        if start_h == end_h:
            return None
        try:
            import datetime as _dt  # noqa: WPS433
            from zoneinfo import ZoneInfo  # noqa: WPS433
            tz = ZoneInfo(tz_name)
            now = _dt.datetime.now(tz)
        except Exception:
            return None
        h = now.hour
        in_mute = (
            (start_h <= h < end_h) if start_h < end_h
            else (h >= start_h or h < end_h)
        )
        if not in_mute:
            return None
        end_today = now.replace(hour=end_h, minute=0, second=0, microsecond=0)
        if end_today <= now:
            end_today = end_today + _dt.timedelta(days=1)
        secs = (end_today - now).total_seconds()
        jitter = 60.0 + float(abs(int(self._chat_id)) % 120)
        return max(0.0, secs) + jitter
