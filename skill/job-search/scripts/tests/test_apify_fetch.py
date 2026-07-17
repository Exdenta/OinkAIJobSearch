#!/usr/bin/env python3
"""Tests for ``apify_fetch.py`` — the experimental Apify-backed fetch_all.

NO real network. ``requests.Session`` is replaced with a fake that returns
canned actor datasets, so we assert the pure logic:

  1. ``record_to_job`` — field-fallback mapping (clean schema + un-careers
     legacy ``jobId``/``description``), and the drop rule (no url + no title).
  2. ``build_input`` — broad parity input (maxItems + cacheTtlSeconds), BYOK
     key injection.
  3. ``enabled_apify_sources`` — enable-gate ∩ ACTOR_MAP.
  4. ``fetch_all_apify`` — fan-out, (jobs, errors, meta) contract, BYOK skip
     when no key, HTTP-error capture, and the ``only=`` subset filter.

Invoke directly or via pytest:

    python3 skill/job-search/scripts/tests/test_apify_fetch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import apify_fetch as af  # noqa: E402
from dedupe import Job  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, data, status=200, text=""):
        self._data = data
        self.status_code = status
        self.text = text

    def json(self):
        return self._data


class _FakeSession:
    """Routes by actor name in the URL. ``responses`` maps an actor-name
    substring → (data, status)."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, params=None, json=None, timeout=None):
        self.calls.append({"url": url, "params": params, "json": json})
        for needle, (data, status) in self.responses.items():
            if needle in url:
                return _FakeResp(data, status=status, text=str(data)[:50])
        return _FakeResp({"error": "no route"}, status=404, text="not found")


def _patch_session(monkeypatch, responses):
    sess = _FakeSession(responses)
    monkeypatch.setattr(af.requests, "Session", lambda: sess)
    return sess


# ---------------------------------------------------------------------------
# record_to_job
# ---------------------------------------------------------------------------

def test_record_to_job_clean_schema():
    j = af.record_to_job("wttj", {
        "id": "abc", "title": "Engineer", "company": "Acme", "location": "Paris",
        "url": "https://w/1", "postedAt": "2026-06-01", "snippet": "body", "salary": "50k",
    })
    assert isinstance(j, Job)
    assert j.source == "wttj" and j.external_id == "abc"
    assert j.title == "Engineer" and j.company == "Acme"
    assert j.snippet == "body" and j.salary == "50k"


def test_record_to_job_legacy_un_careers_fields():
    j = af.record_to_job("un_careers", {
        "jobId": 279994, "title": "Officer", "company": "United Nations",
        "location": "GENEVA", "url": "https://careers.un.org/x/279994",
        "postedAt": "2026-06-26", "description": "the body",
    })
    assert j.external_id == "279994"      # jobId fallback, stringified
    assert j.snippet == "the body"        # description → snippet fallback


def test_record_to_job_drops_empty():
    assert af.record_to_job("x", {"title": "", "url": ""}) is None
    assert af.record_to_job("x", "not a dict") is None


def test_record_to_job_synthesizes_id_from_url():
    j = af.record_to_job("hackernews", {"title": "T", "url": "https://h/9"})
    assert j.external_id and j.external_id != ""  # sha1 of url


# ---------------------------------------------------------------------------
# build_input
# ---------------------------------------------------------------------------

def test_build_input_broad_parity():
    inp = af.build_input("un_careers", {"max_per_source": 36}, cache_ttl=0)
    assert inp == {"maxItems": 36, "cacheTtlSeconds": 0}
    # No keyword/query — broad fetch matches the local global adapters.
    assert "keyword" not in inp and "query" not in inp


def test_build_input_respects_source_schema_cap():
    inp = af.build_input("reliefweb", {"max_per_source": 36}, cache_ttl=0)
    assert inp == {"maxItems": 20, "cacheTtlSeconds": 0}


def test_build_input_query_array_sources():
    qs = ["react engineer", "python backend"]
    # Each trio source gets the list under ITS schema field name.
    assert af.build_input("eures", {}, cache_ttl=0, queries=qs)["keywords"] == qs
    assert af.build_input("jobs_ac_uk", {}, cache_ttl=0, queries=qs)["keywords"] == qs
    assert af.build_input("ycombinator_was", {}, cache_ttl=0, queries=qs)["queries"] == qs
    # Non-query source ignores the list entirely (stays broad).
    inp = af.build_input("hackernews", {}, cache_ttl=0, queries=qs)
    assert "keywords" not in inp and "queries" not in inp and "keyword" not in inp
    # Empty/None queries → broad input, no empty array sent.
    assert "keywords" not in af.build_input("eures", {}, cache_ttl=0, queries=[])
    assert "keywords" not in af.build_input("eures", {}, cache_ttl=0)


