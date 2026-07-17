#!/usr/bin/env python3
"""Tests for telegram_client.py's orchestrated liveness-verifier backend.

Covers the backend toggle (env-var driven, default "claude" unchanged), the
LIVENESS_CLAUDE_CHAT_IDS rollback lever (comma-list + "all" sentinel,
mirroring bot.py's OINK_LOCAL_CHAT_IDS), dispatcher routing (including the
rollback lever overriding an "orchestrated" default for one chat), and
`_verify_listing_orchestrated`'s core logic: fetch -> classify, the bounded
verdict parsing. No real network / no real `claude` CLI —
`sources._detail_fetch.fetch_body_text` and
`wrapped_run_p` are monkeypatched at module level.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import telegram_client as tc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LIVENESS_BACKEND", raising=False)
    monkeypatch.delenv("LIVENESS_CLAUDE_CHAT_IDS", raising=False)
    yield


def _job(url="https://example.com/jobs/123", title="Frontend Engineer", company="Acme",
         source="linkedin"):
    # `source` is required since 7a42da2: the dispatcher's UN-careers fast
    # path reads job.source before routing. Real Job objects always carry it.
    return SimpleNamespace(url=url, title=title, company=company, source=source)


def _envelope(result_obj) -> str:
    return json.dumps({"result": json.dumps(result_obj)})


# --------------------------------------------------------------------------
# Backend toggle
# --------------------------------------------------------------------------

def test_backend_defaults_to_claude():
    assert tc._liveness_backend() == "claude"


def test_backend_invalid_value_falls_back_to_claude(monkeypatch):
    monkeypatch.setenv("LIVENESS_BACKEND", "bogus")
    assert tc._liveness_backend() == "claude"


def test_backend_orchestrated_when_set(monkeypatch):
    monkeypatch.setenv("LIVENESS_BACKEND", "orchestrated")
    assert tc._liveness_backend() == "orchestrated"


# --------------------------------------------------------------------------
# LIVENESS_CLAUDE_CHAT_IDS rollback lever
# --------------------------------------------------------------------------

def test_claude_chat_ids_unset_forces_nobody():
    assert tc._parse_liveness_claude_chat_ids_env() == set()
    assert tc._liveness_claude_all_enabled() is False
    assert tc._liveness_forced_to_claude(12345) is False


def test_claude_chat_ids_comma_list(monkeypatch):
    monkeypatch.setenv("LIVENESS_CLAUDE_CHAT_IDS", "111, 222,333")
    assert tc._parse_liveness_claude_chat_ids_env() == {111, 222, 333}
    assert tc._liveness_forced_to_claude(111) is True
    assert tc._liveness_forced_to_claude(999) is False


def test_claude_chat_ids_bad_tokens_dropped(monkeypatch):
    monkeypatch.setenv("LIVENESS_CLAUDE_CHAT_IDS", "111,not-a-number,222,0")
    assert tc._parse_liveness_claude_chat_ids_env() == {111, 222}


def test_claude_chat_ids_all_sentinel(monkeypatch):
    monkeypatch.setenv("LIVENESS_CLAUDE_CHAT_IDS", "all")
    assert tc._liveness_claude_all_enabled() is True
    assert tc._parse_liveness_claude_chat_ids_env() == set()
    assert tc._liveness_forced_to_claude(1) is True
    assert tc._liveness_forced_to_claude(None) is True


def test_forced_to_claude_none_chat_id_without_all_is_not_forced():
    assert tc._liveness_forced_to_claude(None) is False


# --------------------------------------------------------------------------
# Dispatcher routing
# --------------------------------------------------------------------------

def test_dispatcher_routes_to_claude_by_default(monkeypatch):
    calls = {"orchestrated": 0, "claude": 0}
    monkeypatch.setattr(tc, "_verify_listing_orchestrated",
                         lambda *a, **k: calls.__setitem__("orchestrated", calls["orchestrated"] + 1) or (True, "ok"))
    monkeypatch.setattr(tc, "_web_search_listing_still_open_claude",
                         lambda *a, **k: calls.__setitem__("claude", calls["claude"] + 1) or (True, "ok"))
    tc._web_search_listing_still_open(_job(), chat_id=1)
    assert calls == {"orchestrated": 0, "claude": 1}


def test_dispatcher_routes_to_orchestrated_when_flag_set(monkeypatch):
    monkeypatch.setenv("LIVENESS_BACKEND", "orchestrated")
    calls = {"orchestrated": 0, "claude": 0}
    monkeypatch.setattr(tc, "_verify_listing_orchestrated",
                         lambda *a, **k: calls.__setitem__("orchestrated", calls["orchestrated"] + 1) or (True, "ok"))
    monkeypatch.setattr(tc, "_web_search_listing_still_open_claude",
                         lambda *a, **k: calls.__setitem__("claude", calls["claude"] + 1) or (True, "ok"))
    tc._web_search_listing_still_open(_job(), chat_id=1)
    assert calls == {"orchestrated": 1, "claude": 0}


def test_dispatcher_rollback_lever_overrides_orchestrated_default(monkeypatch):
    """Fleet-wide orchestrated, but this one chat is pinned back to claude."""
    monkeypatch.setenv("LIVENESS_BACKEND", "orchestrated")
    monkeypatch.setenv("LIVENESS_CLAUDE_CHAT_IDS", "42")
    calls = {"orchestrated": 0, "claude": 0}
    monkeypatch.setattr(tc, "_verify_listing_orchestrated",
                         lambda *a, **k: calls.__setitem__("orchestrated", calls["orchestrated"] + 1) or (True, "ok"))
    monkeypatch.setattr(tc, "_web_search_listing_still_open_claude",
                         lambda *a, **k: calls.__setitem__("claude", calls["claude"] + 1) or (True, "ok"))

    tc._web_search_listing_still_open(_job(), chat_id=42)
    assert calls == {"orchestrated": 0, "claude": 1}, "chat 42 is on the rollback list"

    tc._web_search_listing_still_open(_job(), chat_id=7)
    assert calls == {"orchestrated": 1, "claude": 1}, "chat 7 is NOT on the rollback list"


# --------------------------------------------------------------------------
# _verify_listing_orchestrated
# --------------------------------------------------------------------------

def test_orchestrated_no_url_returns_unknown():
    status, reason = tc._verify_listing_orchestrated(_job(url=""))
    assert status is None
    assert reason == "unknown:no_url"


def test_orchestrated_happy_path_open(monkeypatch):
    monkeypatch.setattr("sources._detail_fetch.fetch_body_text",
                         lambda url, **k: "Apply now for this Frontend Engineer role at Acme.")
    # NOTE: `_verify_listing_orchestrated`/`_web_search_listing_still_open_claude`
    # both do a LOCAL `from instrumentation.wrappers import wrapped_run_p[...]`
    # inside the function body (matching this file's established defensive
    # lazy-import style throughout) — patch the SOURCE module's attribute,
    # not `tc.wrapped_run_p`, or the patch is silently a no-op.
    monkeypatch.setattr("instrumentation.wrappers.wrapped_run_p",
                         lambda *a, **k: _envelope({"status": "open", "reason": "Apply CTA visible"}))

    status, reason = tc._verify_listing_orchestrated(_job())

    assert status is True
    assert reason == "ok"


def test_orchestrated_liveness_does_not_spawn_chrome(monkeypatch):
    """Send-time liveness must be unattended; real Chrome can prompt for
    per-site access, so this backend uses the non-interactive detail fetch."""
    called = {"http": 0, "kwargs": None}

    def _http(url, **k):
        called["http"] += 1
        called["kwargs"] = k
        return "HTTP body with Apply now."

    monkeypatch.setattr("sources._detail_fetch.fetch_body_text", _http)
    monkeypatch.setattr(
        "instrumentation.wrappers.wrapped_run_p",
        lambda *a, **k: _envelope({"status": "open", "reason": "apply visible"}),
    )

    status, reason = tc._verify_listing_orchestrated(_job())

    assert status is True
    assert reason == "ok"
    assert called["http"] == 1
    assert called["kwargs"]["allow_chrome_agent"] is False


def test_orchestrated_happy_path_closed(monkeypatch):
    monkeypatch.setattr("sources._detail_fetch.fetch_body_text",
                         lambda url, **k: "This position is closed. No longer accepting applications.")
    monkeypatch.setattr(
        "instrumentation.wrappers.wrapped_run_p",
        lambda *a, **k: _envelope({"status": "closed", "reason": "no longer accepting applications"}),
    )

    status, reason = tc._verify_listing_orchestrated(_job())

    assert status is False
    assert reason.startswith("closed:")


def test_orchestrated_none_stdout_returns_unknown(monkeypatch):
    monkeypatch.setattr("sources._detail_fetch.fetch_body_text", lambda url, **k: "some body text")
    monkeypatch.setattr("instrumentation.wrappers.wrapped_run_p", lambda *a, **k: None)

    status, reason = tc._verify_listing_orchestrated(_job())

    assert status is None
    assert reason == "unknown:cli_unavailable"


def test_orchestrated_and_claude_share_the_same_verdict_rules_text(monkeypatch):
    """The single-source-of-truth guarantee: both backends' actual
    constructed prompts must embed the exact same classification rules
    block — captured via their real code paths, not re-derived."""
    job = _job()
    captured = {}

    monkeypatch.setattr("sources._detail_fetch.fetch_body_text", lambda url, **k: "some body")
    monkeypatch.setattr("instrumentation.wrappers.wrapped_run_p", lambda store, caller, prompt, **k:
                         captured.__setitem__("orchestrated", prompt) or _envelope({"status": "unknown", "reason": ""}))
    tc._verify_listing_orchestrated(job)

    monkeypatch.setattr("instrumentation.wrappers.wrapped_run_p_with_tools", lambda store, caller, prompt, **k:
                         captured.__setitem__("claude", prompt) or _envelope({"status": "unknown", "reason": ""}))
    tc._web_search_listing_still_open_claude(job)

    assert tc._LIVENESS_VERDICT_RULES in captured["orchestrated"]
    assert tc._LIVENESS_VERDICT_RULES in captured["claude"]
