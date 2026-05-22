"""Adaptive source cooldown FSM (algorithm v2.8, P4 pipeline overhaul).

`should_run_source` reads `source_novelty_ratio` over the last 24h and
demotes chronically-quiet sources to half-frequency. Tests drive the FSM
through canned novelty values without going through the real adapter
pipeline.

Strategy
--------
We don't synthesise real `search_fetches` rows for every test — that's
covered by `test_page_memory.py`. Instead we wrap a fresh `DB` in a thin
shim that overrides `source_novelty_ratio` so each test can set the
return value explicitly. The cooldown methods (`get_source_cooldown`,
`upsert_source_cooldown`) are real and run against the SQLite file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import db as db_module  # noqa: E402
import search_jobs  # noqa: E402


class _NoveltyFakeDB:
    """Wraps a real DB instance but overrides `source_novelty_ratio` so
    tests can pin the value. Everything else (cooldown read/write) hits
    the real SQLite file underneath.
    """

    def __init__(self, real_db: db_module.DB, novelty_by_source: dict[str, float]):
        self._db = real_db
        self.novelty = dict(novelty_by_source)

    def source_novelty_ratio(self, source: str, since_seconds_ago: float) -> float:
        return float(self.novelty.get(source, 0.0))

    def get_source_cooldown(self, source: str):
        return self._db.get_source_cooldown(source)

    def upsert_source_cooldown(self, source, state, consec):
        return self._db.upsert_source_cooldown(source, state, consec)


@pytest.fixture
def tmp_db(tmp_path):
    return db_module.DB(tmp_path / "test.db")


# ---------- DB layer ----------

def test_get_returns_none_when_no_row(tmp_db):
    assert tmp_db.get_source_cooldown("linkedin") is None


def test_upsert_round_trips(tmp_db):
    tmp_db.upsert_source_cooldown("linkedin", "half_freq", 7)
    row = tmp_db.get_source_cooldown("linkedin")
    assert row is not None
    assert row["state"] == "half_freq"
    assert row["consecutive_low_novelty_cycles"] == 7
    assert row["last_updated"] > 0


def test_upsert_replaces_existing(tmp_db):
    tmp_db.upsert_source_cooldown("linkedin", "half_freq", 5)
    tmp_db.upsert_source_cooldown("linkedin", "normal", 0)
    row = tmp_db.get_source_cooldown("linkedin")
    assert row["state"] == "normal"
    assert row["consecutive_low_novelty_cycles"] == 0


def test_upsert_clamps_invalid_state(tmp_db):
    tmp_db.upsert_source_cooldown("linkedin", "garbage", 3)
    row = tmp_db.get_source_cooldown("linkedin")
    assert row["state"] == "normal"


# ---------- FSM ----------

def test_high_novelty_stays_normal(tmp_db):
    """50% novelty — every cycle returns True; state remains 'normal'
    and the counter never increments."""
    fake = _NoveltyFakeDB(tmp_db, {"linkedin": 0.5})
    for cycle in range(5):
        assert search_jobs.should_run_source(
            fake, "linkedin", cycle_index=cycle,
            low_novelty_threshold=0.05,
        ) is True
    row = tmp_db.get_source_cooldown("linkedin")
    # The recovery branch upserts state='normal' / counter=0 only when a
    # prior row exists with a different value — a never-seen source
    # stays unwritten. Either outcome is acceptable; the FSM contract is
    # observed via `should_run_source` returning True.
    if row is not None:
        assert row["state"] == "normal"
        assert row["consecutive_low_novelty_cycles"] == 0


def test_low_novelty_warm_up_keeps_running(tmp_db):
    """First two low-novelty cycles increment the counter but the
    source keeps running (state is still 'normal' until the third low
    cycle flips it to 'half_freq')."""
    fake = _NoveltyFakeDB(tmp_db, {"linkedin": 0.01})
    # Cycle 0 → counter goes 0 → 1, state stays 'normal', runs.
    assert search_jobs.should_run_source(
        fake, "linkedin", cycle_index=0, low_novelty_threshold=0.05,
    ) is True
    row = tmp_db.get_source_cooldown("linkedin")
    assert row["consecutive_low_novelty_cycles"] == 1
    assert row["state"] == "normal"

    # Cycle 1 → 1 → 2, still normal.
    assert search_jobs.should_run_source(
        fake, "linkedin", cycle_index=1, low_novelty_threshold=0.05,
    ) is True
    row = tmp_db.get_source_cooldown("linkedin")
    assert row["consecutive_low_novelty_cycles"] == 2
    assert row["state"] == "normal"


def test_low_novelty_for_3_cycles_demotes_to_half_freq(tmp_db):
    """Three consecutive low-novelty cycles → state flips to
    'half_freq'. The third cycle is even, so the source SKIPS (returns
    False)."""
    fake = _NoveltyFakeDB(tmp_db, {"linkedin": 0.01})
    # Burn cycles 0..2 (three checks). The third one demotes AND, since
    # cycle_index=2 is even, returns False.
    assert search_jobs.should_run_source(fake, "linkedin", cycle_index=0) is True
    assert search_jobs.should_run_source(fake, "linkedin", cycle_index=1) is True
    # Third low-novelty check — state flips to half_freq; cycle_index=2
    # is EVEN so the source is now on the OFF half of the alternation.
    third = search_jobs.should_run_source(fake, "linkedin", cycle_index=2)
    row = tmp_db.get_source_cooldown("linkedin")
    assert row["state"] == "half_freq"
    assert row["consecutive_low_novelty_cycles"] == 3
    # cycle_index=2 → even → half_freq returns False.
    assert third is False


def test_half_freq_runs_every_other_cycle(tmp_db):
    """In 'half_freq' state, odd cycles RUN and even cycles SKIP."""
    # Pre-seed the cooldown to half_freq with counter past the demotion
    # threshold so we drive only the parity branch.
    tmp_db.upsert_source_cooldown("linkedin", "half_freq", 5)
    fake = _NoveltyFakeDB(tmp_db, {"linkedin": 0.01})  # still low
    assert search_jobs.should_run_source(fake, "linkedin", cycle_index=0) is False
    assert search_jobs.should_run_source(fake, "linkedin", cycle_index=1) is True
    assert search_jobs.should_run_source(fake, "linkedin", cycle_index=2) is False
    assert search_jobs.should_run_source(fake, "linkedin", cycle_index=3) is True


def test_recovery_to_normal(tmp_db):
    """A 'half_freq' source that recovers (novelty back above the
    threshold) flips state to 'normal' immediately on the next check,
    resets the counter, and runs."""
    tmp_db.upsert_source_cooldown("linkedin", "half_freq", 8)
    fake = _NoveltyFakeDB(tmp_db, {"linkedin": 0.30})
    out = search_jobs.should_run_source(
        fake, "linkedin", cycle_index=0, low_novelty_threshold=0.05,
    )
    assert out is True
    row = tmp_db.get_source_cooldown("linkedin")
    assert row["state"] == "normal"
    assert row["consecutive_low_novelty_cycles"] == 0


def test_recovery_runs_even_on_even_cycle_index(tmp_db):
    """The recovery path returns True regardless of cycle parity — the
    half_freq alternation only applies while state IS half_freq."""
    tmp_db.upsert_source_cooldown("linkedin", "half_freq", 5)
    fake = _NoveltyFakeDB(tmp_db, {"linkedin": 0.5})
    # Even cycle_index — half_freq would have said False; recovery
    # short-circuits to True.
    assert search_jobs.should_run_source(fake, "linkedin", cycle_index=0) is True


def test_db_failure_falls_back_to_running(tmp_db):
    """If novelty_ratio raises, the source still runs. We never want a
    DB hiccup to silently disable a source."""

    class _ExplodingDB:
        def source_novelty_ratio(self, source, since_seconds_ago):
            raise RuntimeError("boom")

        def get_source_cooldown(self, source):
            return None

        def upsert_source_cooldown(self, *args, **kwargs):
            return None

    assert search_jobs.should_run_source(
        _ExplodingDB(), "linkedin", cycle_index=0,
    ) is True


def test_none_db_short_circuits_to_running(tmp_db):
    """Passing db=None always returns True — used by the dry-run path
    where the cooldown table may not even exist."""
    assert search_jobs.should_run_source(None, "linkedin", cycle_index=0) is True
