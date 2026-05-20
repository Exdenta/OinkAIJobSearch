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

import re
import requests
from bs4 import BeautifulSoup

from dedupe import Job
from text_utils import fix_mojibake, strip_html
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

# v2.3: cross-product cap. When user_seeds use the new separated shape
# {queries: [...], geos: [...]}, the dispatcher computes the full
# cross-product and trims to this many (q × geo) combinations after
# _diversify_geo ordering. Default = 10 lets a 3-query × 4-geo set
# saturate without blowing the rate budget.
MAX_LINKEDIN_DISPATCHES = 10

# LinkedIn's guest-search location resolver is broad but inconsistent:
#   * All ISO 3166 country names resolve (Spain, Germany, ...).
#   * Major cities mostly resolve (New York, London, Madrid, Berlin,
#     Barcelona, Paris, Dublin, San Francisco) but some don't
#     (Amsterdam, Bilbao, "Greater X Metropolitan Area"; resolver
#     misses depend on LinkedIn's internal geoUrn registry, which
#     changes over time and has no public list).
#   * Country clusters: "European Union", "European Economic Area",
#     "Asia Pacific", "APAC", "Worldwide" resolve.
#   * Aliases that LOOK like macros DON'T resolve: "EMEA", "LATAM",
#     "Middle East and North Africa".
#
# Rather than maintain a brittle hardcoded list, the dispatcher tries
# every geo Opus proposes and dynamically blacklists those that page-1
# 0-results AT FETCH TIME (see `fetch_for_user`). That way:
#   * Unknown geo wastes ONE search request, not the full pagination
#     budget (4 pages × 1.5s = 6s saved per dead combo).
#   * Same-run sibling dispatches reusing the dead geo are skipped.
#   * Opus picks freely; LinkedIn's actual resolver is the authority.


def _normalize_linkedin_geo(geo: str) -> str | None:
    """Trim + clamp length. No allowlist — let LinkedIn's resolver decide.

    Returns None for empty input so callers can detect "no geo" cleanly.
    """
    g = (geo or "").strip()[:80]
    return g or None

# Per-query cap: each profile query runs independently up to this many
# postings. Total jobs returned by `fetch_for_user` is bounded by
# `MAX_USER_QUERIES * PER_QUERY_CAP`, NOT a single shared `max_per_source`
# ceiling — that legacy behavior caused query 1 to saturate the budget and
# starve queries 2..N (e.g. "frontend Spain" took the whole 12, "vue Germany"
# never executed).
#
# v2.3: doubled from 30 → 60. With PAGINATION_STARTS=(10,25,50) we now
# walk 4 pages per query (start=0/10/25/50), each returning ~25 fresh
# cards after dedupe → ~75-100 unique results per query, 5 queries
# total → max ~300-500 LinkedIn candidates per user per run (capped at
# 60/query). LinkedIn anonymous rate limit handles this comfortably at
# PACE_SECONDS=1.5 between requests.
PER_QUERY_CAP = 60

# How many additional pages to try per query when page 1 is short of
# `PER_QUERY_CAP`. LinkedIn returns 25 cards per page; added start=50
# alongside (10, 25) so each query can sweep four pages. ~75-100
# unique results per query after URL dedupe.
PAGINATION_STARTS = (10, 25, 50)

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
    # `body_resolved` distinguishes "LinkedIn doesn't recognize this geo"
    # (returns a ~26-byte empty placeholder) from "geo resolves fine but
    # this query has no hits today" (returns a normal ~30k-byte page
    # with 0 result cards). The geo-blacklist in fetch_for_user only
    # fires when body_resolved=False so we don't starve sibling queries
    # over an unrelated zero-result event.
    body_resolved = False
    try:
        resp = requests.get(SEARCH, params=params, headers=UA, timeout=20)
        status_code = resp.status_code
        body_resolved = len(resp.text or "") > 500
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
                "body_resolved": body_resolved,
            },
            error=err_payload,
        )
    return out, body_resolved


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


