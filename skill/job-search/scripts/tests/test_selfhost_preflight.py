#!/usr/bin/env python3
"""Tests for ``selfhost_preflight.py`` — the download-and-run doctor.

NO network (``--ping`` path is not exercised). We assert the pure check logic
against a controlled env + a stubbed ``shutil.which`` and ``resolve_token``:

  1. Claude scoring present (claude CLI on PATH) + APIFY_TOKEN → READY.
  2. No claude CLI and no Mistral key → scoring REQUIRED check fails.
  4. FETCH_BACKEND=apify with no token → apify_token REQUIRED check fails.
  5. FETCH_BACKEND=local → apify token NOT required (ok even with no token).

Invoke directly or via pytest:

    python3 skill/job-search/scripts/tests/test_selfhost_preflight.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import apify_fetch  # noqa: E402
import selfhost_preflight as sp  # noqa: E402


def _setup(monkeypatch, *, claude=True, token=None, env=None):
    """Stub the two external probes + reset the relevant env."""
    monkeypatch.setattr(sp.shutil, "which",
                        lambda name: "/usr/bin/claude" if (claude and name == "claude") else None)
    monkeypatch.setattr(apify_fetch, "resolve_token", lambda explicit=None: token)
    for k in ("FETCH_BACKEND", "PROVIDER_JOB_SEARCH", "MISTRAL_API_KEY",
              "APIFY_TOKEN", "APIFY_ACTOR_OWNER", "APIFY_TOKEN_FILE_FALLBACK"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)


def _check(result, name):
    return next(c for c in result["checks"] if c.name == name)


def test_ready_claude_and_token(monkeypatch):
    _setup(monkeypatch, claude=True, token="apify_xxx")
    r = sp.gather(backend="apify")
    assert r["ok"] is True
    assert _check(r, "scoring").ok is True
    assert _check(r, "apify_token").ok is True


def test_no_scoring_provider_fails(monkeypatch):
    _setup(monkeypatch, claude=False, token="apify_xxx")
    r = sp.gather(backend="apify")
    sc = _check(r, "scoring")
    assert sc.ok is False and sc.required is True
    assert r["ok"] is False


def test_apify_backend_requires_token(monkeypatch):
    _setup(monkeypatch, claude=True, token=None)
    r = sp.gather(backend="apify")
    tok = _check(r, "apify_token")
    assert tok.ok is False and tok.required is True
    assert r["ok"] is False


def test_local_backend_token_optional(monkeypatch):
    _setup(monkeypatch, claude=True, token=None)
    r = sp.gather(backend="local")
    tok = _check(r, "apify_token")
    assert tok.required is False
    assert tok.ok is True          # not required → reported ok
    assert r["ok"] is True         # scoring ok, apify not required


def _run_all():
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    _run_all()
