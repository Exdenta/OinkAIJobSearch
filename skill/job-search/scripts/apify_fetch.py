"""Apify-backed analog of ``search_jobs.fetch_all`` (experimental).

This module is the EXPERIMENT half of the "local-scrape vs Apify" comparison.
The production pipeline fetches every global source in-process via the
``sources/*.py`` adapters (HTTP/RSS scraping on the operator's box). This
module fetches the SAME logical sources by calling the project's already-
published Apify actors (one actor per source, under the ``nomad-agent`` account)
over the REST ``run-sync-get-dataset-items`` endpoint, then maps each actor's
dataset records back onto the shared ``dedupe.Job`` dataclass.

Why a separate module (not a flag inside fetch_all)
---------------------------------------------------
The production ``fetch_all`` is load-bearing for 4 live users. The experiment
must be able to run WITHOUT touching that code path, its DB cursors, or its
telemetry. So this is a standalone, side-effect-free fetcher: give it a
``filters`` dict, get back ``(jobs, errors, meta)``. The comparison CLI
(``apify_compare.py``) drives both and reports the delta.

Contract parity with ``fetch_all``
----------------------------------
``fetch_all_apify(filters)`` returns ``(list[Job], list[str], list[dict])``:
the first two match ``fetch_all``'s ``(jobs, errors)`` so the downstream
pipeline slice (dedupe → post_filter → enrich) is identical. The third
(``meta``) is experiment-only: per-source ``{source, actor, count, seconds,
error}`` rows for the side-by-side report.

Caching note (load-bearing for a FAIR comparison)
-------------------------------------------------
Each actor caches its upstream fetch in its Apify key-value store for
``cacheTtlSeconds`` (actor default ~1800s). A re-run inside that window returns
the SAME cached items — fast, but it would give whichever backend ran first a
stale-but-identical set, muddying a repeated A/B. So this module defaults
``cache_ttl=0`` (cache-BUST: every call hits live upstream). The CLI exposes
``--apify-cache-ttl`` to override. Note Apify caching returns the same *set*,
not *more* jobs — the "first run sees more" asymmetry lives on the LOCAL side
(P2 page-cursor memory), which the CLI neutralises by running against a throw-
away DB copy (and optionally ``--reset-local-cursors``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

import requests

import apify_local
from dedupe import Job

log = logging.getLogger("apify-fetch")

_JOB_FIELDS = {f.name for f in fields(Job)}


def _query_sig(dq: list[str] | None) -> str:
    """Stable cache key for a dispatch's query list — '' for broad (no-query) calls."""
    return "|".join(sorted(dq)) if dq else ""

# Apify account that owns the published actors. Actor ref in the REST path is
# "<owner>~<actor-name>". Overridable so a fork can point at its own account.
DEFAULT_ACTOR_OWNER = os.environ.get("APIFY_ACTOR_OWNER", "nomad-agent")

# Map each GLOBAL source key (the keys of search_jobs.SOURCES) to its Apify
# actor name. The naming rule is mechanical — key.replace("_", "-") + "-scraper"
# — but we spell every entry out so a missing/renamed actor is a visible diff,
# not a silently-constructed 404. Per-user sources (linkedin, web_search) are
# deliberately ABSENT: they run inside the recipient loop with profile seeds,
# not in the global fetch, so they're out of scope for this fetch-layer A/B.
#
# One local source key has NO live actor and is omitted on purpose:
#   * curated_boards     — BYOK actor not deployed under this account
#
# academicpositions + wellfound DO have actors now (proxy+browser scrapers that
# clear Cloudflare BFM / DataDome — see apify/{academicpositions,wellfound}-
# scraper/). They are EXPERIMENTAL: the anti-bot gates block local verification,
# so their selectors are unverified. They're listed here (so the wiring exists)
# but default OFF in ``defaults.DEFAULTS`` — an operator flips them on only after
# a live Apify smoke returns > 0 records. Enabling them blind would spend
# residential-proxy budget on an unverified scraper.
ACTOR_MAP: dict[str, str] = {
    "hackernews":       "hackernews-scraper",
    "remote_boards":    "remote-boards-scraper",
    "reliefweb":        "reliefweb-scraper",
    "euraxess":         "euraxess-scraper",
    "un_careers":       "un-careers-scraper",
    "math_ku_phd":      "math-ku-phd-scraper",
    "ub_doctoral":      "ub-doctoral-scraper",
    "eures":            "eures-scraper",
    "infojobs":         "infojobs-scraper",
    "tecnoempleo":      "tecnoempleo-scraper",
    "ai_jobs_net":      "ai-jobs-net-scraper",
    "jobs_ac_uk":       "jobs-ac-uk-scraper",
    "ikerbasque":       "ikerbasque-scraper",
    "ycombinator_was":  "ycombinator-was-scraper",
    "wttj":             "wttj-scraper",
    "builtin":          "builtin-scraper",
    "impactpool":       "impactpool-scraper",
    "devex":            "devex-scraper",
    "justjoinit":       "justjoinit-scraper",
    "nofluffjobs":      "nofluffjobs-scraper",
    # Experimental — proxy+browser actors, default OFF until live smoke verifies.
    "academicpositions": "academicpositions-scraper",
    "wellfound":         "wellfound-scraper",
}

# Actors that need an Anthropic key passed as input (they call Claude
# internally — "bring your own key"). Skipped unless ANTHROPIC_API_KEY is set.
# ub_doctoral left this set on 2026-07-13: it now parses UB's own vacancy board
# (seu.ub.edu) directly instead of asking an LLM, so it needs no key and must
# not be skipped when one is absent.
BYOK_SOURCES: frozenset[str] = frozenset({"devex"})

# Per-actor input schema caps that are stricter than the pipeline-wide
# ``max_per_source`` default. Keep this about transport/schema invariants, not
# matching heuristics: ReliefWeb's published actor schema rejects maxItems > 20.
SOURCE_MAX_ITEMS: dict[str, int] = {
    "reliefweb": 20,
}

# Actors whose input schema takes an ARRAY of free-text search queries — the
# actor fans them out internally, so passing per-user queries costs no extra
# actor-starts. Value = the schema field name. Each of these actors falls back
# to its OWN built-in default seed list when the field is omitted, so today's
# "broad" fetch already means "actor default seeds", not "everything" —
# swapping the defaults for user-relevant queries is free.
QUERY_ARRAY_PARAM: dict[str, str] = {
    "eures":           "keywords",
    "jobs_ac_uk":      "keywords",
    "ycombinator_was": "queries",
}

# Actors whose input schema takes ONE free-text query per run. Value = the
# schema field name. Cost model differs from QUERY_ARRAY_PARAM: feeding N
# queries means N actor-starts for that source, so this path is OPT-IN per
# source (``filters["apify_query_sources"]``) and capped by
# ``filters["apify_query_fanout_cap"]``. Left out: remote_boards (its
# ``keyword`` is a client-side title filter — would cut recall, not focus
# the upstream fetch).
QUERY_SINGLE_PARAM: dict[str, str] = {
    "un_careers": "keyword",
    "euraxess":   "keyword",
    "impactpool": "keyword",
    "infojobs":   "keyword",
    "wttj":       "query",
    "reliefweb":  "search",
    "devex":      "keyword",
}

# Query-capable actors whose queries are USELESS upstream — kept out of the
# per-user query path so QUERY_ARRAY_PARAM stays honest about what the schema
# accepts vs what actually filters. Currently empty: eures sat here 2026-07-01
# → 07-02 (loose EVERYWHERE match + MOST_RECENT sort buried every real match)
# until actor build 0.1.8 switched to BEST_MATCH + publicationPeriod=LAST_WEEK
# (verified 36/36 title-relevant).
QUERY_BROKEN: frozenset[str] = frozenset()

