"""Indeed source. Two paths:

  1. RSS (preferred): Indeed used to expose per-query RSS feeds at
     https://<tld>.indeed.com/rss?q=...&l=...  -- still works in most regions.
  2. HTML scraping fallback: parse /jobs?q=...&l=...  (fragile; blocked often).

NOTE: Indeed's TOS restricts automated scraping. Respect robots.txt and use a
conservative request cadence. This adapter is disabled by default in
config/filters.yaml.
"""
from __future__ import annotations

import logging
import time
import urllib.parse as urlparse

import feedparser
import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (compatible; FindJobs-Bot/1.0)"}


def _rss_url(q: str, l: str, fromage: int = 1, tld: str = "www") -> str:
    base = f"https://{tld}.indeed.com/rss"
    return f"{base}?{urlparse.urlencode({'q': q, 'l': l, 'fromage': fromage, 'sort': 'date'})}"


def _passes(text: str, filters: dict) -> bool:
    t = (text or "").lower()
    req = [k.lower() for k in (filters.get("required_keywords") or []) if k]
    if any(k not in t for k in req):
        return False
    excl = [k.lower() for k in (filters.get("exclude_keywords") or []) if k]
    if any(k in t for k in excl):
        return False
    return True


def fetch(filters: dict) -> list[Job]:
    cfg = filters.get("indeed") or {}
    q = cfg.get("q") or ""
    l = cfg.get("l") or ""
    fromage = int(cfg.get("fromage") or 1)
    if not q:
        log.info("indeed: no query configured, skipping")
        return []

    url = _rss_url(q, l, fromage)
    cap = int(filters.get("max_per_source") or 10)
    jobs: list[Job] = []
    try:
        # feedparser will issue the request for us; pass UA via request_headers.
        parsed = feedparser.parse(url, request_headers=UA)
        if parsed.bozo:
            log.warning("indeed RSS parse warning: %s", parsed.bozo_exception)
        for entry in parsed.entries[: cap * 2]:  # over-fetch, filter after
            text = " ".join([entry.get("title", ""), entry.get("summary", "")])
            if not _passes(text, filters):
                continue
            title = entry.get("title", "")
            # Indeed titles look like "Senior Engineer - Company - City, ST"
            parts = [p.strip() for p in title.split(" - ")]
            role = parts[0] if parts else title
            company = parts[1] if len(parts) >= 2 else ""
            location = parts[2] if len(parts) >= 3 else ""
            jobs.append(Job(
                source="indeed",
                external_id=entry.get("id") or entry.get("link", ""),
                title=fix_mojibake(role),
                company=fix_mojibake(company),
                location=fix_mojibake(location),
                url=entry.get("link", ""),
                posted_at=entry.get("published", ""),
                snippet=clean_snippet(entry.get("summary") or "", max_chars=400),
            ))
            if len(jobs) >= cap:
                break
        # Polite pause between subsequent calls (if the caller loops multiple queries)
        time.sleep(1.0)
    except Exception as e:
        log.error("indeed fetch failed: %s", e)
    return jobs
