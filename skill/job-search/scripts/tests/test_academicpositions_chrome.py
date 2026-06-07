#!/usr/bin/env python3
"""Tests for the chrome-agent fallback wired into the academicpositions adapter.

AcademicPositions sits behind Cloudflare Bot-Fight-Mode, so the plain
``requests`` RSS + HTML paths 403 and return []. This adapter now adds a
last-resort tier: when those paths yield nothing, it drives the operator's real
desktop Chrome via ``chrome_agent_fetch.fetch_listings_via_chrome``.

EVERY assertion here is about the WIRING and the no-regression guarantee — NO
real network, NO real ``claude`` CLI, NO real browser:

  (a) existing path empty + chrome helper returns postings → fallback maps them
      to Job(source="academicpositions", ...) with the right field plumbing and
      a stable external_id;
  (b) the fallback is called with the canonical "all positions, newest" URL,
      the academic instruction, and max_items = max_per_source;
  (c) when the existing path ALREADY returned jobs, the chrome helper is NEVER
      called (no regression to the working path);
  (d) when the chrome helper is disabled (returns [] — its default), the
      adapter's result is [] exactly as today.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import chrome_agent_fetch as caf  # noqa: E402
import sources.academicpositions as ap  # noqa: E402
from dedupe import Job  # noqa: E402


def _empty_rss(*, search, cap):
    """Stub the RSS path to the Cloudflare-blocked steady state: nothing."""
    return 403, "<cloudflare interstitial>", []


def _empty_html(*, search, countries, cap):
    """Stub the HTML path to the Cloudflare-blocked steady state: nothing."""
    return 403, "<cloudflare interstitial>", []


def _block_requests(monkeypatch):
    """Force BOTH requests-based paths to return empty (as they do live)."""
    monkeypatch.setattr(ap, "_try_rss", _empty_rss)
    monkeypatch.setattr(ap, "_try_html", _empty_html)


# --------------------------------------------------------------------------
# (a)+(b) Existing path empty → chrome fallback fires, maps, and is called with
# the canonical URL / instruction / max_items.
# --------------------------------------------------------------------------

def test_fallback_maps_when_existing_path_empty(monkeypatch):
    _block_requests(monkeypatch)

    captured = {}

    def _fake_chrome(*, url, instruction, max_items):
        captured["url"] = url
        captured["instruction"] = instruction
        captured["max_items"] = max_items
        return [
            {
                "title": "Postdoc in Quantum Optics",
                "company": "ETH Zurich",
                "location": "Zurich, Switzerland",
                "url": "https://academicpositions.com/job/postdoc-quantum-998877",
                "posted_at": "2026-06-01",
                "snippet": "A 2-year postdoctoral position in quantum optics.",
            },
            {
                # Missing company/location/posted_at → coerced gracefully.
                "title": "PhD Position, Machine Learning",
                "url": "https://academicpositions.com/job/phd-ml-112233",
                "snippet": "",
            },
        ]

    monkeypatch.setattr(
        caf, "fetch_listings_via_chrome", _fake_chrome, raising=True
    )

    jobs = ap.fetch({"max_per_source": 36})

    # Mapped both postings.
    assert len(jobs) == 2
    assert all(isinstance(j, Job) for j in jobs)
    assert all(j.source == "academicpositions" for j in jobs)

    j0 = jobs[0]
    assert j0.title == "Postdoc in Quantum Optics"
    assert j0.company == "ETH Zurich"
    assert j0.location == "Zurich, Switzerland"
    assert j0.url == "https://academicpositions.com/job/postdoc-quantum-998877"
    assert j0.posted_at == "2026-06-01"
    assert j0.snippet  # snippet flowed through clean_snippet
    # external_id extracted from the URL slug's numeric id.
    assert j0.external_id == "998877"

    j1 = jobs[1]
    assert j1.external_id == "112233"
    assert j1.location == "EU"  # missing location defaults to EU
    assert j1.company == ""

    # Called with the canonical listing URL + academic instruction + cap.
    assert captured["url"] == ap._CHROME_LISTING_URL
    assert "academic" in captured["instruction"].lower()
    assert captured["max_items"] == 36


# --------------------------------------------------------------------------
# (c) Existing path already returned jobs → chrome helper NEVER called.
# --------------------------------------------------------------------------

def test_no_fallback_when_existing_path_has_jobs(monkeypatch):
    existing = Job(
        source="academicpositions",
        external_id="555000",
        title="Lecturer in Physics",
        company="University of Bilbao",
        location="Spain",
        url="https://academicpositions.com/job/lecturer-physics-555000",
        posted_at="",
        snippet="",
    )

    def _rss_with_jobs(*, search, cap):
        return 200, "", [existing]

    monkeypatch.setattr(ap, "_try_rss", _rss_with_jobs)
    # HTML path returns nothing extra; should still not trigger chrome.
    monkeypatch.setattr(ap, "_try_html", _empty_html)

    called = {"chrome": False}

    def _must_not_run(**kwargs):
        called["chrome"] = True
        raise AssertionError("chrome fallback must NOT fire when jobs exist")

    monkeypatch.setattr(caf, "fetch_listings_via_chrome", _must_not_run)

    jobs = ap.fetch({"max_per_source": 36})

    assert called["chrome"] is False
    assert len(jobs) == 1
    assert jobs[0].external_id == "555000"


# --------------------------------------------------------------------------
# (d) Chrome helper disabled (its default → []) → adapter returns [] as today.
# --------------------------------------------------------------------------

def test_disabled_chrome_helper_yields_empty(monkeypatch):
    _block_requests(monkeypatch)

    # Mirror the real default: the gated helper returns [] with no subprocess.
    monkeypatch.setattr(
        caf, "fetch_listings_via_chrome",
        lambda **kwargs: [],
    )

    jobs = ap.fetch({"max_per_source": 36})
    assert jobs == []


# --------------------------------------------------------------------------
# (e) Helper raising (defensive) must not bubble out of fetch().
# --------------------------------------------------------------------------

def test_chrome_helper_raising_is_swallowed(monkeypatch):
    _block_requests(monkeypatch)

    def _boom(**kwargs):
        raise RuntimeError("unexpected chrome failure")

    monkeypatch.setattr(caf, "fetch_listings_via_chrome", _boom)

    # fetch() must never raise; degrades to [].
    jobs = ap.fetch({"max_per_source": 36})
    assert jobs == []
