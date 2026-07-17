#!/usr/bin/env python3
"""Tests for the Wellfound adapter's agentic-Chrome fallback.

The Wellfound adapter is a DataDome-walled stub that historically always
returned ``[]``. It now grows an OPT-IN fallback that drives the operator's
desktop Chrome via the shared ``chrome_agent_fetch.fetch_listings_via_chrome``
helper. These tests pin two properties:

  * When the helper yields listing dicts, each is mapped to a ``Job`` with
    ``source="wellfound"`` and ``external_id``/``url`` set to the posting URL.
  * When the helper returns ``[]`` (the DEFAULT — flag OFF, blocked, or
    nothing parseable) the adapter returns ``[]`` EXACTLY like the historic
    stub. Zero regression.

The helper is fully mocked — NO real ``claude -p``, NO subprocess, NO network.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import sources.wellfound as wellfound  # noqa: E402
from dedupe import Job  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _patch_chrome(monkeypatch, return_value, *, recorder=None):
    """Patch wellfound._try_chrome_fallback's import target.

    The adapter imports ``fetch_listings_via_chrome`` lazily from
    ``chrome_agent_fetch`` *inside* the function, so we patch the attribute
    on that module.
    """
    import chrome_agent_fetch

    def _fake(**kwargs):
        if recorder is not None:
            recorder.update(kwargs)
        if isinstance(return_value, Exception):
            raise return_value
        return return_value

    monkeypatch.setattr(chrome_agent_fetch, "fetch_listings_via_chrome", _fake)


# --------------------------------------------------------------------------
# Mapping: helper rows → Job objects
# --------------------------------------------------------------------------

def test_chrome_rows_mapped_to_jobs(monkeypatch):
    rows = [
        {
            "title": "Senior Frontend Engineer",
            "company": "Acme Startup",
            "location": "Remote (EU)",
            "url": "https://wellfound.com/jobs/123-senior-frontend",
            "posted_at": "2026-06-05",
            "snippet": "React/TypeScript, fully remote.",
        },
        {
            "title": "Backend Engineer",
            "company": "Beta Inc",
            "location": "Remote",
            "url": "https://wellfound.com/jobs/456-backend",
            "posted_at": "",
            "snippet": "Go + Postgres.",
        },
    ]
    _patch_chrome(monkeypatch, rows)

    jobs = wellfound.fetch({"max_per_source": 36})

    assert len(jobs) == 2
    assert all(isinstance(j, Job) for j in jobs)

    j0 = jobs[0]
    assert j0.source == "wellfound"
    assert j0.title == "Senior Frontend Engineer"
    assert j0.company == "Acme Startup"
    assert j0.location == "Remote (EU)"
    assert j0.url == "https://wellfound.com/jobs/123-senior-frontend"
    # external_id mirrors the posting URL.
    assert j0.external_id == "https://wellfound.com/jobs/123-senior-frontend"
    assert j0.posted_at == "2026-06-05"
    assert j0.snippet == "React/TypeScript, fully remote."

    j1 = jobs[1]
    assert j1.company == "Beta Inc"
    assert j1.posted_at == ""  # missing posted_at maps to ""


def test_chrome_invoked_with_expected_args(monkeypatch):
    """The adapter must point the helper at /remote with the contract
    instruction and the max_per_source cap."""
    rec: dict = {}
    _patch_chrome(monkeypatch, [], recorder=rec)

    wellfound.fetch({"max_per_source": 36})

    assert rec["url"] == "https://wellfound.com/remote"
    assert "remote tech and startup job listings" in rec["instruction"]
    assert rec["max_items"] == 36


def test_missing_url_falls_back_to_remote(monkeypatch):
    """A listing dict missing its own URL still maps; external_id/url default
    to the /remote landing URL rather than crashing."""
    _patch_chrome(monkeypatch, [{"title": "Role", "company": "Co"}])

    jobs = wellfound.fetch({"max_per_source": 36})

    assert len(jobs) == 1
    assert jobs[0].url == "https://wellfound.com/remote"
    assert jobs[0].external_id == "https://wellfound.com/remote"
    assert jobs[0].location == ""
    assert jobs[0].snippet == ""


# --------------------------------------------------------------------------
# Empty passthrough: helper returns [] → adapter returns [] (no regression)
# --------------------------------------------------------------------------

def test_empty_helper_returns_empty_list(monkeypatch):
    """Flag OFF / blocked / nothing parseable: helper returns [] →
    the adapter returns [] EXACTLY like the historic DataDome stub.

    The probe path is stubbed out so the test never touches the network.
    """
    _patch_chrome(monkeypatch, [])
    monkeypatch.setattr(
        wellfound, "_probe_block_reason", lambda: ("datadome_403", {})
    )

    out = wellfound.fetch({"max_per_source": 12})
    assert out == []


def test_helper_exception_returns_empty_list(monkeypatch):
    """If the helper raises, the adapter must NOT propagate it — it falls
    through to the stub and returns []."""
    _patch_chrome(monkeypatch, RuntimeError("chrome boom"))
    monkeypatch.setattr(
        wellfound, "_probe_block_reason", lambda: ("datadome_403", {})
    )

    out = wellfound.fetch({"max_per_source": 12})
    assert out == []


def test_flag_off_is_zero_regression(monkeypatch):
    """End-to-end with the REAL helper: with chrome_agent_fallback_enabled
    forced OFF, fetch_listings_via_chrome returns [] WITHOUT spawning a
    subprocess, and the adapter returns [] — the pre-fallback behavior.

    Chrome-agent fallback must stay opt-in for unattended production runs:
    the default/off-switch path must not spawn a browser-access prompt.
    """
    import chrome_agent_fetch

    # Sanity: the shipped default is production-safe/off.
    from defaults import DEFAULTS
    assert DEFAULTS.get("chrome_agent_fallback_enabled", False) is False

    monkeypatch.setattr(
        chrome_agent_fetch, "_chrome_agent_config", lambda: (False, None, 240)
    )
    monkeypatch.setattr(
        wellfound, "_probe_block_reason", lambda: ("datadome_403", {})
    )
    # Real helper, real gate — should short-circuit to [] with no subprocess.
    assert chrome_agent_fetch.fetch_listings_via_chrome(
        url="https://wellfound.com/remote", instruction="x", max_items=5
    ) == []

    assert wellfound.fetch({"max_per_source": 12}) == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
