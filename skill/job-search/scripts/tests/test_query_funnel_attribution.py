"""M2 — per-query funnel attribution plumbing (search_jobs + adapters).

Tests the side-channel attribution dict that the per-user source adapters
populate, and the `_record_query_funnel` roll-up that turns it into
`query_runs` rows. Uses temp DBs + a fake adapter (no network, no LLM).
"""
from __future__ import annotations

import time

import pytest

import db as dbm
from telemetry.store import MonitorStore
import search_jobs
from dedupe import Job


@pytest.fixture
def store(tmp_path):
    db = dbm.DB(tmp_path / "jobs.db")
    return MonitorStore(db), db


def _job(source, ext):
    return Job(source=source, external_id=ext, title="t", company="c",
               location="Remote", url=f"https://x/{ext}", posted_at="2026-06-01")


def test_record_query_funnel_rolls_up_stages(store):
    s, db = store
    j1, j2, j3 = _job("linkedin", "a"), _job("linkedin", "b"), _job("web_search", "c")
    # j1: scored 5, matched, queued, sent. j2: scored 2, not matched.
    # j3 (web_search): scored 4, matched, queued, not sent.
    attribution = {
        j1.job_id: "react developer",
        j2.job_id: "react developer",
        j3.job_id: "web_search:react remote eu",
    }
    enrich = {
        j1.job_id: {"match_score": 5},
        j2.job_id: {"match_score": 2},
        j3.job_id: {"match_score": 4},
    }
    now = time.time()
    search_jobs._record_query_funnel(
        s, pipeline_run_id=1, chat_id=55, attribution=attribution,
        enrichments_by_job_id=enrich, match_floor=4,
        queued_job_ids={j1.job_id, j3.job_id},
        sent_job_ids={j1.job_id},
        started_at=now - 1, finished_at=now,
    )
    rows = s.query_yield_window(55, since_ts=now - 86400, half_life_s=0, now=now)
    by_q = {r["query"]: r for r in rows}

    li = by_q["react developer"]
    assert li["source_key"] == "linkedin"
    assert li["raw_fetched"] == 2     # j1 + j2
    assert li["raw_scored"] == 2      # both had enrichments
    assert li["raw_matched_ge4"] == 1  # only j1 >= 4
    assert li["raw_sent"] == 1        # only j1 sent

    ws = by_q["web_search:react remote eu"]
    assert ws["source_key"] == "web_search"
    assert ws["raw_matched_ge4"] == 1
    assert ws["raw_sent"] == 0        # queued but not sent — cold-start signal


def test_record_query_funnel_empty_attribution_noop(store):
    s, db = store
    now = time.time()
    # Must not raise and must not write rows.
    search_jobs._record_query_funnel(
        s, pipeline_run_id=1, chat_id=1, attribution={},
        enrichments_by_job_id={}, match_floor=4,
        queued_job_ids=set(), sent_job_ids=set(),
        started_at=now, finished_at=now,
    )
    rows = s.query_yield_window(1, since_ts=0, half_life_s=0, now=now)
    assert rows == []


def test_linkedin_attribution_side_channel():
    """`fetch_for_user` populates the attribution map without changing its
    return contract — exercised via the legacy paired-shape branch with a
    monkeypatched `_one_search` so no network is hit."""
    from sources import linkedin

    calls = {}

    def fake_one_search(*, q, geo, f_TPR, remote, cap_remaining, filters,
                        seen_urls, start):
        # Return one fresh job per (q, start) cell on page 1 only. Distinct
        # title+company per query so the cross-geo dedupe (keys on
        # company+title) doesn't collapse the two arms.
        if start != 0:
            return [], True
        ext = f"{q}-{start}"
        jb = Job(source="linkedin", external_id=ext, title=q, company=q,
                 location="Remote", url=f"https://x/{ext}", posted_at="2026-06-01")
        seen_urls.add(jb.url)
        return [jb], True

    orig = linkedin._one_search
    orig_bodies = linkedin._fetch_detail_bodies
    linkedin._one_search = fake_one_search
    linkedin._fetch_detail_bodies = lambda jobs: jobs   # no network
    try:
        attribution: dict = {}
        seeds = {"queries": [
            {"q": "react developer", "geo": "Spain", "f_TPR": "r86400"},
            {"q": "vue developer", "geo": "Spain", "f_TPR": "r86400"},
        ]}
        out = linkedin.fetch_for_user(
            {"remote": "", "max_per_source": 10}, seeds,
            db=None, attribution=attribution,
        )
    finally:
        linkedin._one_search = orig
        linkedin._fetch_detail_bodies = orig_bodies

    assert len(out) == 2
    # Each returned job is attributed to its originating query.
    queries = set(attribution.values())
    assert queries == {"react developer", "vue developer"}
    for j in out:
        assert j.job_id in attribution


def test_web_search_query_key_helper():
    from sources.web_search import _web_search_query_key
    assert _web_search_query_key(
        {"seed_phrases": ["react remote", "frontend eu"]}, None,
    ) == "web_search:react remote | frontend eu"
    assert _web_search_query_key(
        {"focus_notes": "remote EU frontend"}, None,
    ) == "web_search:remote EU frontend"
    assert _web_search_query_key({}, "my prefs text").startswith("web_search:my prefs")
    assert _web_search_query_key(None, None) == "web_search:default"