# Sources where reusing the user's LinkedIn queries verbatim is PROVEN fine
# (2026-07-01 A/B: ycombinator_was 0→28/36 title-relevant). Everywhere else
# LinkedIn-shaped long phrases collapse AND-matched board searches to ~0
# results (jobs_ac_uk), so those sources only get the short
# `search_seeds.boards.keywords` terms generated at profile build.
LINKEDIN_QUERY_FALLBACK_OK: frozenset[str] = frozenset({"ycombinator_was"})

# Boards indexed in their market's own language, not English — a fact about
# the board, not a heuristic about the candidate (cf. _ALLOWED_ATS). English
# `boards.keywords` starve these (2026-07-16: infojobs queried with
# "interior design" for a Bilbao interiorista returned a fraction of what
# "interiorista"/"diseñador de interiores" surfaces). When the profile
# carries `boards.native_keywords` in the board's language, those are
# dispatched INSTEAD of the English terms. eures is deliberately absent:
# it's pan-EU multi-language, so it gets English + native COMBINED below.
BOARD_NATIVE_LANG: dict[str, str] = {
    "infojobs":    "spanish",
    "tecnoempleo": "spanish",
    "wttj":        "french",
}

# Cap on queries forwarded to one actor. Each query is one upstream request
# INSIDE the actor, so this bounds actor runtime, not actor-start count.
MAX_ACTOR_QUERIES = 6


def clean_queries(queries: list | None) -> list[str]:
    """Normalize a raw query list into what an actor should receive.

    Accepts plain strings and LinkedIn paired-shape dicts (``{"q": ...}``) so
    callers can pass ``profile.search_seeds.linkedin.queries`` in either
    schema. Strips, dedupes case-insensitively, trims each to 200 chars and
    the list to ``MAX_ACTOR_QUERIES``. Returns [] for empty/malformed input
    (the actor then uses its built-in defaults).
    """
    out: list[str] = []
    seen: set[str] = set()
    for q in queries or []:
        if isinstance(q, dict):
            q = q.get("q")
        if not isinstance(q, str):
            continue
        q = q.strip()[:200]
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        out.append(q)
        if len(out) >= MAX_ACTOR_QUERIES:
            break
    return out


def profile_source_queries(profile: dict | None) -> dict[str, list[str]]:
    """Resolve a user's per-source query lists for the query-array actors.

    Primary source: ``search_seeds.boards.keywords`` — short AND-safe terms
    the profile builder generates for keyword boards. Fallback: the user's
    ``search_seeds.linkedin.queries``, but ONLY for sources in
    ``LINKEDIN_QUERY_FALLBACK_OK`` (long LinkedIn phrases kill AND-matched
    boards). ``QUERY_BROKEN`` sources are skipped entirely. Boards in
    ``BOARD_NATIVE_LANG`` get ``boards.native_keywords`` INSTEAD when the
    profile's ``native_language`` matches the board's language (eures gets
    English + native combined — it indexes both). Covers BOTH
    query shapes — array sources use the whole list per run; single-keyword
    sources are additionally gated at dispatch time by
    ``filters["apify_query_sources"]`` + fan-out cap (cost). Returns
    ``{source_key: [query, ...]}`` — empty dict means fetch broad everywhere
    (older profiles without a boards block and no safe fallback).
    """
    seeds = ((profile or {}).get("search_seeds") or {})
    boards = seeds.get("boards")
    board_kws = clean_queries(boards.get("keywords") if isinstance(boards, dict) else None)
    native_kws = clean_queries(boards.get("native_keywords") if isinstance(boards, dict) else None)
    native_lang = (
        str(boards.get("native_language") or "").strip().lower()
        if isinstance(boards, dict) else ""
    )
    li = seeds.get("linkedin")
    li_raw = li if isinstance(li, list) else (li.get("queries") if isinstance(li, dict) else None)
    li_queries = clean_queries(li_raw)

    out: dict[str, list[str]] = {}
    for key in (*QUERY_ARRAY_PARAM, *QUERY_SINGLE_PARAM):
        if key in QUERY_BROKEN:
            continue
        qs = board_kws or (li_queries if key in LINKEDIN_QUERY_FALLBACK_OK else [])
        if native_kws:
            if BOARD_NATIVE_LANG.get(key) == native_lang:
                # Native-indexed board in the profile's native language:
                # the English terms would starve it — replace outright.
                qs = native_kws
            elif key == "eures":
                # Pan-EU multi-language board: both vocabularies match
                # real postings; combine. Native FIRST — clean_queries
                # caps at MAX_ACTOR_QUERIES keeping list order, and a
                # full English list (prompt allows up to 6) would
                # otherwise truncate every native term off the end.
                qs = clean_queries([*native_kws, *qs])
        if qs:
            out[key] = qs
    return out

API_BASE = "https://api.apify.com/v2/acts"

_ACTOR_HTTP_ATTEMPTS = 2
_ACTOR_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_ACTOR_RETRY_BACKOFF_S = 1.0


def _max_items_for_source(key: str, filters: dict) -> int:
    try:
        max_items = int(filters.get("max_per_source") or 36)
    except (TypeError, ValueError):
        max_items = 36
    cap = SOURCE_MAX_ITEMS.get(key)
    if cap is not None:
        max_items = min(max_items, cap)
    return max_items


def _retryable_actor_response(status_code: int, body: str) -> bool:
    if status_code in _ACTOR_RETRYABLE_STATUS:
        return True
    # Apify's run-sync endpoint wraps actor run failures in HTTP 400. Do not
    # retry validation errors; only retry actor execution failures that are
    # commonly transient (proxy/IP challenge, run timeout, platform blip).
    low = (body or "").lower()
    return (
        status_code == 400
        and (
            "run-timeout-exceeded" in low
            or ("run-failed" in low and ("timed-out" in low or "failed" in low))
        )
    )


class _LocalRunResult:
    """Duck-typed stand-in for the requests.Response of a successful
    ``run-sync-get-dataset-items`` call — callers only use ``.json()``."""

    def __init__(self, records: list[dict]):
        self._records = records

    def json(self) -> list[dict]:
        return self._records


def _post_actor_run(
    http: Any,
    url: str,
    *,
    params: dict[str, Any],
    payload: dict[str, Any],
    timeout: int,
    label: str,
):
    # APIFY_RUN_MODE=local: run the actor's own source tree as a local
    # subprocess instead of paying for platform compute. Fail-open — any local
    # failure falls through to the normal platform call below.
    if apify_local.enabled():
        actor = apify_local.actor_from_url(url)
        if actor and actor not in apify_local.LOCAL_DENY:
            records, lerr = apify_local.run_actor_local(
                actor, payload,
                run_timeout=int(params.get("timeout") or 300),
                max_items=int(params.get("maxItems") or 0),
            )
            if lerr is None:
                return _LocalRunResult(records), None
            log.warning("apify-local %s failed (%s); falling back to platform",
                        label, lerr)

    last_error: str | None = None
    for attempt in range(1, _ACTOR_HTTP_ATTEMPTS + 1):
        try:
            resp = http.post(url, params=params, json=payload, timeout=timeout)
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:200]}"
            retryable = True
        else:
            if resp.status_code < 400:
                return resp, None
            body = resp.text or ""
            last_error = f"HTTP {resp.status_code}: {body[:200]}"
            retryable = _retryable_actor_response(resp.status_code, body)

        if attempt < _ACTOR_HTTP_ATTEMPTS and retryable:
            log.warning(
                "apify %s attempt %d/%d failed: %s; retrying",
                label, attempt, _ACTOR_HTTP_ATTEMPTS, last_error,
            )
            time.sleep(_ACTOR_RETRY_BACKOFF_S)
            continue
        return None, last_error
    return None, last_error


# ---------------------------------------------------------------------------
# Token + input
# ---------------------------------------------------------------------------

