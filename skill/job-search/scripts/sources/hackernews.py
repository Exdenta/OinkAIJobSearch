"""HackerNews 'Ask HN: Who is hiring?' source.

Strategy:
  1. Hit HN's Algolia search for the most recent "Ask HN: Who is hiring?" thread.
  2. Fetch its top-level comments via the Firebase HN API.
  3. Each comment is one posting; we parse out company/location/remote hints.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from dedupe import Job
from text_utils import strip_html, fix_mojibake

log = logging.getLogger(__name__)

ALGOLIA = "https://hn.algolia.com/api/v1/search"
FIREBASE = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

def _latest_thread_id() -> int | None:
    r = requests.get(ALGOLIA, params={
        "tags": "story,author_whoishiring",
        "query": "Ask HN: Who is hiring?",
        "hitsPerPage": 5,
    }, timeout=15)
    r.raise_for_status()
    hits = r.json().get("hits", [])
    if not hits:
        return None
    # Most recent first
    hits.sort(key=lambda h: h.get("created_at_i", 0), reverse=True)
    return int(hits[0]["objectID"])


def _fetch_item(item_id: int) -> dict[str, Any] | None:
    r = requests.get(FIREBASE.format(id=item_id), timeout=10)
    if not r.ok:
        return None
    return r.json()


def _passes_filters(text: str, filters: dict) -> bool:
    t = text.lower()
    req = [k.lower() for k in (filters.get("required_keywords") or [])]
    for k in req:
        if k and k not in t:
            return False
    excl = [k.lower() for k in (filters.get("exclude_keywords") or [])]
    for k in excl:
        if k and k in t:
            return False
    kws = [k.lower() for k in (filters.get("keywords") or [])]
    if kws and not any(k in t for k in kws):
        return False
    # Remote preference
    remote_pref = (filters.get("remote") or "any").lower()
    has_remote = "remote" in t
    if remote_pref == "require" and not has_remote:
        return False
    if remote_pref == "exclude" and has_remote:
        return False
    # Locations
    locs = [l.lower() for l in (filters.get("locations") or [])]
    if locs and not any(l in t for l in locs):
        return False
    return True


def _extract_company(text: str) -> str:
    """HN Who-is-hiring posts typically start with 'Company | Role | Location'."""
    first = text.split("|", 1)[0].strip()
    # Take first ~60 chars as company name
    return first[:60]


def fetch(filters: dict) -> list[Job]:
    jobs: list[Job] = []
    try:
        thread_id = _latest_thread_id()
        if not thread_id:
            log.warning("hackernews: no who-is-hiring thread found")
            return []
        thread = _fetch_item(thread_id)
        if not thread:
            return []
        kids = thread.get("kids", []) or []
        max_age = int(filters.get("max_age_hours") or 24) * 3600
        now = int(time.time())
        cap = int(filters.get("max_per_source") or 10)
        for kid in kids:
            if len(jobs) >= cap:
                break
            item = _fetch_item(kid)
            if not item or item.get("deleted") or item.get("dead"):
                continue
            # We intentionally allow older comments too — HN threads stay relevant
            # for the month — but users can tighten via max_age_hours.
            if max_age and (now - int(item.get("time", now))) > max_age * 30:
                # very-old cutoff (30× the hours window)
                continue
            raw_html = item.get("text") or ""
            text = fix_mojibake(strip_html(raw_html))
            if not text:
                continue
            if not _passes_filters(text, filters):
                continue
            company = _extract_company(text)
            jobs.append(Job(
                source="hackernews",
                external_id=str(item["id"]),
                title=text[:80].rstrip(" .,|") + ("…" if len(text) > 80 else ""),
                company=company,
                location="",
                url=f"https://news.ycombinator.com/item?id={item['id']}",
                posted_at=time.strftime("%Y-%m-%d", time.gmtime(item.get("time", now))),
                snippet=text,
            ))
    except requests.RequestException as e:
        log.error("hackernews fetch failed: %s", e)
    return jobs
