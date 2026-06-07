#!/usr/bin/env python3
"""Tests for the headless-browser fallback tier (TASK B).

Two units under test:

  * ``browser_fetch.fetch_rendered`` — the new module. We verify it runs the
    SSRF guard FIRST (never launches the browser on a blocked URL), lazily
    imports playwright (returns None gracefully when absent), and cleans the
    rendered HTML the same way the requests path does.

  * ``sources._detail_fetch.fetch_body_text`` wiring — on an anti-bot status
    (403/429/503) AND the config flag ON, the browser fallback is attempted
    and its text is used; on a non-403 success the fallback is NOT attempted;
    with the flag OFF the fallback is never attempted; and when playwright is
    unavailable the body fetch returns its original ("") result.

NO real chromium, NO network, NO real `claude -p`. playwright is mocked via
``sys.modules`` injection; the requests layer is stubbed at ``safe_request``.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import browser_fetch  # noqa: E402
import sources._detail_fetch as detail  # noqa: E402


# --------------------------------------------------------------------------
# Helpers: a fake playwright.sync_api module recording whether launch happened
# --------------------------------------------------------------------------

class _FakePage:
    def __init__(self, html, *, goto_raises=None):
        self._html = html
        self._goto_raises = goto_raises
        self.goto_calls = []

    def goto(self, url, **kw):
        self.goto_calls.append((url, kw))
        if self._goto_raises is not None:
            raise self._goto_raises

    def content(self):
        return self._html


class _FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class _FakeBrowser:
    def __init__(self, page, *, rec):
        self._page = page
        self._rec = rec

    def new_context(self, **kw):
        self._rec["context_kwargs"] = kw
        return _FakeContext(self._page)

    def close(self):
        self._rec["closed"] = True


class _FakeChromium:
    def __init__(self, page, *, rec, launch_raises=None):
        self._page = page
        self._rec = rec
        self._launch_raises = launch_raises

    def launch(self, **kw):
        self._rec["launched"] = True
        self._rec["launch_kwargs"] = kw
        if self._launch_raises is not None:
            raise self._launch_raises
        return _FakeBrowser(self._page, rec=self._rec)


class _FakeSyncPlaywright:
    def __init__(self, chromium):
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install_fake_playwright(monkeypatch, *, html="<html><body>OK</body></html>",
                             rec=None, launch_raises=None, goto_raises=None):
    """Inject a fake ``playwright.sync_api`` into sys.modules. Returns `rec`."""
    rec = rec if rec is not None else {}
    page = _FakePage(html, goto_raises=goto_raises)
    chromium = _FakeChromium(page, rec=rec, launch_raises=launch_raises)

    def _sync_playwright():
        return _FakeSyncPlaywright(chromium)

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = _sync_playwright
    pkg = types.ModuleType("playwright")
    pkg.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", pkg)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    return rec


def _force_playwright_absent(monkeypatch):
    """Make `import playwright.sync_api` fail, simulating an un-installed host."""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)


@pytest.fixture(autouse=True)
def _clear_body_cache():
    detail.clear_cache()
    yield
    detail.clear_cache()


# --------------------------------------------------------------------------
# browser_fetch.fetch_rendered — direct unit tests
# --------------------------------------------------------------------------

def test_fetch_rendered_ssrf_blocked_never_launches(monkeypatch):
    """(e) is_safe_url=False → return None WITHOUT launching the browser."""
    rec = _install_fake_playwright(monkeypatch, rec={})
    monkeypatch.setattr(browser_fetch, "is_safe_url",
                        lambda url: (False, "blocked_ip:127.0.0.1"))

    out = browser_fetch.fetch_rendered("http://127.0.0.1/job", timeout_s=5)

    assert out is None
    assert "launched" not in rec, "browser must NOT launch on an SSRF-blocked URL"


def test_fetch_rendered_playwright_absent_returns_none(monkeypatch):
    """(d) playwright import fails → fetch_rendered returns None gracefully."""
    _force_playwright_absent(monkeypatch)
    monkeypatch.setattr(browser_fetch, "is_safe_url", lambda url: (True, "ok"))

    out = browser_fetch.fetch_rendered("https://example.com/job", timeout_s=5)
    assert out is None


def test_fetch_rendered_success_cleans_html(monkeypatch):
    rec = _install_fake_playwright(
        monkeypatch,
        html="<html><head><style>.x{}</style></head><body>Hello <b>World</b>"
             "<script>var a=1;</script></body></html>",
    )
    monkeypatch.setattr(browser_fetch, "is_safe_url", lambda url: (True, "ok"))

    out = browser_fetch.fetch_rendered("https://example.com/job", timeout_s=5)

    assert rec.get("launched") is True
    assert rec.get("closed") is True, "browser must be closed in finally"
    assert rec["launch_kwargs"].get("headless") is True
    assert "Hello" in out and "World" in out
    # script/style inner text must be stripped, not surfaced as body.
    assert "var a=1" not in out
    assert ".x{}" not in out


def test_fetch_rendered_launch_failure_returns_none(monkeypatch):
    """Missing chromium binary → launch() raises → graceful None, no leak."""
    rec = _install_fake_playwright(
        monkeypatch, launch_raises=RuntimeError("Executable doesn't exist"))
    monkeypatch.setattr(browser_fetch, "is_safe_url", lambda url: (True, "ok"))

    out = browser_fetch.fetch_rendered("https://example.com/job", timeout_s=5)
    assert out is None


def test_fetch_rendered_navigation_failure_closes_browser(monkeypatch):
    rec = _install_fake_playwright(
        monkeypatch, goto_raises=TimeoutError("nav timeout"))
    monkeypatch.setattr(browser_fetch, "is_safe_url", lambda url: (True, "ok"))

    out = browser_fetch.fetch_rendered("https://example.com/job", timeout_s=5)
    assert out is None
    assert rec.get("closed") is True, "browser must close even on nav failure"


def test_fetch_rendered_empty_url_returns_none(monkeypatch):
    rec = _install_fake_playwright(monkeypatch, rec={})
    out = browser_fetch.fetch_rendered("", timeout_s=5)
    assert out is None
    assert "launched" not in rec


# --------------------------------------------------------------------------
# _detail_fetch.fetch_body_text wiring
# --------------------------------------------------------------------------

class _Resp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _stub_safe_request(monkeypatch, resp):
    monkeypatch.setattr(detail, "safe_request",
                        lambda *a, **k: resp)


def test_403_with_flag_on_attempts_fallback(monkeypatch):
    """(a) 403 + flag ON → fallback attempted, its text used."""
    _stub_safe_request(monkeypatch, _Resp(403))
    monkeypatch.setattr(detail, "_browser_fallback_config", lambda: (True, 30.0))

    calls = {}

    def _fake_rendered(url, *, timeout_s, max_chars):
        calls["url"] = url
        calls["timeout_s"] = timeout_s
        return "RECOVERED BODY TEXT"

    fake = types.ModuleType("browser_fetch")
    fake.fetch_rendered = _fake_rendered
    monkeypatch.setitem(sys.modules, "browser_fetch", fake)

    out = detail.fetch_body_text("https://undp.org/job/1", timeout_s=10)

    assert out == "RECOVERED BODY TEXT"
    assert calls["url"] == "https://undp.org/job/1"
    assert calls["timeout_s"] == 30.0


@pytest.mark.parametrize("code", [429, 503])
def test_429_503_also_trigger_fallback(monkeypatch, code):
    _stub_safe_request(monkeypatch, _Resp(code))
    monkeypatch.setattr(detail, "_browser_fallback_config", lambda: (True, 30.0))

    fake = types.ModuleType("browser_fetch")
    fake.fetch_rendered = lambda url, **k: "RENDERED"
    monkeypatch.setitem(sys.modules, "browser_fetch", fake)

    assert detail.fetch_body_text(f"https://x.test/{code}") == "RENDERED"


def test_non_403_success_does_not_attempt_fallback(monkeypatch):
    """(b) 200 success → fallback NOT attempted."""
    _stub_safe_request(monkeypatch, _Resp(200, "<html><body>Real body here</body></html>"))
    monkeypatch.setattr(detail, "_browser_fallback_config", lambda: (True, 30.0))

    called = {"n": 0}
    fake = types.ModuleType("browser_fetch")

    def _boom(url, **k):
        called["n"] += 1
        return "SHOULD NOT BE USED"

    fake.fetch_rendered = _boom
    monkeypatch.setitem(sys.modules, "browser_fetch", fake)

    out = detail.fetch_body_text("https://ok.test/job")
    assert "Real body here" in out
    assert called["n"] == 0


def test_404_does_not_attempt_fallback(monkeypatch):
    """404 is a genuine gone page, not anti-bot → no browser launch."""
    _stub_safe_request(monkeypatch, _Resp(404))
    monkeypatch.setattr(detail, "_browser_fallback_config", lambda: (True, 30.0))

    called = {"n": 0}
    fake = types.ModuleType("browser_fetch")
    fake.fetch_rendered = lambda url, **k: called.__setitem__("n", called["n"] + 1) or "X"
    monkeypatch.setitem(sys.modules, "browser_fetch", fake)

    assert detail.fetch_body_text("https://gone.test/job") == ""
    assert called["n"] == 0


def test_403_with_flag_off_never_attempts_fallback(monkeypatch):
    """(c) flag OFF → fallback never attempted, returns original ""."""
    _stub_safe_request(monkeypatch, _Resp(403))
    monkeypatch.setattr(detail, "_browser_fallback_config", lambda: (False, 30.0))

    called = {"n": 0}
    fake = types.ModuleType("browser_fetch")
    fake.fetch_rendered = lambda url, **k: called.__setitem__("n", called["n"] + 1) or "X"
    monkeypatch.setitem(sys.modules, "browser_fetch", fake)

    assert detail.fetch_body_text("https://undp.org/job/2") == ""
    assert called["n"] == 0


def test_403_flag_on_playwright_absent_returns_original(monkeypatch):
    """(d) flag ON but playwright absent → fetch_body_text returns original "".

    Drives the REAL browser_fetch.fetch_rendered (which lazily fails the
    playwright import) rather than a stub, proving end-to-end graceful
    degradation.
    """
    _stub_safe_request(monkeypatch, _Resp(403))
    monkeypatch.setattr(detail, "_browser_fallback_config", lambda: (True, 30.0))
    monkeypatch.setattr(browser_fetch, "is_safe_url", lambda url: (True, "ok"))
    _force_playwright_absent(monkeypatch)
    # Ensure the real module is imported by the wiring.
    monkeypatch.setitem(sys.modules, "browser_fetch", browser_fetch)

    assert detail.fetch_body_text("https://undp.org/job/3") == ""


def test_403_fallback_render_failure_falls_back_to_empty(monkeypatch):
    """If fetch_rendered raises, the body fetch still returns original ""."""
    _stub_safe_request(monkeypatch, _Resp(403))
    monkeypatch.setattr(detail, "_browser_fallback_config", lambda: (True, 30.0))

    fake = types.ModuleType("browser_fetch")

    def _raise(url, **k):
        raise RuntimeError("boom")

    fake.fetch_rendered = _raise
    monkeypatch.setitem(sys.modules, "browser_fetch", fake)

    assert detail.fetch_body_text("https://undp.org/job/4") == ""


def test_403_fallback_empty_render_falls_back_to_empty(monkeypatch):
    """fetch_rendered returns None/"" → original "" kept (not None)."""
    _stub_safe_request(monkeypatch, _Resp(403))
    monkeypatch.setattr(detail, "_browser_fallback_config", lambda: (True, 30.0))

    fake = types.ModuleType("browser_fetch")
    fake.fetch_rendered = lambda url, **k: None
    monkeypatch.setitem(sys.modules, "browser_fetch", fake)

    assert detail.fetch_body_text("https://undp.org/job/5") == ""


def test_config_fallback_enabled():
    """Config reflects the 2026-06-04 operator decision to ENABLE the fallback
    (playwright + chromium installed on the host). Note the code still degrades
    gracefully to today's behavior wherever playwright/chromium are absent —
    that path is covered by the playwright-unavailable tests above."""
    enabled, timeout_s = detail._browser_fallback_config()
    assert enabled is True
    assert timeout_s > 0
