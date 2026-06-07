#!/usr/bin/env python3
"""Tests for the HTTP-JSON ``sources/un_careers.py`` adapter (2026-06 rewrite).

The adapter was rewritten from a WebFetch Claude-delegation approach to a
DIRECT HTTP call against the UN careers public ``filteredV2`` API, with a
Chrome-agent fallback that is GATED OFF by default.

What these tests assert
-----------------------
1.  Happy path — ``safe_url.safe_request`` returns a sample API envelope:
    items are parsed and mapped to ``Job``s with the correct detail URL,
    title precedence (postingTitle > jobTitle), company (United Nations +
    dept), location (first dutyStation description), and the cap is honored.
2.  The Tier-4 ``db.record_fetch`` row is written when a ``db`` is supplied.
3.  Failure path — ``safe_request`` RAISES: the adapter attempts the Chrome
    fallback and, with the flag OFF (default), returns ``[]`` without raising.
4.  Zero-openings path — the API returns an empty list: the adapter falls
    through to the Chrome fallback (``[]`` when disabled).

Invoke directly or via pytest:

    python3 skill/job-search/scripts/tests/test_un_careers_api.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _sample_envelope() -> dict:
    """A minimal but representative ``filteredV2`` success envelope."""
    return {
        "status": 1,
        "message": "Success.",
        "data": {
            "count": 2,
            "list": [
                {
                    "jobId": 12345,
                    "jobTitle": "Programme Officer",
                    "postingTitle": "Programme Officer (Humanitarian)",
                    "jobDescription": "Lead the field response programme.",
                    "jobLevel": "P-4",
                    "dept": "UNHCR",
                    "dutyStation": [{"description": "HQ Amman"}],
                    "startDate": "2026-06-01T00:00:00Z",
                    "endDate": "2026-07-01T00:00:00Z",
                },
                {
                    # No postingTitle → falls back to jobTitle; no dept →
                    # company stays plain "United Nations"; no dutyStation.
                    "jobId": 67890,
                    "jobTitle": "Data Analyst",
                    "jobDescription": "Analyze operational data.",
                    "startDate": "2026-05-20T00:00:00Z",
                },
            ],
        },
    }


class _FakeResp:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class _FakeDB:
    """Captures the single record_fetch row + answers count_existing_jobs."""

    def __init__(self, existing: int = 0):
        self._existing = existing
        self.record_fetch_calls: list[dict[str, Any]] = []

    def count_existing_jobs(self, job_ids) -> int:
        return self._existing

    def record_fetch(self, source, query, page, location, *, jobs_seen, jobs_new):
        self.record_fetch_calls.append({
            "source": source, "query": query, "page": page,
            "location": location, "jobs_seen": jobs_seen, "jobs_new": jobs_new,
        })


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_api_parsing_and_mapping() -> None:
    from sources import un_careers
    import safe_url

    # First page returns two items; subsequent pages return empty so
    # pagination terminates.
    pages: list[list] = [_sample_envelope()["data"]["list"], []]
    seen_bodies: list[dict] = []

    def _fake_request(method, url, *, timeout, headers=None, json=None, **kw):
        assert method == "POST"
        assert url == un_careers.API_URL
        assert headers and headers.get("Origin") == "https://careers.un.org"
        seen_bodies.append(json)
        page = (json or {}).get("pagination", {}).get("page", 0)
        items = pages[page] if page < len(pages) else []
        return _FakeResp({"status": 1, "data": {"list": items, "count": 2}})

    db = _FakeDB(existing=0)
    original = safe_url.safe_request
    safe_url.safe_request = _fake_request  # type: ignore[assignment]
    try:
        jobs = un_careers.fetch({"max_per_source": 36}, db=db)
    finally:
        safe_url.safe_request = original

    assert len(jobs) == 2, f"expected 2 jobs, got {len(jobs)}"

    j0, j1 = jobs
    # Detail URL is built from jobId.
    assert j0.url == "https://careers.un.org/jobSearchDescription/12345?language=en"
    assert j0.external_id == j0.url
    assert j0.source == "un_careers"
    # postingTitle wins over jobTitle.
    assert j0.title == "Programme Officer (Humanitarian)"
    # Company = United Nations joined with dept.
    assert "United Nations" in j0.company and "UNHCR" in j0.company
    # Location = first dutyStation description.
    assert j0.location == "HQ Amman"
    assert j0.posted_at == "2026-06-01T00:00:00Z"
    assert "field response" in j0.snippet

    # Second item: no postingTitle → jobTitle; no dept → plain UN; no station.
    assert j1.title == "Data Analyst"
    assert j1.company == "United Nations"
    assert j1.location == ""
    assert j1.url == "https://careers.un.org/jobSearchDescription/67890?language=en"

    # Body shape sanity: empty filterConfig, newest-first.
    assert seen_bodies[0]["filterConfig"] == {}
    assert seen_bodies[0]["pagination"]["sortBy"] == "startDate"
    assert seen_bodies[0]["pagination"]["sortDirection"] == -1

    # Tier-4 record_fetch fired once with the fixed single cell.
    assert len(db.record_fetch_calls) == 1
    row = db.record_fetch_calls[0]
    assert row["source"] == "un_careers"
    assert row["query"] == "un_careers"
    assert row["page"] == 1
    assert row["location"] == ""
    assert row["jobs_seen"] == 2
    assert row["jobs_new"] == 2


def test_api_respects_cap() -> None:
    from sources import un_careers
    import safe_url

    # One page with two items, but cap=1 → only the first is kept and we stop.
    def _fake_request(method, url, *, timeout, headers=None, json=None, **kw):
        page = (json or {}).get("pagination", {}).get("page", 0)
        items = _sample_envelope()["data"]["list"] if page == 0 else []
        return _FakeResp({"status": 1, "data": {"list": items}})

    original = safe_url.safe_request
    safe_url.safe_request = _fake_request  # type: ignore[assignment]
    try:
        jobs = un_careers.fetch({"max_per_source": 1})
    finally:
        safe_url.safe_request = original

    assert len(jobs) == 1, f"cap=1 must yield 1 job, got {len(jobs)}"


# ---------------------------------------------------------------------------
# Failure path → Chrome fallback (disabled by default)
# ---------------------------------------------------------------------------

def test_api_raises_triggers_chrome_fallback_disabled_returns_empty() -> None:
    from sources import un_careers
    import safe_url

    fallback_calls: list[dict[str, Any]] = []

    def _raise(method, url, *, timeout, headers=None, json=None, **kw):
        raise RuntimeError("network down")

    def _fake_fallback(*, url, instruction, max_items):
        # Mirror the real helper's GATED-OFF contract: return [] when the
        # chrome flag is disabled (the default).
        fallback_calls.append({"url": url, "instruction": instruction,
                               "max_items": max_items})
        return []

    original_req = safe_url.safe_request
    original_fb = un_careers.fetch_listings_via_chrome
    safe_url.safe_request = _raise  # type: ignore[assignment]
    un_careers.fetch_listings_via_chrome = _fake_fallback  # type: ignore[assignment]
    try:
        jobs = un_careers.fetch({"max_per_source": 10})
    finally:
        safe_url.safe_request = original_req
        un_careers.fetch_listings_via_chrome = original_fb

    # The adapter must NOT raise, and must attempt the chrome fallback.
    assert jobs == [], f"expected [] when API raises + fallback disabled, got {jobs}"
    assert len(fallback_calls) == 1, "chrome fallback must be attempted on API failure"
    assert fallback_calls[0]["url"] == un_careers.SEARCH_URL
    assert fallback_calls[0]["max_items"] == 10


def test_api_zero_openings_triggers_chrome_fallback() -> None:
    from sources import un_careers
    import safe_url

    fallback_calls: list[dict[str, Any]] = []

    def _empty(method, url, *, timeout, headers=None, json=None, **kw):
        return _FakeResp({"status": 1, "data": {"list": [], "count": 0}})

    def _fake_fallback(*, url, instruction, max_items):
        fallback_calls.append({"url": url})
        return []  # disabled-by-default contract

    original_req = safe_url.safe_request
    original_fb = un_careers.fetch_listings_via_chrome
    safe_url.safe_request = _empty  # type: ignore[assignment]
    un_careers.fetch_listings_via_chrome = _fake_fallback  # type: ignore[assignment]
    try:
        jobs = un_careers.fetch({"max_per_source": 10})
    finally:
        safe_url.safe_request = original_req
        un_careers.fetch_listings_via_chrome = original_fb

    assert jobs == [], f"expected [] for zero openings + disabled fallback, got {jobs}"
    assert len(fallback_calls) == 1, "zero openings must fall through to chrome fallback"


def test_chrome_fallback_maps_dicts_to_jobs() -> None:
    """When an operator HAS enabled the chrome tier, the dicts it returns are
    mapped to Jobs (proves the fallback mapping, independent of the flag)."""
    from sources import un_careers
    import safe_url

    def _raise(method, url, *, timeout, headers=None, json=None, **kw):
        raise RuntimeError("blocked")

    def _fake_fallback(*, url, instruction, max_items):
        return [
            {
                "title": "Logistics Officer",
                "company": "WFP",
                "location": "Rome",
                "url": "https://careers.un.org/jobSearchDescription/999?language=en",
                "posted_at": "2026-06-05",
                "snippet": "Manage supply chains.",
            },
            {"url": ""},  # no url → dropped
        ]

    original_req = safe_url.safe_request
    original_fb = un_careers.fetch_listings_via_chrome
    safe_url.safe_request = _raise  # type: ignore[assignment]
    un_careers.fetch_listings_via_chrome = _fake_fallback  # type: ignore[assignment]
    try:
        jobs = un_careers.fetch({"max_per_source": 10})
    finally:
        safe_url.safe_request = original_req
        un_careers.fetch_listings_via_chrome = original_fb

    assert len(jobs) == 1, f"urlless dict must be dropped; got {len(jobs)}"
    assert jobs[0].title == "Logistics Officer"
    assert jobs[0].source == "un_careers"
    assert jobs[0].url.endswith("/999?language=en")


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def _run() -> int:
    tests = [
        test_api_parsing_and_mapping,
        test_api_respects_cap,
        test_api_raises_triggers_chrome_fallback_disabled_returns_empty,
        test_api_zero_openings_triggers_chrome_fallback,
        test_chrome_fallback_maps_dicts_to_jobs,
    ]
    failed: list[str] = []
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"[PASS] {name}")
        except AssertionError as e:
            print(f"[FAIL] {name}: {e}")
            failed.append(name)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"[ERROR] {name}: {e!r}")
            traceback.print_exc()
            failed.append(name)
    if failed:
        print(f"\n{len(failed)}/{len(tests)} tests failed: {failed}")
        return 1
    print(f"\nAll {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
