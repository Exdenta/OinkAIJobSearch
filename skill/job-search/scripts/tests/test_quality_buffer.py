"""Quality-buffer queue tests (algorithm v2.6, P1 pipeline overhaul).

Covers both the raw `DB` methods (enqueue / depth / fetch / clear /
purge_stale) and the `_decide_buffer_flush` helper that wires them
together for `search_jobs.run`.

All tests run on a per-test SQLite file via the `tmp_db` fixture
(same pattern as `test_job_scores_cache.py`).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from dedupe import Job  # noqa: E402
import db as db_module  # noqa: E402
import search_jobs  # noqa: E402


def _make_job(source: str, ext_id: str,
              title: str = "Engineer",
              company: str = "Acme") -> Job:
    return Job(
        source=source,
        external_id=ext_id,
        title=title,
        company=company,
        location="Remote",
        url=f"https://example.com/{source}/{ext_id}",
        posted_at="2026-05-20",
        snippet=f"snippet for {ext_id}",
    )


@pytest.fixture
def tmp_db(tmp_path):
    return db_module.DB(tmp_path / "test.db")


# ----------------------------- raw DB helpers ----------------------------- #


def test_enqueue_dedupes_silently(tmp_db):
    """Re-enqueueing the same (chat, job) must NOT bump queued_at or
    insert a second row."""
    phash = "p1"
    tmp_db.enqueue_match(1, "job_a", phash, 4)
    first = tmp_db.fetch_queue(1, phash)
    assert len(first) == 1
    first_queued_at = first[0]["queued_at"]

    # Ensure a measurable gap before the second call so a stray UPSERT
    # would visibly bump queued_at.
    time.sleep(0.02)
    tmp_db.enqueue_match(1, "job_a", phash, 5)

    rows = tmp_db.fetch_queue(1, phash)
    assert len(rows) == 1
    # INSERT OR IGNORE: original score (4) survives, queued_at unchanged.
    assert rows[0]["match_score"] == 4
    assert rows[0]["queued_at"] == pytest.approx(first_queued_at, abs=1e-6)


def test_queue_depth_filters_profile_hash(tmp_db):
    """queue_depth ignores rows whose profile_hash != arg."""
    tmp_db.enqueue_match(1, "j1", "old_hash", 4)
    tmp_db.enqueue_match(1, "j2", "current", 5)
    tmp_db.enqueue_match(1, "j3", "current", 4)

    assert tmp_db.queue_depth(1, "current") == 2
    assert tmp_db.queue_depth(1, "old_hash") == 1
    assert tmp_db.queue_depth(1, "missing") == 0
    # Wrong chat sees nothing.
    assert tmp_db.queue_depth(2, "current") == 0


def test_purge_stale_drops_old_profile_entries(tmp_db):
    """purge_stale_queue removes != current rows AND leaves matching
    rows intact."""
    tmp_db.enqueue_match(1, "j_stale_a", "old_a", 4)
    tmp_db.enqueue_match(1, "j_stale_b", "old_b", 5)
    tmp_db.enqueue_match(1, "j_live",   "current", 4)

    removed = tmp_db.purge_stale_queue(1, "current")
    assert removed == 2

    remaining = tmp_db.fetch_queue(1, "current")
    assert [r["job_id"] for r in remaining] == ["j_live"]

    # Stale hashes are empty post-purge.
    assert tmp_db.queue_depth(1, "old_a") == 0
    assert tmp_db.queue_depth(1, "old_b") == 0

    # Passing an empty profile_hash drops every row for the user.
    tmp_db.enqueue_match(1, "j_extra", "current", 5)
    removed_all = tmp_db.purge_stale_queue(1, "")
    assert removed_all == 2  # j_live + j_extra


def test_clear_queue_removes_only_specified(tmp_db):
    """clear_queue must spare rows it wasn't asked to drop, and must
    not touch other users' rows."""
    tmp_db.enqueue_match(1, "j_keep", "p1", 4)
    tmp_db.enqueue_match(1, "j_drop", "p1", 5)
    tmp_db.enqueue_match(2, "j_keep", "p1", 4)  # different user

    removed = tmp_db.clear_queue(1, ["j_drop"])
    assert removed == 1

    survivors_1 = {r["job_id"] for r in tmp_db.fetch_queue(1, "p1")}
    survivors_2 = {r["job_id"] for r in tmp_db.fetch_queue(2, "p1")}
    assert survivors_1 == {"j_keep"}
    assert survivors_2 == {"j_keep"}


