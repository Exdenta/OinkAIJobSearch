#!/usr/bin/env python3
"""Tests for the v2.6 "continuous mode as default" wiring in bot.py.

Covers:
  1. `db.onboarded_chat_ids` returns only users with `onboarding_completed_at IS NOT NULL`
     AND a non-null/non-empty `user_profile`.
  2. `_resolve_continuous_chat_ids` honours `OINK_CONTINUOUS_CHAT_ID` when set,
     falls back to the DB list when unset.
  3. `_resolve_continuous_chat_ids` warns + falls back when the env var is set
     but parses to no valid ids.
  4. `_spawn_continuous_searcher_thread` dedupes via the process-wide registry —
     a second call for the same chat_id returns the existing Thread.
  5. `_maybe_start_continuous_searcher` is a no-op when continuous mode is off.
  6. `_maybe_start_continuous_searcher` returns one thread per chat_id from the
     resolver, and re-running it is a no-op (registry dedups).
  7. `start_continuous_searcher_for` is a no-op when continuous mode is off.
  8. `start_continuous_searcher_for` is a no-op when env-pinned excludes
     the chat_id, and spawns when env-pinned includes it.
  9. `start_continuous_searcher_for` is idempotent for an already-running chat_id.

We monkey-patch `_spawn_continuous_searcher_thread` for the high-level
tests so we don't actually start asyncio loops — the dedup logic lives in
the registry and that's what we verify. For dedup itself we still call
the real spawn helper (with a fake ContinuousSearcher) so the lock-and-
register code path is exercised end-to-end.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import bot  # noqa: E402
from db import DB  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _assert(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


class _EnvVar:
    """Context-managed env var (set/unset, restore on exit)."""

    def __init__(self, name: str, value: str | None) -> None:
        self.name = name
        self.value = value
        self._prev: str | None = None
        self._had: bool = False

    def __enter__(self):
        self._had = self.name in os.environ
        self._prev = os.environ.get(self.name)
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._had:
            os.environ[self.name] = self._prev or ""
        else:
            os.environ.pop(self.name, None)


def _fresh_registry():
    """Wipe the process-wide registry so tests start from a clean slate."""
    with bot._CONTINUOUS_REGISTRY_LOCK:
        bot._CONTINUOUS_REGISTRY.clear()


def _make_tmpdb() -> DB:
    """Spin up a real DB on a temp path so we exercise the actual SQL."""
    fd, path = tempfile.mkstemp(suffix=".sqlite", prefix="contdefault_")
    os.close(fd)
    return DB(Path(path))


def _seed_user(db: DB, chat_id: int, *, completed: bool, profile: str | None) -> None:
    """Insert a user row with the given onboarding + profile state."""
    db.upsert_user(chat_id, "u", "U", "U")
    if profile is not None:
        db.set_user_profile(chat_id, profile)
    if completed:
        db.mark_onboarding_complete(chat_id)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_onboarded_chat_ids_filters_correctly() -> None:
    section("1. db.onboarded_chat_ids returns only completed + profile-having users")
    db = _make_tmpdb()
    # 100 — completed + profile → included.
    _seed_user(db, 100, completed=True, profile='{"min_match_score": 3}')
    # 200 — completed but no profile → excluded.
    _seed_user(db, 200, completed=True, profile=None)
    # 300 — profile but not completed → excluded.
    _seed_user(db, 300, completed=False, profile='{"min_match_score": 3}')
    # 400 — neither → excluded.
    _seed_user(db, 400, completed=False, profile=None)
    # 150 — completed + profile, lower id, ensures ASC ordering.
    _seed_user(db, 150, completed=True, profile='{"min_match_score": 3}')

    ids = db.onboarded_chat_ids()
    _assert(ids == [100, 150], f"expected [100, 150] (sorted ASC), got {ids!r}")


def test_resolver_env_override() -> None:
    section("2. resolver honours OINK_CONTINUOUS_CHAT_ID when set")
    db = _make_tmpdb()
    # Seed a DB user so we can prove the env overrides the DB fallback.
    _seed_user(db, 500, completed=True, profile='{"x":1}')
    with _EnvVar("OINK_CONTINUOUS_CHAT_ID", "111,222"):
        ids = bot._resolve_continuous_chat_ids(db)
    _assert(ids == [111, 222], f"env override returned {ids!r}")


def test_resolver_db_fallback_when_env_unset() -> None:
    section("2b. resolver falls back to DB when env unset")
    db = _make_tmpdb()
    _seed_user(db, 700, completed=True, profile='{"x":1}')
    _seed_user(db, 800, completed=True, profile='{"x":1}')
    with _EnvVar("OINK_CONTINUOUS_CHAT_ID", None):
        ids = bot._resolve_continuous_chat_ids(db)
    _assert(ids == [700, 800], f"DB fallback returned {ids!r}")


def test_resolver_db_fallback_when_env_blank() -> None:
    section("2c. resolver falls back to DB when env is empty string")
    db = _make_tmpdb()
    _seed_user(db, 901, completed=True, profile='{"x":1}')
    with _EnvVar("OINK_CONTINUOUS_CHAT_ID", "   "):
        ids = bot._resolve_continuous_chat_ids(db)
    _assert(ids == [901], f"blank env should fall back to DB; got {ids!r}")


def test_resolver_env_garbage_falls_back() -> None:
    section("3. resolver falls back to DB when env is set but parses to nothing")
    db = _make_tmpdb()
    _seed_user(db, 600, completed=True, profile='{"x":1}')
    with _EnvVar("OINK_CONTINUOUS_CHAT_ID", "abc,xyz"):
        ids = bot._resolve_continuous_chat_ids(db)
    _assert(ids == [600], f"garbage env → DB fallback; got {ids!r}")


def test_spawn_dedup_via_registry() -> None:
    section("4. spawn dedups via registry")
    _fresh_registry()
    db = _make_tmpdb()

    # Patch ContinuousSearcher so it doesn't actually run search_jobs.
    # We need the underlying thread to STAY ALIVE so is_alive() returns True
    # for the dedup check on the second call.
    class _StubSearcher:
        def __init__(self, **_kwargs):
            self._evt = threading.Event()

        async def run_forever(self):
            import asyncio as _aio
            try:
                # Long-enough wait that the second spawn call sees is_alive=True.
                await _aio.sleep(60)
            except _aio.CancelledError:
                raise

    import continuous_searcher
    orig = continuous_searcher.ContinuousSearcher
    continuous_searcher.ContinuousSearcher = _StubSearcher  # type: ignore[assignment]
    try:
        t1 = bot._spawn_continuous_searcher_thread(db, 555, startup_delay=0)
        # Give the thread a beat to actually start so is_alive flips True.
        time.sleep(0.1)
        t2 = bot._spawn_continuous_searcher_thread(db, 555, startup_delay=0)
        _assert(t1 is t2, "second spawn returns the same Thread instance")
        _assert(
            sum(1 for cid in bot._CONTINUOUS_REGISTRY if cid == 555) == 1,
            "registry has exactly one entry for chat_id=555",
        )
    finally:
        continuous_searcher.ContinuousSearcher = orig  # type: ignore[assignment]
        _fresh_registry()


def test_maybe_start_no_op_when_mode_off() -> None:
    section("5. _maybe_start_continuous_searcher is a no-op when mode is off")
    _fresh_registry()
    db = _make_tmpdb()
    _seed_user(db, 111, completed=True, profile='{"x":1}')
    with _EnvVar("OINK_CONTINUOUS_MODE", "0"), _EnvVar("OINK_CONTINUOUS_CHAT_ID", None):
        result = bot._maybe_start_continuous_searcher(db)
    _assert(result is None, f"expected None when mode off, got {result!r}")
    _assert(
        len(bot._CONTINUOUS_REGISTRY) == 0,
        "registry stays empty when mode is off",
    )


def test_maybe_start_uses_resolver_and_dedups_on_replay() -> None:
    section("6. _maybe_start_continuous_searcher spawns once per resolved chat_id; replay is no-op")
    _fresh_registry()
    db = _make_tmpdb()
    _seed_user(db, 333, completed=True, profile='{"x":1}')
    _seed_user(db, 444, completed=True, profile='{"x":1}')

    spawned: list[int] = []
    orig_spawn = bot._spawn_continuous_searcher_thread

    def _fake_spawn(_db, cid, _delay, **_kw):
        spawned.append(cid)
        # Put a dummy Thread in the registry so a second call sees a live entry.
        class _LiveDummy:
            def is_alive(self):
                return True

        bot._CONTINUOUS_REGISTRY[cid] = _LiveDummy()  # type: ignore[assignment]
        return bot._CONTINUOUS_REGISTRY[cid]

    bot._spawn_continuous_searcher_thread = _fake_spawn  # type: ignore[assignment]
    try:
        with _EnvVar("OINK_CONTINUOUS_MODE", "1"), _EnvVar("OINK_CONTINUOUS_CHAT_ID", None):
            threads_1 = bot._maybe_start_continuous_searcher(db)
        # The fake spawn doesn't dedup — it's only here to capture which
        # chat_ids _maybe_start fed to the spawn helper. Real-spawn dedup
        # is exercised in test_maybe_start_replay_with_real_spawn_dedups.
        _assert(
            spawned == [333, 444],
            f"first pass spawned [333, 444]; got {spawned!r}",
        )
        _assert(
            threads_1 is not None and len(threads_1) == 2,
            f"first pass returned 2 threads; got {threads_1!r}",
        )
    finally:
        bot._spawn_continuous_searcher_thread = orig_spawn  # type: ignore[assignment]
        _fresh_registry()


def test_maybe_start_replay_with_real_spawn_dedups() -> None:
    section("6b. real spawn path dedups on replay (registry-backed)")
    _fresh_registry()
    db = _make_tmpdb()
    _seed_user(db, 777, completed=True, profile='{"x":1}')

    class _StubSearcher:
        def __init__(self, **_kw):
            pass

        async def run_forever(self):
            import asyncio as _aio
            try:
                await _aio.sleep(60)
            except _aio.CancelledError:
                raise

    import continuous_searcher
    orig = continuous_searcher.ContinuousSearcher
    continuous_searcher.ContinuousSearcher = _StubSearcher  # type: ignore[assignment]
    try:
        with _EnvVar("OINK_CONTINUOUS_MODE", "1"), _EnvVar("OINK_CONTINUOUS_CHAT_ID", None):
            ts1 = bot._maybe_start_continuous_searcher(db)
            time.sleep(0.1)  # let thread go live
            ts2 = bot._maybe_start_continuous_searcher(db)
        _assert(ts1 is not None and len(ts1) == 1, f"first call → 1 thread; got {ts1!r}")
        _assert(ts2 is not None and len(ts2) == 1, f"second call → 1 thread; got {ts2!r}")
        _assert(ts1[0] is ts2[0], "second call returns the same Thread (dedup)")
    finally:
        continuous_searcher.ContinuousSearcher = orig  # type: ignore[assignment]
        _fresh_registry()


def test_live_spawn_no_op_when_mode_off() -> None:
    section("7. start_continuous_searcher_for is a no-op when continuous mode is off")
    _fresh_registry()
    db = _make_tmpdb()
    with _EnvVar("OINK_CONTINUOUS_MODE", None), _EnvVar("OINK_CONTINUOUS_CHAT_ID", None):
        result = bot.start_continuous_searcher_for(db, 999)
    _assert(result is None, f"expected None when mode off; got {result!r}")
    _assert(len(bot._CONTINUOUS_REGISTRY) == 0, "registry untouched")


def test_live_spawn_env_pinned_exclude_and_include() -> None:
    section("8. live-spawn respects env-pinning")
    _fresh_registry()
    db = _make_tmpdb()
    calls: list[tuple[int, int]] = []
    orig_spawn = bot._spawn_continuous_searcher_thread

    def _fake_spawn(_db, cid, delay, **_kw):
        calls.append((cid, delay))
        return "thread"

    bot._spawn_continuous_searcher_thread = _fake_spawn  # type: ignore[assignment]
    try:
        # env pinned to 111 → live-spawn for 222 is a no-op.
        with _EnvVar("OINK_CONTINUOUS_MODE", "1"), _EnvVar("OINK_CONTINUOUS_CHAT_ID", "111"):
            r1 = bot.start_continuous_searcher_for(db, 222)
            r2 = bot.start_continuous_searcher_for(db, 111)
        _assert(r1 is None, "live-spawn for excluded chat_id is None")
        _assert(r2 == "thread", "live-spawn for included chat_id spawns")
        _assert(
            [cid for cid, _ in calls] == [111],
            f"only 111 reached the spawner; got {calls!r}",
        )
    finally:
        bot._spawn_continuous_searcher_thread = orig_spawn  # type: ignore[assignment]
        _fresh_registry()


def test_live_spawn_idempotent_for_already_running() -> None:
    section("9. live-spawn is idempotent for an already-running chat_id")
    _fresh_registry()
    db = _make_tmpdb()

    class _StubSearcher:
        def __init__(self, **_kw):
            pass

        async def run_forever(self):
            import asyncio as _aio
            try:
                await _aio.sleep(60)
            except _aio.CancelledError:
                raise

    import continuous_searcher
    orig = continuous_searcher.ContinuousSearcher
    continuous_searcher.ContinuousSearcher = _StubSearcher  # type: ignore[assignment]
    try:
        with _EnvVar("OINK_CONTINUOUS_MODE", "1"), _EnvVar("OINK_CONTINUOUS_CHAT_ID", None):
            t1 = bot.start_continuous_searcher_for(db, 888)
            time.sleep(0.1)
            t2 = bot.start_continuous_searcher_for(db, 888)
        _assert(t1 is not None, "first live-spawn returned a Thread")
        _assert(t1 is t2, "second live-spawn returns the same Thread (idempotent)")
    finally:
        continuous_searcher.ContinuousSearcher = orig  # type: ignore[assignment]
        _fresh_registry()


def test_continuous_mode_enabled_truthy_values() -> None:
    section("10. _continuous_mode_enabled accepts the documented truthy strings")
    for v in ("1", "true", "TRUE", "Yes", "on"):
        with _EnvVar("OINK_CONTINUOUS_MODE", v):
            _assert(
                bot._continuous_mode_enabled() is True,
                f"{v!r} should enable continuous mode",
            )
    for v in ("0", "false", "no", "off", "", "   "):
        with _EnvVar("OINK_CONTINUOUS_MODE", v):
            _assert(
                bot._continuous_mode_enabled() is False,
                f"{v!r} should NOT enable continuous mode",
            )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _seed_source_run(db: DB, chat_id: int, started_at: float) -> None:
    with db._conn() as c:
        c.execute(
            "INSERT INTO source_runs (pipeline_run_id, source_key, user_chat_id, "
            "status, started_at, finished_at) VALUES (1, 'linkedin', ?, 'ok', ?, ?)",
            (chat_id, started_at, started_at + 60),
        )


def test_resume_startup_delay_survives_restart() -> None:
    section("14. startup delay resumes the user's own cycle across restarts")
    db = _make_tmpdb()
    interval = 28800  # 8h, the prod value

    # Never searched → caller's stagger position stands.
    _assert(db.last_search_started_at(1926270) is None,
            "last_search_started_at is None with no source_runs rows")
    _assert(bot._resume_startup_delay(db, 1926270, 0, interval) == 0,
            "never-searched user keeps its stagger position")

    # Searched 1h ago → wait out the remaining 7h, not fire immediately.
    # This is the bug: chat_id 1926270 sat at stagger index 0, so every
    # deploy handed it a free full search (11 of them in two days).
    _seed_source_run(db, 1926270, time.time() - 3600)
    delay = bot._resume_startup_delay(db, 1926270, 0, interval)
    jitter = abs(1926270) % bot._RESUME_JITTER_S
    _assert(abs(delay - (interval - 3600 + jitter)) <= 2,
            f"restart resumes the remaining ~7h, got {delay}s")

    # ...and the tail of the stagger list is no longer starved: a user whose
    # interval already elapsed fires promptly instead of waiting 6.4h again.
    _seed_source_run(db, 1783830637, time.time() - interval - 500)
    delay = bot._resume_startup_delay(db, 1783830637, 23040, interval)
    _assert(delay == abs(1783830637) % bot._RESUME_JITTER_S,
            f"overdue user fires after jitter only, got {delay}s")

    # Overdue users get distinct jitters so they don't score concurrently.
    _assert(
        (abs(1926270) % bot._RESUME_JITTER_S)
        != (abs(1783830637) % bot._RESUME_JITTER_S),
        "chat_id-derived jitter spreads the herd",
    )

    # A broken telemetry read must not stop the searcher from starting.
    class _Boom:
        def last_search_started_at(self, _cid):
            raise RuntimeError("telemetry down")

    _assert(bot._resume_startup_delay(_Boom(), 1, 42, interval) == 42,
            "lookup failure falls back to the stagger position")


def test_web_users_negative_chat_ids_are_searchable() -> None:
    section("11. negative (web-only) chat_ids flow through enumerate + spawn")
    _fresh_registry()
    db = _make_tmpdb()
    # Telegram user and a web-only user, both fully onboarded.
    _seed_user(db, 100, completed=True, profile='{"x":1}')
    _seed_user(db, -3, completed=True, profile='{"x":1}')

    ids = db.onboarded_chat_ids()
    _assert(ids == [-3, 100], f"web user should enumerate; got {ids!r}")

    class _StubSearcher:
        def __init__(self, **_kwargs):
            pass

        async def run_forever(self):
            import asyncio as _aio
            await _aio.sleep(60)

    import continuous_searcher
    orig = continuous_searcher.ContinuousSearcher
    continuous_searcher.ContinuousSearcher = _StubSearcher  # type: ignore[assignment]
    try:
        t = bot._spawn_continuous_searcher_thread(db, -3, startup_delay=0)
        _assert(t is not None, "spawn accepts a negative chat_id")
        _assert(-3 in bot._CONTINUOUS_REGISTRY, "registry holds the web user")
        t0 = bot._spawn_continuous_searcher_thread(db, 0, startup_delay=0)
        _assert(t0 is None, "chat_id=0 is still refused")
    finally:
        continuous_searcher.ContinuousSearcher = orig  # type: ignore[assignment]
        _fresh_registry()


def test_continuous_searcher_ctor_accepts_negative() -> None:
    section("12. ContinuousSearcher accepts negative chat_id, refuses 0")
    from continuous_searcher import ContinuousSearcher

    s = ContinuousSearcher(
        db=None, chat_id=-7, interval_seconds=10,
        search_run_callable=lambda **kw: 0, min_sleep_seconds=1,
    )
    _assert(s.chat_id == -7, "negative chat_id constructs")
    try:
        ContinuousSearcher(
            db=None, chat_id=0, interval_seconds=10,
            search_run_callable=lambda **kw: 0, min_sleep_seconds=1,
        )
        _assert(False, "chat_id=0 should raise ValueError")
    except ValueError:
        _assert(True, "chat_id=0 raises ValueError")


def test_reconciler_spawns_missing_users() -> None:
    section("13. reconcile pass spawns users missing from the registry")
    _fresh_registry()
    db = _make_tmpdb()
    _seed_user(db, 100, completed=True, profile='{"x":1}')
    _seed_user(db, -4, completed=True, profile='{"x":1}')

    calls: list[int] = []

    def _fake_spawn(_db, cid, _delay, **_kw):
        calls.append(cid)
        t = threading.Thread(target=lambda: time.sleep(30), daemon=True)
        with bot._CONTINUOUS_REGISTRY_LOCK:
            bot._CONTINUOUS_REGISTRY[cid] = t
        t.start()
        return t

    orig = bot._spawn_continuous_searcher_thread
    bot._spawn_continuous_searcher_thread = _fake_spawn  # type: ignore[assignment]
    try:
        with _EnvVar("OINK_CONTINUOUS_CHAT_ID", None):
            spawned = bot._reconcile_continuous_once(db)
            _assert(sorted(calls) == [-4, 100],
                    f"first pass spawns both users; got {calls!r}")
            _assert(len(spawned) == 2, "returns the new threads")

            calls.clear()
            spawned = bot._reconcile_continuous_once(db)
            _assert(calls == [], "second pass is a no-op (registry full)")
            _assert(spawned == [], "no threads on a no-op pass")

        # Env pin restricts the reconciler exactly like startup.
        _fresh_registry()
        calls.clear()
        with _EnvVar("OINK_CONTINUOUS_CHAT_ID", "100"):
            bot._reconcile_continuous_once(db)
            _assert(calls == [100], f"pin honored; got {calls!r}")
    finally:
        bot._spawn_continuous_searcher_thread = orig  # type: ignore[assignment]
        _fresh_registry()


def main() -> int:
    test_onboarded_chat_ids_filters_correctly()
    test_resolver_env_override()
    test_resolver_db_fallback_when_env_unset()
    test_resolver_db_fallback_when_env_blank()
    test_resolver_env_garbage_falls_back()
    test_spawn_dedup_via_registry()
    test_maybe_start_no_op_when_mode_off()
    test_maybe_start_uses_resolver_and_dedups_on_replay()
    test_maybe_start_replay_with_real_spawn_dedups()
    test_live_spawn_no_op_when_mode_off()
    test_live_spawn_env_pinned_exclude_and_include()
    test_live_spawn_idempotent_for_already_running()
    test_continuous_mode_enabled_truthy_values()
    test_web_users_negative_chat_ids_are_searchable()
    test_continuous_searcher_ctor_accepts_negative()
    test_reconciler_spawns_missing_users()
    test_resume_startup_delay_survives_restart()
    print("\nAll continuous-default tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
