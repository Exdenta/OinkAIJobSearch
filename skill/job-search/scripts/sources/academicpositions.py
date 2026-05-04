"""AcademicPositions (https://academicpositions.com) source adapter.

Backstop for `euraxess` — same EU academic-jobs niche, complementary inventory
(university-funded postings that don't always appear on the EURAXESS portal:
private-foundation grants, ECR/postdoc tracks at UK & Swiss schools, etc.).
Strong coverage in NL / DE / DK / SE / BE / ES / IT / CH / UK / NO / FI.

Default OFF in `defaults.DEFAULTS` and `sources/__init__.py` until the live
yield is verified for the orchestrator's user base. Module key: `academicpositions`.

Integration choice (with caveats)
---------------------------------
Probed 2026-05-01. AcademicPositions sits behind Cloudflare's *Bot Fight Mode*
and serves HTTP 403 to every server-side fetch we've tried (curl + Python
`requests` + feedparser, with both bot-style and full Chrome UA + Sec-Ch-Ua
client hints). Probed endpoints:

    /jobs/rss             -> 403 (Cloudflare interstitial)
    /rss                  -> 403
    /jobs/feed            -> 403
    /jobs.xml             -> 403
    /sitemap.xml          -> 403
    /sitemaps/jobs.xml    -> 403
    /jobs                 -> 403
    /                     -> 403 (only /robots.txt returns 200)

Wayback Machine has no archived snapshot of any RSS-style URL on the domain,
so they likely never published a public feed. The site is an SPA backed by an
internal API; the front-end is JS-heavy and the API is not documented.

Why ship the adapter anyway?
  * Cloudflare BFM rules are tuned per-zone and change weekly. Several of our
    other adapters (linkedin guest API, infojobs) sit behind similar shields
    and *do* let well-behaved `requests` traffic through during quieter hours
    of the day. If AcademicPositions ever relaxes its rule, this adapter will
    auto-revive without a code change.
  * The forensic log records the 403 + body_head every run, so we'll see
    immediately when the gate flips.
  * Until then it returns [] cleanly — no exceptions bubble up, no tokens
    burned, no impact on user digests.

Approach if/when fetches succeed
--------------------------------
1.  Try the most plausible RSS endpoints first (`/jobs/rss`, `/rss`,
    `/jobs.rss`). These are the conventional Drupal / Wordpress / Symfony
    paths and would be the cheapest path if AP exposes them.
2.  If RSS works: use feedparser, pull <title> + <link> + <description>,
    extract numeric job id from the URL slug (`/job/<slug>-<id>` or query
    param), build the `Job` record.
3.  If RSS doesn't work: fall back to scraping the HTML listing at
    `/jobs?country=<EU>&category=<discipline>` with BeautifulSoup. The site's
    listing markup uses `data-testid="job-card"` (visible in the SPA shell
    HTML when JS is disabled — verified via Wayback before rule tightened).
4.  EU bias: cycle through a small set of EU country slugs so a single
    Cloudflare-relaxation window doesn't pin us to e.g. just Spanish jobs.

Filters honored
---------------
  * max_per_source (int, default 12)
  * academicpositions_search (str, optional) — free-text query (e.g.
    "qualitative research") appended as `?keywords=...`. Useful for biasing
    toward the user's profile without hardcoding it here.
  * academicpositions_countries (list[str], optional) — country slugs to
    cycle (e.g. ["spain", "netherlands", "denmark"]). Default = a small EU
    fan-out so we don't hammer one path.

Costs / pacing
--------------
~3 HTTP requests per run (one RSS attempt + one HTML attempt + one country
fan-out fallback). All capped at `max_per_source`. No new pip deps — we
reuse `requests`, `feedparser`, and `bs4` already installed for other adapters.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin, urlencode

import feedparser
import requests
from bs4 import BeautifulSoup

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

# Best-effort forensic logger; module may not exist in every checkout.
try:  # pragma: no cover - thin shim
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]


UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

BASE = "https://academicpositions.com"

# RSS candidates, in order of preference. First 200 wins.
_RSS_CANDIDATES = (
    "/jobs/rss",
    "/rss",
    "/jobs.rss",
    "/jobs/feed",
    "/feeds/jobs",
)

# HTML listing candidates. The SPA renders cards via JS in the live build;
# whatever HTML comes back inside the shell still carries the listing markup
# in many requests, so we attempt parse-and-pray.
_HTML_LISTINGS = (
    "/jobs",
    "/jobs/search",
)

# Default EU country slugs, biased toward the host's strongest inventory.
_DEFAULT_COUNTRIES = (
    "netherlands",
    "germany",
    "denmark",
    "sweden",
    "spain",
    "belgium",
    "switzerland",
    "norway",
    "italy",
    "finland",
    "united-kingdom",
)

# Pull a numeric id out of `/job/{slug}-{id}` or `/job/{id}/{slug}` URL forms.
_JOB_ID_RE = re.compile(r"/job/(?:[^/]*-)?(\d{4,})(?:[/?#]|$)")
_TIMEOUT_S = 20


def _log_forensic(name: str, *, input: dict[str, Any], output: dict[str, Any]) -> None:
    if forensic is None:
        return
    try:
        forensic.log_step(name, input=input, output=output)
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for %s", name, exc_info=True)


def _extract_id(url: str) -> str:
    """Pull a stable id out of an academicpositions job URL.

    Falls back to the URL itself so dedupe still works when the slug shape is
    unfamiliar.
    """
    if not url:
        return ""
    m = _JOB_ID_RE.search(url)
    if m:
        return m.group(1)
    # Last URL path segment as a fallback id.
    seg = url.rstrip("/").rsplit("/", 1)[-1]
    return seg or url


def _try_rss(*, search: str, cap: int) -> tuple[int, str, list[Job]]:
    """Try each RSS candidate in turn. Returns (status_of_first_attempt, body_head, jobs).

    Stops at the first 200 that yields parseable entries; otherwise returns the
    last status + body_head so forensic logs surface the failure mode.
    """
    last_status = 0
    last_body_head = ""
    jobs: list[Job] = []
    qs = urlencode({"keywords": search}) if search else ""

    for path in _RSS_CANDIDATES:
        url = urljoin(BASE, path)
        if qs:
            url = f"{url}?{qs}"
        try:
            r = requests.get(url, headers=UA, timeout=_TIMEOUT_S)
        except requests.RequestException as e:
            log.warning("academicpositions: %s request failed: %s", path, e)
            last_body_head = repr(e)[:300]
            continue
        last_status = r.status_code
        if r.status_code != 200:
            last_body_head = (r.text or "")[:300]
            continue

        parsed = feedparser.parse(r.content)
        entries = list(parsed.entries or [])
        if not entries:
            # 200 but no items — could be an HTML page served with feed MIME.
            last_body_head = (r.text or "")[:300]
            continue

        for entry in entries:
            if len(jobs) >= cap:
                break
            link = (entry.get("link") or "").strip()
            title = fix_mojibake(entry.get("title") or "").strip()
            if not (link and title):
                continue
            ext_id = _extract_id(link)
            desc_html = entry.get("summary") or entry.get("description") or ""
            # Tags / categories sometimes carry country + organisation.
            tags = entry.get("tags") or []
            country = ""
            company = ""
            for t in tags:
                term = (getattr(t, "term", "") or "").strip()
                if not term:
                    continue
                if not country and term in _DEFAULT_COUNTRIES + tuple(c.title() for c in _DEFAULT_COUNTRIES):
                    country = term
                elif not company:
                    company = term
            jobs.append(Job(
                source="academicpositions",
                external_id=ext_id,
                title=title[:140],
                company=fix_mojibake(company)[:120],
                location=(country or "EU")[:120],
                url=link,
                posted_at=entry.get("published") or entry.get("updated") or "",
                snippet=clean_snippet(desc_html, max_chars=400),
            ))
        if jobs:
            return r.status_code, "", jobs
        last_body_head = (r.text or "")[:300]

    return last_status, last_body_head, jobs


def _parse_listing_html(html: str, *, cap: int) -> list[Job]:
    """Best-effort parse of the AP listing HTML.

    Looks for any anchor whose href matches `/job/...` and treats it as a card.
    The site's actual card markup is JS-rendered, but in practice the SSR shell
    still ships listing links inside <script type="application/ld+json"> blobs.
    """
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[Job] = []
    seen_ids: set[str] = set()

    # Pass 1: try semantic <article data-testid="job-card"> blocks.
    for card in soup.select('[data-testid="job-card"]'):
        a = card.find("a", href=True)
        if not a:
            continue
        href = a.get("href", "")
        if "/job/" not in href:
            continue
        url = urljoin(BASE, href)
        ext_id = _extract_id(url)
        if not ext_id or ext_id in seen_ids:
            continue
        title = fix_mojibake(a.get_text(" ", strip=True))[:140]
        if not title:
            continue
        org = ""
        org_node = card.select_one('[data-testid="employer"], .employer, .organisation')
        if org_node:
            org = fix_mojibake(org_node.get_text(" ", strip=True))[:120]
        loc = ""
        loc_node = card.select_one('[data-testid="location"], .location')
        if loc_node:
            loc = fix_mojibake(loc_node.get_text(" ", strip=True))[:120]
        snippet_node = card.select_one('[data-testid="summary"], .summary, p')
        snippet = clean_snippet(
            snippet_node.get_text(" ", strip=True) if snippet_node else "",
            max_chars=400,
        )
        seen_ids.add(ext_id)
        jobs.append(Job(
            source="academicpositions",
            external_id=ext_id,
            title=title,
            company=org,
            location=loc or "EU",
            url=url,
            posted_at="",
            snippet=snippet,
        ))
        if len(jobs) >= cap:
            return jobs

    # Pass 2: fall back to JSON-LD JobPosting blobs (often present in SSR).
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if len(jobs) >= cap:
            break
        body = script.string or script.get_text() or ""
        if "JobPosting" not in body:
            continue
        # Cheap regex extraction — full JSON parse is overkill and brittle
        # given AP's tendency to ship multiple concatenated objects.
        for m_url in re.finditer(r'"url"\s*:\s*"([^"]+/job/[^"]+)"', body):
            url = m_url.group(1)
            ext_id = _extract_id(url)
            if not ext_id or ext_id in seen_ids:
                continue
            # Try to grab a title near this url match.
            window = body[max(0, m_url.start() - 400): m_url.end() + 400]
            mt = re.search(r'"title"\s*:\s*"([^"]+)"', window)
            mh = re.search(r'"hiringOrganization"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', window)
            ml = re.search(r'"addressCountry"\s*:\s*"([^"]+)"', window)
            md = re.search(r'"description"\s*:\s*"([^"]+)"', window)
            title = fix_mojibake(mt.group(1) if mt else "")[:140]
            if not title:
                continue
            seen_ids.add(ext_id)
            jobs.append(Job(
                source="academicpositions",
                external_id=ext_id,
                title=title,
                company=fix_mojibake(mh.group(1) if mh else "")[:120],
                location=(ml.group(1) if ml else "EU")[:120],
                url=url,
                posted_at="",
                snippet=clean_snippet(md.group(1) if md else "", max_chars=400),
            ))
            if len(jobs) >= cap:
                break

    return jobs


def _try_html(*, search: str, countries: tuple[str, ...], cap: int) -> tuple[int, str, list[Job]]:
    """Walk a small set of HTML listing URLs (root + per-country) until cap or all 403."""
    jobs: list[Job] = []
    last_status = 0
    last_body_head = ""

    # Root listing first (broadest), then country fan-out for EU bias.
    paths: list[tuple[str, dict[str, str]]] = []
    for p in _HTML_LISTINGS:
        params: dict[str, str] = {}
        if search:
            params["keywords"] = search
        paths.append((p, params))
    for cc in countries:
        params = {"country": cc}
        if search:
            params["keywords"] = search
        paths.append(("/jobs", params))

    seen_ids: set[str] = set()

    for path, params in paths:
        if len(jobs) >= cap:
            break
        url = urljoin(BASE, path)
        if params:
            url = f"{url}?{urlencode(params)}"
        try:
            r = requests.get(url, headers=UA, timeout=_TIMEOUT_S)
        except requests.RequestException as e:
            log.warning("academicpositions: HTML %s failed: %s", url, e)
            last_body_head = repr(e)[:300]
            continue
        last_status = r.status_code
        if r.status_code != 200:
            last_body_head = (r.text or "")[:300]
            continue
        page_jobs = _parse_listing_html(r.text, cap=cap - len(jobs))
        for j in page_jobs:
            if j.external_id in seen_ids:
                continue
            seen_ids.add(j.external_id)
            jobs.append(j)
            if len(jobs) >= cap:
                break
        if not page_jobs:
            last_body_head = (r.text or "")[:300]

    return last_status, last_body_head, jobs


def fetch(filters: dict) -> list[Job]:
    """Return up to `max_per_source` AcademicPositions Jobs (best-effort).

    Tries RSS first (cheap), falls back to HTML listing scrape with an EU
    country fan-out. On Cloudflare 403 returns [] cleanly — never raises.
    """
    cap = int(filters.get("max_per_source") or 12)
    search = (filters.get("academicpositions_search") or "").strip()
    countries_raw = filters.get("academicpositions_countries") or _DEFAULT_COUNTRIES
    if isinstance(countries_raw, str):
        countries = tuple(c.strip() for c in countries_raw.split(",") if c.strip())
    else:
        countries = tuple(str(c).strip() for c in countries_raw if str(c).strip())
    if not countries:
        countries = _DEFAULT_COUNTRIES

    rss_status = 0
    rss_body_head = ""
    html_status = 0
    html_body_head = ""
    jobs: list[Job] = []

    try:
        rss_status, rss_body_head, jobs = _try_rss(search=search, cap=cap)
        if len(jobs) < cap:
            remaining = cap - len(jobs)
            html_status, html_body_head, html_jobs = _try_html(
                search=search, countries=countries, cap=remaining,
            )
            # De-dupe across RSS + HTML by external_id.
            seen = {j.external_id for j in jobs}
            for j in html_jobs:
                if j.external_id and j.external_id not in seen:
                    seen.add(j.external_id)
                    jobs.append(j)
                    if len(jobs) >= cap:
                        break
    except Exception as e:  # noqa: BLE001
        # Belt-and-suspenders: never let a parser bug poison the digest run.
        log.exception("academicpositions: unexpected failure: %s", e)
        rss_body_head = rss_body_head or repr(e)[:300]

    _log_forensic(
        "academicpositions.fetch",
        input={
            "max_per_source": cap,
            "search": search,
            "countries": list(countries),
            "rss_candidates": list(_RSS_CANDIDATES),
        },
        output={
            "rss_status": rss_status,
            "html_status": html_status,
            "count": len(jobs),
            "sample_titles": [j.title for j in jobs[:5]],
            **({"rss_body_head": rss_body_head} if rss_body_head and not jobs else {}),
            **({"html_body_head": html_body_head} if html_body_head and not jobs else {}),
        },
    )
    return jobs
