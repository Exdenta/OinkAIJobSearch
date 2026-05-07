"""LinkedIn Jobs source — HTML scraping of the public search endpoint.

LinkedIn's `guest_jobs` endpoint returns search results without requiring auth:

    https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
        ?keywords=...&location=...&f_TPR=r86400&start=0

Each card is an <li class="result-card">. We rely on LinkedIn's `f_TPR=r86400`
(past-24h) filter to keep things fresh, and walk a small page sequence
(start=0, then 10, 25) per query when page 1 doesn't fill the per-query budget.

Single entry point: `fetch_for_user(filters, user_seeds)` — per-user path that
runs up to `MAX_USER_QUERIES` (3) queries from `user_seeds["queries"]`. Each
query gets its OWN budget of `PER_QUERY_CAP` postings (no shared total cap —
that legacy behavior starved later queries). Queries are reordered via
`_diversify_geo` so consecutive ones hit distinct geos. Dedupes by URL across
queries AND pages. Returns [] when the profile has no LinkedIn seeds.

NOTE: LinkedIn's TOS prohibits automated scraping. Use this only for personal,
low-volume, non-commercial purposes, and don't hammer the endpoint. Toggle
via `defaults.DEFAULTS["sources"]["linkedin"]`.
"""
from __future__ import annotations

import logging
import time

import requests
from bs4 import BeautifulSoup

from dedupe import Job
from text_utils import fix_mojibake
import forensic

log = logging.getLogger(__name__)

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html",
}

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

# Cap the number of per-user queries we'll run in one pass. LinkedIn is
# rate-sensitive; we enforce a hard cap here so a hand-edited profile
# can't run away. 5 queries × 3 pages = 15 HTTP requests per user — still
# well under LinkedIn's anonymous rate limit when paced.
MAX_USER_QUERIES = 5

# Per-query cap: each profile query runs independently up to this many
# postings. Total jobs returned by `fetch_for_user` is bounded by
# `MAX_USER_QUERIES * PER_QUERY_CAP`, NOT a single shared `max_per_source`
# ceiling — that legacy behavior caused query 1 to saturate the budget and
# starve queries 2..N (e.g. "frontend Spain" took the whole 12, "vue Germany"
# never executed). Setting it to 10 matches the LinkedIn page size so a
# single page per query fills it; pagination kicks in only when fewer than
# this many results come back from page 1.
PER_QUERY_CAP = 30

# How many additional pages to try per query when page 1 is short of
# `PER_QUERY_CAP`. LinkedIn returns 25 cards per page, so 2 extra pages
# yields up to 75 candidates per query while keeping request count modest.
PAGINATION_STARTS = (10, 25)

# Polite pause between back-to-back LinkedIn requests. Kept as a named
# constant so tests can monkeypatch it.
PACE_SECONDS = 1.5


def _one_search(
    *,
    q: str,
    geo: str,
    f_TPR: str,
    remote: str,
    cap_remaining: int,
    filters: dict,
    seen_urls: set[str],
    start: int = 0,
) -> list[Job]:
    """Run ONE LinkedIn search page and parse it into Job records.

    Shared between `fetch` (global single query) and `fetch_for_user`
    (per-user multi-query). Returns early if the HTTP layer signals
    rate-limit — the caller decides whether to back off the rest of the batch.

    `cap_remaining` is how many MORE jobs we're allowed to emit; the caller
    tracks the running total across queries. Callers also pass in the
    `seen_urls` set so duplicates across queries collapse in one place.

    `start` is the LinkedIn paging offset (0, 10, 25, ...). Page 1 = 0.
    """
    if cap_remaining <= 0 or not q:
        return []

    params: dict[str, str | int] = {
        "keywords": q,
        "location": geo or "",
        "f_TPR": f_TPR or "r86400",
        "start": int(start),
    }
    # Remote preference → LinkedIn's `f_WT=2` filter. Accepts "remote" from
    # the profile enum (and the legacy "require" alias for safety).
    if (remote or "").lower() in ("require", "remote"):
        params["f_WT"] = "2"

    out: list[Job] = []
    status_code = None
    cards_count = 0
    err_payload = None
    body_head = None
    rate_limited = False
    try:
        resp = requests.get(SEARCH, params=params, headers=UA, timeout=20)
        status_code = resp.status_code
        if resp.status_code == 429:
            log.warning("linkedin: rate-limited (429) on q=%r, skipping", q)
            rate_limited = True
            body_head = (resp.text or "")[:300]
            forensic.log_step(
                "linkedin._one_search",
                input={"q": q, "geo": geo, "f_TPR": f_TPR, "remote": remote},
                output={
                    "status_code": status_code,
                    "rate_limited": True,
                    "body_head": body_head,
                    "count": 0,
                },
            )
            raise _RateLimited()
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("li") or soup.select("div.base-card")
        cards_count = len(cards)
        for card in cards:
            if len(out) >= cap_remaining:
                break
            a = card.find("a", class_="base-card__full-link") or card.find("a")
            if not a:
                continue
            url = (a.get("href") or "").split("?")[0]
            if not url or url in seen_urls:
                continue
            title_el = card.find("h3")
            company_el = card.find("h4")
            loc_el = card.find("span", class_="job-search-card__location")
            title = (title_el.get_text(strip=True) if title_el else a.get_text(strip=True))
            company = company_el.get_text(strip=True) if company_el else ""
            location = loc_el.get_text(strip=True) if loc_el else ""
            # No pre-filter — AI scoring downstream is the sole matching gate.
            seen_urls.add(url)
            out.append(Job(
                source="linkedin",
                external_id=url,
                title=fix_mojibake(title),
                company=fix_mojibake(company),
                location=fix_mojibake(location),
                url=url,
                posted_at="",
                snippet="",
            ))
        # Capture body head when 200 but ZERO results — strong signal for
        # selector rot or an empty search. Cheap to log on a per-query basis.
        if not out and resp.text:
            body_head = (resp.text or "")[:500]
    except _RateLimited:
        raise
    except requests.RequestException as e:
        log.error("linkedin fetch failed (q=%r): %s", q, e)
        err_payload = {"class": type(e).__name__, "message": str(e)[:300]}
    if not rate_limited:
        forensic.log_step(
            "linkedin._one_search",
            input={"q": q, "geo": geo, "f_TPR": f_TPR, "remote": remote, "cap_remaining": cap_remaining},
            output={
                "status_code": status_code,
                "cards_seen": cards_count,
                "count": len(out),
                "sample_titles": [j.title[:80] for j in out[:3]],
                "body_head_on_zero": body_head,
            },
            error=err_payload,
        )
    return out