def test_clean_queries():
    # Strings + paired dicts, dedupe case-insensitive, strip, cap.
    raw = ["  React Engineer ", {"q": "python backend"}, "react engineer",
           "", None, 42, {"q": ""}]
    assert af.clean_queries(raw) == ["React Engineer", "python backend"]
    assert af.clean_queries(None) == []
    many = [f"q{i}" for i in range(20)]
    assert len(af.clean_queries(many)) == af.MAX_ACTOR_QUERIES


def test_build_input_byok_key_injected():
    inp = af.build_input("devex", {"max_per_source": 10}, cache_ttl=5, anthropic_key="sk-x")
    assert inp["anthropicApiKey"] == "sk-x"
    # Non-BYOK never gets the key even if one is around.
    inp2 = af.build_input("hackernews", {"max_per_source": 10}, cache_ttl=5, anthropic_key="sk-x")
    assert "anthropicApiKey" not in inp2


def test_build_input_byok_mistral_fallback():
    # No anthropic key, but a mistral key is available (the project's actual
    # runtime env — see .env's MISTRAL_API_KEY) — actor supports
    # provider="mistral" + mistralApiKey, so we should wire that instead of
    # leaving the BYOK source keyless.
    inp = af.build_input("devex", {"max_per_source": 10}, cache_ttl=5, mistral_key="mk-x")
    assert inp["provider"] == "mistral"
    assert inp["mistralApiKey"] == "mk-x"
    assert "anthropicApiKey" not in inp
    # Anthropic wins if both are present.
    inp2 = af.build_input("devex", {"max_per_source": 10}, cache_ttl=5,
                           anthropic_key="sk-x", mistral_key="mk-x")
    assert inp2["anthropicApiKey"] == "sk-x"
    assert "provider" not in inp2 and "mistralApiKey" not in inp2


# ---------------------------------------------------------------------------
# enabled_apify_sources
# ---------------------------------------------------------------------------

def test_enabled_apify_sources_gate():
    en = af.enabled_apify_sources({"sources": {
        "hackernews": True, "wttj": False,
        "curated_boards": True,   # enabled but NO actor → excluded
        "un_careers": True,
    }})
    assert set(en) == {"hackernews", "un_careers"}


# ---------------------------------------------------------------------------
# fetch_all_apify
# ---------------------------------------------------------------------------

def _ok(records):
    return (records, 200)


def test_fetch_all_apify_happy(monkeypatch):
    _patch_session(monkeypatch, {
        "hackernews-scraper": _ok([
            {"id": "1", "title": "HN", "company": "Y", "url": "https://h/1", "postedAt": "x"},
        ]),
        "wttj-scraper": _ok([
            {"id": "2", "title": "WT", "company": "Z", "url": "https://w/2", "postedAt": "y"},
        ]),
    })
    filters = {"sources": {"hackernews": True, "wttj": True}, "max_per_source": 5}
    jobs, errors, meta = af.fetch_all_apify(filters, token="T", cache_ttl=0, workers=2)
    assert len(jobs) == 2 and errors == []
    assert {j.source for j in jobs} == {"hackernews", "wttj"}
    assert all(m["error"] is None and m["skipped"] is None for m in meta)
    assert {m["source"] for m in meta} == {"hackernews", "wttj"}


def test_fetch_all_apify_byok_skipped_without_key(monkeypatch):
    _patch_session(monkeypatch, {"devex-scraper": _ok([{"title": "T", "url": "u"}])})
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    filters = {"sources": {"devex": True}, "max_per_source": 5}
    jobs, errors, meta = af.fetch_all_apify(filters, token="T", anthropic_key=None, mistral_key=None)
    assert jobs == []
    assert any(m["source"] == "devex" and m["skipped"] for m in meta)


