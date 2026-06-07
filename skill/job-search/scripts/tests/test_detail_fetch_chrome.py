#!/usr/bin/env python3
"""Tests for Tier-3 chrome-agent fallback in ``sources._detail_fetch``.

Tier wiring under test (inside ``fetch_body_text``):

  Tier 1  plain ``safe_request`` GET
  Tier 2  headless Playwright (``_try_browser_fallback``)
  Tier 3  operator desktop Chrome (``chrome_agent_fetch.fetch_page_text_via_
          chrome``) — NEW. Only reached on an anti-bot status (403/429/503)
          when Tier 2 ALSO returned "" (still blocked).

These tests pin Tier 1 to an anti-bot status, stub Tier 2 to "" so control
always falls through to Tier 3, and mock the chrome page-text helper:

  * non-empty  → its text is used AND cached in the per-process _BODY_CACHE.
  * ""         → fetch_body_text returns the original "" (graceful contract).

NO real chromium, NO network, NO real ``claude -p --chrome``. The chrome
helper is mocked via ``sys.modules`` injection (it is lazily imported by the
wiring), and the requests layer is stubbed at ``safe_request``.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import sources._detail_fetch as detail  # noqa: E402


class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _stub_safe_request(monkeypatch, resp):
    monkeypatch.setattr(detail, "safe_request", lambda *a, **k: resp)


def _stub_playwright_empty(monkeypatch):
    """Force Tier 2 (Playwright) to return "" so control falls to Tier 3."""
    monkeypatch.setattr(detail, "_try_browser_fallback", lambda url, *, max_chars: "")


def _install_fake_chrome(monkeypatch, *, text, rec=None):
    """Inject a fake ``chrome_agent_fetch`` module. Returns the call record."""
    rec = rec if rec is not None else {}

    def _fake_page_text(*, url, timeout_s=None, device_id=None):
        rec["called"] = rec.get("called", 0) + 1
        rec["url"] = url
        return text

    fake = types.ModuleType("chrome_agent_fetch")
    fake.fetch_page_text_via_chrome = _fake_page_text
    monkeypatch.setitem(sys.modules, "chrome_agent_fetch", fake)
    return rec


@pytest.fixture(autouse=True)
def _clear_body_cache():
    detail.clear_cache()
    yield
    detail.clear_cache()


# --------------------------------------------------------------------------
# Tier-3 used when Playwright returns "" on an anti-bot status
# --------------------------------------------------------------------------

def test_chrome_agent_used_when_playwright_empty(monkeypatch):
    """403 + Tier 2 "" → Tier 3 chrome helper is called and its text used."""
    _stub_safe_request(monkeypatch, _Resp(403))
    _stub_playwright_empty(monkeypatch)
    rec = _install_fake_chrome(monkeypatch, text="CHROME RECOVERED BODY")

    out = detail.fetch_body_text("https://undp.org/job/1")

    assert out == "CHROME RECOVERED BODY"
    assert rec["called"] == 1
    assert rec["url"] == "https://undp.org/job/1"


def test_chrome_agent_result_is_cached(monkeypatch):
    """The recovered chrome text is cached per-process — second call no re-spawn."""
    _stub_safe_request(monkeypatch, _Resp(403))
    _stub_playwright_empty(monkeypatch)
    rec = _install_fake_chrome(monkeypatch, text="CHROME RECOVERED BODY")

    first = detail.fetch_body_text("https://undp.org/job/2")
    assert first == "CHROME RECOVERED BODY"
    assert detail._BODY_CACHE["https://undp.org/job/2"] == "CHROME RECOVERED BODY"

    # A second call hits the cache and must NOT invoke the chrome helper again.
    second = detail.fetch_body_text("https://undp.org/job/2")
    assert second == "CHROME RECOVERED BODY"
    assert rec["called"] == 1


@pytest.mark.parametrize("code", [403, 429, 503])
def test_chrome_agent_triggered_on_each_antibot_status(monkeypatch, code):
    _stub_safe_request(monkeypatch, _Resp(code))
    _stub_playwright_empty(monkeypatch)
    rec = _install_fake_chrome(monkeypatch, text="VIA CHROME")

    assert detail.fetch_body_text(f"https://x.test/{code}") == "VIA CHROME"
    assert rec["called"] == 1


# --------------------------------------------------------------------------
# Tier-3 empty → graceful "" contract preserved
# --------------------------------------------------------------------------

def test_chrome_agent_empty_returns_empty(monkeypatch):
    """Tier 3 also returns "" (e.g. disabled / still blocked) → original ""."""
    _stub_safe_request(monkeypatch, _Resp(403))
    _stub_playwright_empty(monkeypatch)
    rec = _install_fake_chrome(monkeypatch, text="")

    out = detail.fetch_body_text("https://undp.org/job/3")

    assert out == ""
    assert rec["called"] == 1
    # Empty result is cached as "" too (no infinite re-fetch within a run).
    assert detail._BODY_CACHE["https://undp.org/job/3"] == ""


def test_chrome_agent_raise_falls_back_to_empty(monkeypatch):
    """If the chrome helper raises, fetch_body_text still returns "" (never raises)."""
    _stub_safe_request(monkeypatch, _Resp(403))
    _stub_playwright_empty(monkeypatch)

    def _raise(*, url, timeout_s=None, device_id=None):
        raise RuntimeError("boom")

    fake = types.ModuleType("chrome_agent_fetch")
    fake.fetch_page_text_via_chrome = _raise
    monkeypatch.setitem(sys.modules, "chrome_agent_fetch", fake)

    assert detail.fetch_body_text("https://undp.org/job/4") == ""


# --------------------------------------------------------------------------
# Tier ordering: Tier 3 must NOT run when an earlier tier already succeeded
# or when the status is not anti-bot.
# --------------------------------------------------------------------------

def test_chrome_agent_not_called_when_playwright_succeeds(monkeypatch):
    """Tier 2 returns text → Tier 3 chrome helper must NOT be invoked."""
    _stub_safe_request(monkeypatch, _Resp(403))
    monkeypatch.setattr(detail, "_try_browser_fallback",
                        lambda url, *, max_chars: "PLAYWRIGHT BODY")
    rec = _install_fake_chrome(monkeypatch, text="CHROME BODY")

    out = detail.fetch_body_text("https://undp.org/job/5")

    assert out == "PLAYWRIGHT BODY"
    assert rec.get("called", 0) == 0


def test_chrome_agent_not_called_on_404(monkeypatch):
    """404 is a genuine gone page, not anti-bot → no Tier-3 chrome launch."""
    _stub_safe_request(monkeypatch, _Resp(404))
    _stub_playwright_empty(monkeypatch)
    rec = _install_fake_chrome(monkeypatch, text="CHROME BODY")

    assert detail.fetch_body_text("https://gone.test/job") == ""
    assert rec.get("called", 0) == 0


def test_chrome_agent_not_called_on_200_success(monkeypatch):
    """200 success → body parsed from HTML, no fallback tiers consulted."""
    _stub_safe_request(
        monkeypatch, _Resp(200, "<html><body>Real body here</body></html>"))
    rec = _install_fake_chrome(monkeypatch, text="CHROME BODY")

    out = detail.fetch_body_text("https://ok.test/job")

    assert "Real body here" in out
    assert rec.get("called", 0) == 0
