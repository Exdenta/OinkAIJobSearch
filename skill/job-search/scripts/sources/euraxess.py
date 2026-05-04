"""EURAXESS source adapter — EU researcher mobility job portal.

Portal: https://euraxess.ec.europa.eu/jobs/search

Integration choice: HTML scrape (BeautifulSoup)
-----------------------------------------------
Probed alternatives on 2026-04-29 and none of the structured options work:

  - `?_format=json`     → 406 ("only supports the HTML format")
  - `/api/jobs`         → 404
  - `/jsonapi`          → 404 (Drupal JSON:API module not exposed publicly)
  - `?_format=hal_json` → 406
  - `?format=rss`       → 200 but returns the regular HTML page (no RSS)
  - `<link rel="alternate" type="application/json">` → not present in HTML

The portal is Drupal-based but every JSON/REST endpoint we probed is closed.
What IS reliable: the HTML uses the European Component Library (ECL) class
names, which are versioned and stable across the whole EU web estate. The
result list lives at:

    <ul class="unformatted-list" aria-label="Search results items">
      <li>
        <div id="job-teaser-content">
          <span class="ecl-label ecl-label--highlight">Spain</span>
          <article class="ecl-content-item">
            <ul class="ecl-content-block__primary-meta-container">
              <li><a href="/partnering/...">{organisation}</a></li>
              <li>Posted on: {date}</li>
            </ul>
            <h3 class="ecl-content-block__title">
              <a href="/jobs/{NID}"><span>{title}</span></a>
            </h3>
            <div class="ecl-content-block__description"><p>{snippet}</p></div>
            <div class="id-Work-Locations">... {location text} ...</div>
          </article>
        </div>
      </li>
      ...
    </ul>

Selectors target ECL classes (`ecl-content-block__title`, `ecl-label--highlight`,
`id-Work-Locations`) and the `/jobs/{NID}` href pattern — both of which are
load-bearing public-facing markup conventions and unlikely to churn.

Pagination: `?page=N` (0-indexed, 10 results per page).

Caveats / fragility notes
-------------------------
  - We do NOT add `f[1]=research_field:...` filters: the portal's research-field
    taxonomy uses opaque numeric ids (e.g. `job_research_field:921` → "Other"),
    and downstream AI scoring is the right place to filter by topic.
  - The `id-Work-Locations` block is a free-text concatenation
    ("Number of offers: 1, Spain, Madrid, ..."). We pull the country from the
    highlight label (which is canonical) and use the location text only as a
    fallback for city.
  - Portal date format is human-friendly ("Posted on: 29 April 2026"). We
    convert to ISO `YYYY-MM-DD` when parseable; otherwise pass through.
  - If the markup ever drifts, the parser will return [] and forensic logging
    will record a `body_head` snippet for triage.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

# Best-effort forensic logger; module may not exist in every checkout.
try:
    from forensic import log_step  # type: ignore
except ImportError:  # pragma: no cover - forensic is optional infrastructure
    def log_step(name: str, *, input: dict | None = None, output: dict | None = None) -> None:
        log.info("forensic %s input=%s output=%s", name, input, output)


BASE = "https://euraxess.ec.europa.eu"
SEARCH_URL = f"{BASE}/jobs/search"
DEFAULT_FILTERS = {"f[0]": "offer_type:job_offer"}
PAGE_SIZE = 10  # Drupal listing default; not configurable in the URL.

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en",
}

_POSTED_RE = re.compile(r"Posted on:\s*(.+?)\s*$", re.IGNORECASE)
_NUM_OFFERS_RE = re.compile(r"^\s*Number of offers:\s*\d+\s*,\s*", re.IGNORECASE)


def _parse_posted_date(raw: str) -> str:
    """Convert "Posted on: 29 April 2026" → "2026-04-29". Pass through on failure."""
    if not raw:
        return ""
    m = _POSTED_RE.search(raw)
    candidate = (m.group(1) if m else raw).strip()
    for fmt in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return candidate  # human-readable; downstream will display as-is


def _extract_location(li_root: BeautifulSoup, country_label: str, company: str = "") -> str:
    """Build a "City, Country" string from the result block.

    Country comes from the highlight label (canonical). City comes from the
    free-text Work-Locations block, which looks like:
        "Number of offers: 1, {Country}, {Org}, {City}, {Postcode}, {Street}"
    e.g. "Number of offers: 1, Belgium, Université Libre de Bruxelles,
          Anderlecht, 1070, 808 Route de Lennik"
    We strip the prefix, walk the comma-list after the country, and skip:
      - the org token (matches the parsed company)
      - numeric tokens (postcodes)
      - tokens that look like street addresses (start with a digit)
    Whatever's left (in order) is the most likely city.
    """
    loc_block = li_root.find("div", class_=lambda c: c and "id-Work-Locations" in c)
    city = ""
    if loc_block:
        # Inner div carrying the actual text (skip the icon/label).
        text_div = loc_block.select_one("div.ecl-text-standard") or loc_block
        raw = " ".join(text_div.stripped_strings)
        raw = _NUM_OFFERS_RE.sub("", raw)
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        company_norm = (company or "").strip().lower()
        start = parts.index(country_label) + 1 if country_label and country_label in parts else 0
        for token in parts[start:]:
            tl = token.lower()
            if token.isdigit():
                continue
            if token[:1].isdigit():  # "808 Route de Lennik" → street
                continue
            if company_norm and (tl == company_norm or tl in company_norm or company_norm in tl):
                continue
            if len(token) > 60:  # likely the org name spelled out
                continue
            city = token
            break
        if not city and parts:
            # Fall back to the last non-numeric token.
            for token in reversed(parts):
                if token and not token[:1].isdigit() and len(token) <= 60:
                    city = token
                    break
    pieces = [p for p in (city, country_label) if p]
    return ", ".join(dict.fromkeys(pieces))  # dedupe (city == country)


def _parse_card(li_root: BeautifulSoup) -> dict[str, Any] | None:
    """Pull the fields off one search-result <li>."""
    title_link = li_root.select_one("h3.ecl-content-block__title a")
    if not title_link or not title_link.get("href"):
        return None
    href = title_link["href"]
    m = re.search(r"/jobs/(\d+)", href)
    if not m:
        return None
    nid = m.group(1)
    title_span = title_link.find("span")
    title = (title_span.get_text(strip=True) if title_span else title_link.get_text(strip=True))

    # Primary meta: organisation + posting date.
    primary = li_root.select("ul.ecl-content-block__primary-meta-container > li")
    company = ""
    posted_at = ""
    for meta in primary:
        text = meta.get_text(" ", strip=True)
        if text.lower().startswith("posted on"):
            posted_at = _parse_posted_date(text)
        else:
            anchor = meta.find("a")
            if anchor and anchor.get_text(strip=True):
                company = anchor.get_text(strip=True)
            elif not company:
                company = text

    # Country lives in the highlight label.
    country_label = ""
    highlight = li_root.select_one(".ecl-label--highlight")
    if highlight:
        country_label = highlight.get_text(strip=True)

    location = _extract_location(li_root, country_label, company=company)

    desc_node = li_root.select_one(".ecl-content-block__description")
    snippet_raw = desc_node.get_text(" ", strip=True) if desc_node else ""

    return {
        "external_id": nid,
        "title": title,
        "company": company,
        "location": location,
        "url": urljoin(BASE, href),
        "posted_at": posted_at,
        "snippet": snippet_raw,
    }


def _parse_list_html(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    # Each result lives inside a div carrying the literal id `job-teaser-content`.
    # The id is duplicated across rows (Drupal bug), so we select by attribute
    # match rather than CSS `#id` (which would only match the first one).
    cards = soup.find_all("div", attrs={"id": "job-teaser-content"})
    out: list[dict[str, Any]] = []
    for card in cards:
        # Each card sits inside a <li>; pass the whole subtree to the parser.
        parsed = _parse_card(card)
        if parsed:
            out.append(parsed)
    return out


def _fetch_page(page: int, timeout_s: int = 25) -> tuple[int, str, list[dict[str, Any]]]:
    params = dict(DEFAULT_FILTERS)
    if page > 0:
        params["page"] = str(page)
    # Sort newest first.
    params["sort[name]"] = "created"
    params["sort[direction]"] = "DESC"
    resp = requests.get(SEARCH_URL, params=params, headers=UA, timeout=timeout_s)
    cards: list[dict[str, Any]] = []
    if resp.status_code == 200:
        try:
            cards = _parse_list_html(resp.text)
        except Exception:  # pragma: no cover - defensive
            log.exception("euraxess: parse exception on page %d", page)
            cards = []
    return resp.status_code, resp.text, cards


def fetch(filters: dict) -> list[Job]:
    """Fetch open job offers from EURAXESS.

    Reads `max_per_source` (default 12). Pages through the listing in 10-row
    chunks until the cap is filled. Returns canonical `Job` records.
    """
    cap = int(filters.get("max_per_source") or 12)
    timeout_s = int(filters.get("euraxess_timeout_s") or 25)

    jobs: list[Job] = []
    seen_nids: set[str] = set()
    last_status = 0
    last_body_head = ""
    page = 0
    pages_max = (cap + PAGE_SIZE - 1) // PAGE_SIZE + 1  # +1 buffer for partial pages

    try:
        while len(jobs) < cap and page < pages_max:
            status, body, cards = _fetch_page(page, timeout_s=timeout_s)
            last_status = status
            if status != 200 or not cards:
                last_body_head = (body or "")[:600]
                if status != 200:
                    log.warning(
                        "euraxess: page %d status=%s body_head=%r",
                        page, status, last_body_head,
                    )
                else:
                    log.warning(
                        "euraxess: page %d returned 0 cards (parse mismatch?) "
                        "body_head=%r",
                        page, last_body_head,
                    )
                break
            for c in cards:
                nid = c["external_id"]
                if nid in seen_nids:
                    continue
                seen_nids.add(nid)
                jobs.append(Job(
                    source="euraxess",
                    external_id=nid,
                    title=fix_mojibake(c["title"])[:140],
                    company=fix_mojibake(c["company"])[:120],
                    location=fix_mojibake(c["location"])[:120],
                    url=c["url"],
                    posted_at=c["posted_at"],
                    snippet=clean_snippet(c["snippet"], max_chars=400),
                    salary="",
                ))
                if len(jobs) >= cap:
                    break
            page += 1
    except requests.RequestException as e:
        log.error("euraxess: request failed: %s", e)
        last_body_head = f"<requests error: {e}>"

    log_step(
        "euraxess.fetch",
        input={
            "max_per_source": cap,
            "url": SEARCH_URL,
            "filters": DEFAULT_FILTERS,
        },
        output={
            "status_code": last_status,
            "count": len(jobs),
            "sample_titles": [j.title for j in jobs[:3]],
            **({"body_head": last_body_head} if last_body_head else {}),
        },
    )
    return jobs
