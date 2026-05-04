"""EURES (https://eures.europa.eu) — Official EU job-mobility network.

Status: DEFAULT-OFF. Operator must explicitly enable via filters.yaml.

----------------------------------------------------------------------
Probe summary (2026-05-01)
----------------------------------------------------------------------
EURES has two web surfaces:

  1. https://eures.europa.eu/...           — Drupal-rendered marketing /
     editorial site (announcements, advisor directory, jobseeker tips).
     It does NOT publish individual vacancies inline; every "Find a job"
     button bounces to (2).
  2. https://europa.eu/eures/portal/jv-se  — Angular SPA (the actual
     "Job Vacancy Search Engine"). The SPA shell at any path under
     /eures/portal/* unconditionally returns the same 54 KB bootstrap
     HTML (200 OK), including paths like /jv-se/api/search — this is
     the SPA's history-fallback, not a real API surface.

The SPA talks to a JSON API at `https://europa.eu/eures/api/*`.
Probed (2026-05-01) every endpoint we could enumerate:

  GET  /eures/api/search          → 401 Unauthorized (no body)
  GET  /eures/api/jobs            → 401
  GET  /eures/api/jv/search       → 401
  GET  /eures/api/v1/jv/search    → 401
  POST /eures/api/search          → 403 "An expected CSRF token cannot
                                       be found"

POST is the right verb (Spring Security 401 vs 403 distinction confirms
the endpoint exists — 403 means past auth filter, blocked at CSRF). But
the CSRF token is bound to a server-side session that the SPA negotiates
through EU Login (ECAS); there is NO public anonymous flow:

  - No `Set-Cookie` issued by `/eures/portal/jv-se/*` or
    `/eures/index_en` (verified — both return zero cookies).
  - No `/eures/api/csrf`, `/eures/api/csrf-token`, or `/eures/api/csrfToken`
    endpoint (all 401).
  - CSRF double-submit (matching XSRF-TOKEN cookie + X-XSRF-TOKEN header)
    is rejected with 401, confirming auth gate is stricter than
    double-submit.
  - `apikey: public` and similar conventional shortcuts: 401.
  - No RSS feed at any of /eures/rss, /eures/portal/rss,
    /eures/eures-apps/searchengine/rss, /eures/feeds/jobs.
  - Drupal subpages (/jobseekers_en, /living-and-working/...) contain
    zero direct job links — only links back to the SPA.
  - `/eures/api-docs` returns HTTP 500 (existence hint, but unusable).

Conclusion: there is currently no fee/key-free machine-readable channel
into EURES vacancies. Anyone wanting EURES jobs must register as a
EURES Partner (https://eures.europa.eu/eures-partners-area) and obtain
an EU Login (ECAS) account, then authenticate the search API.

Adapter behaviour
-----------------
We try the public POST `/eures/api/search` once (with a synthetic CSRF
token) so that the moment the gate is loosened — or the moment the
operator wires a real ECAS bearer via `EURES_API_TOKEN` — the adapter
starts producing rows. On the expected 401/403 we return [] and log a
forensic record explaining the skip. We never raise.

Filters consulted
-----------------
  * max_per_source         (int, default 12) — cap on returned jobs
  * eures_country_codes    (list[str], default ["ES"]) — Spain bias
  * eures_keywords         (str, optional) — free-text query
  * eures_api_token        (str, optional) — ECAS Bearer token; if
                                             provided, sent as
                                             Authorization header
  * eures_timeout_s        (int, default 20)

Spain bias is the operator-visible default, since all current FindJobs
users target Spain/EU. If/when EURES opens up, this adapter will
already be correctly biased.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

API_BASE = "https://europa.eu/eures/api"
SEARCH_URL = f"{API_BASE}/search"
JV_PUBLIC_URL = "https://europa.eu/eures/portal/jv-se/jv-details/{nid}?lang=en"

# Best-effort forensic logger; module may not exist in every checkout.
try:  # pragma: no cover - thin shim
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]


def _log_forensic(payload: dict[str, Any]) -> None:
    if forensic is None:
        return
    try:
        forensic.log_step(
            "eures.fetch",
            input=payload.get("input", {}),
            output=payload.get("output", {}),
        )
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for eures.fetch", exc_info=True)


def _build_request_body(filters: dict, cap: int) -> dict[str, Any]:
    """Build the JSON payload the EURES `/api/search` endpoint expects.

    Schema reverse-engineered from the SPA bundle: Spring-Boot-style
    pagination + a `searchCriteria` object with location and keyword
    filters. If the gate ever opens, this body should produce
    Spain-biased, recent-first results.
    """
    countries = filters.get("eures_country_codes") or ["ES"]
    if isinstance(countries, str):
        countries = [c.strip() for c in countries.split(",") if c.strip()]
    keywords = (filters.get("eures_keywords") or "").strip()
    return {
        "page": 1,
        "resultsPerPage": min(cap, 25),
        "sortSearch": "BY_PUBLICATION_DESC",
        "searchCriteria": {
            "keywordsEverywhere": keywords or None,
            "locationCodes": list(countries),
            "publicationPeriod": "LAST_WEEK",
        },
    }


def _parse_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Map EURES JSON rows → dict-cards consumed by `fetch`.

    The exact schema isn't documented publicly; we extract defensively
    and tolerate either `jvs`, `results`, or `content` as the row list
    key (common Spring Data REST aliases).
    """
    rows = (
        payload.get("jvs")
        or payload.get("results")
        or payload.get("content")
        or []
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        nid = (
            r.get("reference")
            or r.get("id")
            or r.get("jvId")
            or r.get("nid")
            or ""
        )
        if not nid:
            continue
        title = (r.get("title") or r.get("jobTitle") or "").strip()
        if not title:
            continue
        # Employer
        employer = r.get("employer") or {}
        company = ""
        if isinstance(employer, dict):
            company = (
                employer.get("name")
                or employer.get("businessName")
                or ""
            )
        if not company:
            company = (r.get("companyName") or "").strip()
        # Location
        location = ""
        loc = r.get("location") or r.get("workLocation") or {}
        if isinstance(loc, dict):
            city = loc.get("city") or loc.get("locationCity") or ""
            country = loc.get("countryName") or loc.get("country") or ""
            location = ", ".join([p for p in (city, country) if p])
        elif isinstance(loc, list) and loc:
            first = loc[0] if isinstance(loc[0], dict) else {}
            city = first.get("city") or ""
            country = first.get("countryName") or first.get("country") or ""
            location = ", ".join([p for p in (city, country) if p])
        # URL
        url = r.get("url") or JV_PUBLIC_URL.format(nid=nid)
        # Snippet
        desc = r.get("description") or r.get("shortDescription") or ""
        # Posted
        posted = (
            r.get("publicationDate")
            or r.get("postedAt")
            or r.get("dateCreated")
            or ""
        )
        out.append({
            "external_id": str(nid),
            "title": title,
            "company": company or "",
            "location": location or "",
            "url": url,
            "posted_at": str(posted),
            "snippet": desc,
        })
    return out


def fetch(filters: dict) -> list[Job]:
    """Best-effort EURES fetch.

    Always returns a list (possibly empty). Never raises. On the expected
    401/403 (CSRF + EU Login gate) it returns [] and forensic-logs the
    skip reason so operators can see why.
    """
    cap = int(filters.get("max_per_source") or 12)
    timeout_s = int(filters.get("eures_timeout_s") or 20)
    api_token = (
        filters.get("eures_api_token")
        or os.environ.get("EURES_API_TOKEN", "")
    ).strip()

    body = _build_request_body(filters, cap)

    # Synthetic CSRF token (double-submit); no effect when ECAS gate is
    # in place, but cheap to send and lets a future relaxation work
    # automatically.
    csrf_token = str(uuid.uuid4())
    headers = {
        **UA,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-XSRF-TOKEN": csrf_token,
        "X-CSRF-TOKEN": csrf_token,
        "Origin": "https://europa.eu",
        "Referer": "https://europa.eu/eures/portal/jv-se/search?lang=en",
    }
    cookies = {"XSRF-TOKEN": csrf_token}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"

    jobs: list[Job] = []
    status_code: int | None = None
    body_head = ""
    skip_reason = ""
    sample_titles: list[str] = []

    try:
        r = requests.post(
            SEARCH_URL,
            json=body,
            headers=headers,
            cookies=cookies,
            timeout=timeout_s,
        )
        status_code = r.status_code
        if status_code in (401, 403):
            body_head = (r.text or "")[:300]
            skip_reason = (
                "EURES API requires EU Login (ECAS) authentication; "
                f"HTTP {status_code}. Set EURES_API_TOKEN env to enable."
            )
            log.info("eures: skipped (gated). status=%s", status_code)
        elif status_code != 200:
            body_head = (r.text or "")[:500]
            skip_reason = f"EURES non-200 status={status_code}"
            log.warning("eures: %s body_head=%r", skip_reason, body_head)
        else:
            try:
                payload = r.json()
            except ValueError:
                body_head = (r.text or "")[:500]
                skip_reason = "EURES returned non-JSON 200"
                log.warning("eures: %s body_head=%r", skip_reason, body_head)
                payload = None

            if isinstance(payload, dict):
                cards = _parse_results(payload)
                for c in cards:
                    if len(jobs) >= cap:
                        break
                    jobs.append(Job(
                        source="eures",
                        external_id=c["external_id"],
                        title=fix_mojibake(c["title"])[:140],
                        company=fix_mojibake(c["company"])[:120],
                        location=fix_mojibake(c["location"])[:120],
                        url=c["url"],
                        posted_at=c["posted_at"],
                        snippet=clean_snippet(c["snippet"], max_chars=400),
                        salary="",
                    ))
                sample_titles = [j.title for j in jobs[:5]]
                if not jobs:
                    skip_reason = "EURES JSON parsed but produced 0 cards"

    except requests.RequestException as e:
        skip_reason = f"EURES request failed: {e!r}"
        body_head = repr(e)[:500]
        log.error("eures fetch failed: %s", e)
    except Exception as e:  # noqa: BLE001
        skip_reason = f"EURES unexpected failure: {e!r}"
        body_head = repr(e)[:500]
        log.exception("eures fetch failed: %s", e)

    _log_forensic({
        "input": {
            "endpoint": SEARCH_URL,
            "max_per_source": cap,
            "country_codes": filters.get("eures_country_codes") or ["ES"],
            "keywords": filters.get("eures_keywords") or "",
            "has_token": bool(api_token),
        },
        "output": {
            "status_code": status_code,
            "count": len(jobs),
            "sample_titles": sample_titles,
            "skip_reason": skip_reason,
            "body_head": body_head if (status_code != 200 or skip_reason) else "",
        },
    })

    return jobs
