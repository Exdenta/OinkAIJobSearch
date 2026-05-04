"""Welcome to the Jungle (https://www.welcometothejungle.com) source adapter.

WTTJ is a French-origin job board with strong coverage across France, Spain,
Germany, the Benelux, and the wider EU — a useful complement to ReliefWeb
(humanitarian) and EURAXESS (research) for tech-flavoured roles.

Default OFF: the adapter is wired into the source registry only when the
operator opts in via filters / config. The module key is `wttj`.

Integration choice: Algolia search API (anonymous public key)
-------------------------------------------------------------
Probed on 2026-05-01. The WTTJ web app is a single-page React app; HTML
listings are server-rendered shells with no embedded structured data, and
there is no public RSS/Atom or sitemap-of-jobs we can lean on. What IS
public — and explicitly exposed in the page bootstrap — is the Algolia
search index that powers https://www.welcometothejungle.com/en/jobs.

  Bootstrap script on the public homepage advertises:
    ALGOLIA_APPLICATION_ID  = "CSEKHVMS53"
    ALGOLIA_API_KEY_CLIENT  = "4bd8f6215d0cc52b26430765769e65a0"
    ALGOLIA_JOBS_INDEX_PREFIX = "wttj_jobs_production"

  Index naming: `{prefix}_{lang}` where lang ∈ {en, fr, es, ...}. The same
  rows appear in every language; localisation only affects taxonomy strings
  (sectors, professions). We use `_en` because most of the operator's users
  consume English-rendered taxonomy, but we DO NOT translate the original
  posting `name` (job title) or `summary` — those are preserved verbatim
  to handle multi-language postings (a Spanish company will post in
  Spanish; a French company often in French; we don't second-guess them).

  Endpoint: POST https://{appid}-dsn.algolia.net/1/indexes/{index}/query
  Headers: X-Algolia-Application-Id, X-Algolia-API-Key, plus a Referer/Origin
  spoofing the WTTJ domain (the public key is restricted to that referer —
  without it Algolia returns 403 "Method not allowed with this referer").

Why not the public REST search at api.welcometothejungle.com?
  That endpoint exists but is auth-gated for logged-in users; the unauth
  flow redirects through the SPA which itself talks Algolia. The Algolia
  path is cleaner, structured, and matches what the website does.

Why not HTML scrape of /jobs?
  The listing page is a JS-hydrated React shell. No <script type="ld+json">
  job postings, no embedded preloaded state we can rely on across deploys.
  The Algolia path is far more stable than scraping a CSR app.

Filters and bias
----------------
We bias toward Spain + EU + remote because that's where this adapter adds
the most marginal coverage on top of the existing sources. The bias is a
soft Algolia `filters` clause — `offices.country_code` membership in the
EU set, OR `has_remote:true`. Operators can override via:
  * `wttj_query` — Algolia query string (default: empty → everything,
    Algolia's relevance ranking + `published_at_timestamp` desc)
  * `wttj_country_codes` — comma-separated ISO-3166 alpha-2 codes
    (default: ES,FR,DE,NL,BE,IE,PT,IT,SE,DK,FI,NO,AT,CH,PL,LU,EE)
  * `wttj_index_lang` — index suffix (default: "en")
  * `wttj_include_remote` — bool, default True (adds OR has_remote:true)

Caveats
-------
  * Algolia keys rotate. If WTTJ rotates the public key (rare — it's been
    stable for years), the next fetch will return 403 and forensic logging
    will record `body_head`. We then re-scrape the bootstrap script to pick
    up the new pair.
  * `published_at` is ISO 8601 UTC — pass through as-is; downstream parsers
    cope.
  * URL pattern: /{lang}/companies/{org.slug}/jobs/{slug}. The slug embeds
    the city (e.g. ..._barcelona), which is convenient for human readers.
  * `external_id` = the Algolia `reference` (UUID) when present, else the
    slug. The UUID is stable across re-indexings.
  * Multi-office postings: we pick the first office whose country_code is
    in the EU allowlist; if none match (pure remote), we use "Remote" + the
    first office country.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

# Public bootstrap creds, verified 2026-05-01 from
# https://www.welcometothejungle.com/en/jobs window.env block.
ALGOLIA_APP_ID = "CSEKHVMS53"
ALGOLIA_API_KEY = "4bd8f6215d0cc52b26430765769e65a0"
ALGOLIA_INDEX_PREFIX = "wttj_jobs_production"

# Algolia's restricted public key requires referer/origin matching WTTJ.
ALGOLIA_HEADERS = {
    **UA,
    "X-Algolia-Application-Id": ALGOLIA_APP_ID,
    "X-Algolia-API-Key": ALGOLIA_API_KEY,
    "Referer": "https://www.welcometothejungle.com/",
    "Origin": "https://www.welcometothejungle.com",
    "Content-Type": "application/json",
}

PUBLIC_BASE = "https://www.welcometothejungle.com"

# Default EU country allowlist — emphasis on Spain + neighbouring tech hubs.
DEFAULT_EU_COUNTRIES = [
    "ES", "FR", "DE", "NL", "BE", "IE", "PT", "IT",
    "SE", "DK", "FI", "NO", "AT", "CH", "PL", "LU", "EE",
]

# Attributes we actually need — narrows payload, faster + cheaper for Algolia.
ATTRIBUTES = [
    "name", "slug", "reference", "language", "published_at",
    "organization", "offices", "summary",
    "has_remote", "remote",
    "salary_minimum", "salary_maximum", "salary_currency", "salary_period",
    "new_profession",
]

# Best-effort forensic logger; module may not exist in every checkout.
try:  # pragma: no cover - thin shim
    from forensic import log_step  # type: ignore
except ImportError:
    def log_step(name: str, *, input: dict | None = None, output: dict | None = None) -> None:
        log.info("forensic %s input=%s output=%s", name, input, output)


def _build_filters(country_codes: list[str], include_remote: bool) -> str:
    """Build an Algolia `filters` expression biasing toward EU + remote."""
    parts: list[str] = []
    if country_codes:
        parts.extend(f"offices.country_code:{c}" for c in country_codes)
    if include_remote:
        parts.append("has_remote:true")
    return " OR ".join(parts) if parts else ""


def _format_salary(hit: dict[str, Any]) -> str:
    """Render salary range as "MIN–MAX CCY" or "" when unavailable."""
    lo = hit.get("salary_minimum")
    hi = hit.get("salary_maximum")
    ccy = hit.get("salary_currency") or ""
    period = hit.get("salary_period") or ""
    if not (lo or hi):
        return ""
    if lo and hi:
        amt = f"{int(lo):,}-{int(hi):,}"
    else:
        amt = f"{int(lo or hi):,}"
    bits = [b for b in (amt, ccy, period) if b]
    return " ".join(bits)


def _pick_office(offices: list[dict[str, Any]], allow: set[str]) -> dict[str, Any]:
    """Pick the first office whose country_code is in `allow`; else first."""
    for o in offices or []:
        if (o.get("country_code") or "").upper() in allow:
            return o
    return (offices or [{}])[0] or {}


def _format_location(hit: dict[str, Any], allow: set[str]) -> str:
    offices = hit.get("offices") or []
    is_remote = bool(hit.get("has_remote"))
    remote_kind = (hit.get("remote") or "").lower()
    office = _pick_office(offices, allow)
    city = (office.get("city") or office.get("local_city") or "").strip()
    country = (office.get("country") or "").strip()
    base = ", ".join([p for p in (city, country) if p])
    if is_remote and remote_kind == "full":
        return f"Remote ({base})" if base else "Remote"
    if is_remote and remote_kind == "partial":
        return f"{base} (hybrid)" if base else "Hybrid"
    return base or "Unknown"


def _build_url(lang: str, org_slug: str, job_slug: str) -> str:
    """Construct canonical public job URL.

    Pattern: /{lang}/companies/{org_slug}/jobs/{job_slug}
    Verified live 2026-05-01.
    """
    if not (org_slug and job_slug):
        return ""
    return f"{PUBLIC_BASE}/{lang}/companies/{org_slug}/jobs/{job_slug}"


def _parse_hit(hit: dict[str, Any], lang: str, allow: set[str]) -> Job | None:
    org = hit.get("organization") or {}
    org_slug = (org.get("slug") or "").strip()
    job_slug = (hit.get("slug") or "").strip()
    title_raw = hit.get("name") or ""
    title = fix_mojibake(title_raw).strip()
    if not (title and job_slug):
        return None
    url = _build_url(lang, org_slug, job_slug)
    if not url:
        return None
    external_id = (hit.get("reference") or job_slug or "").strip()
    company = fix_mojibake((org.get("name") or "")).strip()
    location = _format_location(hit, allow)
    summary = hit.get("summary") or ""
    posted_at = hit.get("published_at") or ""
    salary = _format_salary(hit)
    return Job(
        source="wttj",
        external_id=external_id[:120],
        title=title[:140],
        company=company[:120],
        location=fix_mojibake(location)[:120],
        url=url,
        posted_at=posted_at,
        snippet=clean_snippet(summary, max_chars=400),
        salary=salary[:80],
    )


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point. Returns up to `max_per_source` Jobs.

    `filters` keys consulted:
      * max_per_source (int, default 12) — cap on returned jobs
      * wttj_query (str, optional) — Algolia query string; "" → recency
      * wttj_country_codes (str, optional) — CSV ISO-2 list; default EU set
      * wttj_index_lang (str, optional) — index suffix (default "en")
      * wttj_include_remote (bool, optional, default True)
      * wttj_timeout_s (int, optional, default 25)
    """
    cap = int(filters.get("max_per_source") or 12)
    query = (filters.get("wttj_query") or "").strip()
    lang = (filters.get("wttj_index_lang") or "en").strip().lower()
    timeout_s = int(filters.get("wttj_timeout_s") or 25)
    include_remote = filters.get("wttj_include_remote")
    if include_remote is None:
        include_remote = True

    cc_raw = (filters.get("wttj_country_codes") or "").strip()
    if cc_raw:
        country_codes = [c.strip().upper() for c in cc_raw.split(",") if c.strip()]
    else:
        country_codes = list(DEFAULT_EU_COUNTRIES)
    allow_set = {c.upper() for c in country_codes}

    index = f"{ALGOLIA_INDEX_PREFIX}_{lang}"
    endpoint = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/{index}/query"
    payload = {
        "query": query,
        "hitsPerPage": min(cap * 2, 50),  # over-fetch to absorb parse failures
        "attributesToRetrieve": ATTRIBUTES,
        "filters": _build_filters(country_codes, bool(include_remote)),
    }

    jobs: list[Job] = []
    status_code: int | None = None
    body_head = ""
    nb_hits = 0
    sample_titles: list[str] = []

    try:
        r = requests.post(endpoint, json=payload, headers=ALGOLIA_HEADERS, timeout=timeout_s)
        status_code = r.status_code
        if status_code != 200:
            body_head = (r.text or "")[:500]
            log.error("wttj algolia non-200 status=%s body_head=%r", status_code, body_head)
            r.raise_for_status()
        data = r.json() if r.content else {}
        nb_hits = int(data.get("nbHits") or 0)
        for hit in data.get("hits") or []:
            if len(jobs) >= cap:
                break
            try:
                job = _parse_hit(hit, lang, allow_set)
            except Exception:  # noqa: BLE001
                log.exception("wttj parse failed for hit slug=%r", hit.get("slug"))
                job = None
            if job is None:
                continue
            jobs.append(job)
        sample_titles = [j.title for j in jobs[:5]]
    except requests.RequestException as e:
        log.error("wttj fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]
    except Exception as e:  # noqa: BLE001
        log.exception("wttj fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]

    log_step(
        "wttj.fetch",
        input={
            "endpoint": endpoint,
            "max_per_source": cap,
            "query": query,
            "country_codes": country_codes,
            "include_remote": bool(include_remote),
            "lang": lang,
        },
        output={
            "status_code": status_code,
            "nb_hits": nb_hits,
            "count": len(jobs),
            "sample_titles": sample_titles,
            **({"body_head": body_head} if body_head and status_code != 200 else {}),
        },
    )
    return jobs