def _flatten_user_seeds(user_seeds: dict | None) -> list[dict]:
    """Normalize either schema (paired or separated) into a flat list of
    {q, geo, f_TPR} dispatch dicts.

    Separated shape (v2.3):
        {"queries": [<str>, ...], "geos": [<str>, ...], "f_TPR": "r86400"}
        → cross-product, capped at MAX_LINKEDIN_DISPATCHES after
          _diversify_geo for round-robin geo coverage.

    Paired shape (v2.0 back-compat):
        {"queries": [{"q":..., "geo":..., "f_TPR":...}, ...]}
        → one dispatch per entry, capped at MAX_USER_QUERIES.

    Returns [] for malformed / empty input.
    """
    if not isinstance(user_seeds, dict):
        return []
    raw_queries = user_seeds.get("queries") or []
    if not isinstance(raw_queries, list) or not raw_queries:
        return []

    default_tpr = str(user_seeds.get("f_TPR") or "r86400").strip()[:12]

    # Detect schema variant by the first entry's type.
    first = raw_queries[0]
    if isinstance(first, str):
        # SEPARATED: list-of-strings + a sibling `geos` list.
        q_strings: list[str] = [
            q.strip()[:200]
            for q in raw_queries
            if isinstance(q, str) and q.strip()
        ][:MAX_USER_QUERIES]
        raw_geos = user_seeds.get("geos") or []
        if not isinstance(raw_geos, list):
            raw_geos = []
        geo_strings: list[str] = []
        for g in raw_geos:
            if not isinstance(g, str) or not g.strip():
                continue
            normalized = _normalize_linkedin_geo(g)
            if normalized:
                geo_strings.append(normalized)
        if not geo_strings:
            # No geos provided → run each query without a location filter
            # (LinkedIn defaults to worldwide).
            geo_strings = [""]
        # Cross-product.
        combos: list[dict] = []
        for q in q_strings:
            for g in geo_strings:
                combos.append({"q": q, "geo": g, "f_TPR": default_tpr})
        # _diversify_geo (defined below) reorders so consecutive entries
        # hit distinct geos; trim to dispatch cap.
        combos = _diversify_geo(combos)
        return combos[:MAX_LINKEDIN_DISPATCHES]

    if isinstance(first, dict):
        # PAIRED (v2.0 legacy): each entry is {q, geo, f_TPR}.
        out: list[dict] = []
        for item in raw_queries[:MAX_USER_QUERIES]:
            if not isinstance(item, dict):
                continue
            q = str(item.get("q") or "").strip()
            if not q:
                continue
            raw_geo = str(item.get("geo") or "").strip()
            geo = (_normalize_linkedin_geo(raw_geo) or "") if raw_geo else ""
            out.append({
                "q": q[:200],
                "geo": geo,
                "f_TPR": str(item.get("f_TPR") or default_tpr).strip()[:12],
            })
        return out

    return []


