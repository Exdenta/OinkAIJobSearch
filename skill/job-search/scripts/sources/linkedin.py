"""LinkedIn Jobs source — HTML scraping of the public search endpoint.

LinkedIn's `guest_jobs` endpoint returns search results without requiring auth:

    https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search
        ?keywords=...&location=...&f_TPR=r86400&start=0

Each card is an <li class="result-card">. We parse the first page only and rely
on LinkedIn's `f_TPR=r86400` (past-24h) filter to keep things fresh.

Two entry points:
  - `fetch(filters)`: global single-query path, reads `filters["linkedin"]`
    from config/filters.yaml. This runs once per pipeline pass and produces
    the baseline LinkedIn results shared across all users.
  - `fetch_for_user(filters, user_seeds)`: per-user path — runs up to 3
    queries from `user_seeds["queries"]` (derived from the user's profile),
    dedupes by URL, caps at `max_per_source`. Falls back to `fetch(filters)`
    when `user_seeds` is empty, so users without LinkedIn seeds still get
    the global pass.

NOTE: LinkedIn's TOS prohibits automated scraping. Use this only for personal,
low-volume, non-commercial purposes, and don't hammer the endpoint. Disabled
by default in config/filters.yaml.
"""
from __future__ import annotations

import logging
import time

import requests
from bs4 import BeautifulSoup

from dedupe import Job
from text_utils import fix_mojibake

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
# rate-sensitive, and the profile schema already limits the builder to 3
# entries — we enforce it here too in case the profile JSON was hand-edited.
MAX_USER_QUERIES = 3

# Polite pause between back-to-back LinkedIn requests. Kept as a named
# constant so tests can monkeypatch it.
PACE_SECONDS = 1.5


def _passes(text: str, filters: dict) -> bool:
    t = (text or "").lower()
    req = [k.lower() for k in (filters.get("required_keywords") or []) if k]
    if any(k not in t for k in req):
        return False
    excl = [k.lower() for k in (filters.get("exclude_keywords") or []) if k]
    if any(k in t for k in excl):
        return False
    return True


def _one_search(
    *,
    q: str,
    geo: str,
    f_TPR: str,
    remote: str,
    cap_remaining: int,
    filters: dict,
    seen_urls: set[str],
) -> list[Job]:
    """Run ONE LinkedIn search page and parse it into Job records.

    Shared between `fetch` (global single query) and `fetch_for_user`
    (per-user multi-query). Returns early if the HTTP layer signals
    rate-limit — the caller decides whether to back off the rest of the batch.

    `cap_remaining` is how many MORE jobs we're allowed to emit; the caller
    tracks the running total across queries. Callers also pass in the
    `seen_urls` set so duplicates across queries collapse in one place.
    """
    if cap_remaining <= 0 or not q:
        return []

    params: dict[str, str | int] = {
        "keywords": q,
        "location": geo or "",
        "f_TPR": f_TPR or "r86400",
        "start": 0,
    }
    # Remote preference → LinkedIn's `f_WT=2` filter. Accept "require"
    # (config/filters.yaml) and "remote" (profile enum) so both call sites
    # map to the same LinkedIn parameter.
    if (remote or "").lower() in ("require", "remote"):
        params["f_WT"] = "2"

    out: list[Job] = []
    try:
        resp = requests.get(SEARCH, params=params, headers=UA, timeout=20)
        if resp.status_code == 429:
            log.warning("linkedin: rate-limited (429) on q=%r, skipping", q)
            # Surface the 429 through an empty return — caller sees zero
            # and, if it's running multiple queries, should probably stop
            # early. We raise a sentinel instead of returning [] so the
            # caller can distinguish "rate-limited" from "no results".
            raise _RateLimited()
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.select("li") or soup.select("div.base-card")
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
            blob = " ".join([title, company, location])
            if not _passes(blob, filters):
                continue
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
    except _RateLimited:
        raise
    except requests.RequestException as e:
        log.error("linkedin fetch failed (q=%r): %s", q, e)
    return out


class _RateLimited(Exception):
    """Sentinel: LinkedIn returned 429 for this request."""


def fetch(filters: dict) -> list[Job]:
    """Global single-query fetch — reads `filters["linkedin"]` (q/geo/f_TPR).

    This is the baseline LinkedIn pass that runs once per pipeline run and
    feeds every user. Per-user queries are run separately via
    `fetch_for_user` using each profile's stored seeds.
    """
    cfg = filters.get("linkedin") or {}
    q = cfg.get("q") or ""
    geo = cfg.get("geo") or ""
    f_TPR = cfg.get("f_TPR") or "r86400"
    if not q:
        log.info("linkedin: no query configured, skipping")
        return []

    cap = int(filters.get("max_per_source") or 10)
    seen_urls: set[str] = set()
    try:
        jobs = _one_search(
            q=q,
            geo=geo,
            f_TPR=f_TPR,
            remote=str(filters.get("remote") or ""),
            cap_remaining=cap,
            filters=filters,
            seen_urls=seen_urls,
        )
    except _RateLimited:
        return []
    # Keep the polite pause even for the single-query path so callers that
    # invoke both fetch() and another adapter in quick succession don't
    # hammer LinkedIn with a second unrelated request immediately.
    time.sleep(PACE_SECONDS)
    return jobs


def fetch_for_user(filters: dict, user_seeds: dict | None) -> list[Job]:
    """Per-user LinkedIn fetch — runs up to 3 queries shaped by the profile.

    `user_seeds` is the `search_seeds.linkedin` dict from the user's profile:

        {"queries": [{"q": "...", "geo": "...", "f_TPR": "r86400"}, ...]}

    For each query we run one LinkedIn search page, collect matching cards,
    and fold them into the combined result. We:

      * enforce a hard cap of `MAX_USER_QUERIES` (3) queries;
      * cap the TOTAL postings across all queries at `filters['max_per_source']`
        — not per query — so a high-volume primary query doesn't starve the
        secondary ones AND an overeager seed set can't balloon the digest;
      * dedupe on URL across queries;
      * sleep `PACE_SECONDS` between requests (but NOT after the final one);
      * stop early on rate-limit so we don't burn the next query slot for
        nothing.

    If `user_seeds` is None / missing / has no usable queries, we fall back
    to the single-query global `fetch(filters)` path so users without a
    built profile still get LinkedIn results.
    """
    queries = []
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
        log.debug("linkedin: no user_seeds provided, falling back to global fetch()")
        return fetch(filters)

    cap = int(filters.get("max_per_source") or 10)
    remote = str(filters.get("remote") or "")
    seen_urls: set[str] = set()
    combined: list[Job] = []

    for idx, query in enumerate(queries):
        remaining = cap - len(combined)
        if remaining <= 0:
            break
        try:
            batch = _one_search(
                q=query["q"],
                geo=query["geo"],
                f_TPR=query["f_TPR"],
                remote=remote,
                cap_remaining=remaining,
                filters=filters,
                seen_urls=seen_urls,
            )
        except _RateLimited:
            log.warning("linkedin: rate-limited on query %d/%d, stopping batch",
                        idx + 1, len(queries))
            break
        combined.extend(batch)
        log.info("linkedin[user]: query %d/%d %r @ %r → %d jobs (total %d)",
                 idx + 1, len(queries), query["q"], query["geo"],
                 len(batch), len(combined))
        # Pace between requests, but not after the final one.
        if idx + 1 < len(queries):
            time.sleep(PACE_SECONDS)

    return combined
