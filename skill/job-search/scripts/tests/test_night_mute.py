"""Night-mute quality-buffer tests (algorithm v2.6, P7).

Covers both the standalone ``_is_in_night_mute_window`` helper (driven
with frozen ``now`` datetimes against real ``zoneinfo`` data) and the
integration with ``_decide_buffer_flush`` (driven with the helper
monkeypatched to a fixed bool so the flush gate is tested in isolation
from wall-clock).

Mirrors the fixtures/style in ``test_quality_buffer.py``.
"""
from __future__ import annotations

import datetime
import logging
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from dedupe import Job  # noqa: E402
import db as db_module  # noqa: E402
import search_jobs  # noqa: E402


# --------------------------- shared helpers ------------------------------- #


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
        posted_at="2026-05-23",
        snippet=f"snippet for {ext_id}",
    )


def _save_job(tmp_db, job: Job) -> None:
    tmp_db.upsert_job(job.as_db_dict())


def _filters(
    threshold: int = 5,
    max_hours: float = 48.0,
    *,
    night_start: int = 23,
    night_end: int = 9,
    tz: str = "Europe/Madrid",
) -> dict:
    return {
        "quality_send_threshold": threshold,
        "max_queue_latency_hours": max_hours,
        "night_mute_tz": tz,
        "night_mute_start_hour": night_start,
        "night_mute_end_hour": night_end,
    }


def _madrid(year: int, month: int, day: int, hour: int,
            minute: int = 0) -> datetime.datetime:
    """Build a tz-aware Madrid wall-clock datetime."""
    return datetime.datetime(
        year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Madrid"),
    )


@pytest.fixture
def tmp_db(tmp_path):
    return db_module.DB(tmp_path / "test_night_mute.db")


# --------------------- _is_in_night_mute_window unit tests ---------------- #


def test_helper_returns_false_outside_window():
    """12:00 Madrid is clearly outside the default 23-09 window."""
    now = _madrid(2026, 5, 23, 12, 0)
    assert search_jobs._is_in_night_mute_window(now) is False


def test_helper_returns_true_inside_window_evening():
    """23:30 Madrid is inside the default 23-09 window (evening side)."""
    now = _madrid(2026, 5, 23, 23, 30)
    assert search_jobs._is_in_night_mute_window(now) is True


def test_helper_returns_true_inside_window_morning():
    """03:00 Madrid is inside the default 23-09 window (morning side)."""
    now = _madrid(2026, 5, 24, 3, 0)
    assert search_jobs._is_in_night_mute_window(now) is True


def test_helper_returns_false_at_exact_end_hour():
    """09:00 Madrid sharp is NOT muted (end_hour is exclusive)."""
    now = _madrid(2026, 5, 24, 9, 0)
    assert search_jobs._is_in_night_mute_window(now) is False


def test_helper_returns_true_at_exact_start_hour():
    """23:00 Madrid sharp IS muted (start_hour is inclusive)."""
    now = _madrid(2026, 5, 23, 23, 0)
    assert search_jobs._is_in_night_mute_window(now) is True


def test_helper_disabled_when_start_equals_end():
    """start_hour == end_hour disables the feature for every wall-clock."""
    for hour in (0, 6, 12, 18, 23):
        now = _madrid(2026, 5, 23, hour, 0)
        assert search_jobs._is_in_night_mute_window(
            now, start_hour=0, end_hour=0,
        ) is False
        assert search_jobs._is_in_night_mute_window(
            now, start_hour=9, end_hour=9,
        ) is False


def test_helper_non_wrapping_window():
    """start < end is a same-day window. 10-14 mutes 10:00-13:59,
    NOT 09:00 or 15:00."""
    inside = _madrid(2026, 5, 23, 12, 0)
    before = _madrid(2026, 5, 23, 9, 0)
    after = _madrid(2026, 5, 23, 15, 0)
    boundary_start = _madrid(2026, 5, 23, 10, 0)
    boundary_end = _madrid(2026, 5, 23, 14, 0)

    assert search_jobs._is_in_night_mute_window(
        inside, start_hour=10, end_hour=14,
    ) is True
    assert search_jobs._is_in_night_mute_window(
        before, start_hour=10, end_hour=14,
    ) is False
    assert search_jobs._is_in_night_mute_window(
        after, start_hour=10, end_hour=14,
    ) is False
    # Start inclusive, end exclusive — same semantics as the wrap-around case.
    assert search_jobs._is_in_night_mute_window(
        boundary_start, start_hour=10, end_hour=14,
    ) is True
    assert search_jobs._is_in_night_mute_window(
        boundary_end, start_hour=10, end_hour=14,
    ) is False


