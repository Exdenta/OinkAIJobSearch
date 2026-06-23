"""EURES (https://europa.eu/eures) — Official EU job-mobility network.

Status: DEFAULT-OFF. Operator must explicitly enable via the source toggle.

----------------------------------------------------------------------
Integration: public Job-Vacancy-Search REST API (no auth)
----------------------------------------------------------------------
The EURES portal is an Angular SPA backed by a JSON API. An earlier probe
(2026-05-01) tested `https://europa.eu/eures/api/search` and got 401/403,
and wrongly concluded EURES required an EU-Login (ECAS) account. That was
the WRONG endpoint. The SPA actually talks to a **public, unauthenticated**
search endpoint under the `/jv-searchengine/public/` prefix (the same
prefix the portal's anonymous `getNumberOfJobs` statistics call uses):

    POST https://europa.eu/eures/api/jv-searchengine/public/jv-search/search
    Content-Type: application/json

It returns structured JSON — `{"numberRecords", "jvs":[...], "facets"}` —
with each vacancy carrying `title`, `description` (HTML), `id` (base64),
`creationDate` (epoch ms), `locationMap` ({countryCode: [NUTS...]}),
`employer` ({name, ...}), ESCO `jobCategoriesCodes`, and `translations`.
Verified anonymously 2026-06-21. No CSRF, no cookies, no ECAS.

Server-side filtering (keyword / occupation / location / schedule)
matters here: EURES aggregates ~2.2M national-PES vacancies, heavily
generic/blue-collar, so an unfiltered pull is mostly noise. We fan a
small set of keyword queries in (overridable via `eures_keywords`),
dedupe by id, cap at `max_per_source`, and let downstream AI scoring do
the per-user relevance gate. Employer is frequently the literal string
"non renseigné" (not specified) on the French feed — normalised to "".

----------------------------------------------------------------------
Terms of use — OPERATOR NOTE
----------------------------------------------------------------------
The EURES portal terms prohibit automated extraction of vacancy data for
further processing / re-publishing. This adapter is DEFAULT-OFF for that
reason. It is intended only for PERSONAL job-alert use that links the
user back to the original EURES posting (the same posture as the bot's
other sources). The operator opts in by enabling the `eures` source.

Filters consulted
-----------------
  * max_per_source         (int, default 12) — cap on returned jobs
  * eures_keywords         (list[str]) — keyword fan-in; default below
  * eures_location_codes   (list[str], optional) — NUTS/country codes to
                            restrict to (e.g. ["es", "de"]); empty = all EU
  * eures_timeout_s        (int, default 20)
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

SEARCH_URL = (
    "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
)
JV_PUBLIC_URL = "https://europa.eu/eures/portal/jv-se/jv-details/{nid}?lang=en"

# Keyword fan-in default. EURES is EU-wide and noisy; these professional
# seeds bias toward the kinds of roles FindJobs users target (tech +
# research/policy) without hardcoding a single profile. Overridable via
# `filters["eures_keywords"]`. Each keyword is one API call.
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "software engineer",
    "frontend developer",
    "researcher",
    "data analyst",
    "project officer",
)

# EURES marks an unspecified employer with this literal French sentinel.
_EMPLOYER_SENTINEL = "non renseigné"

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


def _resolve_keywords(filters: dict) -> list[str]:
    kws = filters.get("eures_keywords")
    if kws:
        out = [str(k).strip() for k in kws if str(k).strip()]
        if out:
            return out
    return list(DEFAULT_KEYWORDS)


def _build_request_body(
    keyword: str, *, per_page: int, location_codes: list[str], session_id: str,
) -> dict[str, Any]:
    """Build the POST payload for the public jv-search endpoint. Schema
    matches the live SPA request (verified 2026-06-21)."""
    return {
        "resultsPerPage": per_page,
        "page": 1,
        "sortSearch": "MOST_RECENT",
        "keywords": [{"keyword": keyword, "specificSearchCode": "EVERYWHERE"}],
        "publicationPeriod": None,
        "occupationUris": [],
        "skillUris": [],
        "requiredExperienceCodes": [],
        "positionScheduleCodes": [],
        "sectorCodes": [],
        "educationAndQualificationLevelCodes": [],
        "positionOfferingCodes": [],
        "locationCodes": list(location_codes),
        "euresFlagCodes": [],
        "otherBenefitsCodes": [],
        "requiredLanguages": [],
        "minNumberPost": None,
        "sessionId": session_id,
        "requestLanguage": "en",
    }


def _posted_at(creation_ms: Any) -> str:
    """EURES `creationDate` is epoch MILLISECONDS. Emit an ISO date the
    downstream age gate parses (it reads epoch *seconds*, so we must not
    hand it the raw ms value). '' when missing/garbage."""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(creation_ms) / 1000))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _employer_name(jv: dict) -> str:
    emp = jv.get("employer")
    name = (emp.get("name") if isinstance(emp, dict) else "") or ""
    name = name.strip()
    if not name or name.lower() == _EMPLOYER_SENTINEL:
        return ""
    return name


def _location(jv: dict) -> str:
    """Render `locationMap` ({"SE": ["SE232"]}) as a compact country string.
    The detail page / description carries finer geo; country is enough for
    the downstream geo filter + AI scorer."""
    loc = jv.get("locationMap")
    if not isinstance(loc, dict) or not loc:
        return ""
    return ", ".join(sorted(str(c).upper() for c in loc.keys() if c))


def _best_text(jv: dict, field: str) -> str:
    """Prefer the English translation when EURES returns the posting in
    another language; fall back to the top-level field."""
    tr = jv.get("translations")
    if isinstance(tr, dict):
        en = tr.get("en")
        if isinstance(en, dict) and (en.get(field) or "").strip():
            return str(en[field])
    return str(jv.get(field) or "")


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", " ", s or "")


def fetch(filters: dict) -> list[Job]:
    """Best-effort EURES fetch via the public jv-search REST API.

    Fans the keyword set in (one POST each), dedupes by vacancy id, caps at
    `max_per_source`. Always returns a list; never raises.
    """
    cap = int(filters.get("max_per_source") or 12)
    timeout_s = int(filters.get("eures_timeout_s") or 20)
    keywords = _resolve_keywords(filters)
    location_codes = [
        str(c).strip() for c in (filters.get("eures_location_codes") or [])
        if str(c).strip()
    ]
    # Ask for a little headroom per keyword so the fan-in has slack to dedupe.
    per_page = max(5, min(25, cap))

    headers = {**UA, "Accept": "application/json", "Content-Type": "application/json"}

    jobs: list[Job] = []
    seen_ids: set[str] = set()
    per_query: list[dict[str, Any]] = []
    body_head = ""

    for idx, keyword in enumerate(keywords):
        if len(jobs) >= cap:
            break
        status_code: int | None = None
        added = 0
        try:
            body = _build_request_body(
                keyword,
                per_page=per_page,
                location_codes=location_codes,
                session_id=f"findjobs-{idx}",
            )
            r = requests.post(SEARCH_URL, json=body, headers=headers, timeout=timeout_s)
            status_code = r.status_code
            if status_code != 200:
                body_head = (r.text or "")[:300] or body_head
                log.warning("eures: search non-200 status=%s kw=%r", status_code, keyword)
                per_query.append({"keyword": keyword, "status": status_code, "added": 0})
                continue

            payload = r.json()
            jvs = payload.get("jvs") if isinstance(payload, dict) else None
            if not isinstance(jvs, list):
                per_query.append({"keyword": keyword, "status": 200, "added": 0})
                continue

            for jv in jvs:
                if len(jobs) >= cap:
                    break
                if not isinstance(jv, dict):
                    continue
                jid = str(jv.get("id") or "").strip()
                if not jid or jid in seen_ids:
                    continue
                title = fix_mojibake(_best_text(jv, "title")).strip()
                if not title:
                    continue
                seen_ids.add(jid)
                desc = _strip_html(_best_text(jv, "description"))
                jobs.append(Job(
                    source="eures",
                    external_id=jid,
                    title=title[:140],
                    company=fix_mojibake(_employer_name(jv))[:120],
                    location=fix_mojibake(_location(jv))[:120],
                    url=JV_PUBLIC_URL.format(nid=jid),
                    posted_at=_posted_at(jv.get("creationDate")),
                    snippet=clean_snippet(fix_mojibake(desc), max_chars=400),
                    salary="",
                ))
                added += 1
            per_query.append({"keyword": keyword, "status": 200, "added": added})
        except requests.RequestException as e:
            log.warning("eures: request failed kw=%r: %s", keyword, e)
            body_head = body_head or repr(e)[:300]
            per_query.append({"keyword": keyword, "status": status_code, "error": repr(e)[:200]})
            continue
        except Exception as e:  # noqa: BLE001
            log.exception("eures: unexpected failure kw=%r: %s", keyword, e)
            body_head = body_head or repr(e)[:300]
            continue

    log.info("eures: %d vacancies across %d keyword queries", len(jobs), len(keywords))
    _log_forensic({
        "input": {
            "endpoint": SEARCH_URL,
            "keywords": keywords,
            "location_codes": location_codes,
            "max_per_source": cap,
        },
        "output": {
            "count": len(jobs),
            "sample_titles": [j.title for j in jobs[:5]],
            "per_query": per_query,
            "body_head": body_head,
        },
    })
    return jobs
