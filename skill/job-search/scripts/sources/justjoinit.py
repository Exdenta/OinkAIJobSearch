"""JustJoin.it (https://justjoin.it) source adapter — Polish/CEE tech jobs.

JustJoin.it is the dominant Polish IT job board with strong cross-border CEE
coverage (Poland, Germany, Czechia, Slovakia, Hungary, Lithuania, plus
remote-EU). Heavy frontend representation (React/Vue/Angular/TypeScript).
Adds value for the Bilbao/EU frontend persona (user 433775883) where
LinkedIn + WTTJ skew French/Spanish.

Module key: ``justjoinit``. Default OFF — opt-in per `defaults.py`.

Integration choice: public JSON API
-----------------------------------
JustJoin.it ships a public REST endpoint that powers their own SPA:

    GET https://api.justjoin.it/v2/user-panel/offers
        ?categories=1
        ?perPage=50
        ?sortBy=published

The API is unauth, returns JSON, and the response shape is documented
implicitly through the SPA bundle. Each offer carries:

    {
      "id": "abc123-uuid",
      "slug": "react-developer-acme-warsaw",
      "title": "React Developer",
      "companyName": "Acme",
      "city": "Warsaw",
      "remoteInterview": true,
      "workplaceType": "remote" | "hybrid" | "office",
      "publishedAt": "2026-05-05T10:30:00Z",
      "categoryId": 1,                  # 1 = JavaScript, 2 = HTML, ...
      "requiredSkills": ["React","TypeScript",...],
      "employmentTypes": [{"type":"b2b","from":...,"to":...,"currency":"PLN"}],
      "experienceLevel": "junior" | "mid" | "senior" | "c-level",
      "multilocation": [{"city":"...","slug":"..."}, ...],
      "openToHireUkrainians": false,
    }

Filtering strategy
------------------
We do NOT pass keyword/skill filters — the AI score gate downstream is the
sole matching pass. We DO scope the request to relevant categories so we
don't flood the pool with non-tech roles:

  * Default categories: JavaScript, HTML, frontend-adjacent. Operators may
    override via `justjoinit_categories` env var (comma-separated category
    ids, see https://justjoin.it for the live list).
  * `perPage=max_per_source` so we get exactly one page worth.
  * Sorted by `published` desc so the freshest postings come first.

We bias toward remote + EU geographies in the post-fetch sort (not in the
request) to avoid second-guessing the API's own ranking.

Posting URL: https://justjoin.it/job-offer/{slug} — stable & user-shareable.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Iterable

import requests

from dedupe import Job
from text_utils import fix_mojibake

log = logging.getLogger(__name__)

API = "https://api.justjoin.it/v2/user-panel/offers"
JOB_URL_PREFIX = "https://justjoin.it/job-offer/"

UA = {
    "User-Agent": "FindJobs-Bot/1.0 (+personal job-alert)",
    "Accept": "application/json",
    "Version": "2",
    # JustJoin's API rejects requests without a Referer pointing at the SPA.
    "Referer": "https://justjoin.it/",
    "Origin": "https://justjoin.it",
}

# Default category set: JavaScript-family + HTML/CSS frontend roles. The
# JustJoin category ids are stable; operator can override by env. We don't
# include category 0 (Other) to keep the pool focused.
DEFAULT_CATEGORIES = (1, 2)  # 1=JavaScript, 2=HTML

try:  # pragma: no cover - forensic optional
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]


def _categories_from_env() -> tuple[int, ...]:
    raw = os.environ.get("JUSTJOINIT_CATEGORIES", "").strip()
    if not raw:
        return DEFAULT_CATEGORIES
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            continue
    return tuple(out) if out else DEFAULT_CATEGORIES


def _parse_offer(item: dict) -> Job | None:
    """Map one JSON offer to a `Job`. Returns None on missing essentials."""
    if not isinstance(item, dict):
        return None
    slug = (item.get("slug") or "").strip()
    title = (item.get("title") or "").strip()
    if not slug or not title:
        return None
    url = JOB_URL_PREFIX + slug
    company = (item.get("companyName") or "").strip()
    city = (item.get("city") or "").strip()
    workplace = (item.get("workplaceType") or "").strip()
    extra_cities = []
    multi = item.get("multilocation")
    if isinstance(multi, list):
        for loc in multi:
            if not isinstance(loc, dict):
                continue
            c = (loc.get("city") or "").strip()
            if c and c.lower() != city.lower():
                extra_cities.append(c)
    location_parts = [city] if city else []
    if extra_cities:
        location_parts.append("+ " + ", ".join(extra_cities[:3]))
    if workplace and workplace.lower() != "office":
        location_parts.append(f"({workplace})")
    location = " ".join(location_parts).strip()

    skills = item.get("requiredSkills")
    skill_str = ""
    if isinstance(skills, list) and skills:
        skill_str = ", ".join(s for s in (str(x).strip() for x in skills) if s)[:240]

    salary_str = ""
    employment = item.get("employmentTypes")
    if isinstance(employment, list) and employment:
        for emp in employment:
            if not isinstance(emp, dict):
                continue
            lo = emp.get("from")
            hi = emp.get("to")
            cur = (emp.get("currency") or "").strip()
            etype = (emp.get("type") or "").strip().upper()
            if lo or hi:
                rng = f"{lo or '?'}–{hi or '?'} {cur}".strip()
                salary_str = f"{etype} {rng}".strip()
                break

    snippet_bits = []
    exp = (item.get("experienceLevel") or "").strip()
    if exp:
        snippet_bits.append(f"level={exp}")
    if skill_str:
        snippet_bits.append(f"skills={skill_str}")
    if salary_str:
        snippet_bits.append(f"salary={salary_str}")
    snippet = "; ".join(snippet_bits)[:500]

    return Job(
        source="justjoinit",
        external_id=slug,
        title=fix_mojibake(title),
        company=fix_mojibake(company),
        location=fix_mojibake(location),
        url=url,
        posted_at=(item.get("publishedAt") or ""),
        snippet=fix_mojibake(snippet),
        salary=salary_str,
    )


def fetch(filters: dict) -> list[Job]:
    """One JSON page per requested category, deduped by slug.

    Reads `filters['max_per_source']` for the per-category page size (and the
    final cap). Multiple categories run sequentially; results dedupe by slug.
    Forensic-instrumented per request.
    """
    cap = int(filters.get("max_per_source") or 30)
    categories = _categories_from_env()
    seen_slugs: set[str] = set()
    out: list[Job] = []
    status_codes: list[int | None] = []
    err_payload: dict | None = None

    for cat in categories:
        if len(out) >= cap:
            break
        per_cat_remaining = cap - len(out)
        # JustJoin's API expects array params with bracket syntax
        # (`categories[0]=1`) — single-key `categories=1` returns 400.
        params = [
            ("categories[0]", str(cat)),
            ("perPage", str(min(per_cat_remaining, 50))),
            ("sortBy", "newest"),
        ]
        offers: list[dict] = []
        status_code: int | None = None
        try:
            resp = requests.get(API, params=params, headers=UA, timeout=20)
            status_code = resp.status_code
            resp.raise_for_status()
            payload = resp.json()
            # API shape: { "data": [...], "meta": {...} } OR a bare list,
            # depending on endpoint version. Tolerate both.
            if isinstance(payload, dict):
                offers = payload.get("data") or payload.get("offers") or []
            elif isinstance(payload, list):
                offers = payload
        except requests.RequestException as e:
            log.error("justjoinit fetch failed (cat=%s): %s", cat, e)
            err_payload = {"class": type(e).__name__, "message": str(e)[:300]}
        except ValueError as e:  # JSON decode
            log.error("justjoinit json parse failed (cat=%s): %s", cat, e)
            err_payload = {"class": "JSONDecodeError", "message": str(e)[:300]}
        status_codes.append(status_code)

        added = 0
        for item in offers:
            if len(out) >= cap:
                break
            slug = (item.get("slug") or "").strip() if isinstance(item, dict) else ""
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            job = _parse_offer(item)
            if job is None:
                continue
            out.append(job)
            added += 1
        log.info("justjoinit[cat=%s] status=%s offers=%d added=%d (total=%d)",
                 cat, status_code, len(offers), added, len(out))

        if forensic is not None:
            try:
                forensic.log_step(
                    "sources.justjoinit.page",
                    input={"category": cat, "perPage": dict(params).get("perPage")},
                    output={
                        "status_code": status_code,
                        "offers_seen": len(offers),
                        "added": added,
                        "running_total": len(out),
                        "sample_titles": [j.title[:80] for j in out[-3:]],
                    },
                    error=err_payload,
                )
            except Exception:
                log.debug("justjoinit forensic emit failed", exc_info=True)
        err_payload = None  # reset between categories

    return out
