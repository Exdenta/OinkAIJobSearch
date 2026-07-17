#!/usr/bin/env python3
"""Tests for the per-user Apify/legacy backend wiring.

Covers the plumbing that lets ONE user's continuous searcher run the Apify
fetch backend while everyone else stays on the legacy in-process scrapers, in
the same bot process:

  1. ``search_jobs.run`` validates ``fetch_backend``.
  2. ``search_jobs._fetch_global`` dispatches local vs apify (no network — both
     fetchers are monkeypatched).
  3. ``ContinuousSearcher`` threads ``fetch_backend`` into the search callable.
  4. ``bot._parse_apify_chat_ids_env`` / ``_backend_for_chat`` select the
     backend per chat from OINK_APIFY_CHAT_IDS.

Invoke directly or via pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import pytest  # noqa: E402


# ---------------------------------------------------------------------------
# search_jobs.run / _fetch_global
# ---------------------------------------------------------------------------

def test_run_rejects_unknown_backend():
    import search_jobs
    with pytest.raises(ValueError):
        search_jobs.run(fetch_backend="bogus")


def test_fetch_global_dispatch(monkeypatch):
    import search_jobs
    import apify_fetch

    seen = {}

    def fake_local(filters, **kw):
        seen["backend"] = "local"
        seen["kw"] = kw
        return (["L1", "L2"], [])

    def fake_apify(filters, **kw):
        seen["backend"] = "apify"
        seen["kw"] = kw
        return (["A1"], [], [{"source": "hackernews", "count": 1}])

    monkeypatch.setattr(search_jobs, "fetch_all", fake_local)
    monkeypatch.setattr(apify_fetch, "fetch_all_apify", fake_apify)

    f = {"apify_cache_ttl_s": 0, "apify_run_timeout_s": 120,
         "apify_workers": 6, "apify_actor_owner": "nomad-agent"}

    jobs, errs = search_jobs._fetch_global(f, backend="local", db=None, cycle_index=3)
    assert jobs == ["L1", "L2"] and seen["backend"] == "local"
    # local backend forwards cursor/telemetry args
    assert seen["kw"]["cycle_index"] == 3

    jobs, errs = search_jobs._fetch_global(f, backend="apify", db=None, cycle_index=3)
    assert jobs == ["A1"] and seen["backend"] == "apify"
    # apify backend reads its knobs from filters
    assert seen["kw"]["cache_ttl"] == 0 and seen["kw"]["owner"] == "nomad-agent"


def test_fetch_global_apify_records_source_runs(monkeypatch):
    import search_jobs
    import apify_fetch

    def fake_apify(filters, **kw):
        return (
            ["A1", "A2"],
            ["wellfound: timeout"],
            [
                {"source": "hackernews", "actor": "hackernews-scraper",
                 "count": 1, "seconds": 1.25, "error": None, "skipped": None},
                {"source": "wellfound", "actor": "wellfound-scraper",
                 "count": 0, "seconds": 2.5, "error": "timeout", "skipped": None},
            ],
        )

    class FakeStore:
        def __init__(self):
            self.rows = []

        def record_source_run(self, *args, **kwargs):
            self.rows.append((args, kwargs))

    monkeypatch.setattr(apify_fetch, "fetch_all_apify", fake_apify)

    store = FakeStore()
    filters = {"apify_cache_ttl_s": 0, "apify_run_timeout_s": 120,
               "apify_workers": 6, "apify_actor_owner": "nomad-agent"}
    jobs, errs = search_jobs._fetch_global(
        filters,
        backend="apify",
        store=store,
        pipeline_run_id=42,
    )

    assert jobs == ["A1", "A2"]
    assert errs == ["wellfound: timeout"]
    assert [(r[0][1], r[0][2], r[0][3]) for r in store.rows] == [
        ("hackernews", "ok", 1),
        ("wellfound", "failed", 0),
    ]
    assert store.rows[1][1]["error_class"] == "ApifyError"


def test_fetch_global_bad_backend_raises():
    import search_jobs
    with pytest.raises(ValueError):
        search_jobs._fetch_global({}, backend="nope")


# ---------------------------------------------------------------------------
# ContinuousSearcher threads fetch_backend
# ---------------------------------------------------------------------------

def test_searcher_threads_fetch_backend():
    from continuous_searcher import ContinuousSearcher

    class _DB:
        def get_auto_search_enabled(self, c):
            return True

    rec = {}
    cs = ContinuousSearcher(
        db=_DB(), chat_id=433775883, interval_seconds=10,
        search_run_callable=lambda **kw: rec.update(kw) or 0,
        fetch_backend="apify",
    )
    cs._invoke_search()
    assert rec["fetch_backend"] == "apify"
    assert rec["only_chat"] == 433775883


def test_searcher_defaults_apify():
    # Apify is the default; local is deprecated. A searcher built without an
    # explicit backend drives apify.
    from continuous_searcher import ContinuousSearcher

    class _DB:
        def get_auto_search_enabled(self, c):
            return True

    rec = {}
    cs = ContinuousSearcher(
        db=_DB(), chat_id=111, interval_seconds=10,
        search_run_callable=lambda **kw: rec.update(kw) or 0,
    )
    cs._invoke_search()
    assert rec["fetch_backend"] == "apify"


# ---------------------------------------------------------------------------
# bot backend selection from env (Apify default, OINK_LOCAL_CHAT_IDS opt-out)
# ---------------------------------------------------------------------------

def test_parse_local_chat_ids_env(monkeypatch):
    import bot
    monkeypatch.setenv("OINK_LOCAL_CHAT_IDS", "433775883, 999 ,, bad,0")
    assert bot._parse_local_chat_ids_env() == {433775883, 999}


def test_backend_for_chat_defaults_apify(monkeypatch):
    import bot
    # No env → everyone (incl. new users) on apify.
    monkeypatch.delenv("OINK_LOCAL_CHAT_IDS", raising=False)
    assert bot._backend_for_chat(433775883) == "apify"
    assert bot._backend_for_chat(111) == "apify"


def test_backend_for_chat_local_optout(monkeypatch):
    import bot
    # Per-user rollback: only listed chats go local.
    monkeypatch.setenv("OINK_LOCAL_CHAT_IDS", "111")
    assert bot._backend_for_chat(111) == "local"
    assert bot._backend_for_chat(433775883) == "apify"


def test_backend_for_chat_all_rollback(monkeypatch):
    import bot
    # Whole-fleet kill-switch.
    monkeypatch.setenv("OINK_LOCAL_CHAT_IDS", "all")
    assert bot._backend_for_chat(111) == "local"
    assert bot._backend_for_chat(433775883) == "local"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