class _RateLimited(Exception):
    """Sentinel: LinkedIn returned 429 for this request."""


def _diversify_geo(queries: list[dict]) -> list[dict]:
    """Reorder queries so consecutive entries hit different `geo` values.

    The previous ordering sometimes ran two queries against the same geo
    back-to-back (e.g. Spain frontend Q1, Spain remote Q2), which deduped
    heavily and starved the Q3 geo. We greedy-pick each next query so its
    geo differs from the one just chosen — preserving the original order
    among queries with the same geo, but interleaving across distinct geos.
    """
    if len(queries) <= 1:
        return list(queries)
    remaining = list(queries)
    out: list[dict] = []
    last_geo: str | None = None
    while remaining:
        # Prefer a query whose geo differs from `last_geo`; fall back to the
        # head of the list if every remaining entry shares the same geo.
        pick_idx = next(
            (i for i, q in enumerate(remaining) if q.get("geo") != last_geo),
            0,
        )
        chosen = remaining.pop(pick_idx)
        out.append(chosen)
        last_geo = chosen.get("geo")
    return out


def fetch_for_user(filters: dict, user_seeds: dict | None) -> list[Job]:
    """Per-user LinkedIn fetch — runs up to 3 queries shaped by the profile.

    `user_seeds` is the `search_seeds.linkedin` dict from the user's profile:

        {"queries": [{"q": "...", "geo": "...", "f_TPR": "r86400"}, ...]}

    For each query we run a small page sequence, collect matching cards,
    and fold them into the combined result. Behavior:

      * enforce a hard cap of `MAX_USER_QUERIES` (3) queries;
      * each query gets its OWN budget of `PER_QUERY_CAP` postings — no
        shared total cap. Q1 saturating the budget no longer starves Q2/Q3;
      * if page 1 returns fewer than `PER_QUERY_CAP` unique results, walk
        `PAGINATION_STARTS` (start=10, then start=25) until the budget fills
        or LinkedIn runs out of results;
      * `_diversify_geo` reorders queries so the i-th query's geo differs
        from the (i-1)-th when possible — keeps each geo's first page fresh
        (LinkedIn dedups same-geo searches heavily);
      * dedupe on URL across queries AND pages;
      * sleep `PACE_SECONDS` between requests (but NOT after the final one);
      * stop early on rate-limit — within a query we abort that query's
        pagination; across queries we abort the whole batch.

    If `user_seeds` is None / missing / has no usable queries, returns []
    — there's no global default query to fall back to. Users get LinkedIn
    results only after the Opus profile builder has produced search seeds.
    """
    queries: list[dict] = []
    if isinstance(user_seeds, dict):
        raw_queries = user_seeds.get("queries") or []
        if isinstance(raw_queries, list):
            for item in raw_queries[:MAX_USER_QUERIES]:
                if not isinstance(item, dict):
                    continue
                q = str(item.get("q") or "").strip()
                if not q:
                    continue
                queries.append({
                    "q": q[:200],
                    "geo": str(item.get("geo") or "").strip()[:80],
                    "f_TPR": str(item.get("f_TPR") or "r86400").strip()[:12],
                })

    if not queries:
        log.debug("linkedin: no user_seeds provided, returning []")
        return []

    queries = _diversify_geo(queries)
    remote = str(filters.get("remote") or "")
    seen_urls: set[str] = set()
    combined: list[Job] = []
    request_idx = 0

    for q_idx, query in enumerate(queries):
        per_query_total = 0
        # Page sequence: start=0 always, then PAGINATION_STARTS if budget
        # not yet filled. Stops as soon as a page returns 0 (no more results).
        for start in (0, *PAGINATION_STARTS):
            remaining = PER_QUERY_CAP - per_query_total
            if remaining <= 0:
                break
            if request_idx > 0:
                time.sleep(PACE_SECONDS)
            request_idx += 1
            try:
                batch = _one_search(
                    q=query["q"],
                    geo=query["geo"],
                    f_TPR=query["f_TPR"],
                    remote=remote,
                    cap_remaining=remaining,
                    filters=filters,
                    seen_urls=seen_urls,
                    start=start,
                )
            except _RateLimited:
                log.warning("linkedin: rate-limited on query %d/%d (start=%d), aborting batch",
                            q_idx + 1, len(queries), start)
                return combined
            combined.extend(batch)
            per_query_total += len(batch)
            log.info(
                "linkedin[user]: query %d/%d %r @ %r start=%d → %d jobs (q-total %d, run-total %d)",
                q_idx + 1, len(queries), query["q"], query["geo"],
                start, len(batch), per_query_total, len(combined),
            )
            # Empty page → no point trying further offsets for this query.
            if not batch:
                break

    return combined
