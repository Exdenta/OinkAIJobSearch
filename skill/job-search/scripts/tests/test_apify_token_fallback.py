#!/usr/bin/env python3
"""Tests for the gated bundled-token fallback in ``apify_fetch.resolve_token``.

The fallback that reads a token from ``apify/<actor>/.env`` is a leak risk in a
public/forked checkout, so it is OFF unless ``APIFY_TOKEN_FILE_FALLBACK`` is
truthy. We assert:

  1. Explicit arg wins over everything.
  2. ``APIFY_TOKEN`` env wins when no explicit arg.
  3. No env + fallback DISABLED (default) → None, even if a bundled .env exists.
  4. No env + fallback ENABLED → reads the bundled .env.

A temp repo tree (``<root>/apify/un-careers-scraper/.env`` + a script under
``<root>/skill/job-search/scripts/``) is built so ``resolve_token``'s parent
walk finds the file, without touching the real repo.

    python3 skill/job-search/scripts/tests/test_apify_token_fallback.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import apify_fetch  # noqa: E402


def _fake_tree(tmp_path: Path, token: str) -> Path:
    """Build <root>/apify/un-careers-scraper/.env and a fake module file under
    <root>/skill/job-search/scripts/apify_fetch.py; return the module file path."""
    env = tmp_path / "apify" / "un-careers-scraper" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text(f"APIFY_TOKEN={token}\n")
    modfile = tmp_path / "skill" / "job-search" / "scripts" / "apify_fetch.py"
    modfile.parent.mkdir(parents=True)
    modfile.write_text("# placeholder\n")
    return modfile


def test_explicit_wins(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "env_tok")
    assert apify_fetch.resolve_token("explicit_tok") == "explicit_tok"


def test_env_wins_no_explicit(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "env_tok")
    monkeypatch.delenv("APIFY_TOKEN_FILE_FALLBACK", raising=False)
    assert apify_fetch.resolve_token() == "env_tok"


def test_fallback_disabled_by_default(monkeypatch, tmp_path):
    modfile = _fake_tree(tmp_path, "file_tok")
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.delenv("APIFY_TOKEN_FILE_FALLBACK", raising=False)
    monkeypatch.setattr(apify_fetch, "__file__", str(modfile))
    # Bundled .env exists, but fallback is OFF → must NOT read it.
    assert apify_fetch.resolve_token() is None


def test_fallback_reads_file_when_enabled(monkeypatch, tmp_path):
    modfile = _fake_tree(tmp_path, "file_tok")
    monkeypatch.delenv("APIFY_TOKEN", raising=False)
    monkeypatch.setenv("APIFY_TOKEN_FILE_FALLBACK", "1")
    monkeypatch.setattr(apify_fetch, "__file__", str(modfile))
    assert apify_fetch.resolve_token() == "file_tok"


def _run_all():
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    _run_all()
