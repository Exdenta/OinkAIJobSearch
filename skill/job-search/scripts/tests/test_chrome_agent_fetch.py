#!/usr/bin/env python3
"""Tests for the chrome-agent fallback tier (chrome_agent_fetch.py).

Units under test: ``fetch_listings_via_chrome`` and
``fetch_page_text_via_chrome``. Both drive the operator's real desktop Chrome
via ``claude -p --chrome`` — so EVERY assertion here is about the defensive
contract, with NO real browser, NO real ``claude`` CLI, and NO subprocess that
actually runs. The subprocess layer is stubbed at
``chrome_agent_fetch._run_chrome_agent`` (or, where the "no spawn" guarantee is
under test, at ``subprocess.run`` so we can assert it was never reached).

Cases covered:
  (a) disabled flag → []/"" and NO subprocess spawned;
  (b) SSRF-unsafe url → []/"" and NO subprocess spawned;
  (c) a mocked successful claude JSON envelope → parsed listings / text;
  (d) malformed / empty stdout → []/"";
  (e) subprocess TimeoutExpired → []/"".
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import chrome_agent_fetch as caf  # noqa: E402


SAFE_URL = "https://example.com/jobs"


def _enable(monkeypatch, *, enabled=True, device_id="", timeout_s=240):
    """Force the lazily-read config to a known state."""
    monkeypatch.setattr(
        caf, "_chrome_agent_config",
        lambda: (enabled, device_id or None, timeout_s),
    )


def _force_safe(monkeypatch, safe=True):
    monkeypatch.setattr(caf, "is_safe_url", lambda url: (safe, "" if safe else "blocked"))


def _envelope(result_text: str) -> str:
    """Build a `claude -p --output-format json` envelope around result_text."""
    return json.dumps({"result": result_text, "is_error": False})


# --------------------------------------------------------------------------
# (a) Disabled flag → []/"" and NO subprocess spawned
# --------------------------------------------------------------------------

def test_disabled_listings_returns_empty_no_subprocess(monkeypatch):
    _enable(monkeypatch, enabled=False)
    _force_safe(monkeypatch, safe=True)

    spawned = {"called": False}

    def _boom(*a, **k):
        spawned["called"] = True
        raise AssertionError("subprocess.run must NOT be called when disabled")

    monkeypatch.setattr(subprocess, "run", _boom)

    out = caf.fetch_listings_via_chrome(url=SAFE_URL, instruction="frontend")
    assert out == []
    assert spawned["called"] is False


def test_disabled_page_text_returns_empty_no_subprocess(monkeypatch):
    _enable(monkeypatch, enabled=False)
    _force_safe(monkeypatch, safe=True)

    def _boom(*a, **k):
        raise AssertionError("subprocess.run must NOT be called when disabled")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert caf.fetch_page_text_via_chrome(url=SAFE_URL) == ""


# --------------------------------------------------------------------------
# (b) SSRF-unsafe url → []/"" and NO subprocess spawned
# --------------------------------------------------------------------------

def test_ssrf_unsafe_listings_returns_empty_no_subprocess(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=False)

    def _boom(*a, **k):
        raise AssertionError("subprocess.run must NOT be called on unsafe URL")

    monkeypatch.setattr(subprocess, "run", _boom)

    out = caf.fetch_listings_via_chrome(
        url="http://169.254.169.254/latest/meta-data/", instruction="x",
    )
    assert out == []


def test_ssrf_unsafe_page_text_returns_empty_no_subprocess(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=False)

    def _boom(*a, **k):
        raise AssertionError("subprocess.run must NOT be called on unsafe URL")

    monkeypatch.setattr(subprocess, "run", _boom)

    assert caf.fetch_page_text_via_chrome(url="http://127.0.0.1:8000/") == ""


# --------------------------------------------------------------------------
# (c) Mocked successful claude JSON envelope → parsed listings / text
# --------------------------------------------------------------------------

def test_successful_listings_parsed(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)

    payload = {
        "jobs": [
            {
                "title": "Senior Frontend Engineer",
                "company": "Acme",
                "location": "Remote EU",
                "url": "https://acme.example/jobs/1",
                "posted_at": "2026-06-01",
                "snippet": "React + TS",
            },
            # Missing fields + a non-string value to exercise coercion.
            {"title": "Vue Dev", "company": None, "extra": "ignored", "url": 42},
        ]
    }
    monkeypatch.setattr(
        caf, "_run_chrome_agent",
        lambda prompt, *, timeout_s: _envelope(json.dumps(payload)),
    )

    out = caf.fetch_listings_via_chrome(
        url=SAFE_URL, instruction="frontend", max_items=20,
    )
    assert len(out) == 2
    first = out[0]
    assert set(first.keys()) == {
        "title", "company", "location", "url", "posted_at", "snippet",
    }
    assert first["title"] == "Senior Frontend Engineer"
    assert first["company"] == "Acme"
    second = out[1]
    assert second["title"] == "Vue Dev"
    assert second["company"] == ""          # None → ""
    assert second["url"] == "42"            # non-string coerced to str
    assert all(isinstance(v, str) for v in second.values())


def test_successful_listings_capped_at_max_items(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    payload = {"jobs": [{"title": f"job {i}"} for i in range(50)]}
    monkeypatch.setattr(
        caf, "_run_chrome_agent",
        lambda prompt, *, timeout_s: _envelope(json.dumps(payload)),
    )
    out = caf.fetch_listings_via_chrome(
        url=SAFE_URL, instruction="x", max_items=5,
    )
    assert len(out) == 5


def test_successful_listings_with_fenced_json(monkeypatch):
    """parse_json_block must tolerate code fences around the JSON."""
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    fenced = "```json\n" + json.dumps({"jobs": [{"title": "X"}]}) + "\n```"
    monkeypatch.setattr(
        caf, "_run_chrome_agent",
        lambda prompt, *, timeout_s: _envelope(fenced),
    )
    out = caf.fetch_listings_via_chrome(url=SAFE_URL, instruction="x")
    assert out == [{
        "title": "X", "company": "", "location": "",
        "url": "", "posted_at": "", "snippet": "",
    }]


def test_successful_page_text_parsed(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    body = "Job title\nWe are hiring a frontend engineer in Bilbao."
    monkeypatch.setattr(
        caf, "_run_chrome_agent",
        lambda prompt, *, timeout_s: _envelope(body),
    )
    assert caf.fetch_page_text_via_chrome(url=SAFE_URL) == body


# --------------------------------------------------------------------------
# (d) Malformed / empty stdout → []/""
# --------------------------------------------------------------------------

def test_malformed_listings_returns_empty(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    # Envelope whose result is not JSON at all.
    monkeypatch.setattr(
        caf, "_run_chrome_agent",
        lambda prompt, *, timeout_s: _envelope("totally not json"),
    )
    assert caf.fetch_listings_via_chrome(url=SAFE_URL, instruction="x") == []


def test_listings_missing_jobs_key_returns_empty(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    monkeypatch.setattr(
        caf, "_run_chrome_agent",
        lambda prompt, *, timeout_s: _envelope(json.dumps({"results": []})),
    )
    assert caf.fetch_listings_via_chrome(url=SAFE_URL, instruction="x") == []


def test_empty_stdout_listings_returns_empty(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    monkeypatch.setattr(caf, "_run_chrome_agent", lambda prompt, *, timeout_s: "")
    assert caf.fetch_listings_via_chrome(url=SAFE_URL, instruction="x") == []


def test_none_stdout_page_text_returns_empty(monkeypatch):
    """_run_chrome_agent returns None on CLI failure → ""."""
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    monkeypatch.setattr(caf, "_run_chrome_agent", lambda prompt, *, timeout_s: None)
    assert caf.fetch_page_text_via_chrome(url=SAFE_URL) == ""


def test_empty_page_text_returns_empty(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    monkeypatch.setattr(
        caf, "_run_chrome_agent",
        lambda prompt, *, timeout_s: _envelope("   \n  "),
    )
    assert caf.fetch_page_text_via_chrome(url=SAFE_URL) == ""


# --------------------------------------------------------------------------
# (e) Subprocess timeout → []/""
# --------------------------------------------------------------------------

def test_subprocess_timeout_listings_returns_empty(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    monkeypatch.setattr(caf.shutil, "which", lambda _: "/usr/bin/claude")

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=240)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert caf.fetch_listings_via_chrome(url=SAFE_URL, instruction="x") == []


def test_subprocess_timeout_page_text_returns_empty(monkeypatch):
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    monkeypatch.setattr(caf.shutil, "which", lambda _: "/usr/bin/claude")

    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=240)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert caf.fetch_page_text_via_chrome(url=SAFE_URL) == ""


def test_cli_missing_returns_empty(monkeypatch):
    """No `claude` on PATH → _run_chrome_agent returns None → []/""."""
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    monkeypatch.setattr(caf.shutil, "which", lambda _: None)

    def _boom(*a, **k):
        raise AssertionError("subprocess.run must NOT be reached when CLI missing")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert caf.fetch_listings_via_chrome(url=SAFE_URL, instruction="x") == []
    assert caf.fetch_page_text_via_chrome(url=SAFE_URL) == ""


def test_nonzero_exit_returns_empty(monkeypatch):
    """rc != 0 → None → []/""."""
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    monkeypatch.setattr(caf.shutil, "which", lambda _: "/usr/bin/claude")

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    assert caf.fetch_listings_via_chrome(url=SAFE_URL, instruction="x") == []


def test_command_shape_and_real_subprocess_path(monkeypatch):
    """End-to-end through the real _run_chrome_agent with subprocess.run stubbed.

    Verifies the argv shape: -p, --chrome, --output-format json, the allow-list
    and the deny-shell-fs deny-list — and that a clean rc==0 envelope flows back
    into parsed listings.
    """
    _enable(monkeypatch, enabled=True)
    _force_safe(monkeypatch, safe=True)
    monkeypatch.setattr(caf.shutil, "which", lambda _: "/usr/bin/claude")

    captured = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps({"result": json.dumps({"jobs": [{"title": "Z"}]})})
        stderr = ""

    def _run(cmd, **k):
        captured["cmd"] = cmd
        captured["timeout"] = k.get("timeout")
        return _Proc()

    monkeypatch.setattr(subprocess, "run", _run)

    out = caf.fetch_listings_via_chrome(
        url=SAFE_URL, instruction="x", timeout_s=99,
    )
    assert out and out[0]["title"] == "Z"

    cmd = captured["cmd"]
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "--chrome" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--allowed-tools") + 1] == caf.ALLOWED
    assert cmd[cmd.index("--disallowed-tools") + 1] == caf.TOOLS_DENY_SHELL_FS
    assert captured["timeout"] == 99
    # Sanity: the allow-list carries the chrome MCP tools, not shell/fs.
    assert "mcp__claude-in-chrome__navigate" in caf.ALLOWED
    assert "Bash" not in caf.ALLOWED


def test_device_id_threaded_into_prompt(monkeypatch):
    """An explicit device_id arg reaches select_browser in the prompt."""
    _enable(monkeypatch, enabled=True, device_id="")
    _force_safe(monkeypatch, safe=True)

    captured = {}

    def _fake_agent(prompt, *, timeout_s):
        captured["prompt"] = prompt
        return _envelope(json.dumps({"jobs": []}))

    monkeypatch.setattr(caf, "_run_chrome_agent", _fake_agent)
    caf.fetch_listings_via_chrome(
        url=SAFE_URL, instruction="x", device_id="dev-123",
    )
    assert "select_browser" in captured["prompt"]
    assert "dev-123" in captured["prompt"]
