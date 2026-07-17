#!/usr/bin/env python3
"""Tests for curated_boards.py's orchestrated (non-agentic) backend.

Covers the backend toggle (env-var driven, default "claude" unchanged), the
single-fetch happy path, the bounded 2nd-page fetch when the model reports a
`next_page_url`, and that a thin/empty fetch degrades to [] rather than
raising. No real network / no real `claude` CLI — `_fetch_listing_text` and
`wrapped_run_p` are monkeypatched at module level.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import sources.curated_boards as cb  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CURATED_BOARDS_BACKEND", raising=False)
    yield


def _envelope(result_obj) -> str:
    return json.dumps({"result": json.dumps(result_obj)})


def test_backend_defaults_to_claude():
    assert cb._curated_boards_backend() == "claude"


def test_backend_invalid_value_falls_back_to_claude(monkeypatch):
    monkeypatch.setenv("CURATED_BOARDS_BACKEND", "bogus")
    assert cb._curated_boards_backend() == "claude"


def test_dispatcher_routes_to_orchestrated_when_flag_set(monkeypatch):
    monkeypatch.setenv("CURATED_BOARDS_BACKEND", "orchestrated")
    calls = {"orchestrated": 0, "claude": 0}
    monkeypatch.setattr(cb, "_scrape_one_orchestrated",
                         lambda *a, **k: calls.__setitem__("orchestrated", calls["orchestrated"] + 1) or [])
    monkeypatch.setattr(cb, "_scrape_one_claude",
                         lambda *a, **k: calls.__setitem__("claude", calls["claude"] + 1) or [])
    cb._scrape_one("remocate", "https://x.test", {})
    assert calls == {"orchestrated": 1, "claude": 0}


def test_dispatcher_routes_to_claude_by_default(monkeypatch):
    calls = {"orchestrated": 0, "claude": 0}
    monkeypatch.setattr(cb, "_scrape_one_orchestrated",
                         lambda *a, **k: calls.__setitem__("orchestrated", calls["orchestrated"] + 1) or [])
    monkeypatch.setattr(cb, "_scrape_one_claude",
                         lambda *a, **k: calls.__setitem__("claude", calls["claude"] + 1) or [])
    cb._scrape_one("remocate", "https://x.test", {})
    assert calls == {"orchestrated": 0, "claude": 1}


def test_orchestrated_single_page_happy_path(monkeypatch):
    monkeypatch.setattr(cb, "_fetch_listing_text",
                         lambda url: "Frontend Engineer at Acme")
    monkeypatch.setattr(cb, "wrapped_run_p", lambda *a, **k: _envelope({
        "jobs": [{"title": "Frontend Engineer", "company": "Acme",
                   "location": "Remote", "url": "https://x.test/job/1"}],
        "next_page_url": "",
    }))

    out = cb._scrape_one_orchestrated("remocate", "https://x.test", {"max_per_source": 10})

    assert len(out) == 1
    assert out[0].title == "Frontend Engineer"
    assert out[0].source == "remocate"


def test_orchestrated_empty_fetch_returns_no_jobs(monkeypatch):
    monkeypatch.setattr(cb, "_fetch_listing_text", lambda url: "")
    called = {"n": 0}
    monkeypatch.setattr(cb, "wrapped_run_p",
                         lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    out = cb._scrape_one_orchestrated("remocate", "https://x.test", {})

    assert out == []
    assert called["n"] == 0, "extraction call must not fire on an empty fetch"


def test_orchestrated_follows_next_page_url_once(monkeypatch):
    fetch_calls = []

    def _fake_fetch(url):
        fetch_calls.append(url)
        return f"body for {url}"

    monkeypatch.setattr(cb, "_fetch_listing_text", _fake_fetch)

    extract_calls = {"n": 0}

    def _fake_run_p(store, caller, prompt, **k):
        extract_calls["n"] += 1
        if extract_calls["n"] == 1:
            return _envelope({
                "jobs": [{"title": "Job A", "company": "Acme", "url": "https://x.test/a"}],
                "next_page_url": "https://x.test/page2",
            })
        return _envelope({
            "jobs": [{"title": "Job B", "company": "Acme", "url": "https://x.test/b"}],
            "next_page_url": "https://x.test/page3",  # must NOT be followed — bounded to 1 hop
        })

    monkeypatch.setattr(cb, "wrapped_run_p", _fake_run_p)

    out = cb._scrape_one_orchestrated("remocate", "https://x.test", {"max_per_source": 10})

    assert fetch_calls == ["https://x.test", "https://x.test/page2"]
    assert extract_calls["n"] == 2
    assert {j.title for j in out} == {"Job A", "Job B"}


def test_orchestrated_skips_next_page_when_cap_already_met(monkeypatch):
    monkeypatch.setattr(cb, "_fetch_listing_text", lambda url: "x")
    extract_calls = {"n": 0}

    def _fake_run_p(store, caller, prompt, **k):
        extract_calls["n"] += 1
        return _envelope({
            "jobs": [{"title": "Job A", "company": "Acme", "url": "https://x.test/a"}],
            "next_page_url": "https://x.test/page2",
        })

    monkeypatch.setattr(cb, "wrapped_run_p", _fake_run_p)

    out = cb._scrape_one_orchestrated("remocate", "https://x.test", {"max_per_source": 1})

    assert extract_calls["n"] == 1, "cap already met after page 1 — must not fetch page 2"
    assert len(out) == 1
