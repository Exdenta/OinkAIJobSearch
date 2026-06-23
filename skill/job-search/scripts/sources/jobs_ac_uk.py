"""jobs.ac.uk source adapter — UK + EU academic / research job portal.

Portal: https://www.jobs.ac.uk

Used to surface academic and research positions across UK and European
universities — postdocs, lectureships, research fellows, PhD studentships,
teaching associates. Coverage is heaviest in UK higher education but the
portal also lists EU and (some) international academic posts. Sweet spot for
qualitative researchers, social scientists, humanities scholars, and anyone
hunting university research roles.

This adapter is **DEFAULT OFF**. Wire it in by adding `jobs_ac_uk` to a
profile's enabled sources list once a user has expressed interest in
UK / EU academic roles.

Integration choice: HTML search results (keyword fan-in)
--------------------------------------------------------
History: this adapter originally consumed per-subject-area RSS feeds at
`https://www.jobs.ac.uk/jobs/<slug>/?format=rss`. **Those feeds were
retired by the portal** (verified 2026-06-20): the `/feeds` discovery
index now 404s, the per-category `?format=rss` URLs 301→500 with an HTML
error body, and the `/jobs/<slug>/` category paths redirect to the
homepage. There is no RSS surface left.

The public search page, however, is **server-rendered** and needs no
auth or JS:

    https://www.jobs.ac.uk/search/?keywords=<kw>&sortOrder=1&pageSize=<n>&startIndex=1

`sortOrder=1` is newest-first; each result row carries title, employer,
location, salary, AND a "Date Placed" line (the RSS feed never exposed a
per-item date — so the migration is a net gain for the downstream age
gate). We fan a small set of research-leaning keyword queries in, dedupe
by URL, and cap at `max_per_source`.

  Why a *fan-in* across multiple queries?
    A single query (e.g. "social science") misses cross-listed roles
    tagged under humanities, psychology, or politics. We issue a handful
    of research-leaning queries, dedupe by URL, then cap. The query list
    biases toward qualitative-research-friendly disciplines but is
    overridable via the `jobs_ac_uk_keywords` filter. The legacy
    `jobs_ac_uk_categories` filter (subject-area slugs) is still honoured
    for backward-compat — slugs are mapped to readable keyword phrases.

Caveats / fragility notes
-------------------------
  * Result rows are parsed from `div.j-search-result__result` blocks with
    BeautifulSoup. Field extraction is prefix-based ("Location:",
    "Salary:", "Date Placed:") rather than class-locked, so a CSS rename
    degrades gracefully (the field empties; the row still renders).
  * "Date Placed" is rendered as "12 Jun" with no year. We infer the year
    (current year, or previous year when that would put the date in the
    future) and emit an ISO `YYYY-MM-DD` string the age gate parses.
  * The job ID lives in the URL path: `/job/<EXTID>/<slug>` where
    `<EXTID>` is an alphanumeric short code (e.g. `DRJ953`). It's stable
    across re-fetches and suitable for `external_id`.
  * No server-side topic filter beyond the keyword query; downstream AI
    scoring handles fine-grained relevance.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Any
from urllib.parse import quote, urlencode, urljoin

import requests
from bs4 import BeautifulSoup

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

BASE = "https://www.jobs.ac.uk"
SEARCH_PATH = "/search/"

# Default research-leaning keyword fan-in. Biases toward qualitative
# researcher / social-science / humanities profiles. Overridable via
# `filters["jobs_ac_uk_keywords"]`.
DEFAULT_KEYWORDS: tuple[str, ...] = (
    "social science",
    "politics",
    "psychology",
    "humanities",
    "international development",
    "research",
)

# Legacy subject-area slugs → readable keyword phrases. Lets an existing
# `jobs_ac_uk_categories` filter keep working after the RSS→HTML switch.
_SLUG_TO_KEYWORD: dict[str, str] = {
    "social-sciences-and-social-care": "social science",
    "historical-and-philosophical-studies": "history philosophy",
    "languages-literature-and-culture": "languages literature",
    "psychology": "psychology",
    "politics-and-government": "politics",
    "academic-or-research": "research",
}

# /job/<EXTID>/<slug> — EXTID is alphanumeric (e.g. DRJ953, ABC1234).
_JOB_ID_RE = re.compile(r"/job/([A-Za-z0-9]+)")
_MONTHS = {
    m: i + 1 for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}
# "12 Jun", "1 June 2026", "08 Jun" → capture day + month (+ optional year).
_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})(?:\s+(\d{4}))?")

# Optional: forensic logger is wired separately; degrade gracefully if absent.
try:  # pragma: no cover - thin shim
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]


def _norm(s: str) -> str:
    """Collapse all runs of whitespace (incl. newlines inside a single text
    node, which `get_text` does not touch) to single spaces."""
    return re.sub(r"\s+", " ", s or "").strip()


def _extract_id(url: str) -> str:
    """Pull the alphanumeric job id out of a jobs.ac.uk job URL."""
    m = _JOB_ID_RE.search(url or "")
    return m.group(1) if m else (url or "")


def _resolve_keywords(filters: dict) -> list[str]:
    """Keyword fan-in. Prefers `jobs_ac_uk_keywords`; falls back to mapping
    the legacy `jobs_ac_uk_categories` slugs; else the research default."""
    kws = filters.get("jobs_ac_uk_keywords")
    if kws:
        out = [str(k).strip() for k in kws if str(k).strip()]
        if out:
            return out
    cats = filters.get("jobs_ac_uk_categories")
    if cats:
        out = []
        for c in cats:
            slug = str(c).strip().strip("/")
            kw = _SLUG_TO_KEYWORD.get(slug)
            # Unknown slug → use it verbatim as a keyword (hyphens → spaces).
            out.append(kw or slug.replace("-", " "))
        out = [k for k in out if k]
        if out:
            return out
    return list(DEFAULT_KEYWORDS)


def _build_search_urls(filters: dict, page_size: int) -> list[tuple[str, str]]:
    """Return [(keyword, url)] for the newest-first search of each keyword.

    Spaces are `%20`-encoded (``quote_via=quote``), NOT `+`: jobs.ac.uk
    returns a ~2.7 KB empty stub page for `+`-encoded multi-word queries
    (e.g. ``keywords=social+science``) but the full result set for the
    `%20` form. Verified 2026-06-20.
    """
    urls: list[tuple[str, str]] = []
    for kw in _resolve_keywords(filters):
        qs = urlencode({
            "keywords": kw,
            "sortOrder": 1,          # 1 = newest first
            "pageSize": page_size,
            "startIndex": 1,
        }, quote_via=quote)          # %20 not + — see _build_search_urls note
        urls.append((kw, f"{BASE}{SEARCH_PATH}?{qs}"))
    return urls


def _parse_date_placed(text: str) -> str:
    """'Date Placed: 12 Jun' → ISO 'YYYY-MM-DD'. Year inferred (current,
    or previous when that lands the date in the future). '' if unparseable."""
    m = _DATE_RE.search(text or "")
    if not m:
        return ""
    day = int(m.group(1))
    mon = _MONTHS.get(m.group(2)[:3].lower())
    if not mon:
        return ""
    year = int(m.group(3)) if m.group(3) else None
    today = _dt.date.today()
    if year is None:
        year = today.year
        try:
            cand = _dt.date(year, mon, day)
        except ValueError:
            return ""
        # No future-dating: a parsed date ahead of today means last year.
        if (cand - today).days > 1:
            year -= 1
    try:
        return _dt.date(year, mon, day).isoformat()
    except ValueError:
        return ""


def _field_by_prefix(result, prefix: str) -> str:
    """Find the first descendant text starting with `prefix` (e.g.
    'Location:', 'Salary:', 'Date Placed:') and return the remainder."""
    low = prefix.lower()
    for div in result.find_all(["div", "li", "span"]):
        txt = _norm(div.get_text(" ", strip=True))
        if txt and txt.lower().startswith(low):
            return txt[len(prefix):].strip(" : ")
    return ""


def _parse_search_html(html: str) -> list[dict[str, Any]]:
    """Parse server-rendered search results into card dicts."""
    soup = BeautifulSoup(html, "html.parser")
    cards: list[dict[str, Any]] = []
    for result in soup.select("div.j-search-result__result"):
        link = result.select_one('a[href^="/job/"]')
        if link is None:
            continue
        href = (link.get("href") or "").strip()
        if not href:
            continue
        title = _norm(fix_mojibake(link.get_text(" ", strip=True)))
        if not title:
            continue
        employer_el = result.select_one(".j-search-result__employer")
        employer = (
            _norm(fix_mojibake(employer_el.get_text(" ", strip=True)))
            if employer_el else ""
        )
        location = fix_mojibake(_field_by_prefix(result, "Location:"))
        salary = fix_mojibake(_field_by_prefix(result, "Salary:"))
        posted = _parse_date_placed(_field_by_prefix(result, "Date Placed:"))
        cards.append({
            "url": urljoin(BASE, href),
            "external_id": _extract_id(href),
            "title": title,
            "company": employer,
            "location": location or "United Kingdom",
            "salary": salary,
            "posted_at": posted,
        })
    return cards


def _log_forensic(payload: dict[str, Any]) -> None:
    if forensic is None:
        return
    try:
        forensic.log_step(
            "jobs_ac_uk.fetch",
            input=payload.get("input", {}),
            output=payload.get("output", {}),
        )
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for jobs_ac_uk.fetch", exc_info=True)


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point. Returns up to `max_per_source` Jobs.

    `filters` keys consulted:
      * max_per_source (int, default 12) — cap on returned jobs
      * jobs_ac_uk_keywords (list[str], optional) — override the research
        keyword fan-in, e.g. ["machine learning", "bioinformatics"].
      * jobs_ac_uk_categories (list[str], optional, legacy) — subject-area
        slugs, mapped to keyword phrases for backward-compat.
    """
    cap = int(filters.get("max_per_source") or 12)
    # Ask for a little more than the cap per query so the fan-in has slack
    # to dedupe against; the portal honours pageSize up to 25.
    page_size = max(10, min(25, cap))
    search_urls = _build_search_urls(filters, page_size)

    jobs: list[Job] = []
    seen_urls: set[str] = set()
    per_query_status: list[dict[str, Any]] = []
    body_head = ""

    try:
        for keyword, url in search_urls:
            if len(jobs) >= cap:
                break
            status_code: int | None = None
            entry_count = 0
            try:
                r = requests.get(url, headers=UA, timeout=20, allow_redirects=True)
                status_code = r.status_code
                if status_code != 200:
                    body_head = (r.text or "")[:300] or body_head
                    log.warning(
                        "jobs_ac_uk search non-200 status=%s kw=%r url=%s",
                        status_code, keyword, url,
                    )
                    per_query_status.append(
                        {"keyword": keyword, "status": status_code, "entries": 0}
                    )
                    continue

                cards = _parse_search_html(r.text)
                entry_count = len(cards)
                if not cards:
                    body_head = (r.text or "")[:300] or body_head
                    log.warning(
                        "jobs_ac_uk: 0 result blocks parsed for kw=%r "
                        "(layout change?)", keyword,
                    )

                for c in cards:
                    if len(jobs) >= cap:
                        break
                    if c["url"] in seen_urls or not c["external_id"]:
                        continue
                    seen_urls.add(c["url"])
                    jobs.append(Job(
                        "jobs_ac_uk",
                        c["external_id"],
                        c["title"][:140],
                        c["company"][:120],
                        c["location"][:120],
                        c["url"],
                        c["posted_at"],
                        clean_snippet(
                            f"{c['company']} — {c['title']}", max_chars=400,
                        ),
                        (c["salary"] or "")[:120],
                    ))

                per_query_status.append(
                    {"keyword": keyword, "status": status_code, "entries": entry_count}
                )
            except requests.RequestException as e:
                log.error("jobs_ac_uk search failed for kw=%r: %s", keyword, e)
                body_head = body_head or repr(e)[:300]
                per_query_status.append(
                    {"keyword": keyword, "status": status_code, "error": repr(e)[:200]}
                )
                continue

    except Exception as e:  # noqa: BLE001
        log.exception("jobs_ac_uk fetch failed: %s", e)
        body_head = body_head or repr(e)[:300]

    # The search-result snippet is just employer + title. The real job
    # spec (eligibility, PhD reqs, funding terms, "Home/UK students only")
    # lives on the detail page; fetch it inline so Sonnet scores on the
    # full text, not a one-line stub.
    if jobs:
        try:
            from sources._detail_fetch import fetch_many_bodies
            body_map = fetch_many_bodies(
                [j.url for j in jobs], max_chars=4000, workers=8,
            )
            enriched = 0
            for i, j in enumerate(jobs):
                body = body_map.get(j.url, "")
                if body and len(body) > len(j.snippet):
                    jobs[i] = Job(
                        j.source, j.external_id, j.title, j.company,
                        j.location, j.url, j.posted_at, body,
                        getattr(j, "salary", ""),
                    )
                    enriched += 1
            log.info(
                "jobs_ac_uk: detail-page bodies fetched for %d/%d postings",
                enriched, len(jobs),
            )
        except Exception:
            log.exception("jobs_ac_uk: detail-page fetch raised; continuing")

    sample_titles = [j.title for j in jobs[:5]]
    log.info("jobs_ac_uk: %d postings across %d queries",
             len(jobs), len(search_urls))

    _log_forensic(
        {
            "input": {
                "search_urls": [u for _, u in search_urls],
                "keywords": _resolve_keywords(filters),
                "max_per_source": cap,
            },
            "output": {
                "count": len(jobs),
                "sample_titles": sample_titles,
                "per_query": per_query_status,
                "body_head": body_head,
            },
        }
    )

    return jobs
