"""Auto profile rebuild trigger (algorithm v2.8, P4 pipeline overhaul).

`_maybe_auto_rebuild_profile` watches `users.skip_events_since_rebuild`
and kicks off `profile_builder.rebuild_profile` once the counter
crosses the configured threshold. Tests drive the FSM without burning
real Opus calls — `rebuild_profile` is injected via `_trigger_rebuild`.

DB-layer coverage of the counter (`append_skip_note` bumping it,
`reset_skip_events_since_rebuild` zeroing it, `get_…` reading it) is
exercised alongside the search-loop helper.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import db as db_module  # noqa: E402
import search_jobs  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path):
    return db_module.DB(tmp_path / "test.db")


def _register(db, chat_id: int) -> None:
    """Insert a minimal user row so append_skip_note has something to
    UPDATE. The bot's upsert path normally handles this but tests skip
    that for brevity."""
    db.upsert_user(chat_id)


# ----------------------------- counter wiring ----------------------------- #


def test_counter_starts_at_zero(tmp_db):
    _register(tmp_db, 1)
    assert tmp_db.get_skip_events_since_rebuild(1) == 0


def test_counter_increments_on_skip(tmp_db):
    """A single `append_skip_note` call bumps the counter from 0 to 1."""
    _register(tmp_db, 1)
    tmp_db.append_skip_note(1, "not interested in fintech")
    assert tmp_db.get_skip_events_since_rebuild(1) == 1


def test_counter_increments_repeatedly(tmp_db):
    _register(tmp_db, 1)
    for i in range(4):
        tmp_db.append_skip_note(1, f"reason #{i}")
    assert tmp_db.get_skip_events_since_rebuild(1) == 4


def test_empty_skip_note_does_not_bump_counter(tmp_db):
    """Whitespace-only feedback is treated as a no-op upstream — must
    not contribute to the rebuild trigger."""
    _register(tmp_db, 1)
    tmp_db.append_skip_note(1, "   ")
    tmp_db.append_skip_note(1, "")
    assert tmp_db.get_skip_events_since_rebuild(1) == 0


def test_reset_zeroes_the_counter(tmp_db):
    _register(tmp_db, 1)
    for _ in range(3):
        tmp_db.append_skip_note(1, "x")
    assert tmp_db.get_skip_events_since_rebuild(1) == 3
    tmp_db.reset_skip_events_since_rebuild(1)
    assert tmp_db.get_skip_events_since_rebuild(1) == 0


# ----------------------------- trigger helper ----------------------------- #


def _ok_result(**overrides):
    """Build a SimpleNamespace standing in for BuildResult.status='ok'."""
    base = {"status": "ok", "profile": {}, "error": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def _fail_result(**overrides):
    base = {"status": "cli_missing_or_timeout", "profile": None, "error": "timeout"}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_below_threshold_no_rebuild(tmp_db):
    _register(tmp_db, 7)
    for _ in range(4):
        tmp_db.append_skip_note(7, "reason")
    calls: list[tuple] = []

    def _trigger(db, chat_id):
        calls.append((db, chat_id))
        return _ok_result()

    fired = search_jobs._maybe_auto_rebuild_profile(
        tmp_db, 7, threshold=5, _trigger_rebuild=_trigger,
    )
    assert fired is False
    assert calls == []
    # Counter must NOT be touched.
    assert tmp_db.get_skip_events_since_rebuild(7) == 4


def test_at_threshold_triggers_rebuild_and_resets(tmp_db):
    _register(tmp_db, 9)
    for _ in range(5):
        tmp_db.append_skip_note(9, "x")
    assert tmp_db.get_skip_events_since_rebuild(9) == 5

    calls: list[tuple] = []

    def _trigger(db, chat_id):
        calls.append((db, chat_id))
        return _ok_result()

    fired = search_jobs._maybe_auto_rebuild_profile(
        tmp_db, 9, threshold=5, _trigger_rebuild=_trigger,
    )
    assert fired is True
    assert calls == [(tmp_db, 9)]
    # Counter resets to 0 on success.
    assert tmp_db.get_skip_events_since_rebuild(9) == 0


def test_above_threshold_also_triggers(tmp_db):
    """If the user managed to overshoot the threshold (race, retry that
    consumed events), the helper still fires once and resets — it
    doesn't require an EXACT equality."""
    _register(tmp_db, 11)
    for _ in range(8):
        tmp_db.append_skip_note(11, "x")

    def _trigger(db, chat_id):
        return _ok_result()

    fired = search_jobs._maybe_auto_rebuild_profile(
        tmp_db, 11, threshold=5, _trigger_rebuild=_trigger,
    )
    assert fired is True
    assert tmp_db.get_skip_events_since_rebuild(11) == 0


def test_rebuild_failure_does_not_reset_counter(tmp_db):
    """If `rebuild_profile` returns a non-ok BuildResult, the counter
    must NOT be reset — so the next iteration retries against the same
    accumulated signal."""
    _register(tmp_db, 13)
    for _ in range(5):
        tmp_db.append_skip_note(13, "x")

    def _trigger(db, chat_id):
        return _fail_result()

    fired = search_jobs._maybe_auto_rebuild_profile(
        tmp_db, 13, threshold=5, _trigger_rebuild=_trigger,
    )
    assert fired is False
    # Counter STAYS — next iteration will retry.
    assert tmp_db.get_skip_events_since_rebuild(13) == 5


def test_rebuild_exception_does_not_reset_counter(tmp_db):
    """If `rebuild_profile` RAISES, the helper swallows the exception
    (logs it) and leaves the counter untouched."""
    _register(tmp_db, 15)
    for _ in range(5):
        tmp_db.append_skip_note(15, "x")

    def _trigger(db, chat_id):
        raise RuntimeError("boom")

    fired = search_jobs._maybe_auto_rebuild_profile(
        tmp_db, 15, threshold=5, _trigger_rebuild=_trigger,
    )
    assert fired is False
    assert tmp_db.get_skip_events_since_rebuild(15) == 5


def test_threshold_zero_disables_helper(tmp_db):
    """threshold<=0 → never fire. Operators flip the gate off by
    setting defaults.auto_rebuild_skip_threshold to 0."""
    _register(tmp_db, 17)
    for _ in range(20):
        tmp_db.append_skip_note(17, "x")

    calls: list[tuple] = []

    def _trigger(db, chat_id):
        calls.append((db, chat_id))
        return _ok_result()

    fired = search_jobs._maybe_auto_rebuild_profile(
        tmp_db, 17, threshold=0, _trigger_rebuild=_trigger,
    )
    assert fired is False
    assert calls == []