def test_oldest_age_seconds_returns_none_when_empty(tmp_db):
    assert tmp_db.queue_oldest_age_seconds(1, "p") is None
    tmp_db.enqueue_match(1, "j", "p", 4)
    age = tmp_db.queue_oldest_age_seconds(1, "p")
    assert age is not None and age >= 0.0


# ----------------------------- decision helper ---------------------------- #


def _save_job(tmp_db, job: Job) -> None:
    """Mirror the orchestrator's save path so get_jobs_by_ids can rehydrate."""
    tmp_db.upsert_job(job.as_db_dict())


def _filters(threshold: int = 5, max_hours: float = 48.0) -> dict:
    return {
        "quality_send_threshold": threshold,
        "max_queue_latency_hours": max_hours,
    }


def test_threshold_flush(tmp_db):
    """Fewer than `threshold` enqueues = hold; reaching `threshold`
    flushes the entire buffer."""
    phash = db_module.profile_hash("resume", "prefs")
    jobs = [_make_job("linkedin", f"j{n}") for n in range(5)]
    for j in jobs:
        _save_job(tmp_db, j)

    enrichments = {
        j.job_id: {"match_score": 4, "why_match": "", "why_mismatch": "",
                   "key_details": {}}
        for j in jobs
    }

    filters = _filters(threshold=5)

    # 4 alive_floor enqueues → hold.
    out_jobs, flush, depth, _age = search_jobs._decide_buffer_flush(
        tmp_db, 42, jobs[:4], enrichments, phash, filters,
    )
    assert flush is False
    assert out_jobs == []
    assert depth == 4

    # 5th enqueue trips the threshold; flush returns ALL 5 jobs.
    out_jobs, flush, depth, _age = search_jobs._decide_buffer_flush(
        tmp_db, 42, [jobs[4]], enrichments, phash, filters,
    )
    assert flush is True
    assert depth == 5
    assert {j.job_id for j in out_jobs} == {j.job_id for j in jobs}


def test_age_flush_fires_when_oldest_over_threshold(tmp_db):
    """A single row with queued_at = now - 49h must trip the flush even
    though depth (1) is below the threshold (5)."""
    phash = db_module.profile_hash("resume", "prefs")
    job = _make_job("linkedin", "j_ancient")
    _save_job(tmp_db, job)
    enrichments = {
        job.job_id: {"match_score": 4, "why_match": "", "why_mismatch": "",
                     "key_details": {}}
    }

    # Pre-seed the queue with a 49h-old row.
    tmp_db.enqueue_match(42, job.job_id, phash, 4)
    with tmp_db._conn() as c:
        c.execute(
            "UPDATE queued_matches SET queued_at = ? WHERE job_id = ?",
            (time.time() - 49 * 3600, job.job_id),
        )

    out_jobs, flush, depth, age = search_jobs._decide_buffer_flush(
        tmp_db, 42, [], {}, phash, _filters(threshold=5, max_hours=48.0),
    )
    assert flush is True
    assert depth == 1
    assert age >= 48 * 3600
    assert len(out_jobs) == 1 and out_jobs[0].job_id == job.job_id


def test_profile_hash_change_silently_drops(tmp_db):
    """An edit to resume / prefs (new profile_hash) must invalidate the
    queue: stale rows are purged, depth resets to 0, no flush."""
    job = _make_job("linkedin", "j1")
    _save_job(tmp_db, job)

    # Seed under the old hash.
    tmp_db.enqueue_match(42, job.job_id, "old_hash", 4)
    assert tmp_db.queue_depth(42, "old_hash") == 1

    new_hash = db_module.profile_hash("resume v2", "prefs v2")
    out_jobs, flush, depth, _age = search_jobs._decide_buffer_flush(
        tmp_db, 42, [], {}, new_hash, _filters(),
    )
    assert flush is False
    assert out_jobs == []
    assert depth == 0
    # Stale row is gone for good.
    assert tmp_db.queue_depth(42, "old_hash") == 0


def test_score_below_4_is_not_enqueued(tmp_db):
    """Score-floor gating happens upstream: _decide_buffer_flush only
    sees alive_floor. Anything below the floor never reaches the queue.

    Re-asserts the invariant by passing an empty alive_floor (the
    upstream gate dropped everything) and confirming the queue does
    not grow."""
    phash = db_module.profile_hash("resume", "prefs")

    # alive_floor is empty (every candidate scored < 4 upstream).
    out_jobs, flush, depth, _age = search_jobs._decide_buffer_flush(
        tmp_db, 42, [], {}, phash, _filters(threshold=5),
    )
    assert flush is False
    assert out_jobs == []
    assert depth == 0

    # And the table is genuinely empty — no defensive write happened.
    with tmp_db._conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM queued_matches").fetchone()
        assert row["n"] == 0
