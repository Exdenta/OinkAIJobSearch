"""Regression tests for the P6-T1 fix: adaptive source cooldown was
demoting every uninstrumented source because `source_novelty_ratio`
coerced "no data" to 0.0, which the FSM read as "0% novelty".

Two layers covered:

  1. ``db.source_novelty_ratio`` — distinguishes "no rows in window"
     (None) from "rows exist but all jobs_seen==0" (0.0).
  2. ``search_jobs.should_run_source`` — treats None as "no signal":
     does not demote, leaves the FSM state alone, but still honours a
     pre-existing ``half_freq`` row via the parity gate.
  3. ``db.reset_uninstrumented_source_cooldowns`` — one-shot migration
     that unsticks wrongly-demoted rows from the live DB; idempotent
     on subsequent runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import db as db_module  # noqa: E402
import search_jobs  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path):
    """Use a real file-backed SQLite so the DB class's `_conn()`
    (which expects a Path) operates the same way as in production.
    """
    return db_module.DB(tmp_path / "test.db")


class _DirectFakeDB:
    """Routes `source_novelty_ratio` to a real DB and exposes the rest
    of the cooldown surface verbatim. Used to test the FSM against
    REAL `search_fetches` data (instead of stubbed novelty values),
    which is the situation the production bug arose in.
    """

    def __init__(self, real_db: db_module.DB):
        self._db = real_db

    def source_novelty_ratio(self, source, since_seconds_ago):
        return self._db.source_novelty_ratio(source, since_seconds_ago)

    def get_source_cooldown(self, source):
        return self._db.get_source_cooldown(source)

    def upsert_source_cooldown(self, source, state, consec):
        return self._db.upsert_source_cooldown(source, state, consec)


# ---------- (A) db.source_novelty_ratio: None vs 0.0 ----------

def test_source_novelty_ratio_returns_none_on_no_rows(tmp_db):
    """Empty `search_fetches` for the source → returns None.

    This is the no-instrumentation signal that
    `should_run_source` reads to decide "no FSM update".
    """
    out = tmp_db.source_novelty_ratio("hackernews", since_seconds_ago=86400)
    assert out is None


def test_source_novelty_ratio_returns_zero_on_seen_zero(tmp_db):
    """Rows exist but ``SUM(jobs_seen) == 0`` → returns 0.0, not None.

    Distinct from the no-data channel: this is "we fetched, the
    source returned nothing", which IS a real (terrible) novelty
    signal that should drive the FSM.
    """
    tmp_db.record_fetch("linkedin", "q1", 1, "", jobs_seen=0, jobs_new=0)
    tmp_db.record_fetch("linkedin", "q2", 1, "", jobs_seen=0, jobs_new=0)
    out = tmp_db.source_novelty_ratio("linkedin", since_seconds_ago=86400)
    assert out == 0.0
    assert out is not None  # belt-and-suspenders: the 0.0 channel is real.


# ---------- (B) should_run_source: None means "no signal" ----------

def test_should_run_source_no_signal_does_not_demote(tmp_db):
    """Uninstrumented source in 'normal' state — after 5 iterations
    the FSM has neither incremented `low_cycles` nor demoted.

    Pre-fix behaviour: after 3 iterations the source would have
    flipped to `half_freq` (3 consecutive "low novelty" cycles).
    """
    fake = _DirectFakeDB(tmp_db)
    for cycle in range(5):
        # All iterations should run — never demote.
        assert search_jobs.should_run_source(
            fake, "hackernews", cycle_index=cycle,
            low_novelty_threshold=0.05,
        ) is True

    row = tmp_db.get_source_cooldown("hackernews")
    # No instrumentation → no upsert → no row exists. (Or, if one had
    # been seeded as 'normal' / 0, it would still be normal / 0.)
    assert row is None


def test_should_run_source_no_signal_preserves_half_freq(tmp_db):
    """A source with a pre-existing 'half_freq' row (e.g. from a real
    demotion years ago, before the bug was fixed) and no current
    signal: state is preserved unchanged. The parity gate still
    controls whether it runs THIS cycle.
    """
    # Seed the cooldown — pretend an older code path left this stuck.
    tmp_db.upsert_source_cooldown("hackernews", "half_freq", 5)
    fake = _DirectFakeDB(tmp_db)

    # Even cycle → half_freq says skip.
    assert search_jobs.should_run_source(
        fake, "hackernews", cycle_index=0,
        low_novelty_threshold=0.05,
    ) is False
    # Odd cycle → half_freq says run.
    assert search_jobs.should_run_source(
        fake, "hackernews", cycle_index=1,
        low_novelty_threshold=0.05,
    ) is True

    # State unchanged — the no-signal branch never wrote.
    row = tmp_db.get_source_cooldown("hackernews")
    assert row is not None
    assert row["state"] == "half_freq"
    assert row["consecutive_low_novelty_cycles"] == 5


def test_should_run_source_instrumented_still_demotes_on_low_novelty(tmp_db):
    """Instrumented source with 1% novelty for 3 cycles → demoted as
    before. The fix is targeted: it changes ONLY the no-data branch;
    real low-novelty observations still trip the FSM.
    """
    # 1 jobs_new out of 100 jobs_seen = 1% novelty (< 5% threshold).
    tmp_db.record_fetch("linkedin", "q1", 1, "", jobs_seen=100, jobs_new=1)
    fake = _DirectFakeDB(tmp_db)

    # First two low cycles — counter increments, state stays normal.
    assert search_jobs.should_run_source(
        fake, "linkedin", cycle_index=0, low_novelty_threshold=0.05,
    ) is True
    assert tmp_db.get_source_cooldown("linkedin")["state"] == "normal"
    assert search_jobs.should_run_source(
        fake, "linkedin", cycle_index=1, low_novelty_threshold=0.05,
    ) is True
    assert tmp_db.get_source_cooldown("linkedin")["state"] == "normal"

    # Third low cycle — state flips to half_freq.
    third = search_jobs.should_run_source(
        fake, "linkedin", cycle_index=2, low_novelty_threshold=0.05,
    )
    row = tmp_db.get_source_cooldown("linkedin")
    assert row["state"] == "half_freq"
    assert row["consecutive_low_novelty_cycles"] == 3
    # cycle_index=2 is even → half_freq skips.
    assert third is False


# ---------- (C) reset_uninstrumented_source_cooldowns ----------

# Match the live whitelist used by `search_jobs.run` so the migration
# we exercise here is byte-for-byte the one production runs. Sourced from
# the module constant (not a hardcoded literal) so Tier-4's added keys —
# devex / the curated sub-boards — stay in lockstep.
_INSTRUMENTED = set(search_jobs._P2_INSTRUMENTED_SOURCES)

# A representative subset of the uninstrumented sources that the live DB
# had stuck at `half_freq, low_cycles=11`. NOTE: devex is now instrumented
# (Tier 4), so it must NOT appear here — these are sources that genuinely
# never call record_fetch.
_UNINSTRUMENTED_SAMPLE = ["hackernews", "reliefweb", "euraxess"]


def test_reset_uninstrumented_source_cooldowns(tmp_db):
    """Seed 5 instrumented (half_freq, low_cycles=11) + 3
    uninstrumented (half_freq, low_cycles=11) rows. Reset with the
    instrumented whitelist. Verify the uninstrumented 3 are now
    normal/0 and the instrumented 5 are unchanged.
    """
    for src in _INSTRUMENTED:
        tmp_db.upsert_source_cooldown(src, "half_freq", 11)
    for src in _UNINSTRUMENTED_SAMPLE:
        tmp_db.upsert_source_cooldown(src, "half_freq", 11)

    updated = tmp_db.reset_uninstrumented_source_cooldowns(_INSTRUMENTED)
    assert updated == len(_UNINSTRUMENTED_SAMPLE)

    # Uninstrumented → normal / 0.
    for src in _UNINSTRUMENTED_SAMPLE:
        row = tmp_db.get_source_cooldown(src)
        assert row is not None, f"{src} row should still exist"
        assert row["state"] == "normal", (
            f"{src} expected 'normal' after reset, got {row['state']!r}"
        )
        assert row["consecutive_low_novelty_cycles"] == 0

    # Instrumented → untouched.
    for src in _INSTRUMENTED:
        row = tmp_db.get_source_cooldown(src)
        assert row is not None, f"{src} row should still exist"
        assert row["state"] == "half_freq", (
            f"{src} should be untouched by the migration, got {row['state']!r}"
        )
        assert row["consecutive_low_novelty_cycles"] == 11


def test_reset_idempotent(tmp_db):
    """Second call returns 0 rows updated — the WHERE clause filters
    rows already at state='normal' AND consecutive_low_novelty_cycles=0.
    """
    for src in _UNINSTRUMENTED_SAMPLE:
        tmp_db.upsert_source_cooldown(src, "half_freq", 11)

    first = tmp_db.reset_uninstrumented_source_cooldowns(_INSTRUMENTED)
    assert first == len(_UNINSTRUMENTED_SAMPLE)

    second = tmp_db.reset_uninstrumented_source_cooldowns(_INSTRUMENTED)
    assert second == 0

    # State still normal / 0 — no churn.
    for src in _UNINSTRUMENTED_SAMPLE:
        row = tmp_db.get_source_cooldown(src)
        assert row["state"] == "normal"
        assert row["consecutive_low_novelty_cycles"] == 0
