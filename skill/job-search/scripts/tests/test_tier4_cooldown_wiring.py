#!/usr/bin/env python3
"""Tier 4 — wire devex / curated sub-boards into the adaptive
source-cooldown FSM.

Before Tier 4, ``devex`` and the curated sub-boards
(``remocate`` / ``wantapply`` / ``remoterocketship``) never called
``db.record_fetch``. They emitted no novelty signal, so the FSM's
``source_novelty_ratio`` returned ``None`` (no-data channel) and they ran
EVERY cycle no matter how little they found.

This suite proves the wiring without touching the network or the real
``claude`` CLI:

  A. The newly-instrumented source KEYS flow through the EXISTING FSM
     identically to the P2 set — 3 consecutive low-novelty cycles demote
     to ``half_freq``, one good cycle recovers. Driven against REAL
     ``search_fetches`` rows + the REAL ``should_run_source`` (no stubbed
     novelty), so it exercises the production code path end to end.

  B. Each adapter calls ``record_fetch`` under its OWN source key when a
     ``db`` is supplied, and records NOTHING when ``db`` is None (the
     dry-run preview path). The CLI is stubbed to return a canned JSON
     envelope.

  C. ``_curated_subboards_to_run`` demotes one curated board while leaving
     its siblings running — the per-sub-board granularity the task asked
     for.

  D. The instrumentation/whitelist invariants hold: every new key is in
     ``_P2_INSTRUMENTED_SOURCES`` (so the P6-T1 migration won't wrongly
     un-demote it) and the curated MODULE key is NOT (it cools down per
     sub-board, not as a unit).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import db as db_module  # noqa: E402
import search_jobs  # noqa: E402


# The source keys Tier 4 newly instruments (devex + the three curated
# sub-boards). The curated MODULE key `curated_boards` is deliberately
# absent — it cools down per sub-board.
_NEW_KEYS = ["devex", "remocate", "wantapply", "remoterocketship"]


@pytest.fixture
def tmp_db(tmp_path):
    """Real file-backed SQLite so `_conn()` behaves exactly like prod."""
    return db_module.DB(tmp_path / "test.db")


class _DirectFakeDB:
    """Routes `source_novelty_ratio` to a real DB and exposes the rest of
    the cooldown surface verbatim — so the FSM runs against REAL
    `search_fetches` data, the situation Tier 4 actually operates in.
    Mirrors the shim in test_cooldown_uninstrumented_fix.py.
    """

    def __init__(self, real_db: db_module.DB):
        self._db = real_db

    def source_novelty_ratio(self, source, since_seconds_ago):
        return self._db.source_novelty_ratio(source, since_seconds_ago)

    def get_source_cooldown(self, source):
        return self._db.get_source_cooldown(source)

    def upsert_source_cooldown(self, source, state, consec):
        return self._db.upsert_source_cooldown(source, state, consec)


# --------------------------------------------------------------------------- #
# (A) FSM parity — the new keys demote + recover like the P2 set            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("source", _NEW_KEYS)
def test_new_key_demotes_after_three_low_cycles(tmp_db, source):
    """1% novelty (< 5% threshold) for 3 consecutive cycles flips the new
    source to half_freq — identical to the linkedin path. Uses REAL
    novelty data so the whole ratio→FSM chain is exercised."""
    # 1 new out of 100 seen = 1% novelty.
    tmp_db.record_fetch(source, source, 1, "", jobs_seen=100, jobs_new=1)
    fake = _DirectFakeDB(tmp_db)

    # Cycles 0 and 1: counter climbs, state stays normal, source runs.
    assert search_jobs.should_run_source(
        fake, source, cycle_index=0, low_novelty_threshold=0.05,
    ) is True
    assert tmp_db.get_source_cooldown(source)["state"] == "normal"
    assert search_jobs.should_run_source(
        fake, source, cycle_index=1, low_novelty_threshold=0.05,
    ) is True
    assert tmp_db.get_source_cooldown(source)["state"] == "normal"

    # Cycle 2 (the 3rd low check): demote to half_freq. cycle_index=2 is
    # even, so half_freq skips this cycle.
    third = search_jobs.should_run_source(
        fake, source, cycle_index=2, low_novelty_threshold=0.05,
    )
    row = tmp_db.get_source_cooldown(source)
    assert row["state"] == "half_freq"
    assert row["consecutive_low_novelty_cycles"] == 3
    assert third is False


@pytest.mark.parametrize("source", _NEW_KEYS)
def test_new_key_recovers_on_one_good_cycle(tmp_db, source):
    """A demoted new source recovers to normal the moment its 24h novelty
    climbs back above threshold — the existing 1-good-cycle recovery."""
    # Pre-demote.
    tmp_db.upsert_source_cooldown(source, "half_freq", 8)
    # Record a high-novelty fetch (50% new) — recovery signal.
    tmp_db.record_fetch(source, source, 1, "", jobs_seen=10, jobs_new=5)
    fake = _DirectFakeDB(tmp_db)

    out = search_jobs.should_run_source(
        fake, source, cycle_index=0, low_novelty_threshold=0.05,
    )
    assert out is True  # recovery runs regardless of cycle parity
    row = tmp_db.get_source_cooldown(source)
    assert row["state"] == "normal"
    assert row["consecutive_low_novelty_cycles"] == 0


@pytest.mark.parametrize("source", _NEW_KEYS)
def test_new_key_half_freq_parity(tmp_db, source):
    """While demoted AND still quiet, the new source runs only on odd
    cycles — the standard half-frequency alternation."""
    tmp_db.upsert_source_cooldown(source, "half_freq", 5)
    tmp_db.record_fetch(source, source, 1, "", jobs_seen=100, jobs_new=1)  # 1% — still low
    fake = _DirectFakeDB(tmp_db)
    assert search_jobs.should_run_source(fake, source, cycle_index=0) is False
    assert search_jobs.should_run_source(fake, source, cycle_index=1) is True
    assert search_jobs.should_run_source(fake, source, cycle_index=2) is False
    assert search_jobs.should_run_source(fake, source, cycle_index=3) is True


@pytest.mark.parametrize("source", _NEW_KEYS)
def test_new_key_no_signal_does_not_demote(tmp_db, source):
    """Before the adapter has recorded anything, novelty is None (no-data
    channel) and the FSM must NOT demote — the P6-T1 contract still holds
    for the freshly-added keys."""
    fake = _DirectFakeDB(tmp_db)
    for cycle in range(5):
        assert search_jobs.should_run_source(
            fake, source, cycle_index=cycle, low_novelty_threshold=0.05,
        ) is True
    # No instrumentation row yet → no FSM write.
    assert tmp_db.get_source_cooldown(source) is None


# --------------------------------------------------------------------------- #
# (B) adapters call record_fetch under their own key                        #
# --------------------------------------------------------------------------- #


# JSON envelope shaped like `claude -p --output-format json` output: the
# adapters unwrap `result` then json.loads the inner string. Two postings
# so jobs_seen is non-trivial.
def _fake_cli_stdout(url_a: str, url_b: str) -> str:
    import json
    inner = json.dumps({
        "jobs": [
            {"title": "Frontend Engineer", "company": "Acme",
             "location": "Remote", "url": url_a, "posted_at": "", "snippet": "x"},
            {"title": "Vue Developer", "company": "Globex",
             "location": "EU", "url": url_b, "posted_at": "", "snippet": "y"},
        ]
    })
    return json.dumps({"result": inner})


def _patch_cli(monkeypatch, module, stdout: str, attr: str) -> None:
    """Stub a delegation adapter's Claude wrapper to return canned JSON
    and force the wrapped-path flag on so we don't fall through to the
    plain run_p_with_tools branch."""
    monkeypatch.setattr(module, attr, lambda *a, **k: stdout, raising=False)
    if hasattr(module, "_HAS_WRAPPED"):
        monkeypatch.setattr(module, "_HAS_WRAPPED", True, raising=False)


def _suppress_detail_fetch(monkeypatch) -> None:
    """devex enriches via sources._detail_fetch.fetch_many_bodies (network).
    Stub it to a no-op map so the test never touches the wire."""
    import sources._detail_fetch as detail
    monkeypatch.setattr(detail, "fetch_many_bodies",
                        lambda urls, **k: {}, raising=False)


def test_devex_records_fetch_under_devex_key(tmp_db, monkeypatch):
    from sources import devex
    _patch_cli(
        monkeypatch, devex,
        _fake_cli_stdout(
            "https://www.devex.com/jobs/role-a-at-unhcr-900001",
            "https://www.devex.com/jobs/role-b-at-undp-900002",
        ),
        "_wrapped_run_p_with_tools",
    )
    _suppress_detail_fetch(monkeypatch)

    jobs = devex.fetch({"max_per_source": 5, "ai_scrape_timeout_s": 30}, db=tmp_db)
    assert len(jobs) == 2

    row = tmp_db.get_fetch("devex", "devex", 1, "")
    assert row is not None, "devex must record a search_fetches row"
    assert row["jobs_seen"] == 2
    # Both URLs are brand new → both novel.
    assert row["jobs_new"] == 2
    # Novelty ratio is now available for the FSM.
    assert tmp_db.source_novelty_ratio("devex", 86400) == 1.0


def test_curated_boards_records_per_subboard(tmp_db, monkeypatch):
    """Only the enabled board records, and it records under its OWN key
    (not under `curated_boards`)."""
    from sources import curated_boards
    monkeypatch.setattr(
        curated_boards, "wrapped_run_p_with_tools",
        lambda *a, **k: _fake_cli_stdout(
            "https://remocate.app/jobs/aa", "https://remocate.app/jobs/bb",
        ),
        raising=False,
    )
    filters = {
        "sources": {"remocate": True, "wantapply": False, "remoterocketship": False},
        "max_per_source": 5,
        "ai_scrape_timeout_s": 30,
    }
    jobs = curated_boards.fetch(filters, db=tmp_db)
    assert len(jobs) == 2
    assert all(j.source == "remocate" for j in jobs)

    # Recorded under the sub-board key.
    row = tmp_db.get_fetch("remocate", "remocate", 1, "")
    assert row is not None and row["jobs_seen"] == 2
    # NOT under the module key, and NOT under the disabled siblings.
    assert tmp_db.get_fetch("curated_boards", "curated_boards", 1, "") is None
    assert tmp_db.get_fetch("wantapply", "wantapply", 1, "") is None
    assert tmp_db.get_fetch("remoterocketship", "remoterocketship", 1, "") is None


def test_devex_db_none_records_nothing(tmp_db, monkeypatch):
    """The dry-run preview path (db=None) must record no novelty — exactly
    like web_search / justjoinit when invoked without a cursor db."""
    from sources import devex
    _patch_cli(
        monkeypatch, devex,
        _fake_cli_stdout(
            "https://www.devex.com/jobs/x-at-org-900003",
            "https://www.devex.com/jobs/y-at-org-900004",
        ),
        "_wrapped_run_p_with_tools",
    )
    _suppress_detail_fetch(monkeypatch)

    jobs = devex.fetch({"max_per_source": 5, "ai_scrape_timeout_s": 30})
    assert len(jobs) == 2  # still returns postings
    # No row written for any plausible cell.
    assert tmp_db.get_fetch("devex", "devex", 1, "") is None


def test_curated_db_none_records_nothing(tmp_db, monkeypatch):
    from sources import curated_boards
    monkeypatch.setattr(
        curated_boards, "wrapped_run_p_with_tools",
        lambda *a, **k: _fake_cli_stdout(
            "https://remocate.app/jobs/cc", "https://remocate.app/jobs/dd",
        ),
        raising=False,
    )
    filters = {
        "sources": {"remocate": True, "wantapply": False, "remoterocketship": False},
        "max_per_source": 5,
    }
    jobs = curated_boards.fetch(filters)  # db omitted
    assert len(jobs) == 2
    assert tmp_db.get_fetch("remocate", "remocate", 1, "") is None


# --------------------------------------------------------------------------- #
# (C) per-sub-board dispatch split                                          #
# --------------------------------------------------------------------------- #


def test_curated_subboards_split_demotes_one_keeps_others(tmp_db):
    """One demoted board (half_freq, even cycle) is cooled while its
    enabled siblings keep running — the per-board independence."""
    enabled = {"remocate": True, "wantapply": True, "remoterocketship": False}
    # remocate demoted + still quiet → cooled on even cycle.
    tmp_db.upsert_source_cooldown("remocate", "half_freq", 5)
    tmp_db.record_fetch("remocate", "remocate", 1, "", jobs_seen=100, jobs_new=1)
    # wantapply healthy → runs.
    tmp_db.record_fetch("wantapply", "wantapply", 1, "", jobs_seen=10, jobs_new=5)

    to_run, cooled = search_jobs._curated_subboards_to_run(
        tmp_db, enabled, cycle_index=0, low_novelty_threshold=0.05,
    )
    assert to_run == ["wantapply"]
    assert cooled == ["remocate"]
    # remoterocketship is disabled → never considered.
    assert "remoterocketship" not in to_run
    assert "remoterocketship" not in cooled


def test_curated_subboards_dry_run_bypasses_gate(tmp_db):
    """db=None → every enabled board runs, nothing cooled (preview shows
    the full source set)."""
    enabled = {"remocate": True, "wantapply": True, "remoterocketship": True}
    # Even a pre-existing demotion is ignored when db is None.
    to_run, cooled = search_jobs._curated_subboards_to_run(
        None, enabled, cycle_index=0, low_novelty_threshold=0.05,
    )
    assert set(to_run) == {"remocate", "wantapply", "remoterocketship"}
    assert cooled == []


def test_curated_subboards_all_cooled_returns_empty(tmp_db):
    """When every enabled board is demoted AND on the OFF half, nothing
    runs — the dispatcher will skip the whole module this cycle."""
    enabled = {"remocate": True, "wantapply": True, "remoterocketship": False}
    for b in ("remocate", "wantapply"):
        tmp_db.upsert_source_cooldown(b, "half_freq", 5)
        tmp_db.record_fetch(b, b, 1, "", jobs_seen=100, jobs_new=1)
    to_run, cooled = search_jobs._curated_subboards_to_run(
        tmp_db, enabled, cycle_index=0, low_novelty_threshold=0.05,  # even → OFF
    )
    assert to_run == []
    assert set(cooled) == {"remocate", "wantapply"}


# --------------------------------------------------------------------------- #
# (D) instrumentation / whitelist invariants                                #
# --------------------------------------------------------------------------- #


def test_new_keys_in_instrumented_whitelist():
    """Every newly-instrumented sub-board/source key is in
    `_P2_INSTRUMENTED_SOURCES` so the P6-T1 migration
    (`reset_uninstrumented_source_cooldowns`) treats it as FSM-driven and
    never wrongly resets a real demotion."""
    for key in _NEW_KEYS:
        assert key in search_jobs._P2_INSTRUMENTED_SOURCES, key


def test_curated_module_key_not_in_whitelist():
    """The curated MODULE key must NOT be in the instrumented set — it
    never records under `curated_boards` (only per sub-board), so listing
    it would mislead the migration whitelist."""
    assert "curated_boards" not in search_jobs._P2_INSTRUMENTED_SOURCES


def test_reset_migration_leaves_new_keys_alone(tmp_db):
    """A real demotion on a Tier-4 key survives the P6-T1 migration: the
    key is whitelisted, so `reset_uninstrumented_source_cooldowns` does
    not flip it back to normal."""
    tmp_db.upsert_source_cooldown("devex", "half_freq", 11)
    tmp_db.upsert_source_cooldown("remocate", "half_freq", 11)
    # An actually-uninstrumented source SHOULD be reset.
    tmp_db.upsert_source_cooldown("hackernews", "half_freq", 11)

    tmp_db.reset_uninstrumented_source_cooldowns(
        set(search_jobs._P2_INSTRUMENTED_SOURCES),
    )

    assert tmp_db.get_source_cooldown("devex")["state"] == "half_freq"
    assert tmp_db.get_source_cooldown("remocate")["state"] == "half_freq"
    assert tmp_db.get_source_cooldown("hackernews")["state"] == "normal"


# --------------------------------------------------------------------------- #
# (E) fetch_all integration — the override actually reaches the adapter      #
# --------------------------------------------------------------------------- #


class _FakeCuratedModule:
    """Stand-in for sources.curated_boards: records the `sources` map it was
    handed so we can assert the dispatcher narrowed it to the survivors."""

    def __init__(self):
        self.calls: list[dict] = []

    def fetch(self, filters, *, db=None):  # noqa: ANN001 — stub
        srcs = dict(filters.get("sources") or {})
        self.calls.append(srcs)
        return []


def _run_fetch_all_with_only_curated(monkeypatch, tmp_db, *, enabled,
                                     cycle_index, workers):
    """Drive fetch_all against a SOURCES registry containing ONLY the
    curated module so the test isolates the per-sub-board dispatch."""
    fake = _FakeCuratedModule()
    monkeypatch.setattr(search_jobs, "SOURCES", {"curated_boards": fake})
    filters = {
        "sources": dict(enabled),
        "ai_source_workers": workers,
        "source_low_novelty_threshold": 0.05,
        # Disable the periodic cursor reset so it doesn't interfere.
        "cursor_reset_every_n_cycles": 0,
    }
    jobs, errors = search_jobs.fetch_all(
        filters, db=tmp_db, cycle_index=cycle_index,
    )
    return fake, jobs, errors


@pytest.mark.parametrize("workers", [1, 4])
def test_fetch_all_narrows_curated_sources_to_survivors(tmp_db, monkeypatch, workers):
    """A demoted curated board is dropped from the `sources` map the module
    receives; the healthy sibling stays enabled. Verified on both the
    serial (workers=1) and threaded (workers=4) dispatch paths."""
    enabled = {"remocate": True, "wantapply": True, "remoterocketship": False}
    # remocate demoted + quiet → cooled on even cycle 0.
    tmp_db.upsert_source_cooldown("remocate", "half_freq", 5)
    tmp_db.record_fetch("remocate", "remocate", 1, "", jobs_seen=100, jobs_new=1)
    # wantapply healthy → survives.
    tmp_db.record_fetch("wantapply", "wantapply", 1, "", jobs_seen=10, jobs_new=5)

    fake, _jobs, _errs = _run_fetch_all_with_only_curated(
        monkeypatch, tmp_db, enabled=enabled, cycle_index=0, workers=workers,
    )

    assert len(fake.calls) == 1, "curated module should be dispatched once"
    handed = fake.calls[0]
    # The override flips the demoted board OFF, keeps the survivor ON.
    assert handed["remocate"] is False
    assert handed["wantapply"] is True
    # Disabled-from-the-start board is untouched (still False).
    assert handed["remoterocketship"] is False


def test_fetch_all_skips_curated_when_all_cooled(tmp_db, monkeypatch):
    """When every enabled curated board is demoted + on the OFF half, the
    module is not dispatched at all this cycle."""
    enabled = {"remocate": True, "wantapply": True, "remoterocketship": False}
    for b in ("remocate", "wantapply"):
        tmp_db.upsert_source_cooldown(b, "half_freq", 5)
        tmp_db.record_fetch(b, b, 1, "", jobs_seen=100, jobs_new=1)

    fake, _jobs, _errs = _run_fetch_all_with_only_curated(
        monkeypatch, tmp_db, enabled=enabled, cycle_index=0, workers=1,
    )
    assert fake.calls == [], "fully-cooled curated module must be skipped"


def test_fetch_all_passes_full_sources_when_none_cooled(tmp_db, monkeypatch):
    """No demotions → no override is built and the module sees the original
    enabled map (the common, zero-overhead path)."""
    enabled = {"remocate": True, "wantapply": True, "remoterocketship": True}
    for b in ("remocate", "wantapply", "remoterocketship"):
        tmp_db.record_fetch(b, b, 1, "", jobs_seen=10, jobs_new=5)  # healthy

    fake, _jobs, _errs = _run_fetch_all_with_only_curated(
        monkeypatch, tmp_db, enabled=enabled, cycle_index=0, workers=1,
    )
    assert len(fake.calls) == 1
    handed = fake.calls[0]
    assert handed["remocate"] is True
    assert handed["wantapply"] is True
    assert handed["remoterocketship"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
