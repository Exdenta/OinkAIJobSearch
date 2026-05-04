"""HackerNews 'Ask HN: Who is hiring?' source.

Strategy:
  1. Locate the most recent monthly "Ask HN: Who is hiring?" thread via
     Algolia. We hit `/search_by_date` (chronological — `/search` ranks by
     RELEVANCE which pinned a 2020 "Ask HN: Who is hiring right now?" thread
     to position 0 indefinitely). Tags `story,author_whoishiring` (comma=AND)
     scope to whoishiring's stories. `numericFilters=created_at_i>{now-90d}`
     bounds the lookup window. Hits are scanned newest-first; titles are
     validated against /Ask HN.*Who is hiring/i so the sibling threads
     ("Who wants to be hired?", "Freelancer? Seeking freelancer?") are
     skipped. A final freshness check rejects anything older than 60 days
     so a stale-thread bug doesn't burn 190s scanning aged-out comments.
  2. Fetch the thread's top-level comments via the Firebase HN API in
     parallel — `concurrent.futures.ThreadPoolExecutor` with 8 workers and
     a 64-future sliding submission window so cap-based and stale-streak
     early-stops can cancel pending work. Sequential fetch was ~190s on a
     500-kid thread; parallel runs ~30s.
  3. Each comment is one posting; we parse out company.

There is no per-comment filter — AI scoring downstream is the sole matching
gate. Forensic logs the kids_count + skipped breakdown + sample_titles so a
0-result run is debuggable post-hoc without a re-run.
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

import requests

from dedupe import Job
from text_utils import strip_html, fix_mojibake
import forensic

log = logging.getLogger(__name__)

ALGOLIA_BY_DATE = "https://hn.algolia.com/api/v1/search_by_date"
FIREBASE = "https://hacker-news.firebaseio.com/v0/item/{id}.json"

# Title must literally look like the monthly Who-is-hiring thread.
_WHO_IS_HIRING_RE = re.compile(r"Ask HN.*Who is hiring", re.IGNORECASE)

# Hard freshness ceiling for the located thread (seconds). 60 days.
_MAX_THREAD_AGE_S = 60 * 86400
# How far back we ask Algolia to look. Slightly wider than the freshness
# ceiling so a delayed-by-a-few-days post still surfaces.
_ALGOLIA_LOOKBACK_S = 90 * 86400

# Concurrency knobs (module-level so tests can monkeypatch).
MAX_WORKERS = 8        # in-flight cap; HN/Firebase generous, we are polite
EARLY_STOP_STALE = 20  # consecutive past-cutoff kids before bail
SUBMIT_WINDOW = 64     # how many futures to keep queued ahead of cursor


def _latest_thread_id() -> int | None:
    """Return the HN item id of the most recent monthly Who-is-hiring thread.

    Returns None (logs a warning) when no matching thread is found within
    the last 60 days — search_jobs.py treats that as "skip HN this run".
    """
    now = int(time.time())
    cutoff = now - _ALGOLIA_LOOKBACK_S
    try:
        r = requests.get(ALGOLIA_BY_DATE, params={
            "tags": "story,author_whoishiring",
            "numericFilters": f"created_at_i>{cutoff}",
            "hitsPerPage": 20,
        }, timeout=15)
        forensic.log_step(
            "hackernews._latest_thread_id",
            input={"endpoint": ALGOLIA_BY_DATE, "lookback_days": _ALGOLIA_LOOKBACK_S // 86400},
            output={
                "status_code": r.status_code,
                "ok": r.ok,
                "body_head": (r.text or "")[:300] if not r.ok else None,
            },
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("hackernews: Algolia lookup failed: %s", e)
        forensic.log_step(
            "hackernews._latest_thread_id",
            error={"class": type(e).__name__, "message": str(e)[:300]},
        )
        return None

    hits = r.json().get("hits", []) or []
    # Defensive sort: search_by_date is already newest-first, but don't trust it.
    hits.sort(key=lambda h: h.get("created_at_i", 0), reverse=True)

    for h in hits:
        title = (h.get("title") or "").strip()
        created = int(h.get("created_at_i") or 0)
        if not _WHO_IS_HIRING_RE.search(title):
            continue
        if created <= 0 or (now - created) > _MAX_THREAD_AGE_S:
            log.warning(
                "hackernews: newest matching thread %s (%s) is older than 60 days; skipping",
                h.get("objectID"), title,
            )
            forensic.log_step(
                "hackernews._latest_thread_id",
                output={
                    "rejected_id": h.get("objectID"),
                    "title": title,
                    "age_days": (now - created) / 86400,
                    "reason": "older than 60 days",
                },
            )
            return None
        try:
            tid = int(h["objectID"])
            forensic.log_step(
                "hackernews._latest_thread_id",
                output={
                    "thread_id": tid,
                    "title": title,
                    "age_days": (now - created) / 86400,
                    "hits_total": len(hits),
                },
            )
            return tid
        except (KeyError, TypeError, ValueError):
            continue

    log.warning("hackernews: no Who-is-hiring thread found in last %d days",
                _ALGOLIA_LOOKBACK_S // 86400)
    forensic.log_step(
        "hackernews._latest_thread_id",
        output={"hits_count": len(hits), "reason": "no matching title in window"},
    )
    return None


def _fetch_item(item_id: int) -> dict[str, Any] | None:
    """Fetch one HN Firebase item. Returns None on non-200.

    Forensic logs only on non-200 to avoid one line per comment (HN threads
    can have hundreds of kids). Errors are the interesting case.
    """
    r = requests.get(FIREBASE.format(id=item_id), timeout=10)
    if not r.ok:
        forensic.log_step(
            "hackernews._fetch_item",
            input={"item_id": item_id},
            output={
                "status_code": r.status_code,
                "ok": False,
                "body_head": (r.text or "")[:300],
            },
        )
        return None
    return r.json()


def _extract_company(text: str) -> str:
    """HN Who-is-hiring posts typically start with 'Company | Role | Location'."""
    first = text.split("|", 1)[0].strip()
    return first[:60]


def fetch(filters: dict) -> list[Job]:
    """Fetch HN 'who is hiring' postings in parallel. See module docstring.

    Concurrency model: ThreadPoolExecutor(MAX_WORKERS=8) with a sliding
    submission window of SUBMIT_WINDOW=64 futures ahead of the consumption
    cursor. Two early-stop conditions (cap met, EARLY_STOP_STALE consecutive
    past-cutoff kids) cancel pending futures and break the consume loop.
    Output is deterministic (cursor-ordered consumption).
    """
    jobs: list[Job] = []
    skipped = {
        "deleted_or_dead": 0,
        "too_old": 0,
        "empty_text": 0,
        "fetch_failed": 0,
        "cancelled_after_cap": 0,
        "early_stop_stale": 0,
    }
    sample_titles: list[str] = []
    kids_count = 0
    thread_id: int | None = None

    try:
        thread_id = _latest_thread_id()
        if not thread_id:
            forensic.log_step("hackernews.fetch", output={"reason": "no_thread", "jobs": 0})
            return []
        thread = _fetch_item(thread_id)
        if not thread:
            forensic.log_step(
                "hackernews.fetch",
                input={"thread_id": thread_id},
                output={"reason": "thread_fetch_failed", "jobs": 0},
            )
            return []
        kids: list[int] = thread.get("kids", []) or []
        kids_count = len(kids)
        max_age = int(filters.get("max_age_hours") or 24) * 3600
        now = int(time.time())
        cap = int(filters.get("max_per_source") or 10)
        very_old_cutoff = max_age * 30 if max_age else 0

        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="hn-kid")
        pending: dict[int, Future] = {}
        consecutive_stale = 0
        next_to_submit = 0
        cursor = 0
        stop_submitting = False

        def _submit_more() -> None:
            nonlocal next_to_submit
            while (
                not stop_submitting
                and next_to_submit < len(kids)
                and (next_to_submit - cursor) < SUBMIT_WINDOW
            ):
                pending[next_to_submit] = executor.submit(_fetch_item, kids[next_to_submit])
                next_to_submit += 1

        try:
            _submit_more()
            while cursor < len(kids) and pending:
                fut = pending.pop(cursor, None)
                if fut is None:
                    break
                cursor_kid_idx = cursor
                cursor += 1

                try:
                    item = fut.result()
                except Exception as e:
                    log.debug("hackernews: kid %s fetch raised: %s", kids[cursor_kid_idx], e)
                    item = None

                if not stop_submitting:
                    _submit_more()

                if item is None:
                    skipped["fetch_failed"] += 1
                    continue
                if item.get("deleted") or item.get("dead"):
                    skipped["deleted_or_dead"] += 1
                    continue
                if very_old_cutoff and (now - int(item.get("time", now))) > very_old_cutoff:
                    skipped["too_old"] += 1
                    consecutive_stale += 1
                    if consecutive_stale >= EARLY_STOP_STALE:
                        log.info(
                            "hackernews: early-stop after %d consecutive past-cutoff kids",
                            consecutive_stale,
                        )
                        skipped["early_stop_stale"] = consecutive_stale
                        stop_submitting = True
                        for f in pending.values():
                            f.cancel()
                        pending.clear()
                        break
                    continue
                consecutive_stale = 0

                raw_html = item.get("text") or ""
                text = fix_mojibake(strip_html(raw_html))
                if not text:
                    skipped["empty_text"] += 1
                    continue
                # No pre-filter — AI scoring downstream is the sole matching gate.
                company = _extract_company(text)
                title = text[:80].rstrip(" .,|") + ("…" if len(text) > 80 else "")
                jobs.append(Job(
                    source="hackernews",
                    external_id=str(item["id"]),
                    title=title,
                    company=company,
                    location="",
                    url=f"https://news.ycombinator.com/item?id={item['id']}",
                    posted_at=time.strftime("%Y-%m-%d", time.gmtime(item.get("time", now))),
                    snippet=text,
                ))
                if len(sample_titles) < 5:
                    sample_titles.append(title)

                if len(jobs) >= cap:
                    skipped["cancelled_after_cap"] = len(pending)
                    stop_submitting = True
                    for f in pending.values():
                        f.cancel()
                    pending.clear()
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        forensic.log_step(
            "hackernews.fetch",
            input={"thread_id": thread_id, "kids_count": kids_count, "cap": cap},
            output={
                "jobs": len(jobs),
                "skipped": skipped,
                "sample_titles": sample_titles,
            },
        )

    except requests.RequestException as e:
        log.error("hackernews fetch failed: %s", e)
        forensic.log_step(
            "hackernews.fetch",
            input={"thread_id": thread_id, "kids_count": kids_count},
            error={"class": type(e).__name__, "message": str(e)[:300]},
            output={"jobs": len(jobs), "skipped": skipped},
        )
    return jobs