def test_helper_unknown_tz_returns_false(caplog):
    """Bad tz name must fail-open (return False) and log a WARNING.
    A typo in defaults.py must never permanently muzzle the user."""
    now = _madrid(2026, 5, 23, 2, 0)  # would normally be muted
    with caplog.at_level(logging.WARNING, logger="job-search"):
        muted = search_jobs._is_in_night_mute_window(now, tz_name="Mars/Olympus")
    assert muted is False
    assert any(
        "Mars/Olympus" in rec.message and rec.levelno >= logging.WARNING
        for rec in caplog.records
    ), f"expected WARNING log mentioning the bad tz, got: {caplog.records}"


# ----------------- _decide_buffer_flush integration tests ----------------- #


def test_decide_flush_normal_during_day(tmp_db, monkeypatch):
    """Depth=5 + night-mute returns False (daytime) ⇒ flush fires normally."""
    monkeypatch.setattr(
        search_jobs, "_is_in_night_mute_window", lambda **kw: False,
    )
    phash = db_module.profile_hash("resume", "prefs")
    jobs = [_make_job("linkedin", f"day_j{n}") for n in range(5)]
    for j in jobs:
        _save_job(tmp_db, j)
    enrichments = {
        j.job_id: {"match_score": 4, "why_match": "", "why_mismatch": "",
                   "key_details": {}}
        for j in jobs
    }
    filters = _filters(threshold=5)

    out_jobs, flush_now, depth, _age = search_jobs._decide_buffer_flush(
        tmp_db, 100, jobs, enrichments, phash, filters,
    )

    assert flush_now is True
    assert depth == 5
    assert {j.job_id for j in out_jobs} == {j.job_id for j in jobs}


def test_decide_flush_held_at_night_threshold_met(tmp_db, monkeypatch, caplog):
    """Depth=5 + night-mute True ⇒ flush_now=False, queue NOT cleared,
    log mentions 'night-mute' / 'holding until morning'."""
    monkeypatch.setattr(
        search_jobs, "_is_in_night_mute_window", lambda **kw: True,
    )
    phash = db_module.profile_hash("resume", "prefs")
    chat_id = 101
    jobs = [_make_job("linkedin", f"night_j{n}") for n in range(5)]
    for j in jobs:
        _save_job(tmp_db, j)
    enrichments = {
        j.job_id: {"match_score": 4, "why_match": "", "why_mismatch": "",
                   "key_details": {}}
        for j in jobs
    }
    filters = _filters(threshold=5)

    with caplog.at_level(logging.INFO, logger="job-search"):
        out_jobs, flush_now, depth, _age = search_jobs._decide_buffer_flush(
            tmp_db, chat_id, jobs, enrichments, phash, filters,
        )

    assert flush_now is False
    assert out_jobs == []
    # depth is still reported (pre-leak-purge) — the decision used this value.
    assert depth == 5
    # Queue rows must SURVIVE the hold — nothing got dropped just because
    # we muted. Next iteration (morning) flushes them.
    surviving = {r["job_id"] for r in tmp_db.fetch_queue(chat_id, phash)}
    assert surviving == {j.job_id for j in jobs}
    # Log evidence.
    assert any(
        "night-mute" in rec.message and "holding until morning" in rec.message
        for rec in caplog.records
    ), f"expected night-mute hold log line, got: {caplog.records}"


