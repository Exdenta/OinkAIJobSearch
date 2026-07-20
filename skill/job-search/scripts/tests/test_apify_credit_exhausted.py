#!/usr/bin/env python3
"""Tests for ``apify_fetch.credit_exhausted`` — the account-level billing
signal that flips a run from the Apify backend to the local scrapers.

Detection matters because it's the trigger for the whole-run fallback + the
one-shot self-hoster notification in ``search_jobs.run``. We assert it fires
on the real wire shapes (the ``"<source>: HTTP 402: ..."`` string the fetch
builds, plus Apify's usage-limit error types) and does NOT fire on ordinary
transient/source errors that should stay on Apify.

    python3 skill/job-search/scripts/tests/test_apify_credit_exhausted.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import apify_fetch  # noqa: E402


def test_fires_on_http_402():
    # Exactly the string `_use_jobs` builds: "<key>: HTTP 402: <body>".
    assert apify_fetch.credit_exhausted(
        ["hackernews: HTTP 402: {\"error\":{\"type\":\"insufficient-balance\"}}"]
    )


def test_fires_on_usage_limit_error_types():
    for e in [
        "un_careers: HTTP 402: monthly-usage-hard-limit-exceeded",
        "eures: Payment Required",
        "devex: insufficient-balance",
    ]:
        assert apify_fetch.credit_exhausted([e]), e


def test_fires_if_any_error_matches():
    # One 402 among transient failures still means the account is blocked.
    assert apify_fetch.credit_exhausted([
        "hackernews: HTTP 503: bad gateway",
        "eures: HTTP 402: monthly usage hard limit",
    ])


def test_no_false_positive_on_transient_errors():
    # Timeouts, 5xx, and non-402 4xx must NOT trip the fallback — Apify stays.
    assert not apify_fetch.credit_exhausted([
        "hackernews: HTTP 429: rate limited",
        "eures: HTTP 500: run-failed timed-out",
        "wttj: ConnectionError: connection reset",
        "reliefweb: HTTP 400: schema validation failed",
    ])


def test_empty_and_none():
    assert not apify_fetch.credit_exhausted([])
    assert not apify_fetch.credit_exhausted(None)


def _run_all():
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))


if __name__ == "__main__":
    _run_all()
