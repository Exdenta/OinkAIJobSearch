"""NoFluffJobs (https://nofluffjobs.com) source adapter — Polish/CEE tech jobs.

NoFluffJobs is the second-largest Polish IT board (after JustJoin.it), with
strong DACH spillover and salary-mandatory listings (rare in EU tech). Heavy
frontend representation, especially React/Vue/Angular contract roles.
Complement to `justjoinit` for the Bilbao/EU frontend persona.

Module key: ``nofluffjobs``. Default OFF — opt-in per `defaults.py`.

Integration choice: public JSON API
-----------------------------------
NoFluffJobs ships an unauth REST endpoint that powers their web SPA:

    POST https://nofluffjobs.com/api/search/posting
        Content-Type: application/json
        body: {
          "rawSearch": "",
          "page": 1,
          "size": 50,
          "criteria": "",
          "language": "en"
        }

The endpoint returns:

    {
      "postings": [
        {
          "id": "abc-uuid",
          "url": "react-developer-acme-warsaw-xyz",      # slug only
          "name": "Senior React Developer",
          "title": "Frontend",                            # category label
          "category": "frontend",
          "seniority": ["senior"] | ["mid","senior"],
          "salary": {"from": 18000, "to": 25000, "currency":"PLN", "type":"b2b"},
          "location": {
            "places": [
              {"city": "Warsaw", "country": {"code":"PL"}, "url": "warsaw"},
              {"city": "Remote, Poland", "isRemote": true}
            ],
            "fullyRemote": true
          },
          "musts": ["React","TypeScript",...],
          "name": "...",
          "posted": 1730731200000,
          "renewed": 1730731200000,
          "tile": "...",
          "company": {"name":"Acme","url":"acme"}
        },
        ...
      ],
      "totalCount": 1234,
      "totalPages": 25
    }

Filter strategy
---------------
We POST with `category=frontend` (and `backend` falsy, etc.) to scope to
frontend-flavoured listings. Operators may override via env var
`NOFLUFFJOBS_CATEGORIES`.

Posting URL: https://nofluffjobs.com/job/{slug}
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable

import requests

from dedupe import Job
from text_utils import fix_mojibake

log = logging.getLogger(__name__)

API = "https://nofluffjobs.com/api/search/posting"
JOB_URL_PREFIX = "https://nofluffjobs.com/job/"

UA = {
    "User-Agent": "FindJobs-Bot/1.0 (+personal job-alert)",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Referer": "https://nofluffjobs.com/",
    "Origin": "https://nofluffjobs.com",
}

# Categories to request. NoFluffJobs uses lower-case category strings; the
# UI exposes: backend, frontend, fullstack, mobile, embedded, devops,
# testing, ux, business-analyst, project-manager, ai-data, security, ...
# We default to frontend only (this adapter is FE-first); operators can
# override via env to include adjacent categories.
DEFAULT_CATEGORIES = ("frontend",)

try:  # pragma: no cover
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]


def _categories_from_env() -> tuple[str, ...]:
    raw = os.environ.get("NOFLUFFJOBS_CATEGORIES", "").strip()
    if not raw:
        return DEFAULT_CATEGORIES
    out = tuple(t.strip().lower() for t in raw.split(",") if t.strip())
    return out or DEFAULT_CATEGORIES


def _parse_posting(item: dict) -> Job | None:
    """Map one NoFluffJobs posting to a `Job`.

    Field mapping landmines (probed 2026-05-06):
      * `title` is the JOB title (e.g. "Senior React Developer")
      * `name`  is the COMPANY (e.g. "Sopra Steria Poland") — NOT the title
      * `url`   is a URL slug (lowercased), unlike `id` which preserves case
      * `company` is usually None; rely on `name` for company.
    """
    if not isinstance(item, dict):
        return None
    slug = (item.get("url") or "").strip()
    title = (item.get("title") or "").strip()
    if not slug or not title:
        return None
    url = JOB_URL_PREFIX + slug
    # Company lives in `name` on this API; the `company` field is reserved
    # for an embedded company object that's frequently null.
    company_obj = item.get("company")
    company = ""
    if isinstance(company_obj, dict):
        company = (company_obj.get("name") or "").strip()
    elif isinstance(company_obj, str):
        company = company_obj.strip()
    if not company:
        company = (item.get("name") or "").strip()

    location_str = ""
    loc = item.get("location")
    if isinstance(loc, dict):
        cities: list[str] = []
        places = loc.get("places")
        if isinstance(places, list):
            for p in places[:4]:
                if not isinstance(p, dict):
                    continue
                c = (p.get("city") or "").strip()
                if c:
                    cities.append(c)
        if loc.get("fullyRemote"):
            cities.append("(remote)")
        location_str = ", ".join(cities)

    salary_str = ""
    sal = item.get("salary")
    if isinstance(sal, dict):
        lo = sal.get("from")
        hi = sal.get("to")
        cur = (sal.get("currency") or "").strip()
        stype = (sal.get("type") or "").strip().upper()
        if lo or hi:
            salary_str = f"{stype} {lo or '?'}–{hi or '?'} {cur}".strip()

    seniority = item.get("seniority")
    seniority_str = ""
    if isinstance(seniority, list) and seniority:
        seniority_str = ", ".join(str(s) for s in seniority)

    musts = item.get("musts")
    skills_str = ""
    if isinstance(musts, list) and musts:
        skills_str = ", ".join(str(m) for m in musts[:8])

    snippet_bits = []
    if seniority_str:
        snippet_bits.append(f"level={seniority_str}")
    if skills_str:
        snippet_bits.append(f"skills={skills_str}")
    if salary_str:
        snippet_bits.append(f"salary={salary_str}")
    snippet = "; ".join(snippet_bits)[:500]

    posted_ms = item.get("posted") or item.get("renewed")
    posted_at = ""
    if isinstance(posted_ms, (int, float)) and posted_ms > 0:
        # Keep ISO-ish; downstream code accepts arbitrary strings.
        from datetime import datetime, timezone
        try:
            posted_at = datetime.fromtimestamp(
                int(posted_ms) / 1000, tz=timezone.utc
            ).isoformat()
        except (ValueError, OSError):
            posted_at = ""

    return Job(
        source="nofluffjobs",
        external_id=slug,
        title=fix_mojibake(title),
        company=fix_mojibake(company),
        location=fix_mojibake(location_str),
        url=url,
        posted_at=posted_at,
        snippet=fix_mojibake(snippet),
        salary=salary_str,
    )


# P2 page-memory cursor: NoFluffJobs returns 50 postings/page; 5 covers
# the freshest ~250 listings/category which exceeds `max_per_source`
# trims even on aggressive operator configs.
NOFLUFFJOBS_MAX_PAGE = 5


def fetch(
    filters: dict,
    *,
    db=None,
    min_revisit_age_s: int = 21600,
) -> list[Job]:
    """One POST per category, dedupe by (company, title), cap at `max_per_source`.

    NoFluffJobs returns one row per (posting, region) pair — the same listing
    appears N times when a company opens a role across N cities. The `url`
    slug carries the region suffix so it differs per row, making slug-based
    dedupe useless. We dedupe on the lower-cased (company, title) tuple,
    which collapses the regional duplicates into one entry while still
    keeping distinct postings from the same company that share neither
    title nor region.

    Cursor mode (`db` provided): each category advances ONE page on the
    `search_fetches` cursor per call (via `db.next_page_for`). Without
    `db` the adapter always fetches pageNumber=1, preserving pre-P2
    behaviour.
    """
    cap = int(filters.get("max_per_source") or 30)
    categories = _categories_from_env()
    seen_keys: set[tuple[str, str]] = set()
    out: list[Job] = []
    err_payload: dict | None = None

    for cat in categories:
        if len(out) >= cap:
            break

        # Cursor decides which page this category fetches next.
        if db is not None:
            page_num = db.next_page_for(
                "nofluffjobs", str(cat), "",
                max_page=NOFLUFFJOBS_MAX_PAGE,
                min_revisit_age_s=min_revisit_age_s,
            )
            if page_num == -1:
                log.info(
                    "nofluffjobs[cat=%s]: all pages 1..%d fresh within %ds — skipping",
                    cat, NOFLUFFJOBS_MAX_PAGE, min_revisit_age_s,
                )
                continue
        else:
            page_num = 1

        size = min(cap - len(out), 50)
        body = {"criteriaSearch": {"category": [cat]}}
        params = {
            "pageSize": str(size),
            "pageNumber": str(page_num),
            "salaryCurrency": "EUR",
            "salaryPeriod": "month",
            "region": "pl",
        }
        postings: list[dict] = []
        status_code: int | None = None
        try:
            resp = requests.post(API, params=params, json=body, headers=UA, timeout=20)
            status_code = resp.status_code
            resp.raise_for_status()
            payload = resp.json()
            if isinstance(payload, dict):
                postings = payload.get("postings") or []
        except requests.RequestException as e:
            log.error("nofluffjobs fetch failed (cat=%s): %s", cat, e)
            err_payload = {"class": type(e).__name__, "message": str(e)[:300]}
        except ValueError as e:
            log.error("nofluffjobs json parse failed (cat=%s): %s", cat, e)
            err_payload = {"class": "JSONDecodeError", "message": str(e)[:300]}

        added = 0
        cat_seen_ids: list[str] = []
        for item in postings:
            if len(out) >= cap:
                break
            if not isinstance(item, dict):
                continue
            job = _parse_posting(item)
            if job is None:
                continue
            key = (job.company.strip().lower(), job.title.strip().lower())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            cat_seen_ids.append(f"nofluffjobs:{job.external_id}")
            out.append(job)
            added += 1
        log.info("nofluffjobs[cat=%s page=%s] status=%s postings=%d added=%d (total=%d)",
                 cat, page_num, status_code, len(postings), added, len(out))

        # Cursor advancement + jobs_seen / jobs_new telemetry.
        if db is not None:
            try:
                existing = db.count_existing_jobs(cat_seen_ids) if cat_seen_ids else 0
                db.record_fetch(
                    "nofluffjobs", str(cat), int(page_num), "",
                    jobs_seen=len(cat_seen_ids),
                    jobs_new=max(0, len(cat_seen_ids) - existing),
                )
            except Exception:
                log.debug("nofluffjobs: db.record_fetch raised; continuing",
                          exc_info=True)

        if forensic is not None:
            try:
                forensic.log_step(
                    "sources.nofluffjobs.page",
                    input={"category": cat, "size": size, "page": page_num},
                    output={
                        "status_code": status_code,
                        "postings_seen": len(postings),
                        "added": added,
                        "running_total": len(out),
                        "sample_titles": [j.title[:80] for j in out[-3:]],
                    },
                    error=err_payload,
                )
            except Exception:
                log.debug("nofluffjobs forensic emit failed", exc_info=True)
        err_payload = None

    return out