def test_decide_flush_held_at_night_age_met(tmp_db, monkeypatch):
    """Age-based flush (depth=1, queued_at=now-49h) is ALSO muted at night.
    Confirms the gate covers BOTH branches, not just the threshold one."""
    monkeypatch.setattr(
        search_jobs, "_is_in_night_mute_window", lambda **kw: True,
    )
    phash = db_module.profile_hash("resume", "prefs")
    chat_id = 102
    job = _make_job("linkedin", "ancient_at_night")
    _save_job(tmp_db, job)

    tmp_db.enqueue_match(chat_id, job.job_id, phash, 4)
    with tmp_db._conn() as c:
        c.execute(
            "UPDATE queued_matches SET queued_at = ? WHERE job_id = ?",
            (time.time() - 49 * 3600, job.job_id),
        )

    out_jobs, flush_now, depth, age = search_jobs._decide_buffer_flush(
        tmp_db, chat_id, [], {}, phash, _filters(threshold=5, max_hours=48.0),
    )

    assert flush_now is False
    assert out_jobs == []
    assert depth == 1
    assert age >= 48 * 3600  # the row IS old enough to flush — only night-mute holds it
    # Row still in queue, will flush next morning.
    surviving = {r["job_id"] for r in tmp_db.fetch_queue(chat_id, phash)}
    assert surviving == {job.job_id}


def test_decide_flush_resumes_at_window_end(tmp_db, monkeypatch):
    """At exactly 09:00 Madrid (end_hour exclusive), the helper returns
    False ⇒ a depth-5 buffer flushes immediately. Drives the REAL
    helper (no monkeypatch) with a frozen ``datetime.now`` so we
    exercise the helper + gate together."""
    # Freeze the helper's now() to 09:00 Madrid by wrapping the REAL
    # helper (captured BEFORE monkeypatch swaps the module attribute,
    # so the inner call doesn't recurse into the wrapper) with a frozen
    # `now`. This exercises BOTH the gate-calls-helper wiring AND the
    # exact-end-hour boundary (09:00 ⇒ helper False ⇒ flush fires).
    frozen_now = _madrid(2026, 5, 24, 9, 0).astimezone(datetime.timezone.utc)
    real_helper = search_jobs._is_in_night_mute_window

    def _wrapped(*, tz_name="Europe/Madrid", start_hour=23, end_hour=9):
        return real_helper(
            frozen_now, tz_name=tz_name,
            start_hour=start_hour, end_hour=end_hour,
        )

    monkeypatch.setattr(search_jobs, "_is_in_night_mute_window", _wrapped)

    phash = db_module.profile_hash("resume", "prefs")
    jobs = [_make_job("linkedin", f"morning_j{n}") for n in range(5)]
    for j in jobs:
        _save_job(tmp_db, j)
    enrichments = {
        j.job_id: {"match_score": 4, "why_match": "", "why_mismatch": "",
                   "key_details": {}}
        for j in jobs
    }

    out_jobs, flush_now, depth, _age = search_jobs._decide_buffer_flush(
        tmp_db, 103, jobs, enrichments, phash, _filters(threshold=5),
    )

    assert flush_now is True
    assert depth == 5
    assert {j.job_id for j in out_jobs} == {j.job_id for j in jobs}


def test_night_mute_doesnt_affect_enqueue(tmp_db, monkeypatch):
    """Night-mute is a SEND-decision gate only. Enqueue must still run
    (so the morning flush has a populated queue) and no exception is
    raised by routing through the muted code path."""
    monkeypatch.setattr(
        search_jobs, "_is_in_night_mute_window", lambda **kw: True,
    )
    phash = db_module.profile_hash("resume", "prefs")
    chat_id = 104

    # Queue starts empty.
    assert tmp_db.queue_depth(chat_id, phash) == 0

    jobs = [_make_job("linkedin", f"enq_j{n}") for n in range(3)]
    for j in jobs:
        _save_job(tmp_db, j)
    enrichments = {
        j.job_id: {"match_score": 5, "why_match": "", "why_mismatch": "",
                   "key_details": {}}
        for j in jobs
    }

    out_jobs, flush_now, depth, _age = search_jobs._decide_buffer_flush(
        tmp_db, chat_id, jobs, enrichments, phash, _filters(threshold=5),
    )

    # depth(3) < threshold(5) AND night-mute active ⇒ no flush. But the
    # 3 enqueues DID land — the queue has them ready for the morning.
    assert flush_now is False
    assert out_jobs == []
    assert depth == 3
    surviving = {r["job_id"] for r in tmp_db.fetch_queue(chat_id, phash)}
    assert surviving == {j.job_id for j in jobs}