def _file_fallback_enabled() -> bool:
    """Whether to read the Apify token from a bundled actor ``.env`` file.

    OFF by default. This fallback is a convenience for LOCAL actor development
    (the publish scripts keep the token in ``apify/<actor>/.env``). It is
    DANGEROUS in any shipped/forked checkout: it could silently pick up a
    bundled token, or mask a missing ``APIFY_TOKEN`` instead of failing loudly.
    A self-hoster must set their OWN ``APIFY_TOKEN`` — so the public default is
    env-var-only. Opt in with ``APIFY_TOKEN_FILE_FALLBACK=1`` for actor-dev.
    """
    return (os.environ.get("APIFY_TOKEN_FILE_FALLBACK") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def resolve_token(explicit: str | None = None) -> str | None:
    """Resolve the Apify API token.

    Order: explicit arg → ``APIFY_TOKEN`` env → (opt-in only) the actors' local
    ``apify/un-careers-scraper/.env``. The file fallback is gated behind
    ``APIFY_TOKEN_FILE_FALLBACK=1`` (see ``_file_fallback_enabled``) so a public
    or forked checkout never reads a bundled token. Returns None when nothing is
    found (the caller decides whether that's fatal).
    """
    if explicit:
        return explicit.strip()
    env = (os.environ.get("APIFY_TOKEN") or "").strip()
    if env:
        return env
    if not _file_fallback_enabled():
        return None
    # Opt-in dev fallback: read the file the apify/ publish scripts source.
    here = Path(__file__).resolve()
    # repo root = .../FindJobs ; scripts live at skill/job-search/scripts
    for root in here.parents:
        cand = root / "apify" / "un-careers-scraper" / ".env"
        if cand.exists():
            try:
                for line in cand.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("APIFY_TOKEN=") and "=" in line:
                        return line.split("=", 1)[1].strip()
            except Exception:
                pass
            break
    return None


def build_input(
    key: str,
    filters: dict,
    *,
    cache_ttl: int,
    anthropic_key: str | None = None,
    mistral_key: str | None = None,
    queries: list[str] | None = None,
) -> dict[str, Any]:
    """Build the actor input for one source.

    Default parity choice: the LOCAL global adapters fetch BROAD (no per-user
    keyword — the LLM scorer is the sole matching gate downstream), so with no
    ``queries`` we mirror that and pass NO keyword/query. Only the universal
    knobs are set:

      * ``maxItems``        — capped at ``filters['max_per_source']``.
      * ``cacheTtlSeconds`` — 0 to cache-bust (see module docstring).

    ``queries`` opt-in: for sources in ``QUERY_ARRAY_PARAM`` a cleaned query
    list is forwarded under the actor's array field (one actor call fans them
    out internally — same actor-start cost as broad). Ignored for every other
    source, so a caller can pass one list unconditionally.

    Extra input fields the actor's schema doesn't declare are ignored by the
    platform (schemas here don't set ``additionalProperties:false``), so a
    universal input is safe across actors with differing schemas. BYOK actors
    additionally get ``anthropicApiKey`` when a key is available, or —
    project preference is Mistral over Anthropic (see repo CLAUDE.md) —
    ``provider="mistral"`` + ``mistralApiKey`` when only a Mistral key is
    available (the actor's own ``_resolve_provider`` also infers "mistral"
    from a mistralApiKey alone, but we set ``provider`` explicitly so intent
    isn't left to inference). Anthropic wins if both happen to be set, since
    ``anthropicApiKey`` was the actor's original/default provider.
    """
    max_items = _max_items_for_source(key, filters)
    inp: dict[str, Any] = {
        "maxItems": max_items,
        "cacheTtlSeconds": int(cache_ttl),
    }
    if key in QUERY_ARRAY_PARAM:
        cleaned = clean_queries(queries)
        if cleaned:
            inp[QUERY_ARRAY_PARAM[key]] = cleaned
    elif key in QUERY_SINGLE_PARAM:
        cleaned = clean_queries(queries)
        if cleaned:
            # One query per actor run — the dispatcher fans out N runs and
            # passes a single-element list per call.
            inp[QUERY_SINGLE_PARAM[key]] = cleaned[0]
    if key in BYOK_SOURCES:
        if anthropic_key:
            inp["anthropicApiKey"] = anthropic_key
        elif mistral_key:
            inp["provider"] = "mistral"
            inp["mistralApiKey"] = mistral_key
    return inp


# ---------------------------------------------------------------------------
# Record → Job
# ---------------------------------------------------------------------------

def record_to_job(key: str, rec: dict) -> Job | None:
    """Map one actor dataset record onto a ``Job``.

    The actors share a clean schema (``id/title/company/location/url/postedAt/
    snippet``) but a few use legacy field names (un-careers emits ``jobId`` /
    ``description``). We resolve each field through a small fallback chain so
    one mapper covers every actor. Records with neither a url nor a title are
    dropped (nothing downstream can key or score them).
    """
    if not isinstance(rec, dict):
        return None
    url = (rec.get("url") or rec.get("link") or "").strip()
    title = (rec.get("title") or rec.get("jobTitle") or "").strip()
    if not url and not title:
        return None

    ext = (
        rec.get("id")
        or rec.get("jobId")
        or rec.get("external_id")
        or rec.get("slug")
        or rec.get("threadId")
    )
    if ext in (None, ""):
        # Stable synthetic id from the url so dedupe/seen keys are deterministic
        # across re-runs (Job.job_id hashes source::external_id|url anyway, but
        # an explicit id keeps the two backends' keys aligned when both carry a
        # url).
        ext = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] if url else title[:64]

    return Job(
        source=key,
        external_id=str(ext),
        title=title,
        company=(rec.get("company") or rec.get("organization") or "").strip(),
        location=(rec.get("location") or "").strip(),
        url=url,
        posted_at=str(rec.get("postedAt") or rec.get("posted_at") or rec.get("date") or ""),
        snippet=(rec.get("snippet") or rec.get("description") or rec.get("summary") or ""),
        salary=str(rec.get("salary") or rec.get("salaryText") or ""),
    )


# ---------------------------------------------------------------------------
# One actor call
# ---------------------------------------------------------------------------

def call_actor(
    key: str,
    filters: dict,
    *,
    token: str,
    owner: str,
    cache_ttl: int,
    run_timeout: int,
    anthropic_key: str | None,
    mistral_key: str | None = None,
    session: requests.Session | None = None,
    queries: list[str] | None = None,
) -> tuple[str, list[Job], str | None, float]:
    """Run ONE actor synchronously and return ``(key, jobs, err, seconds)``.

    Uses ``run-sync-get-dataset-items`` so a single HTTP call both runs the
    actor and returns its dataset (a JSON array of records). ``err`` is None on
    success, a short string on failure. Never raises — failures are returned so
    the orchestrator can keep going, exactly like ``_fetch_one_source``.
    """
    actor = ACTOR_MAP[key]
    inp = build_input(key, filters, cache_ttl=cache_ttl, anthropic_key=anthropic_key,
                      mistral_key=mistral_key, queries=queries)
    max_items = _max_items_for_source(key, filters)

    url = f"{API_BASE}/{owner}~{actor}/run-sync-get-dataset-items"
    params = {
        "token": token,
        "timeout": run_timeout,   # cap the actor run server-side
        "maxItems": max_items,    # hard cap on returned dataset items
    }
    http = session or requests
    started = time.time()
    resp, err = _post_actor_run(
        http, url, params=params, payload=inp,
        timeout=run_timeout + 30, label=f"{key} ({actor})",
    )
    if err:
        return key, [], err, time.time() - started
    try:
        data = resp.json()
    except Exception as e:
        return key, [], f"{type(e).__name__}: {str(e)[:200]}", time.time() - started

    elapsed = time.time() - started
    if not isinstance(data, list):
        return key, [], "actor returned non-array body", elapsed

    jobs: list[Job] = []
    for rec in data:
        job = record_to_job(key, rec)
        if job is not None:
            jobs.append(job)
    log.info("  apify %s (%s) → %d records in %.1fs", key, actor, len(jobs), elapsed)
    return key, jobs, None, elapsed


