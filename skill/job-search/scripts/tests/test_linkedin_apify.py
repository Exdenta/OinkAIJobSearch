#!/usr/bin/env python3
"""Tests for ``apify_fetch.fetch_linkedin_apify`` — per-user LinkedIn via the
published linkedin-scraper actor.

NO real network. ``requests.Session`` is replaced with a fake that returns
canned actor datasets keyed on the actor INPUT (keyword/location), so we assert:

  1. Seed flattening → one actor run per (query, geo) combo, with the actor
     input fields (keyword/location/remote/timeFilter/maxItems) built right.
  2. ``_linkedin_record_to_job`` pins ``external_id`` to the URL (dedupe-key
     parity with the in-process ``sources.linkedin`` adapter).
  3. Cross-geo requisition de-dupe collapses same-company+title dupes.
  4. ``attribution`` maps each job back to its originating query.
  5. Guard rails: no seeds → ([], []); no token → ([], [err]).

Invoke directly or via pytest:

    python3 skill/job-search/scripts/tests/test_linkedin_apify.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import apify_fetch as af  # noqa: E402
from dedupe import Job  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, data, status=200, text=""):
        self._data = data
        self.status_code = status
        self.text = text

    def json(self):
        return self._data


class _LinkedInSession:
    """Fake session for the linkedin-scraper actor. Returns records built from
    the (keyword, location) input so each combo yields distinct, inspectable
    data. ``per_combo`` maps ``(keyword, location)`` → list-of-record-dicts;
    unmatched combos return []."""

    def __init__(self, per_combo, status=200):
        self.per_combo = per_combo
        self.status = status
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, params=None, json=None, timeout=None):
        self.calls.append({"url": url, "params": params, "json": json})
        key = (json.get("keyword"), json.get("location"))
        data = self.per_combo.get(key, [])
        return _FakeResp(data, status=self.status, text=str(data)[:50])


def _patch(monkeypatch, session):
    monkeypatch.setattr(af.requests, "Session", lambda: session)
    return session


# ---------------------------------------------------------------------------
# Happy path — flatten, dispatch, map
# ---------------------------------------------------------------------------

def test_fetch_linkedin_apify_dispatches_per_combo(monkeypatch):
    # Separated shape: 1 query × 2 geos → 2 actor runs.
    seeds = {"queries": ["react typescript"], "geos": ["Spain", "Germany"], "f_TPR": "r86400"}
    sess = _LinkedInSession({
        ("react typescript", "Spain"): [
            {"title": "Frontend Engineer", "company": "AcmeES", "location": "Madrid",
             "url": "https://li/es/1", "description": "body es"},
        ],
        ("react typescript", "Germany"): [
            {"title": "Frontend Engineer", "company": "AcmeDE", "location": "Berlin",
             "url": "https://li/de/1", "description": "body de"},
        ],
    })
    _patch(monkeypatch, sess)

    jobs, errors = af.fetch_linkedin_apify(seeds, {"remote": "remote"}, token="T", workers=2)

    assert errors == []
    assert len(sess.calls) == 2  # one actor run per (query, geo)
    # Actor input fields built correctly.
    inp = sess.calls[0]["json"]
    assert inp["keyword"] == "react typescript"
    assert inp["remote"] is True                    # "remote" filter → f_WT
    assert inp["timeFilter"] == "r86400"
    assert inp["maxItems"] == af.LINKEDIN_PER_QUERY_CAP
    assert inp["includeDescription"] is True
    assert sess.calls[0]["url"].endswith("linkedin-scraper/run-sync-get-dataset-items")

    assert {j.source for j in jobs} == {"linkedin"}
    # external_id pinned to URL (dedupe-key parity with sources.linkedin).
    assert {j.external_id for j in jobs} == {"https://li/es/1", "https://li/de/1"}
    # description → snippet.
    assert {j.snippet for j in jobs} == {"body es", "body de"}


def test_fetch_linkedin_apify_cross_geo_dedupe(monkeypatch):
    # Same company + title surfaced under two country pages (distinct URLs) →
    # collapses to one after _dedupe_cross_geo.
    seeds = {"queries": ["react"], "geos": ["Spain", "France"]}
    sess = _LinkedInSession({
        ("react", "Spain"): [
            {"title": "Staff Engineer (Remote)", "company": "Stripe",
             "url": "https://li/es/staff"},
        ],
        ("react", "France"): [
            {"title": "Staff Engineer - Remote", "company": "Stripe",
             "url": "https://li/fr/staff"},
        ],
    })
    _patch(monkeypatch, sess)

    jobs, errors = af.fetch_linkedin_apify(seeds, {}, token="T", workers=2)
    assert len(jobs) == 1  # cross-geo requisition dedupe collapsed the pair


def test_fetch_linkedin_apify_attribution(monkeypatch):
    seeds = {"queries": ["vue"], "geos": ["Spain"]}
    sess = _LinkedInSession({
        ("vue", "Spain"): [
            {"title": "Vue Dev", "company": "X", "url": "https://li/vue/1"},
        ],
    })
    _patch(monkeypatch, sess)

    attribution: dict = {}
    jobs, errors = af.fetch_linkedin_apify(seeds, {}, token="T", attribution=attribution)
    assert len(jobs) == 1
    assert attribution[jobs[0].job_id] == "vue"


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------

def test_fetch_linkedin_apify_no_seeds():
    jobs, errors = af.fetch_linkedin_apify(None, {}, token="T")
    assert jobs == [] and errors == []
    jobs, errors = af.fetch_linkedin_apify({"queries": []}, {}, token="T")
    assert jobs == [] and errors == []


def test_fetch_linkedin_apify_no_token(monkeypatch):
    monkeypatch.setattr(af, "resolve_token", lambda explicit=None: None)
    jobs, errors = af.fetch_linkedin_apify(
        {"queries": ["react"], "geos": ["Spain"]}, {},
    )
    assert jobs == []
    assert errors and "APIFY_TOKEN" in errors[0]


def test_fetch_linkedin_apify_http_error_captured(monkeypatch):
    sess = _LinkedInSession({}, status=500)
    _patch(monkeypatch, sess)
    jobs, errors = af.fetch_linkedin_apify(
        {"queries": ["react"], "geos": ["Spain"]}, {}, token="T",
    )
    assert jobs == []
    assert errors and "HTTP 500" in errors[0]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
