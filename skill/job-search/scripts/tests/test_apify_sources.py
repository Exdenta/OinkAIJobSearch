#!/usr/bin/env python3
"""Tests for the optional Apify routing on the wellfound / academicpositions
adapters and the shared `sources._apify.run_actor` transport.

Two properties pinned:

  * APIFY_TOKEN unset (default) -> zero network calls, `fetch()` still
    returns a list (today's stub behavior, byte-for-byte).
  * APIFY_TOKEN set -> `run_actor` is called with the v2
    run-sync-get-dataset-items URL (tilde-encoded slug) and the returned
    dataset items map onto the exact `Job` shape used elsewhere in the repo.

No real network calls anywhere in this file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import requests  # noqa: E402

import sources.academicpositions as academicpositions  # noqa: E402
import sources.wellfound as wellfound  # noqa: E402
from dedupe import Job  # noqa: E402
from sources._apify import run_actor  # noqa: E402


# --------------------------------------------------------------------------
# APIFY_TOKEN unset: no network, fetch() returns a list
# --------------------------------------------------------------------------

def _boom_post(*a, **k):
    raise AssertionError("no Apify (POST) call expected when APIFY_TOKEN is unset")


def _unreachable_get(*a, **k):
    # Simulates the sandboxed-CI reality (no egress) instead of letting the
    # legacy probe/RSS/HTML requests.get calls hang on a real connect —
    # these pre-existing paths already catch RequestException and degrade
    # to [], so this exercises exactly that fallback quickly.
    raise requests.RequestException("network disabled in test")


def test_wellfound_fetch_no_token_no_apify_call(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr(requests, "post", _boom_post)
    monkeypatch.setattr(requests, "get", _unreachable_get)
    # Pre-existing chrome-agent fallback tier (unrelated to Apify, and
    # enabled in this checkout's defaults.py): neutralize it exactly like
    # tests/test_wellfound_chrome.py does, so this test exercises only the
    # Apify-routing change and never spawns a real `claude -p --chrome`.
    import chrome_agent_fetch
    monkeypatch.setattr(chrome_agent_fetch, "fetch_listings_via_chrome", lambda **k: [])

    out = wellfound.fetch({"max_per_source": 5})
    assert isinstance(out, list)


def test_academicpositions_fetch_no_token_no_apify_call(monkeypatch):
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setattr(requests, "post", _boom_post)
    monkeypatch.setattr(requests, "get", _unreachable_get)
    import chrome_agent_fetch
    monkeypatch.setattr(chrome_agent_fetch, "fetch_listings_via_chrome", lambda **k: [])

    out = academicpositions.fetch({"max_per_source": 5})
    assert isinstance(out, list)


# --------------------------------------------------------------------------
# _apify.run_actor: URL shape (tilde-encoded slug, v2 endpoint)
# --------------------------------------------------------------------------

def test_run_actor_builds_expected_url(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return [{"ok": True}]

    def _fake_post(url, params=None, json=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _Resp()

    monkeypatch.setattr(requests, "post", _fake_post)

    out = run_actor("nomad-agent/wellfound-scraper", {"keyword": "x"}, token="tok123")

    assert captured["url"] == (
        "https://api.apify.com/v2/acts/nomad-agent~wellfound-scraper/run-sync-get-dataset-items"
    )
    assert captured["params"] == {"token": "tok123"}
    assert out == [{"ok": True}]


def test_run_actor_degrades_to_empty_list(monkeypatch):
    def _fake_post(*a, **k):
        raise requests.RequestException("boom")

    monkeypatch.setattr(requests, "post", _fake_post)
    assert run_actor("nomad-agent/wellfound-scraper", {}, token="tok") == []

    class _Resp:
        status_code = 500

        def json(self):  # pragma: no cover - not reached, status checked first
            return []

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    assert run_actor("nomad-agent/wellfound-scraper", {}, token="tok") == []


# --------------------------------------------------------------------------
# Item -> Job mapping (canned dataset items, run_actor mocked)
# --------------------------------------------------------------------------

def test_wellfound_apify_item_maps_to_job(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok123")
    item = {
        "id": "abc123",
        "title": "Senior Backend Engineer",
        "company": "Acme Startup",
        "location": "Remote (EU)",
        "url": "https://wellfound.com/jobs/abc123",
        "postedAt": "2026-07-10",
        "snippet": "Go + Postgres, fully remote.",
        "salary": "$120k-$160k",
    }
    monkeypatch.setattr(
        "sources._apify.run_actor", lambda *a, **k: [item]
    )

    jobs = wellfound.fetch({"max_per_source": 10})

    assert len(jobs) == 1
    j = jobs[0]
    assert isinstance(j, Job)
    assert j.source == "wellfound"
    assert j.external_id == "abc123"
    assert j.title == "Senior Backend Engineer"
    assert j.company == "Acme Startup"
    assert j.location == "Remote (EU)"
    assert j.url == "https://wellfound.com/jobs/abc123"
    assert j.posted_at == "2026-07-10"
    assert j.snippet == "Go + Postgres, fully remote."
    assert j.salary == "$120k-$160k"


def test_academicpositions_apify_item_maps_to_job(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "tok123")
    item = {
        "title": "Postdoc in Machine Learning",
        "company": "TU Delft",
        "location": "Netherlands",
        "url": "https://academicpositions.com/ad/tu-delft-postdoc-ml",
        "postedAt": "2026-07-09",
        "snippet": "3-year postdoc position in ML.",
        "salary": "€3800/month",
        "globalId": "ap-98765",
    }
    monkeypatch.setattr(
        "sources._apify.run_actor", lambda *a, **k: [item]
    )

    jobs = academicpositions.fetch({"max_per_source": 10})

    assert len(jobs) == 1
    j = jobs[0]
    assert isinstance(j, Job)
    assert j.source == "academicpositions"
    assert j.external_id == "ap-98765"
    assert j.title == "Postdoc in Machine Learning"
    assert j.company == "TU Delft"
    assert j.location == "Netherlands"
    assert j.url == "https://academicpositions.com/ad/tu-delft-postdoc-ml"
    assert j.posted_at == "2026-07-09"
    assert j.snippet == "3-year postdoc position in ML."
    assert j.salary == "€3800/month"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