# ---------------------------------------------------------------------------
# fetch_all_apify
# ---------------------------------------------------------------------------

def enabled_apify_sources(filters: dict) -> list[str]:
    """Return the source keys that are (a) enabled in ``filters['sources']``
    AND (b) have a live actor in ``ACTOR_MAP``. Mirrors the production
    ``fetch_all`` enable-gate so the A/B compares the SAME source set.
    """
    enabled = filters.get("sources") or {}
    return [k for k in ACTOR_MAP if enabled.get(k, False)]


# Apify billing signals in an error string. When the account's prepaid monthly
# usage credit is spent — or a configured ``maxMonthlyUsageUsd`` hard limit is
# hit — the platform rejects run-sync calls with HTTP 402 and a usage-limit
# error type. The block is ACCOUNT-level, so one such error means every actor
# run is dead for the rest of the billing cycle. Matching the wire signal by
# string is a transport/billing INVARIANT (cf. the ATS allowlist in
# profile_builder), not a matching heuristic — a hardcoded marker set is the
# right layer here, and the repo's "prefer prompts over hardcoded logic" rule
# explicitly exempts HTTP transport invariants.
_CREDIT_EXHAUSTED_MARKERS = (
    "http 402",
    "payment required",
    "monthly-usage-hard-limit",
    "monthly usage hard limit",
    "usage-hard-limit-exceeded",
    "insufficient-balance",
    "not enough usage",
)


def credit_exhausted(errors: list[str] | None) -> bool:
    """True if any Apify error string signals exhausted usage credit / a hit
    monthly-usage hard limit (HTTP 402 or a usage-limit error type).

    Account-level: a single such error means the whole Apify fetch is blocked
    for this billing cycle, so the caller should fall back to the local
    scrapers for the rest of the run.
    """
    for e in errors or []:
        low = str(e).lower()
        if any(m in low for m in _CREDIT_EXHAUSTED_MARKERS):
            return True
    return False