def fetch_for_user(filters: dict, user_seeds: dict | None) -> list[Job]:
    """Per-user LinkedIn fetch — dispatches a cross-product of queries × geos.

    Two `user_seeds.linkedin` schemas supported:

      v2.3 SEPARATED (new):
          {"queries": ["frontend react typescript", "react remote"],
           "geos":    ["Spain", "European Union", "Bilbao, Basque Country"],
           "f_TPR":   "r86400"}
        → cross-product: each query is run against EVERY geo, capped at
          `MAX_LINKEDIN_DISPATCHES` total (q × geo) combinations after
          _diversify_geo ordering.

      v2.0 PAIRED (legacy, still supported):
          {"queries": [{"q":"react remote", "geo":"Spain", "f_TPR":"r86400"},
                       ...]}
        → one dispatch per entry, same behavior as before. Existing v4
          profiles built before 2026-05-12 use this shape; they keep
          working.

    Behavior per dispatch:
      * each (q, geo) combo gets its OWN budget of `PER_QUERY_CAP` postings;
      * walks pages `0, *PAGINATION_STARTS` (start=0,10,25,50) until the
        budget fills or LinkedIn returns 0;
      * dedupes on URL across all dispatches and pages;
      * `PACE_SECONDS` sleep between requests;
      * `_RateLimited` aborts the whole batch (returns what we have).

    If user_seeds is None / missing / has no usable queries → returns [].
    """
    queries: list[dict] = _flatten_user_seeds(user_seeds)

    if not queries:
        log.debug("linkedin: no user_seeds provided, returning []")
        return []

    # _flatten_user_seeds already _diversify_geo'd the cross-product
    # branch. Re-call here for the legacy paired branch (cheap; for an
    # already-diversified list it's idempotent).
    queries = _diversify_geo(queries)
    remote = str(filters.get("remote") or "")
    seen_urls: set[str] = set()
    combined: list[Job] = []
    request_idx = 0
    # Dynamic geo blacklist for this run. When a geo returns page-1
    # 0-results, sibling combos reusing that geo are skipped — saves
    # the pagination budget (3 extra requests × 1.5s) on every dead
    # combo. Geo "" (worldwide) is never blacklisted; LinkedIn always
    # returns something for an empty location.
    dead_geos: set[str] = set()

    for q_idx, query in enumerate(queries):
        if query["geo"] and query["geo"] in dead_geos:
            log.info(
                "linkedin[user]: skipping query %d/%d (geo %r already returned 0 this run)",
                q_idx + 1, len(queries), query["geo"],
            )
            continue
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
                batch, body_resolved = _one_search(
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
            # Blacklist the geo ONLY when LinkedIn's response was the
            # empty placeholder (`body_resolved=False`) — that means
            # the geo string didn't resolve to anything in LinkedIn's
            # internal entity registry. A normal 30 KB response with 0
            # result cards just means THIS QUERY had no hits today;
            # the geo is fine and sibling queries should keep running.
            if not batch and start == 0 and query["geo"] and not body_resolved:
                dead_geos.add(query["geo"])
                log.warning(
                    "linkedin: geo %r did not resolve (empty body) — "
                    "blacklisted for the remainder of this run",
                    query["geo"],
                )
            # Empty page → no point trying further offsets for this query.
            if not batch:
                break

    # Cross-geo requisition dedupe. LinkedIn cross-posts the same global
    # remote-first role across N country pages (Primer, Stripe etc. observed
    # producing 4-8 dupes per run). URL-level dedupe doesn't catch this —
    # each country page gets its own /pt/ /pl/ /ro/ /hu/ URL with a
    # different job-id. Collapse by (company_lc, normalized_title) keeping
    # the FIRST occurrence (preserves the canonical /www.linkedin.com/
    # entry when present). Snippet rarely differs across mirrors, so we
    # don't gate the merge on body equality.
    if combined:
        combined = _dedupe_cross_geo(combined)

    # Algorithm v2.2 — Option 4: LinkedIn search cards only carry title +
    # company + location. Without the detail-page body, Sonnet scored every
    # LinkedIn posting 0 even on perfectly targeted queries (594-job run
    # 2026-05-12 produced 0/42 ≥2 from linkedin). Fetch the body for each
    # card here, paced via PACE_SECONDS so we stay under LinkedIn's
    # anonymous rate limit. Single _RateLimited hit aborts the body-fetch
    # loop but keeps whatever bodies we've already fetched.
    if combined:
        combined = _fetch_detail_bodies(combined)

    return combined


def _normalize_title_for_dedupe(title: str) -> str:
    """Lowercase, strip seniority modifiers + punctuation + whitespace so
    "Frontend Developer", "frontend-developer", and " Frontend Developer "
    collapse to the same key. We keep the seniority prefix as part of the
    key — a "Senior Frontend" and a "Frontend" posting at the same company
    are different roles, not dupes. We do strip trailing geo/branding tags
    ("(Remote)", "- Remote", " | Acme", "/m/f/d", etc.) since the same req
    often gets re-tagged per-geo.
    """
    t = (title or "").lower().strip()
    # Strip common trailing tags.
    for tag in (
        " (remote)", " - remote", " · remote", " — remote",
        " (m/f/d)", " (m/w/d)", " (f/m/d)", " (m/f/x)",
        " (h/f)", " (h/m)", " (w/m/d)", " (m/f)", " (f/m)",
        " - hybrid", " - onsite", " (hybrid)", " (onsite)",
        " - eu remote", " - europe", " - 100% remote",
        " - full time", " - part time", " - contract",
        " (full-time)", " (part-time)", " (contract)",
    ):
        if t.endswith(tag):
            t = t[: -len(tag)].strip()
    # Collapse multiple spaces, strip surrounding punctuation.
    t = re.sub(r"\s+", " ", t)
    t = t.strip(" -—·|/,.")
    return t


def _dedupe_cross_geo(jobs: list[Job]) -> list[Job]:
    """Collapse same-req-different-country-page dupes.

    Key: (lowercased company, normalized title). First entry wins.
    Logs the suppression count.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Job] = []
    dropped = 0
    for j in jobs:
        co = (j.company or "").strip().lower()
        title_key = _normalize_title_for_dedupe(j.title or "")
        if not co or not title_key:
            out.append(j)
            continue
        key = (co, title_key)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(j)
    if dropped:
        log.info(
            "linkedin: cross-geo dedupe collapsed %d duplicate requisitions "
            "(same company+title across LinkedIn country pages)",
            dropped,
        )
    return out


# LinkedIn's public detail page (`/jobs/view/...`) is gated — anonymous
# visitors only see "sign in" prompts and similar-job sidebars, not the
# description body. The PUBLIC GUEST endpoint
# `/jobs-guest/jobs/api/jobPosting/<id>` returns the same posting's
# description as an unauthenticated HTML fragment, wrapped in a
# `show-more-less-html__markup` div. We hit THAT, not the gated view.
_LI_GUEST_POST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"
_LI_DESC_BLOCK_RE = re.compile(
    r'show-more-less-html__markup[^>]*>(.*?)</div>',
    re.DOTALL,
)
_LI_JOB_ID_RE = re.compile(r"(\d{8,})(?:[/?#]|$)")


def _linkedin_job_id_from_url(url: str) -> str | None:
    """LinkedIn job-view URLs end with `<slug>-<jobId>[?...]`. Pull the id.

    Returns None on URLs that don't match — caller falls back to the
    raw HTML fetch / skips the body.
    """
    if not url:
        return None
    m = _LI_JOB_ID_RE.search(url)
    return m.group(1) if m else None


def _fetch_detail_bodies(jobs: list[Job]) -> list[Job]:
    """Populate ``Job.snippet`` for each LinkedIn card from the public
    `jobs-guest/jobs/api/jobPosting/<id>` endpoint.

    Sequentially paced via ``PACE_SECONDS`` (LinkedIn rate-limits
    concurrent guest IPs aggressively). Stops early on the first 429/403
    — we'd rather return jobs with empty snippets than burn the rest of
    the day's quota.

    Returns a new list (we reconstruct each entry so partial-state
    mutations aren't visible to callers).
    """
    enriched: list[Job] = []
    rate_limit_hit = False
    fetched_n = 0
    for j in jobs:
        if rate_limit_hit or not j.url:
            enriched.append(j)
            continue
        jid = _linkedin_job_id_from_url(j.url)
        if not jid:
            enriched.append(j)
            continue
        time.sleep(PACE_SECONDS)
        endpoint = _LI_GUEST_POST_URL.format(jid=jid)
        try:
            resp = requests.get(endpoint, headers=UA, timeout=15)
        except requests.RequestException as e:
            log.debug("linkedin detail %s: %s", endpoint, e)
            enriched.append(j)
            continue
        if resp.status_code in (429, 403):
            log.warning(
                "linkedin guest detail rate-limited (%d) at %s — aborting body fetches",
                resp.status_code, endpoint,
            )
            rate_limit_hit = True
            enriched.append(j)
            continue
        if resp.status_code >= 400:
            enriched.append(j)
            continue
        m = _LI_DESC_BLOCK_RE.search(resp.text or "")
        if not m:
            enriched.append(j)
            continue
        # Inside show-more-less-html__markup: <strong>, <ul>, <li>, <br>,
        # plain text. Strip tags and unescape entities → clean body text.
        from text_utils import strip_html as _strip
        body = fix_mojibake(_strip(m.group(1)))
        if len(body) > 4000:
            body = body[:4000].rstrip() + "…"
        if body and len(body) > len(j.snippet or ""):
            enriched.append(Job(
                source=j.source, external_id=j.external_id,
                title=j.title, company=j.company, location=j.location,
                url=j.url, posted_at=j.posted_at, snippet=body,
                salary=getattr(j, "salary", ""),
            ))
            fetched_n += 1
        else:
            enriched.append(j)
    log.info(
        "linkedin: guest-API bodies fetched for %d/%d cards (rate_limited=%s)",
        fetched_n, len(jobs), rate_limit_hit,
    )
    return enriched