def test_fetch_all_apify_http_error_captured(monkeypatch):
    _patch_session(monkeypatch, {"hackernews-scraper": ({"error": "boom"}, 500)})
    monkeypatch.setattr(af.time, "sleep", lambda _s: None)
    filters = {"sources": {"hackernews": True}, "max_per_source": 5}
    jobs, errors, meta = af.fetch_all_apify(filters, token="T")
    assert jobs == []
    assert errors and "hackernews" in errors[0] and "HTTP 500" in errors[0]
    assert meta[0]["error"] and meta[0]["count"] == 0


def test_fetch_all_apify_retries_transient_endpoint_error(monkeypatch):
    class _SeqSession:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, params=None, json=None, timeout=None):
            self.calls.append({"url": url, "params": params, "json": json})
            if len(self.calls) == 1:
                return _FakeResp({"error": "bad gateway"}, status=502, text="bad gateway")
            return _FakeResp([{"id": "1", "title": "HN", "url": "https://h/1"}])

    sess = _SeqSession()
    monkeypatch.setattr(af.requests, "Session", lambda: sess)
    monkeypatch.setattr(af.time, "sleep", lambda _s: None)
    filters = {"sources": {"hackernews": True}, "max_per_source": 5}
    jobs, errors, meta = af.fetch_all_apify(filters, token="T")
    assert errors == []
    assert len(jobs) == 1 and jobs[0].source == "hackernews"
    assert len(sess.calls) == 2
    assert meta[0]["error"] is None


def test_fetch_all_apify_reliefweb_caps_body_and_query_param(monkeypatch):
    sess = _patch_session(monkeypatch, {
        "reliefweb-scraper": _ok([{"id": "1", "title": "R", "url": "https://r/1"}]),
    })
    filters = {"sources": {"reliefweb": True}, "max_per_source": 36}
    jobs, errors, meta = af.fetch_all_apify(filters, token="T")
    assert errors == [] and len(jobs) == 1
    assert sess.calls[0]["json"]["maxItems"] == 20
    assert sess.calls[0]["params"]["maxItems"] == 20
    assert meta[0]["source"] == "reliefweb"


def test_fetch_all_apify_only_subset(monkeypatch):
    _patch_session(monkeypatch, {
        "hackernews-scraper": _ok([{"id": "1", "title": "HN", "url": "https://h/1"}]),
        "wttj-scraper": _ok([{"id": "2", "title": "WT", "url": "https://w/2"}]),
    })
    filters = {"sources": {"hackernews": True, "wttj": True}, "max_per_source": 5}
    jobs, errors, meta = af.fetch_all_apify(filters, token="T", only=["hackernews"])
    assert {j.source for j in jobs} == {"hackernews"}
    assert {m["source"] for m in meta} == {"hackernews"}


def test_fetch_all_apify_queries_forwarded(monkeypatch):
    sess = _patch_session(monkeypatch, {
        "eures-scraper": _ok([{"id": "1", "title": "E", "url": "https://e/1"}]),
        "hackernews-scraper": _ok([{"id": "2", "title": "HN", "url": "https://h/2"}]),
    })
    filters = {"sources": {"eures": True, "hackernews": True}, "max_per_source": 5}
    jobs, errors, meta = af.fetch_all_apify(
        filters, token="T", queries=["react engineer", "React Engineer", "go dev"])
    assert errors == []
    by_actor = {c["url"].split("~")[1].split("/")[0]: c["json"] for c in sess.calls}
    # Query-array actor gets the cleaned list; broad actor input untouched.
    assert by_actor["eures-scraper"]["keywords"] == ["react engineer", "go dev"]
    assert "keywords" not in by_actor["hackernews-scraper"]
    # Meta records the queries only on the query-array source.
    mm = {m["source"]: m for m in meta}
    assert mm["eures"]["queries"] == ["react engineer", "go dev"]
    assert "queries" not in mm["hackernews"]


