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
import forensic

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


def _fetch_remoteok(filters: dict) -> list[Job]:
    out: list[Job] = []
    status_code = None
    err_payload = None
    try:
        r = requests.get(REMOTEOK_URL, headers=UA, timeout=20)
        status_code = r.status_code
        r.raise_for_status()
        data = r.json()
        # First element is the metadata ("legal" disclaimer) -- skip it.
        # No pre-filter — AI scoring downstream is the sole matching gate.
        for item in data[1:]:
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
        err_payload = {"class": type(e).__name__, "message": str(e)[:300]}
    forensic.log_step(
        "remote_boards._fetch_remoteok",
        input={"endpoint": REMOTEOK_URL},
        output={
            "status_code": status_code,
            "count": len(out),
            "sample_titles": [j.title[:80] for j in out[:3]],
        },
        error=err_payload,
    )
    return out


def _fetch_remotive(filters: dict) -> list[Job]:
    out: list[Job] = []
    status_code = None
    err_payload = None
    try:
        r = requests.get(REMOTIVE_URL, headers=UA, timeout=20)
        status_code = r.status_code
        r.raise_for_status()
        for item in r.json().get("jobs", []):
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
        err_payload = {"class": type(e).__name__, "message": str(e)[:300]}
    forensic.log_step(
        "remote_boards._fetch_remotive",
        input={"endpoint": REMOTIVE_URL},
        output={
            "status_code": status_code,
            "count": len(out),
            "sample_titles": [j.title[:80] for j in out[:3]],
        },
        error=err_payload,
    )
    return out


def _fetch_wwr(filters: dict) -> list[Job]:
    """Fetch WWR RSS feeds.

    NOTE: feedparser.parse(url) uses Python's stdlib urllib for HTTP, which on
    some installs (notably python.org macOS builds without
    `Install Certificates.command` having been run) lacks a CA bundle and
    fails SSL verification against Cloudflare-fronted hosts like WWR. The
    symptom is `bozo=True` with a `URLError(SSLCertVerificationError ...)`
    and zero entries. Workaround: fetch the bytes with `requests` (which
    uses certifi) and hand them to feedparser. This also matches the pattern
    used by `_fetch_remoteok` / `_fetch_remotive`.
    """
    out: list[Job] = []
    per_feed_counts: list[dict] = []
    for feed_url in WWR_FEEDS:
        feed_count = 0
        feed_err = None
        status_code = None
        bozo_flag = None
        bozo_msg = None
        parsed = None
        try:
            r = requests.get(feed_url, headers=UA, timeout=20)
            status_code = r.status_code
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            bozo_flag = bool(getattr(parsed, "bozo", 0))
            if bozo_flag:
                bozo_msg = repr(getattr(parsed, "bozo_exception", None))[:200]
                # feedparser is permissive — bozo doesn't always mean unusable.
                # Log it but continue iterating entries if any landed.
                log.warning(
                    "wwr feed %s bozo=True (status=%s): %s",
                    feed_url, status_code, bozo_msg,
                )
            for entry in parsed.entries:
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
                feed_count += 1
        except requests.RequestException as e:
            log.error("wwr feed %s fetch failed: %s", feed_url, e)
            feed_err = {"class": type(e).__name__, "message": str(e)[:200]}
        except Exception as e:
            log.error("wwr feed %s parse failed: %s", feed_url, e)
            feed_err = {"class": type(e).__name__, "message": str(e)[:200]}
        per_feed_counts.append({
            "feed_url": feed_url,
            "status_code": status_code,
            "count": feed_count,
            "bozo": bozo_flag,
            "bozo_exception": bozo_msg,
            "error": feed_err,
        })
    forensic.log_step(
        "remote_boards._fetch_wwr",
        input={"feed_count": len(WWR_FEEDS)},
        output={
            "total": len(out),
            "per_feed": per_feed_counts,
        },
    )
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
