"""Remote-focused boards: RemoteOK, Remotive, WeWorkRemotely.

All three expose structured feeds, so no HTML scraping is needed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import feedparser
import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

REMOTEOK_URL = "https://remoteok.com/api"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
]


def _passes(text: str, filters: dict) -> bool:
    t = (text or "").lower()
    req = [k.lower() for k in (filters.get("required_keywords") or []) if k]
    if any(k not in t for k in req):
        return False
    excl = [k.lower() for k in (filters.get("exclude_keywords") or []) if k]
    if any(k in t for k in excl):
        return False
    kws = [k.lower() for k in (filters.get("keywords") or []) if k]
    if kws and not any(k in t for k in kws):
        return False
    return True


def _fetch_remoteok(filters: dict) -> list[Job]:
    out: list[Job] = []
    try:
        r = requests.get(REMOTEOK_URL, headers=UA, timeout=20)
        r.raise_for_status()
        data = r.json()
        # First element is the metadata ("legal" disclaimer) -- skip it.
        for item in data[1:]:
            text = " ".join([item.get("position", ""), item.get("company", ""),
                             item.get("description", ""), " ".join(item.get("tags", []))])
            if not _passes(text, filters):
                continue
            out.append(Job(
                source="remoteok",
                external_id=str(item.get("id") or item.get("slug") or item.get("url", "")),
                title=fix_mojibake(item.get("position", "")),
                company=fix_mojibake(item.get("company", "")),
                location=fix_mojibake(item.get("location") or "Remote"),
                url=item.get("url") or item.get("apply_url") or "",
                posted_at=item.get("date", ""),
                snippet=clean_snippet(item.get("description") or "", max_chars=400),
                salary=_fmt_salary(item.get("salary_min"), item.get("salary_max")),
            ))
    except requests.RequestException as e:
        log.error("remoteok fetch failed: %s", e)
    return out


def _fetch_remotive(filters: dict) -> list[Job]:
    out: list[Job] = []
    try:
        r = requests.get(REMOTIVE_URL, headers=UA, timeout=20)
        r.raise_for_status()
        for item in r.json().get("jobs", []):
            text = " ".join([item.get("title", ""), item.get("company_name", ""),
                             item.get("description", ""), " ".join(item.get("tags", []))])
            if not _passes(text, filters):
                continue
            out.append(Job(
                source="remotive",
                external_id=str(item.get("id")),
                title=fix_mojibake(item.get("title", "")),
                company=fix_mojibake(item.get("company_name", "")),
                location=fix_mojibake(item.get("candidate_required_location") or "Remote"),
                url=item.get("url", ""),
                posted_at=item.get("publication_date", ""),
                snippet=clean_snippet(item.get("description") or "", max_chars=400),
                salary=fix_mojibake(item.get("salary", "")),
            ))
    except requests.RequestException as e:
        log.error("remotive fetch failed: %s", e)
    return out


def _fetch_wwr(filters: dict) -> list[Job]:
    out: list[Job] = []
    for feed_url in WWR_FEEDS:
        try:
            parsed = feedparser.parse(feed_url, request_headers=UA)
            for entry in parsed.entries:
                text = " ".join([entry.get("title", ""), entry.get("summary", "")])
                if not _passes(text, filters):
                    continue
                title = entry.get("title", "")
                # WWR titles are "Company: Role Title"
                company, _, role = title.partition(":")
                out.append(Job(
                    source="weworkremotely",
                    external_id=entry.get("id") or entry.get("link", ""),
                    title=fix_mojibake((role or title).strip()),
                    company=fix_mojibake(company.strip()) if role else "",
                    location="Remote",
                    url=entry.get("link", ""),
                    posted_at=entry.get("published", ""),
                    snippet=clean_snippet(entry.get("summary") or "", max_chars=400),
                ))
        except Exception as e:
            log.error("wwr feed %s failed: %s", feed_url, e)
    return out


def _fmt_salary(lo, hi) -> str:
    if not lo and not hi:
        return ""
    try:
        lo = int(lo) if lo else None
        hi = int(hi) if hi else None
    except (TypeError, ValueError):
        return ""
    if lo and hi:
        return f"${lo // 1000}k–${hi // 1000}k"
    return f"${(lo or hi) // 1000}k+"


def fetch(filters: dict) -> list[Job]:
    """Aggregate all enabled remote boards respecting per-source toggles."""
    srcs = (filters.get("sources") or {})
    cap = int(filters.get("max_per_source") or 10)

    all_jobs: list[Job] = []
    if srcs.get("remoteok", True):
        all_jobs += _fetch_remoteok(filters)[:cap]
    if srcs.get("remotive", True):
        all_jobs += _fetch_remotive(filters)[:cap]
    if srcs.get("weworkremotely", True):
        all_jobs += _fetch_wwr(filters)[:cap]
    return all_jobs