def test_profile_source_queries_resolution():
    # boards.keywords present → used for every non-broken query source.
    prof = {"search_seeds": {
        "boards": {"keywords": ["react", "frontend"]},
        "linkedin": {"queries": ["senior frontend engineer react typescript remote"]},
    }}
    q = af.profile_source_queries(prof)
    assert q["ycombinator_was"] == ["react", "frontend"]
    assert q["jobs_ac_uk"] == ["react", "frontend"]
    assert q["eures"] == ["react", "frontend"]   # un-gated after 0.1.8 fix
    assert q["un_careers"] == ["react", "frontend"]  # single-param sources too
    # QUERY_BROKEN sources (none currently) must be skipped when present.
    assert not (af.QUERY_BROKEN & set(q))

    # No boards block → linkedin fallback ONLY for fallback-safe sources.
    prof2 = {"search_seeds": {"linkedin": {"queries": ["react engineer"]}}}
    q2 = af.profile_source_queries(prof2)
    assert q2 == {"ycombinator_was": ["react engineer"]}

    # Nothing usable → broad everywhere.
    assert af.profile_source_queries(None) == {}
    assert af.profile_source_queries({"search_seeds": {}}) == {}


def test_profile_source_queries_native_language_routing():
    # Spanish native keywords: replace English on Spanish-indexed boards,
    # combine on eures, leave English-indexed + other-language boards alone.
    prof = {"search_seeds": {"boards": {
        "keywords": ["interior design", "interior designer"],
        "native_keywords": ["interiorista", "diseñador de interiores"],
        "native_language": "spanish",
    }}}
    q = af.profile_source_queries(prof)
    assert q["infojobs"] == ["interiorista", "diseñador de interiores"]
    assert q["jobs_ac_uk"] == ["interior design", "interior designer"]
    assert q["wttj"] == ["interior design", "interior designer"]  # french board, spanish seeds
    assert q["eures"] == ["interiorista", "diseñador de interiores",
                          "interior design", "interior designer"]

    # Full English list (6 = MAX_ACTOR_QUERIES) must NOT truncate the
    # native terms off eures — native go first, cap trims English.
    prof_full = {"search_seeds": {"boards": {
        "keywords": ["k1", "k2", "k3", "k4", "k5", "k6"],
        "native_keywords": ["n1", "n2", "n3", "n4"],
        "native_language": "spanish",
    }}}
    qf = af.profile_source_queries(prof_full)
    assert qf["eures"] == ["n1", "n2", "n3", "n4", "k1", "k2"]
    assert qf["jobs_ac_uk"] == ["k1", "k2", "k3", "k4", "k5", "k6"]
    assert qf["infojobs"] == ["n1", "n2", "n3", "n4"]

    # native_language mismatch / absent → English everywhere (old behavior).
    prof2 = {"search_seeds": {"boards": {
        "keywords": ["interior design"],
        "native_keywords": ["architecte d'intérieur"],
        "native_language": "french",
    }}}
    q2 = af.profile_source_queries(prof2)
    assert q2["wttj"] == ["architecte d'intérieur"]
    assert q2["infojobs"] == ["interior design"]

    # native_keywords without a language tag → never routed as native.
    prof3 = {"search_seeds": {"boards": {
        "keywords": ["interior design"],
        "native_keywords": ["interiorista"],
    }}}
    q3 = af.profile_source_queries(prof3)
    assert q3["infojobs"] == ["interior design"]
    assert q3["eures"] == ["interiorista", "interior design"]


def test_fetch_all_apify_per_source_query_dict(monkeypatch):
    sess = _patch_session(monkeypatch, {
        "ycombinator-was-scraper": _ok([{"id": "1", "title": "Y", "url": "https://y/1"}]),
        "jobs-ac-uk-scraper": _ok([{"id": "2", "title": "J", "url": "https://j/2"}]),
    })
    filters = {"sources": {"ycombinator_was": True, "jobs_ac_uk": True}, "max_per_source": 5}
    jobs, errors, meta = af.fetch_all_apify(
        filters, token="T",
        queries={"ycombinator_was": ["react engineer"], "hackernews": ["ignored"]})
    assert errors == []
    by_actor = {c["url"].split("~")[1].split("/")[0]: c["json"] for c in sess.calls}
    assert by_actor["ycombinator-was-scraper"]["queries"] == ["react engineer"]
    assert "keywords" not in by_actor["jobs-ac-uk-scraper"]  # not in dict → broad
    mm = {m["source"]: m for m in meta}
    assert mm["ycombinator_was"]["queries"] == ["react engineer"]
    assert "queries" not in mm["jobs_ac_uk"]


