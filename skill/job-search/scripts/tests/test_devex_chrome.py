#!/usr/bin/env python3
"""Chrome-agent fallback wiring for the DevEx adapter (task #13).

The DevEx adapter's PRIMARY path is the WORKING Claude WebSearch/WebFetch
delegation. DevEx is DataDome-walled, so on a bad run that path returns ZERO
jobs. This test pins the contract for the new last-resort Chrome-agent tier:

  * When the existing path returns 0 jobs (blocked / paywalled), the adapter
    calls ``chrome_agent_fetch.fetch_listings_via_chrome`` against
    https://www.devex.com/jobs/search and maps the recovered postings to
    ``Job(source="devex", ...)``.
  * When the existing path ALREADY returned jobs, the fallback is NOT called —
    the working path's results are preserved untouched (zero regression).
  * The chrome helper is called with the right url / instruction / max_items
    (max_per_source, defaulting to 36).
  * With the gate OFF (the real default), the real helper returns [] with NO
    subprocess, so the adapter's observable behavior is identical to today.

NO real `claude -p`, NO network: the Claude CLI wrapper is stubbed to canned
JSON, the chrome helper is stubbed at the module it's imported from, and the
detail-page enrichment is suppressed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import devex  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _envelope(jobs: list[dict]) -> str:
    """Wrap a jobs list in the claude-CLI --output-format json envelope."""
    return json.dumps({"result": json.dumps({"jobs": jobs})})


def _patch_primary(monkeypatch, jobs: list[dict]) -> None:
    """Stub the WebSearch/WebFetch delegation to return canned JSON."""
    monkeypatch.setattr(
        devex, "_run_claude",
        lambda prompt, *, timeout_s: _envelope(jobs),
        raising=True,
    )


def _suppress_detail_fetch(monkeypatch) -> None:
    """devex enriches non-empty results via _detail_fetch.fetch_many_bodies
    (network). Stub it to a no-op so the test never touches the wire."""
    import sources._detail_fetch as detail
    monkeypatch.setattr(detail, "fetch_many_bodies",
                        lambda urls, **k: {}, raising=False)


def _stub_chrome(monkeypatch, returns):
    """Stub ``chrome_agent_fetch.fetch_listings_via_chrome`` (the symbol the
    adapter imports lazily) and record the kwargs it was called with."""
    import chrome_agent_fetch
    calls: list[dict] = []

    def _fake(*, url, instruction, max_items=20, timeout_s=None, device_id=None):
        calls.append({
            "url": url, "instruction": instruction, "max_items": max_items,
        })
        return list(returns)

    monkeypatch.setattr(chrome_agent_fetch, "fetch_listings_via_chrome",
                        _fake, raising=True)
    return calls


# --------------------------------------------------------------------------
# Fallback FIRES when the primary path is empty
# --------------------------------------------------------------------------

def test_fallback_fires_and_maps_when_primary_empty(monkeypatch):
    _patch_primary(monkeypatch, jobs=[])  # primary returns ZERO jobs
    _suppress_detail_fetch(monkeypatch)
    calls = _stub_chrome(monkeypatch, returns=[
        {
            "title": "Research Officer | Devex",
            "company": "International Rescue Committee",
            "location": "Geneva, Switzerland",
            "url": "https://www.devex.com/jobs/research-officer-at-irc-900111",
            "posted_at": "2026-06-01",
            "snippet": "Qualitative research role in displacement contexts.",
        },
        {
            "title": "M&E Specialist",
            "company": "UNHCR",
            "location": "Remote",
            "url": "https://www.devex.com/jobs/me-specialist-at-unhcr-900112",
            "posted_at": "2026-05-20",
            "snippet": "Monitoring and evaluation.",
        },
    ])

    out = devex.fetch({"max_per_source": 36, "ai_scrape_timeout_s": 30}, db=None)

    # Fallback fired exactly once, against the listings landing page.
    assert len(calls) == 1
    assert calls[0]["url"] == "https://www.devex.com/jobs/search"
    assert calls[0]["max_items"] == 36
    assert "international development" in calls[0]["instruction"].lower()

    # Recovered postings were mapped to Job(source="devex", ...).
    assert len(out) == 2
    assert all(j.source == "devex" for j in out)
    urls = {j.url for j in out}
    assert "https://www.devex.com/jobs/research-officer-at-irc-900111" in urls
    assert "https://www.devex.com/jobs/me-specialist-at-unhcr-900112" in urls

    by_url = {j.url: j for j in out}
    j = by_url["https://www.devex.com/jobs/research-officer-at-irc-900111"]
    # Trailing " | Devex" stripped from the title.
    assert j.title == "Research Officer"
    assert j.company == "International Rescue Committee"
    assert j.location == "Geneva, Switzerland"
    # external_id is the numeric tail of the URL slug.
    assert j.external_id == "900111"


def test_fallback_uses_default_max_items_when_unset(monkeypatch):
    """No max_per_source in filters → fallback max_items defaults to 36."""
    _patch_primary(monkeypatch, jobs=[])
    _suppress_detail_fetch(monkeypatch)
    calls = _stub_chrome(monkeypatch, returns=[])

    devex.fetch({"ai_scrape_timeout_s": 30}, db=None)

    assert len(calls) == 1
    assert calls[0]["max_items"] == 36


def test_fallback_skips_non_devex_urls(monkeypatch):
    """A recovered posting without a valid devex.com/jobs/ URL is skipped."""
    _patch_primary(monkeypatch, jobs=[])
    _suppress_detail_fetch(monkeypatch)
    _stub_chrome(monkeypatch, returns=[
        {"title": "Bogus", "company": "X", "location": "",
         "url": "https://evil.example.com/phish", "posted_at": "", "snippet": ""},
        {"title": "Real Role", "company": "UNDP", "location": "",
         "url": "https://www.devex.com/jobs/real-role-at-undp-900200",
         "posted_at": "", "snippet": ""},
    ])

    out = devex.fetch({"max_per_source": 36, "ai_scrape_timeout_s": 30}, db=None)

    assert len(out) == 1
    assert out[0].url == "https://www.devex.com/jobs/real-role-at-undp-900200"


# --------------------------------------------------------------------------
# Fallback does NOT fire when the primary path already returned jobs
# --------------------------------------------------------------------------

def test_fallback_does_not_fire_when_primary_nonempty(monkeypatch):
    _patch_primary(monkeypatch, jobs=[
        {
            "title": "Policy Analyst",
            "company": "OECD",
            "location": "Paris",
            "url": "https://www.devex.com/jobs/policy-analyst-at-oecd-900300",
            "posted_at": "2026-06-02",
            "snippet": "Policy analysis role.",
        },
    ])
    _suppress_detail_fetch(monkeypatch)
    calls = _stub_chrome(monkeypatch, returns=[
        {"title": "SHOULD NOT APPEAR", "company": "Z", "location": "",
         "url": "https://www.devex.com/jobs/should-not-appear-999999",
         "posted_at": "", "snippet": ""},
    ])

    out = devex.fetch({"max_per_source": 36, "ai_scrape_timeout_s": 30}, db=None)

    # The working path's result is preserved and the chrome helper is NEVER
    # called (zero regression when the primary path succeeds).
    assert calls == []
    assert len(out) == 1
    assert out[0].url == "https://www.devex.com/jobs/policy-analyst-at-oecd-900300"
    assert all("should-not-appear" not in j.url for j in out)


# --------------------------------------------------------------------------
# Gate OFF (the real default) → identical to today, no subprocess
# --------------------------------------------------------------------------

def test_gate_off_real_helper_returns_empty_no_regression(monkeypatch):
    """With chrome_agent_fallback_enabled False (the DEFAULT) the REAL helper
    short-circuits to [] with NO subprocess, so an empty primary path yields
    an empty result — identical to today's behavior."""
    _patch_primary(monkeypatch, jobs=[])  # primary returns ZERO jobs
    _suppress_detail_fetch(monkeypatch)

    import chrome_agent_fetch
    # Force the gate OFF explicitly (independent of the live default value).
    monkeypatch.setattr(
        chrome_agent_fetch, "_chrome_agent_config",
        lambda: (False, None, 240), raising=True,
    )
    # Guard: the CLI runner must NEVER be spawned when the gate is off.
    def _boom(*a, **k):
        raise AssertionError("subprocess must not spawn when gate is OFF")
    monkeypatch.setattr(chrome_agent_fetch, "_run_chrome_agent", _boom,
                        raising=True)

    out = devex.fetch({"max_per_source": 36, "ai_scrape_timeout_s": 30}, db=None)

    assert out == []


def test_fallback_helper_raising_is_swallowed(monkeypatch):
    """If the chrome helper itself raises, fetch() still returns [] (never
    raises out of the adapter)."""
    _patch_primary(monkeypatch, jobs=[])
    _suppress_detail_fetch(monkeypatch)

    import chrome_agent_fetch

    def _raise(*, url, instruction, max_items=20, timeout_s=None, device_id=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(chrome_agent_fetch, "fetch_listings_via_chrome",
                        _raise, raising=True)

    out = devex.fetch({"max_per_source": 36, "ai_scrape_timeout_s": 30}, db=None)
    assert out == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
