"""Persistent per-user score cache tests.

Exercises `db.job_scores` + `enrich_jobs_ai`'s cache wiring end-to-end:

  * fresh DB → first enrichment call goes to the fake scorer for every
    job, and writes one row per job into job_scores.
  * second call with the same inputs hits the cache for every job and
    never invokes the scorer at all.
  * editing prefs_text flips the profile_hash and forces a fresh score.
  * partial cache hits scope the model call to only the unscored jobs.
  * per-user isolation: chat_id A's rows aren't visible to chat_id B.
  * purge_job_scores_for_user removes only the target user's rows.

All tests run on :memory: SQLite so they're hermetic and fast.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from dedupe import Job  # noqa: E402
import db as db_module  # noqa: E402
import job_enrich  # noqa: E402


def _make_job(source: str, ext_id: str, title: str = "Engineer",
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
    """Per-test DB on a tmp_path-rooted SQLite file (not literally
    :memory: because DB._conn opens a fresh connection per call —
    :memory: would lose state between methods)."""
    return db_module.DB(tmp_path / "test.db")


@pytest.fixture
def fake_scorer(monkeypatch):
    """Replace `job_enrich._enrich_pool` with a deterministic fake.

    Records every (model_label, scored_jobs) call into `.calls` so
    tests can assert how many jobs the real model would have been
    asked to score. Returns a fresh {external_id: enrichment} dict
    per call; match_score is derived from a hash of external_id so
    successive runs against the same job produce the same verdict.
    """
    calls: list[dict] = []

    def _fake(jobs, resume_text, prefs_text, timeout_s, max_jobs_per_call,
              *, model, pass_label, workers=4):
        calls.append({
            "model": model, "pass_label": pass_label,
            "ext_ids": [j.external_id for j in jobs],
            "n": len(jobs),
        })
        out: dict[str, dict] = {}
        for j in jobs:
            # Deterministic score derived from external_id so re-runs
            # are stable. 0..5 inclusive.
            score = (sum(ord(c) for c in j.external_id) % 5) + 1
            out[j.external_id] = {
                "match_score": score,
                "why_match": f"matches {j.title}",
                "why_mismatch": "",
                "key_details": {"stack": "python", "remote_policy": "remote"},
            }
        return out

    monkeypatch.setattr(job_enrich, "_enrich_pool", _fake)
    return calls


def _run(jobs, db, chat_id, prefs_text="prefs"):
    return job_enrich.enrich_jobs_ai(
        jobs,
        resume_text="resume body text",
        prefs_text=prefs_text,
        db=db,
        chat_id=chat_id,
    )


def test_cache_miss_writes_then_hit_reads(tmp_db, fake_scorer):
    jobs = [_make_job("linkedin", f"j{n}") for n in range(3)]

    # First call: cold cache, fake scorer fires once across the 3 jobs.
    out1 = _run(jobs, tmp_db, chat_id=42)
    assert set(out1.keys()) == {j.external_id for j in jobs}
    assert sum(c["n"] for c in fake_scorer) == 3
    n_calls_first = len(fake_scorer)
    assert n_calls_first >= 1

    # Second call: every job should be cached → scorer must NOT be invoked.
    out2 = _run(jobs, tmp_db, chat_id=42)
    assert set(out2.keys()) == {j.external_id for j in jobs}
    # No new entries in `calls` after the second run.
    assert len(fake_scorer) == n_calls_first, (
        f"scorer was invoked on a full cache hit: {fake_scorer[n_calls_first:]}"
    )

    # Verdicts must match what the first call produced.
    for j in jobs:
        assert out2[j.external_id]["match_score"] == out1[j.external_id]["match_score"]
        assert out2[j.external_id]["why_match"] == out1[j.external_id]["why_match"]


def test_profile_hash_change_invalidates(tmp_db, fake_scorer):
    jobs = [_make_job("indeed", "j1")]

    _run(jobs, tmp_db, chat_id=7, prefs_text="loves python")
    n_after_first = len(fake_scorer)
    assert n_after_first == 1

    # Same chat_id, same job, DIFFERENT prefs → new profile_hash → miss.
    out = _run(jobs, tmp_db, chat_id=7, prefs_text="loves rust")
    assert "j1" in out
    assert len(fake_scorer) == 2, (
        "scorer should have been invoked again after prefs edit; calls=%r"
        % fake_scorer
    )


def test_partial_cache_hit(tmp_db, fake_scorer):
    jobs = [_make_job("wellfound", f"j{n}") for n in range(3)]

    # Pre-warm the cache with the first 2 jobs only by upserting directly.
    phash = db_module.profile_hash("resume body text", "prefs")
    seed = {
        jobs[0].job_id: {
            "match_score": 4, "why_match": "seed", "why_mismatch": "",
            "key_details": {}, "model": "sonnet",
        },
        jobs[1].job_id: {
            "match_score": 2, "why_match": "seed", "why_mismatch": "",
            "key_details": {}, "model": "sonnet",
        },
    }
    written = tmp_db.upsert_scores(99, phash, seed, model="sonnet")
    assert written == 2

    out = _run(jobs, tmp_db, chat_id=99)
    assert set(out.keys()) == {j.external_id for j in jobs}

    # The fake scorer should have been called for ONLY the 1 uncached job.
    total_scored = sum(c["n"] for c in fake_scorer)
    assert total_scored == 1, (
        f"expected scorer to see 1 job, saw {total_scored}: {fake_scorer}"
    )
    # That one job should be the third one (j2).
    seen_ids = {eid for c in fake_scorer for eid in c["ext_ids"]}
    assert seen_ids == {jobs[2].external_id}

    # Pre-seeded entries flow through unchanged.
    assert out[jobs[0].external_id]["match_score"] == 4
    assert out[jobs[1].external_id]["match_score"] == 2


def test_per_user_isolation(tmp_db, fake_scorer):
    job = _make_job("linkedin", "shared")

    # Cache for chat A.
    _run([job], tmp_db, chat_id=1)
    assert sum(c["n"] for c in fake_scorer) == 1

    # Same job for chat B → cache must NOT hit; scorer must re-run.
    _run([job], tmp_db, chat_id=2)
    assert sum(c["n"] for c in fake_scorer) == 2, (
        "chat 2 should have missed chat 1's cache entry"
    )

    # And chat A still hits its own cache (no extra calls).
    n_before = len(fake_scorer)
    _run([job], tmp_db, chat_id=1)
    assert len(fake_scorer) == n_before, "chat 1 should still hit cache"


def test_purge_user(tmp_db, fake_scorer):
    job_a = _make_job("a", "ja")
    job_b = _make_job("b", "jb")

    _run([job_a], tmp_db, chat_id=10)
    _run([job_b], tmp_db, chat_id=20)
    assert sum(c["n"] for c in fake_scorer) == 2

    removed = tmp_db.purge_job_scores_for_user(10)
    assert removed == 1

    # chat 20's row must survive.
    phash = db_module.profile_hash("resume body text", "prefs")
    survived = tmp_db.get_cached_scores(20, [job_b.job_id], phash)
    assert job_b.job_id in survived
    cleared = tmp_db.get_cached_scores(10, [job_a.job_id], phash)
    assert cleared == {}

    # Re-running chat 10 should miss → scorer fires again.
    n_before = len(fake_scorer)
    _run([job_a], tmp_db, chat_id=10)
    assert len(fake_scorer) == n_before + 1


def test_profile_hash_strips_whitespace(tmp_db, fake_scorer):
    """Trivial whitespace differences must NOT invalidate the cache."""
    job = _make_job("a", "j1")

    job_enrich.enrich_jobs_ai(
        [job], resume_text="resume body text", prefs_text="prefs",
        db=tmp_db, chat_id=33,
    )
    n_first = len(fake_scorer)

    # Pad both inputs with leading/trailing whitespace — hash must match.
    job_enrich.enrich_jobs_ai(
        [job], resume_text="  resume body text  \n",
        prefs_text="\n prefs   ",
        db=tmp_db, chat_id=33,
    )
    assert len(fake_scorer) == n_first, (
        "whitespace-only diff should hit cache; got new calls %r"
        % fake_scorer[n_first:]
    )


def test_no_cache_when_kwargs_absent(tmp_db, fake_scorer):
    """Calling enrich_jobs_ai without db/chat_id falls back to the
    legacy uncached path — table stays empty, scorer fires every time."""
    job = _make_job("a", "j1")

    job_enrich.enrich_jobs_ai([job], resume_text="r", prefs_text="p")
    job_enrich.enrich_jobs_ai([job], resume_text="r", prefs_text="p")

    assert len(fake_scorer) == 2

    # Nothing should have landed in the cache.
    phash = db_module.profile_hash("r", "p")
    assert tmp_db.get_cached_scores(1, [job.job_id], phash) == {}


def test_ttl_purge(tmp_db):
    """purge_job_scores_older_than removes only stale rows."""
    phash = db_module.profile_hash("r", "p")
    tmp_db.upsert_scores(
        1, phash,
        {"job_old": {"match_score": 3, "why_match": "", "why_mismatch": "",
                     "key_details": {}}},
        model="haiku",
    )

    # Backdate the row to 10 days ago.
    import time
    with tmp_db._conn() as c:
        c.execute(
            "UPDATE job_scores SET scored_at = ? WHERE job_id = 'job_old'",
            (time.time() - 10 * 86400,),
        )

    # Add a fresh row that must survive the sweep.
    tmp_db.upsert_scores(
        1, phash,
        {"job_new": {"match_score": 5, "why_match": "", "why_mismatch": "",
                     "key_details": {}}},
        model="haiku",
    )

    removed = tmp_db.purge_job_scores_older_than(7 * 86400)
    assert removed == 1

    survivors = tmp_db.get_cached_scores(1, ["job_old", "job_new"], phash)
    assert "job_new" in survivors
    assert "job_old" not in survivors