def fetch_all_apify(
    filters: dict,
    *,
    token: str | None = None,
    owner: str | None = None,
    cache_ttl: int = 0,
    run_timeout: int = 600,
    workers: int = 6,
    only: list[str] | None = None,
    anthropic_key: str | None = None,
    mistral_key: str | None = None,
    queries: list[str] | dict[str, list] | None = None,
    db: Any = None,
    result_cache_s: int = 0,
) -> tuple[list[Job], list[str], list[dict]]:
    """Apify-backed ``fetch_all``. Returns ``(jobs, errors, meta)``.

    * ``jobs``   — flat ``list[Job]`` across all enabled actors.
    * ``errors`` — ``"<key>: <reason>"`` strings (parity with fetch_all).
    * ``meta``   — per-source ``{source, actor, count, seconds, error,
                   skipped}`` rows for the comparison report (plus
                   ``queries`` for QUERY_ARRAY_PARAM sources when set).

    ``only`` restricts to a subset of source keys (intersected with the
    enable-gate). ``cache_ttl=0`` cache-busts (that flag controls the
    ACTOR's own internal cache; see the module docstring). BYOK actors are
    skipped (and recorded as ``skipped``) unless an Anthropic key is
    available. ``queries`` (opt-in) is forwarded to QUERY_ARRAY_PARAM
    sources only, replacing those actors' built-in default seed lists;
    everything else stays broad. Accepts a flat list (applied to every
    query-array source) or a per-source dict —
    ``profile_source_queries(profile)`` builds the latter with the
    boards/linkedin fallback rules.

    ``db`` + ``result_cache_s`` (opt-in, both default off): a CLIENT-side
    result cache on top of the actor's own cache, needed because separate
    ``fetch_all_apify`` calls (different users' staggered continuous-
    searcher cycles, manual /checknow triggers, ...) are separate Apify
    runs with separate storage — the actor's own cache can't see across
    them. When set, a dispatch whose ``(source, query-signature)`` was
    fetched within ``result_cache_s`` reuses that stored job list and
    skips the actor call (and the live hit on the upstream site) entirely
    for this cycle; otherwise it fetches live and stores the result for
    the next caller. Leave ``db=None`` (the default) for callers that need
    every call to hit live upstream — e.g. ``apify_compare.py``'s A/B
    report, where a cache hit would bias the comparison.
    """
    token = resolve_token(token)
    if not token:
        return [], ["apify: no APIFY_TOKEN (env or apify/un-careers-scraper/.env)"], []
    owner = owner or DEFAULT_ACTOR_OWNER
    anthropic_key = anthropic_key or (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None
    # Project preference is Mistral over Anthropic (see repo CLAUDE.md / the
    # project's mistral-routing-decisions note) — the bot's own env carries
    # MISTRAL_API_KEY, not ANTHROPIC_API_KEY, so without this fallback these
    # BYOK actors were skipped on every run. All three support
    # provider="mistral" + mistralApiKey (see build_input).
    mistral_key = mistral_key or (os.environ.get("MISTRAL_API_KEY") or "").strip() or None

    keys = enabled_apify_sources(filters)
    if only:
        only_set = set(only)
        keys = [k for k in keys if k in only_set]

    jobs: list[Job] = []
    errors: list[str] = []
    meta: list[dict] = []
    to_run: list[str] = []

    for key in keys:
        if key in BYOK_SOURCES and not anthropic_key and not mistral_key:
            meta.append({
                "source": key, "actor": ACTOR_MAP[key], "count": 0,
                "seconds": 0.0, "error": None,
                "skipped": "BYOK: no ANTHROPIC_API_KEY or MISTRAL_API_KEY",
            })
            continue
        to_run.append(key)

    if not to_run:
        return jobs, errors, meta

    # Normalize queries to a per-source map: flat list applies to every
    # query-capable source; dict is already per-source (unknown keys dropped).
    query_capable = set(QUERY_ARRAY_PARAM) | set(QUERY_SINGLE_PARAM)
    if isinstance(queries, dict):
        qmap = {k: clean_queries(v) for k, v in queries.items() if k in query_capable}
        qmap = {k: v for k, v in qmap.items() if v}
    else:
        flat = clean_queries(queries)
        qmap = {k: flat for k in query_capable} if flat else {}

    # Build the dispatch list. Most sources = ONE broad call. Query-array
    # sources with queries = ONE call carrying the whole list (the actor
    # fans out internally, no extra cost). Single-keyword sources = one
    # call PER query — an actor-start each — so they're gated on the
    # per-source opt-in ``filters["apify_query_sources"]`` and capped by
    # ``filters["apify_query_fanout_cap"]``.
    single_enabled = {str(s) for s in (filters.get("apify_query_sources") or [])}
    try:
        fanout_cap = max(1, int(filters.get("apify_query_fanout_cap") or 2))
    except (TypeError, ValueError):
        fanout_cap = 2

    dispatches: list[tuple[str, list[str] | None]] = []
    for key in to_run:
        if key in qmap and key in QUERY_SINGLE_PARAM and key in single_enabled:
            for q in qmap[key][:fanout_cap]:
                dispatches.append((key, [q]))
        elif key in qmap and key in QUERY_ARRAY_PARAM:
            dispatches.append((key, qmap[key]))
        else:
            dispatches.append((key, None))

    # Split into cache hits (resolved locally, no actor call) and dispatches
    # that still need a live run. A hit only fires when the caller opted in
    # (db + result_cache_s>0) — see the docstring for why apify_compare.py
    # must NOT opt in.
    to_dispatch: list[tuple[str, list[str] | None]] = []
    cache_hits = 0
    seen_job_ids: set[str] = set()

    def _use_jobs(key: str, dq: list[str] | None, src_jobs: list[Job],
                   err: str | None, seconds: float, cached: bool) -> None:
        qlabel = f"[{dq[0]!r}]" if dq and key in QUERY_SINGLE_PARAM else ""
        if err:
            errors.append(f"{key}{qlabel}: {err}")
        fresh = []
        for j in src_jobs:
            if j.job_id in seen_job_ids:
                continue
            seen_job_ids.add(j.job_id)
            fresh.append(j)
        jobs.extend(fresh)
        row = {
            "source": key, "actor": ACTOR_MAP[key], "count": len(fresh),
            "seconds": round(seconds, 2), "error": err, "skipped": None,
        }
        if dq:
            row["queries"] = dq
        if cached:
            row["cached"] = True
        meta.append(row)

    if db is not None and result_cache_s > 0:
        for key, dq in dispatches:
            cached = None
            try:
                cached = db.recent_fetch_jobs(key, _query_sig(dq), "", result_cache_s)
            except Exception:
                log.debug("apify-fetch: cache lookup failed for %s", key, exc_info=True)
            if cached is None:
                to_dispatch.append((key, dq))
                continue
            cache_hits += 1
            src_jobs = []
            for d in cached:
                if not isinstance(d, dict):
                    continue
                try:
                    src_jobs.append(Job(**{k: v for k, v in d.items() if k in _JOB_FIELDS}))
                except TypeError:
                    continue
            _use_jobs(key, dq, src_jobs, None, 0.0, cached=True)
    else:
        to_dispatch = list(dispatches)

    log.info("apify fetch: dispatching %d actor runs (%d sources) across %d workers "
             "(cache_ttl=%ds, %d cache hit(s)%s)",
             len(to_dispatch), len(to_run), workers, cache_ttl, cache_hits,
             f", user queries for {sorted(k for k, q in dispatches if q)}"
             if any(q for _, q in dispatches) else "")

    # One shared session for connection reuse across the pool.
    if to_dispatch:
        with requests.Session() as session:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                futs = {
                    ex.submit(
                        call_actor, key, filters,
                        token=token, owner=owner, cache_ttl=cache_ttl,
                        run_timeout=run_timeout, anthropic_key=anthropic_key,
                        mistral_key=mistral_key,
                        session=session, queries=dq,
                    ): (key, dq)
                    for key, dq in to_dispatch
                }
                for fut in as_completed(futs):
                    key, dq = futs[fut]
                    qlabel = f"[{dq[0]!r}]" if dq and key in QUERY_SINGLE_PARAM else ""
                    try:
                        k, src_jobs, err, seconds = fut.result()
                    except Exception as e:  # last-resort guard
                        errors.append(f"{key}{qlabel}: {type(e).__name__}: {str(e)[:160]}")
                        meta.append({
                            "source": key, "actor": ACTOR_MAP[key], "count": 0,
                            "seconds": 0.0, "error": str(e)[:160], "skipped": None,
                        })
                        continue
                    before = len(jobs)
                    _use_jobs(k, dq, src_jobs, err, seconds, cached=False)
                    if db is not None and result_cache_s > 0 and not err:
                        fresh = jobs[before:]
                        try:
                            existing = db.count_existing_jobs([j.job_id for j in fresh])
                            db.record_fetch(
                                k, _query_sig(dq), 0, "",
                                jobs_seen=len(fresh),
                                jobs_new=max(0, len(fresh) - existing),
                                jobs_json=json.dumps([asdict(j) for j in fresh]),
                            )
                        except Exception:
                            log.debug("apify-fetch: cache write failed for %s", k, exc_info=True)

    meta.sort(key=lambda m: m["source"])
    log.info("apify fetch: %d total records across %d actors (%d errors, %d cached)",
             len(jobs), len(to_run), len(errors), cache_hits)
    return jobs, errors, meta


# ---------------------------------------------------------------------------
# Per-user LinkedIn via the published linkedin-scraper actor
# ---------------------------------------------------------------------------
#
# Unlike the 20 GLOBAL actors above (one broad run per source, NO keyword),
# LinkedIn is a PER-USER source: it runs the user's profile seed queries
# (queries × geos) through the published ``<owner>~linkedin-scraper`` actor,
# one actor run per (query, geo) combo. The actor walks its own 8-page sequence
# and de-dupes cross-geo internally, so there is NO DB cursor here — the
# ``apify`` backend trades P2 incremental page-walking for a stateless fetch,
# exactly like the global apify path drops the cursor. This is the Apify analog
# of ``sources.linkedin.fetch_for_user``.

LINKEDIN_ACTOR = "linkedin-scraper"

# Per-(query,geo) posting cap. Mirrors ``sources.linkedin.PER_QUERY_CAP`` so raw
# LinkedIn intake is the same whichever backend runs — the LLM scorer downstream
# is the sole matching gate either way.
LINKEDIN_PER_QUERY_CAP = 72


def _linkedin_record_to_job(rec: dict) -> Job | None:
    """Map one linkedin-scraper dataset record onto a ``Job``.

    Differs from ``record_to_job`` in ONE way: ``external_id`` is pinned to the
    posting URL (not a synthetic sha1), matching how the in-process
    ``sources.linkedin`` adapter keys its jobs. That keeps the dedupe/seen keys
    identical across the two backends, so a user switching from local to apify
    doesn't re-receive postings already sent under the other backend.
    """
    if not isinstance(rec, dict):
        return None
    url = (rec.get("url") or rec.get("link") or "").strip()
    title = (rec.get("title") or rec.get("jobTitle") or "").strip()
    if not url and not title:
        return None
    return Job(
        source="linkedin",
        external_id=url or title[:64],
        title=title,
        company=(rec.get("company") or "").strip(),
        location=(rec.get("location") or "").strip(),
        url=url,
        posted_at=str(rec.get("postedAt") or rec.get("posted_at") or ""),
        snippet=(rec.get("description") or rec.get("snippet") or "").strip(),
        salary=str(rec.get("salary") or ""),
    )


def _call_linkedin_actor(
    combo: dict,
    *,
    remote: bool,
    per_query_cap: int,
    token: str,
    owner: str,
    cache_ttl: int,
    run_timeout: int,
    session: requests.Session | None = None,
) -> tuple[dict, list[Job], str | None, float]:
    """Run the linkedin-scraper actor for ONE (query, geo) combo.

    Returns ``(combo, jobs, err, seconds)`` — ``err`` None on success, a short
    string on failure. Never raises: failures come back so the pool keeps going.
    """
    inp = {
        "keyword": combo.get("q") or "",
        "location": combo.get("geo") or "",
        "remote": bool(remote),
        "timeFilter": combo.get("f_TPR") or "r86400",
        "maxItems": int(per_query_cap),
        "includeDescription": True,
        "cacheTtlSeconds": int(cache_ttl),
    }
    url = f"{API_BASE}/{owner}~{LINKEDIN_ACTOR}/run-sync-get-dataset-items"
    params = {"token": token, "timeout": run_timeout, "maxItems": int(per_query_cap)}
    http = session or requests
    started = time.time()
    resp, err = _post_actor_run(
        http, url, params=params, payload=inp,
        timeout=run_timeout + 30,
        label=f"linkedin[{combo.get('q')!r}@{combo.get('geo')!r}]",
    )
    if err:
        return combo, [], err, time.time() - started
    try:
        data = resp.json()
    except Exception as e:
        return combo, [], f"{type(e).__name__}: {str(e)[:200]}", time.time() - started
    elapsed = time.time() - started
    if not isinstance(data, list):
        return combo, [], "actor returned non-array body", elapsed
    jobs = [j for j in (_linkedin_record_to_job(r) for r in data) if j is not None]
    log.info("  apify linkedin q=%r geo=%r → %d records in %.1fs",
             combo.get("q"), combo.get("geo"), len(jobs), elapsed)
    return combo, jobs, None, elapsed


def _linkedin_cache_query(combo: dict) -> str:
    """Cache key's query part for a (query, geo) combo — includes the time
    filter since 'r86400' vs 'r604800' aren't the same search."""
    return f"{combo.get('q') or ''}::{combo.get('f_TPR') or 'r86400'}"


def fetch_linkedin_apify(
    seeds: dict | None,
    filters: dict,
    *,
    token: str | None = None,
    owner: str | None = None,
    cache_ttl: int = 0,
    run_timeout: int = 600,
    workers: int = 4,
    per_query_cap: int | None = None,
    attribution: dict | None = None,
    db: Any = None,
    result_cache_s: int = 0,
) -> tuple[list[Job], list[str]]:
    """Per-user LinkedIn fetch via the published ``linkedin-scraper`` actor.

    The Apify analog of ``sources.linkedin.fetch_for_user``: flattens the user's
    ``search_seeds.linkedin`` into (query, geo) dispatches (reusing the SAME
    ``_flatten_user_seeds`` logic the local adapter uses, so the two backends
    run identical query sets), runs each combo through the actor concurrently,
    then applies the same cross-geo requisition de-dupe. Returns
    ``(jobs, errors)`` — the caller logs ``errors`` and folds ``jobs`` into the
    user pool exactly as it does the local adapter's output.

    ``attribution`` (side-channel): when a mutable dict is passed, each returned
    job's ``job_id`` is mapped to its originating query string (first-combo-wins;
    parity with the local adapter's per-query attribution, modulo actor
    completion order).

    ``db``/``result_cache_s`` (opt-in, same contract as ``fetch_all_apify``):
    the cache key is ``(source="linkedin", query, geo)`` — NOT user-scoped, so
    two different users searching the same (query, geo) combo (common —
    "react developer" @ "European Union" shows up across many profiles) share
    a hit. That's the whole point: LinkedIn is the single biggest source of
    live-upstream traffic (10+ actor-starts per user cycle), so cross-user
    reuse here matters more than on the global sources.
    """
    # Lazy import: sources.linkedin pulls bs4/forensic/text_utils, which we
    # don't want at module import time for the global-fetch path.
    from sources.linkedin import _dedupe_cross_geo, _flatten_user_seeds

    combos = _flatten_user_seeds(seeds)
    if not combos:
        return [], []

    token = resolve_token(token)
    if not token:
        return [], ["linkedin-apify: no APIFY_TOKEN (env APIFY_TOKEN)"]
    owner = owner or DEFAULT_ACTOR_OWNER
    remote = str(filters.get("remote") or "").lower() in ("require", "remote")
    cap = int(per_query_cap or LINKEDIN_PER_QUERY_CAP)

    jobs: list[Job] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    cache_hits = 0

    def _fold(rcombo: dict, src_jobs: list[Job]) -> None:
        for j in src_jobs:
            if j.url and j.url in seen_urls:
                continue
            if j.url:
                seen_urls.add(j.url)
            jobs.append(j)
            if attribution is not None:
                try:
                    attribution.setdefault(j.job_id, rcombo.get("q") or "")
                except Exception:
                    pass

    to_dispatch: list[dict] = []
    if db is not None and result_cache_s > 0:
        for combo in combos:
            cached = None
            try:
                cached = db.recent_fetch_jobs(
                    "linkedin", _linkedin_cache_query(combo),
                    combo.get("geo") or "", result_cache_s,
                )
            except Exception:
                log.debug("apify linkedin: cache lookup failed for %r", combo, exc_info=True)
            if cached is None:
                to_dispatch.append(combo)
                continue
            cache_hits += 1
            src_jobs = []
            for d in cached:
                if not isinstance(d, dict):
                    continue
                try:
                    src_jobs.append(Job(**{k: v for k, v in d.items() if k in _JOB_FIELDS}))
                except TypeError:
                    continue
            _fold(combo, src_jobs)
    else:
        to_dispatch = list(combos)

    log.info("apify linkedin: dispatching %d (query,geo) combos across %d workers (%d cache hit(s))",
             len(to_dispatch), workers, cache_hits)

    if to_dispatch:
        with requests.Session() as session:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                futs = {
                    ex.submit(
                        _call_linkedin_actor, combo,
                        remote=remote, per_query_cap=cap, token=token, owner=owner,
                        cache_ttl=cache_ttl, run_timeout=run_timeout, session=session,
                    ): combo
                    for combo in to_dispatch
                }
                for fut in as_completed(futs):
                    combo = futs[fut]
                    try:
                        rcombo, src_jobs, err, _sec = fut.result()
                    except Exception as e:  # last-resort guard
                        errors.append(f"linkedin[{combo.get('q')!r}]: {type(e).__name__}: {str(e)[:160]}")
                        continue
                    if err:
                        errors.append(f"linkedin[{rcombo.get('q')!r}@{rcombo.get('geo')!r}]: {err}")
                    _fold(rcombo, src_jobs)
                    if db is not None and result_cache_s > 0 and not err:
                        try:
                            existing = db.count_existing_jobs([j.job_id for j in src_jobs])
                            db.record_fetch(
                                "linkedin", _linkedin_cache_query(rcombo), 0,
                                rcombo.get("geo") or "",
                                jobs_seen=len(src_jobs),
                                jobs_new=max(0, len(src_jobs) - existing),
                                jobs_json=json.dumps([asdict(j) for j in src_jobs]),
                            )
                        except Exception:
                            log.debug("apify linkedin: cache write failed for %r", rcombo, exc_info=True)

    # Cross-geo requisition dedupe (same role, different country page), matching
    # the local adapter's final pass.
    if jobs:
        jobs = _dedupe_cross_geo(jobs)

    log.info("apify linkedin: %d combos → %d jobs (%d errors, %d cached)",
             len(combos), len(jobs), len(errors), cache_hits)
    return jobs, errors


# ---------------------------------------------------------------------------
# Per-user web_search via the published web-search-scraper actor (BYOK)
# ---------------------------------------------------------------------------
#
# Apify analog of ``sources.web_search.fetch``. The actor runs a Claude
# web-searching agent (BYOK — the caller's ANTHROPIC_API_KEY goes into the
# actor input), so like the local adapter it is agentic and stateless: one
# actor run per user cycle, no DB cursor. Seed mapping:
#   keywords        ← search_seeds.web_search.seed_phrases (agent treats each
#                     as a search hint; site:-operator phrases are fine — the
#                     agent forms its own queries around them)
#   userDescription ← focus_notes + the user's free-text prefs
#   remote          ← filters["remote"] ("require" → "remote-only")

WEB_SEARCH_ACTOR = "web-search-scraper"

# Schema hard cap on the actor's maxItems.
WEB_SEARCH_MAX_ITEMS = 30


def _web_search_record_to_job(rec: dict) -> Job | None:
    """Map one web-search-scraper record onto a ``Job``. ``external_id`` is
    pinned to the URL (parity with the local web_search adapter's keying), so
    seen/dedupe state survives a backend switch."""
    if not isinstance(rec, dict):
        return None
    url = (rec.get("url") or rec.get("link") or "").strip()
    title = (rec.get("title") or rec.get("jobTitle") or "").strip()
    if not url and not title:
        return None
    return Job(
        source="web_search",
        external_id=url or title[:64],
        title=title,
        company=(rec.get("company") or "").strip(),
        location=(rec.get("location") or "").strip(),
        url=url,
        posted_at=str(rec.get("postedAt") or rec.get("posted_at") or ""),
        snippet=(rec.get("snippet") or rec.get("description") or "").strip(),
        salary=str(rec.get("salary") or ""),
    )


def fetch_web_search_apify(
    seeds: dict | None,
    filters: dict,
    *,
    free_text: str | None = None,
    token: str | None = None,
    owner: str | None = None,
    anthropic_key: str | None = None,
    mistral_key: str | None = None,
    cache_ttl: int = 0,
    run_timeout: int | None = None,
    attribution: dict | None = None,
    db: Any = None,
    result_cache_s: int = 0,
) -> tuple[list[Job], list[str]]:
    """Per-user web_search fetch via the ``web-search-scraper`` actor.

    Returns ``(jobs, errors)`` like ``fetch_linkedin_apify``. BYOK: without
    EITHER an Anthropic key or a Mistral key the fetch is SKIPPED with a
    logged error string (parity with how ``BYOK_SOURCES`` are skipped in the
    global fetch) — the caller treats it like any other per-source error.
    Project preference is Mistral over Anthropic (see repo CLAUDE.md); the
    actor accepts ``provider="mistral"`` + ``mistralApiKey`` and the bot's
    own env carries ``MISTRAL_API_KEY`` (not an Anthropic key), so the
    Mistral fallback is what actually lets this source run in practice.
    ``attribution`` maps each job_id to the seed phrase list head, matching
    the local adapter's query attribution granularity as closely as a
    single agentic run allows.

    ``db``/``result_cache_s`` (opt-in, same contract as ``fetch_all_apify``):
    unlike LinkedIn's literal (query, geo), this actor is AGENTIC — its
    ``userDescription`` (focus_notes + free text) steers what the Claude
    agent searches for, so two users with different context can legitimately
    get different results for the "same" keywords. The cache key folds in a
    hash of that description, so a hit only replays for a near-identical
    profile (in practice: the same user re-running soon, or two users who
    happen to share both keywords and description) — safe by construction,
    just a lower hit rate than LinkedIn's.
    """
    seeds = seeds if isinstance(seeds, dict) else {}
    phrases = [p.strip() for p in (seeds.get("seed_phrases") or [])
               if isinstance(p, str) and p.strip()]
    if not phrases and not (free_text or "").strip():
        return [], []

    token = resolve_token(token)
    if not token:
        return [], ["web_search-apify: no APIFY_TOKEN (env APIFY_TOKEN)"]
    anthropic_key = anthropic_key or (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None
    mistral_key = mistral_key or (os.environ.get("MISTRAL_API_KEY") or "").strip() or None
    if not anthropic_key and not mistral_key:
        return [], ["web_search-apify: skipped (BYOK: no ANTHROPIC_API_KEY or MISTRAL_API_KEY)"]
    owner = owner or DEFAULT_ACTOR_OWNER
    timeout = int(run_timeout or filters.get("ai_web_search_timeout_s") or 240)

    try:
        max_items = min(int(filters.get("max_per_source") or 36), WEB_SEARCH_MAX_ITEMS)
    except (TypeError, ValueError):
        max_items = WEB_SEARCH_MAX_ITEMS

    remote_pref = str(filters.get("remote") or "").lower()
    desc_parts = [str(seeds.get("focus_notes") or "").strip(),
                  (free_text or "").strip()]
    user_desc = "\n".join(p for p in desc_parts if p)[:2000]

    cache_query = (
        "|".join(sorted(phrases[:12])) + "::" + remote_pref + "::"
        + hashlib.sha1(user_desc.encode("utf-8")).hexdigest()[:12]
    )
    if db is not None and result_cache_s > 0:
        cached = None
        try:
            cached = db.recent_fetch_jobs("web_search", cache_query, "", result_cache_s)
        except Exception:
            log.debug("apify web_search: cache lookup failed", exc_info=True)
        if cached is not None:
            jobs: list[Job] = []
            seen_urls: set[str] = set()
            attr_q = phrases[0] if phrases else "web_search"
            for d in cached:
                if not isinstance(d, dict):
                    continue
                try:
                    j = Job(**{k: v for k, v in d.items() if k in _JOB_FIELDS})
                except TypeError:
                    continue
                if j.url and j.url in seen_urls:
                    continue
                if j.url:
                    seen_urls.add(j.url)
                jobs.append(j)
                if attribution is not None:
                    try:
                        attribution.setdefault(j.job_id, attr_q)
                    except Exception:
                        pass
            log.info("apify web_search: %d records from cache", len(jobs))
            return jobs, []

    inp: dict[str, Any] = {
        "keywords": phrases[:12],
        "remote": "remote-only" if remote_pref in ("require", "remote") else "any",
        "maxItems": max_items,
        "cacheTtlSeconds": int(cache_ttl),
    }
    if anthropic_key:
        inp["anthropicApiKey"] = anthropic_key
    elif mistral_key:
        inp["provider"] = "mistral"
        inp["mistralApiKey"] = mistral_key
    if user_desc:
        inp["userDescription"] = user_desc

    url = f"{API_BASE}/{owner}~{WEB_SEARCH_ACTOR}/run-sync-get-dataset-items"
    params = {"token": token, "timeout": timeout, "maxItems": max_items}
    started = time.time()
    resp, err = _post_actor_run(
        requests, url, params=params, payload=inp,
        timeout=timeout + 30, label="web_search",
    )
    if err:
        return [], [f"web_search-apify: {err}"]
    try:
        data = resp.json()
    except Exception as e:
        return [], [f"web_search-apify: {type(e).__name__}: {str(e)[:200]}"]

    if not isinstance(data, list):
        return [], ["web_search-apify: actor returned non-array body"]

    jobs = []
    seen_urls = set()
    attr_q = phrases[0] if phrases else "web_search"
    for rec in data:
        j = _web_search_record_to_job(rec)
        if j is None or (j.url and j.url in seen_urls):
            continue
        if j.url:
            seen_urls.add(j.url)
        jobs.append(j)
        if attribution is not None:
            try:
                attribution.setdefault(j.job_id, attr_q)
            except Exception:
                pass
    log.info("apify web_search: %d records in %.1fs", len(jobs), time.time() - started)
    if db is not None and result_cache_s > 0:
        try:
            existing = db.count_existing_jobs([j.job_id for j in jobs])
            db.record_fetch(
                "web_search", cache_query, 0, "",
                jobs_seen=len(jobs),
                jobs_new=max(0, len(jobs) - existing),
                jobs_json=json.dumps([asdict(j) for j in jobs]),
            )
        except Exception:
            log.debug("apify web_search: cache write failed", exc_info=True)
    return jobs, []


# ---------------------------------------------------------------------------
# External-usage report  (who-but-me is running these actors, and what it earns)
# ---------------------------------------------------------------------------
#
# An actor published under this account is PUBLIC: any Apify user can run it
# under THEIR OWN account + token. Those runs are invisible to ``/runs`` (which
# only lists runs *I* started), but the platform still counts them in the
# actor's aggregate ``stats``. We recover external usage from two independent
# signals that should agree:
#
#   external_lifetime = stats.totalRuns - (runs I started, by userId)
#   external_30d      = stats.publicActorRunStats30Days.TOTAL   (non-owner runs)
#
# Earnings (PAY_PER_EVENT) are split into a FIRM part and an ESTIMATED part:
#   * actor-start charge — deterministic per run → firm = ext_runs * start_price
#   * per-result charge  — needs dataset item counts, which we CANNOT see for
#     external runs. We estimate items/run from a sample of MY OWN recent runs.
# Both are net of Apify's margin (``apifyMarginPercentage``). Pricing that has
# not reached its ``startedAt`` yet is reported as PENDING and earns $0 today.

_API_V2 = "https://api.apify.com/v2"


def _apify_get(session: requests.Session, token: str, path: str) -> Any:
    """GET an Apify REST path, return the unwrapped ``data`` (or raw on no wrap)."""
    resp = session.get(
        f"{_API_V2}{path}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    return body.get("data", body) if isinstance(body, dict) else body


def _active_pricing(act: dict, now_iso: str) -> dict | None:
    """Return a flat pricing summary for an actor, or None if unpriced.

    Picks the most recently *created* ``pricingInfos`` entry and extracts the
    actor-start + per-result event prices and the Apify margin. ``active`` is
    True once ``startedAt <= now``; until then the prices are scheduled but not
    charged.
    """
    infos = act.get("pricingInfos") or []
    if not infos:
        return None
    latest = max(infos, key=lambda p: p.get("createdAt") or "")
    events = ((latest.get("pricingPerEvent") or {}).get("actorChargeEvents")) or {}

    def _price(name: str) -> float:
        return float((events.get(name) or {}).get("eventPriceUsd") or 0.0)

    started = latest.get("startedAt") or ""
    return {
        "model": latest.get("pricingModel"),
        "started_at": started,
        "active": bool(started) and started <= now_iso,
        "margin": float(latest.get("apifyMarginPercentage") or 0.0),
        "start_price": _price("actor-start"),
        "result_price": _price("result"),
    }


def _avg_items_per_run(
    session: requests.Session, token: str, runs: list[dict], sample: int
) -> float | None:
    """Average dataset item count over the most recent ``sample`` succeeded runs.

    Run-list rows don't carry an item count, so we resolve each sampled run's
    ``defaultDatasetId`` to its ``itemCount``. Returns None when nothing usable
    is sampled (caller then omits the result-charge estimate).
    """
    if sample <= 0:
        return None
    picked = [r for r in runs if r.get("status") == "SUCCEEDED" and r.get("defaultDatasetId")][:sample]
    counts: list[int] = []
    for r in picked:
        try:
            ds = _apify_get(session, token, f"/datasets/{r['defaultDatasetId']}")
            counts.append(int(ds.get("itemCount") or 0))
        except Exception:
            continue
    return (sum(counts) / len(counts)) if counts else None


def external_usage_report(
    token: str | None = None,
    *,
    owner: str | None = None,
    sample: int = 3,
    estimate_results: bool = True,
) -> dict:
    """Build a per-actor external-usage + projected-earnings report.

    Lists the actors owned by the token's account, and for each one separates
    runs *I* started from runs started by everyone else, then projects earnings
    under the actor's current pay-per-event pricing. Returns a structured dict
    (``account``, ``rows``, ``totals``, ``generated_at``) suitable for ``--json``
    or the text formatter. Raises on auth/network failure (this is an operator
    tool, so failing loudly is correct).
    """
    from datetime import datetime, timezone

    token = resolve_token(token)
    if not token:
        raise RuntimeError("no APIFY_TOKEN (env APIFY_TOKEN, or set APIFY_TOKEN_FILE_FALLBACK=1)")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    rows: list[dict] = []
    with requests.Session() as s:
        me = _apify_get(s, token, "/users/me")
        my_id = me.get("id")
        acts = _apify_get(s, token, "/acts?my=1&limit=1000").get("items", [])

        for a in acts:
            act = _apify_get(s, token, f"/acts/{a['id']}")
            st = act.get("stats") or {}
            total_runs = int(st.get("totalRuns") or 0)

            runs = _apify_get(s, token, f"/acts/{a['id']}/runs?limit=1000&desc=1").get("items", [])
            my_runs = sum(1 for r in runs if r.get("userId") == my_id)
            ext_life = max(0, total_runs - my_runs)
            ext_30d = int((st.get("publicActorRunStats30Days") or {}).get("TOTAL") or 0)

            pricing = _active_pricing(act, now_iso)
            avg_items = (
                _avg_items_per_run(s, token, runs, sample)
                if (estimate_results and pricing) else None
            )

            net_life = net_30d = 0.0
            if pricing:
                keep = 1.0 - pricing["margin"]
                def _net(n_runs: int) -> float:
                    gross = n_runs * pricing["start_price"]
                    if avg_items is not None:
                        gross += n_runs * avg_items * pricing["result_price"]
                    return gross * keep
                net_life = _net(ext_life)
                net_30d = _net(ext_30d)

            rows.append({
                "actor": a["name"],
                "total_runs": total_runs,
                "my_runs": my_runs,
                "external_runs": ext_life,
                "external_runs_30d": ext_30d,
                "total_users": int(st.get("totalUsers") or 0),
                "last_run_at": st.get("lastRunStartedAt"),
                "is_public": bool(act.get("isPublic")),
                "pricing": pricing,
                "avg_items_per_run": round(avg_items, 1) if avg_items is not None else None,
                "projected_net_usd_lifetime": round(net_life, 4),
                "projected_net_usd_30d": round(net_30d, 4),
            })

    rows.sort(key=lambda r: (-r["external_runs"], r["actor"]))
    any_active = any((r["pricing"] or {}).get("active") for r in rows)
    totals = {
        "actors": len(rows),
        "actors_with_external_use": sum(1 for r in rows if r["external_runs"] > 0),
        "external_runs_lifetime": sum(r["external_runs"] for r in rows),
        "external_runs_30d": sum(r["external_runs_30d"] for r in rows),
        "max_total_users": max((r["total_users"] for r in rows), default=0),
        "pricing_live": any_active,
        # Earnings are $0 until pricing goes live; we still surface the
        # projection so you can see what the *current* traffic would earn.
        "earned_net_usd_lifetime": round(
            sum(r["projected_net_usd_lifetime"] for r in rows if (r["pricing"] or {}).get("active")), 2),
        "projected_net_usd_lifetime": round(sum(r["projected_net_usd_lifetime"] for r in rows), 2),
        "projected_net_usd_30d": round(sum(r["projected_net_usd_30d"] for r in rows), 2),
    }
    return {
        "account": {"username": me.get("username"), "id": my_id,
                    "plan_tier": (me.get("plan") or {}).get("tier")},
        "generated_at": now_iso,
        "rows": rows,
        "totals": totals,
    }


def _format_usage_report(rep: dict) -> str:
    """Render ``external_usage_report`` output as an aligned text table."""
    acc = rep["account"]
    out: list[str] = []
    out.append(f"Apify external-usage report — account '{acc['username']}' ({acc['id']}, {acc.get('plan_tier')})")
    out.append(f"generated {rep['generated_at']}")
    out.append("")
    out.append(f"{'actor':28} {'total':>6} {'mine':>6} {'EXT':>5} {'30d':>4} {'users':>6} {'pricing':>22} {'net$ life':>10}")
    out.append("-" * 96)
    for r in rep["rows"]:
        p = r["pricing"]
        if not p:
            pstr = "none"
        elif p["active"]:
            pstr = f"LIVE {p['start_price']}+{p['result_price']}/item"
        else:
            pstr = f"pending {(p['started_at'] or '')[:10]}"
        flag = " <==" if r["external_runs"] > 0 else ""
        out.append(
            f"{r['actor']:28} {r['total_runs']:>6} {r['my_runs']:>6} {r['external_runs']:>5} "
            f"{r['external_runs_30d']:>4} {r['total_users']:>6} {pstr:>22} {r['projected_net_usd_lifetime']:>10.4f}{flag}"
        )
    t = rep["totals"]
    out.append("-" * 96)
    out.append(
        f"{t['actors_with_external_use']}/{t['actors']} actors used by others | "
        f"external runs: {t['external_runs_lifetime']} lifetime, {t['external_runs_30d']} in 30d | "
        f"max users/actor: {t['max_total_users']} (incl. you)"
    )
    if t["pricing_live"]:
        out.append(f"EARNED net (live pricing): ${t['earned_net_usd_lifetime']:.2f} lifetime")
    else:
        out.append(
            f"pricing NOT live yet → earned $0.00. "
            f"PROJECTED at current pricing: ${t['projected_net_usd_lifetime']:.2f} lifetime, "
            f"${t['projected_net_usd_30d']:.2f}/30d (result-charge ESTIMATED from sampled item counts)"
        )
    return "\n".join(out)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Apify actor utilities")
    ap.add_argument("--external-usage", action="store_true",
                    help="report who-but-me runs these actors + projected earnings")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--sample", type=int, default=3,
                    help="my-runs to sample per actor for avg items/run (0 = skip result estimate)")
    ap.add_argument("--no-estimate-results", action="store_true",
                    help="skip per-result earnings estimate (start-charge only)")
    ap.add_argument("--owner", default=None, help="(reserved) actor owner override")
    args = ap.parse_args()

    if args.external_usage:
        rep = external_usage_report(
            owner=args.owner, sample=args.sample,
            estimate_results=not args.no_estimate_results,
        )
        print(json.dumps(rep, indent=2) if args.json else _format_usage_report(rep))
    else:
        ap.error("nothing to do — pass --external-usage")