def test_build_input_single_keyword_sources():
    qs = ["react", "frontend"]
    # Single-param source gets ONE query (dispatcher passes one per call).
    assert af.build_input("un_careers", {}, cache_ttl=0, queries=["react"])["keyword"] == "react"
    assert af.build_input("wttj", {}, cache_ttl=0, queries=qs)["query"] == "react"
    assert af.build_input("reliefweb", {}, cache_ttl=0, queries=qs)["search"] == "react"
    assert "keyword" not in af.build_input("un_careers", {}, cache_ttl=0)


def test_fetch_all_apify_single_keyword_fanout(monkeypatch):
    sess = _patch_session(monkeypatch, {
        "un-careers-scraper": _ok([{"jobId": 1, "title": "Officer", "url": "https://u/1"}]),
    })
    filters = {
        "sources": {"un_careers": True}, "max_per_source": 5,
        "apify_query_sources": ["un_careers"], "apify_query_fanout_cap": 2,
    }
    jobs, errors, meta = af.fetch_all_apify(
        filters, token="T", queries={"un_careers": ["migration", "policy", "extra"]})
    # Fan-out: 2 actor calls (cap), each with ONE keyword.
    kws = sorted(c["json"]["keyword"] for c in sess.calls)
    assert kws == ["migration", "policy"]
    # Same posting from both calls collapses by job_id → 1 job, counts honest.
    assert len(jobs) == 1
    assert len(meta) == 2 and sum(m["count"] for m in meta) == 1
    assert all(m["queries"] for m in meta)


def test_fetch_all_apify_single_keyword_gated_off(monkeypatch):
    sess = _patch_session(monkeypatch, {
        "un-careers-scraper": _ok([{"jobId": 1, "title": "Officer", "url": "https://u/1"}]),
    })
    # No apify_query_sources opt-in → single-keyword source fetches broad.
    filters = {"sources": {"un_careers": True}, "max_per_source": 5}
    jobs, errors, meta = af.fetch_all_apify(
        filters, token="T", queries={"un_careers": ["migration"]})
    assert len(sess.calls) == 1
    assert "keyword" not in sess.calls[0]["json"]
    assert "queries" not in meta[0]


# ---------------------------------------------------------------------------
# fetch_web_search_apify
# ---------------------------------------------------------------------------

_WS_SEEDS = {"seed_phrases": ["react remote europe", "site:greenhouse.io react"],
             "focus_notes": "Prefer EU remote."}


def test_web_search_apify_byok_skip(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    jobs, errs = af.fetch_web_search_apify(_WS_SEEDS, {}, token="T", anthropic_key=None, mistral_key=None)
    assert jobs == [] and errs and "BYOK" in errs[0]


def test_web_search_apify_no_seeds_no_text():
    jobs, errs = af.fetch_web_search_apify({}, {}, token="T", anthropic_key="sk-x")
    assert jobs == [] and errs == []


def test_web_search_apify_happy(monkeypatch):
    calls = []

    def fake_post(url, params=None, json=None, timeout=None):
        calls.append({"url": url, "params": params, "json": json})
        return _FakeResp([
            {"title": "React Dev", "company": "Acme", "url": "https://a/1"},
            {"title": "React Dev dup", "url": "https://a/1"},   # url dupe dropped
        ])

    monkeypatch.setattr(af.requests, "post", fake_post)
    attribution = {}
    jobs, errs = af.fetch_web_search_apify(
        _WS_SEEDS, {"remote": "require", "max_per_source": 36},
        free_text="senior roles only", token="T", anthropic_key="sk-x",
        attribution=attribution,
    )
    assert errs == [] and len(jobs) == 1
    j = jobs[0]
    assert j.source == "web_search" and j.external_id == "https://a/1"
    inp = calls[0]["json"]
    assert inp["anthropicApiKey"] == "sk-x"
    assert inp["keywords"] == _WS_SEEDS["seed_phrases"]
    assert inp["remote"] == "remote-only"
    assert inp["maxItems"] == af.WEB_SEARCH_MAX_ITEMS  # 36 capped to schema max
    assert "Prefer EU remote." in inp["userDescription"]
    assert "senior roles only" in inp["userDescription"]
    assert attribution[j.job_id] == "react remote europe"


def test_fetch_all_apify_no_token(monkeypatch):
    monkeypatch.setattr(af, "resolve_token", lambda explicit=None: None)
    jobs, errors, meta = af.fetch_all_apify({"sources": {"hackernews": True}})
    assert jobs == [] and meta == []
    assert errors and "APIFY_TOKEN" in errors[0]


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
