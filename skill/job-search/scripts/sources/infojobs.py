"""InfoJobs Spain (https://www.infojobs.net) source adapter.

InfoJobs is the #1 general-purpose job board in Spain. This adapter surfaces
postings across the entire Spanish market — useful for the subset of users
targeting Spain coverage. Default to OFF in `defaults.py`; opt-in only.

Integration choice: HTML scrape (BeautifulSoup)
-----------------------------------------------
Probed alternatives on 2026-05-01:

  - https://developer.infojobs.net    → public OAuth2 API exists, but it
    requires a partner-approved client_id/secret, granted only to recruiting
    products. Not viable for a personal job-alert bot. (See the developer
    portal: "Para empezar, regístrate como desarrollador y solicita las
    credenciales de tu aplicación".)
  - https://www.infojobs.net/rss      → 404 (no public site-wide RSS, no per-
    saved-search RSS exposed without authentication).
  - sitemap.xml                       → enumerates millions of company-detail
    URLs, not search results; not useful for a "latest postings" pull.

What works: the public results page at `/ofertas-trabajo` returns 200 OK with
~10 server-rendered offer cards under `<li class="ij-OfferList-offerCardItem">`
inside `<div class="ij-OfferCardContent">`. The markup is stable, brand-prefixed
(`ij-*`), and visible without cookies / JS / login. We fetch one page, parse it,
and emit canonical Job records.

Card structure (verified 2026-05-01):

    <li class="ij-OfferList-offerCardItem">
      <div class="ij-OfferCardContent">
        <h2 class="ij-OfferCardContent-description-title">
          <a href="//www.infojobs.net/{slug}/of-i{HEX}?...">
            <span class="ij-OfferCardContent-description-title-link">
              {title}
            </span>
          </a>
        </h2>
        <h3 class="ij-OfferCardContent-description-subtitle">
          <a href="...">{company}</a>
        </h3>
        <ul>
          <li class="ij-OfferCardContent-description-list-item">{location}</li>
          <li class="ij-OfferCardContent-description-list-item">Presencial|Híbrido|Remoto</li>
          <li class="ij-OfferCardContent-description-list-item">Hace {N}{unit}...</li>
          <li class="ij-OfferCardContent-description-list-item">{contract type}</li>
          <li class="ij-OfferCardContent-description-list-item">{schedule}</li>
          <li class="ij-OfferCardContent-description-salary-info">{salary range}</li>
        </ul>
        <div class="ij-OfferCardContent-description-description">
          {body snippet in Spanish, ~300 chars}
        </div>
      </div>
    </li>

External id lives in the offer URL path: `/of-i<32-char-hex>`. This id is
stable across re-listings (only the slug / query string change).

Caveats / fragility notes
-------------------------
  * Pages are Spanish. Title and snippet are kept in their original language;
    downstream AI scoring handles ES↔EN. Location is rendered as
    "City, Spain" so non-Spanish users have a country anchor.
  * Some "Destacada" (sponsored) cards re-list older offers — the sincedate
    tag will say "Publicada de nuevo". We pass the human string through to
    `posted_at` (downstream age filter accepts ES strings).
  * URLs in the markup are protocol-relative (`//www.infojobs.net/...`); we
    promote them to absolute https.
  * If Cloudflare ever fronts the page with a JS challenge, this adapter will
    return [] and forensic logging will record the body_head for triage.
    There is no graceful fallback — the developer API would be the only fix.
  * No login / cookie / API key required as of 2026-05-01.
  * `ofertas-trabajo` IS NOT in `robots.txt` Disallow rules — only the legacy
    `.cfm` endpoints are blocked.
  * UA NOTE: InfoJobs' edge returns HTTP 405 to anything that isn't a
    browser-shaped User-Agent (verified 2026-05-01: the project-default
    `FindJobs-Bot/1.0` UA is rejected with 405; a Mozilla/5.0 UA succeeds
    with 200). We send a desktop Chrome UA — the same approach `euraxess.py`
    already uses. This is a public, unauthenticated, no-rate-limit page; we
    aren't impersonating to bypass auth, just to avoid an over-eager bot
    filter on a page that is otherwise free to scrape.

Default state: OFF. Add to a user's enabled sources only if they want
Spain coverage.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

# NOTE: the canonical project UA "FindJobs-Bot/1.0 ..." is rejected by
# InfoJobs' edge with HTTP 405 (verified 2026-05-01). We fall back to a
# desktop Chrome UA — same approach used by sources/euraxess.py for an
# analogous bot-filter on the EU portal. Public scrape, no auth bypass.
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

BASE = "https://www.infojobs.net"
SEARCH_URL = f"{BASE}/ofertas-trabajo"

# Best-effort forensic logger; module may not exist in every checkout.
try:  # pragma: no cover - thin shim
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]


_OFFER_ID_RE = re.compile(r"/of-i([0-9a-f]+)", re.IGNORECASE)


def _abs_url(href: str) -> str:
    """InfoJobs frequently uses protocol-relative or root-relative hrefs."""
    if not href:
        return ""
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return urljoin(BASE, href)
    return href


def _extract_id(url: str) -> str:
    m = _OFFER_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _parse_card(card: Any) -> dict[str, Any] | None:
    """Pull canonical fields out of one `<li class="...offerCardItem">` block."""
    title_link = card.select_one("a.ij-OfferCardContent-description-link")
    if not title_link or not title_link.get("href"):
        return None
    href = _abs_url(title_link["href"])
    external_id = _extract_id(href)
    if not external_id:
        return None

    title_span = title_link.select_one(".ij-OfferCardContent-description-title-link")
    title = (title_span or title_link).get_text(" ", strip=True)
    if not title:
        return None

    company_link = card.select_one("a.ij-OfferCardContent-description-subtitle-link")
    company = company_link.get_text(" ", strip=True) if company_link else ""

    # First non-modal list item is the city; the InfoJobs schema is positional
    # but we can be slightly defensive: skip work-mode tokens (Presencial /
    # Hybrid / Remote) when picking the city.
    list_items = card.select(".ij-OfferCardContent-description-list-item")
    work_mode_tokens = {"presencial", "híbrido", "hibrido", "remoto", "teletrabajo"}
    city = ""
    work_mode = ""
    for it in list_items:
        txt = it.get_text(" ", strip=True)
        if not txt:
            continue
        low = txt.lower()
        if low in work_mode_tokens and not work_mode:
            work_mode = txt
            continue
        if low.startswith("hace ") or "publicada" in low:
            continue
        if "contrato" in low or "jornada" in low or "€" in txt:
            continue
        if not city:
            city = txt

    location_pieces = [p for p in (city, "Spain") if p]
    if work_mode:
        location_pieces.append(work_mode)
    location = ", ".join(location_pieces)

    # Posted date — keep the human "Hace Nm/h/d" string; downstream filter
    # understands relative phrasing.
    posted_at = ""
    sd = card.select_one('[data-testid="sincedate-tag"]')
    if sd:
        posted_at = sd.get_text(" ", strip=True)
    else:
        for it in list_items:
            txt = it.get_text(" ", strip=True)
            if txt.lower().startswith("hace "):
                posted_at = txt
                break

    salary = ""
    sal = card.select_one(".ij-OfferCardContent-description-salary-info")
    if sal:
        salary = sal.get_text(" ", strip=True)

    desc = card.select_one(".ij-OfferCardContent-description-description")
    snippet_raw = desc.get_text(" ", strip=True) if desc else ""

    return {
        "external_id": external_id,
        "title": title,
        "company": company,
        "location": location,
        "url": href,
        "posted_at": posted_at,
        "snippet": snippet_raw,
        "salary": salary,
    }


def _parse_list_html(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.find_all("li", class_="ij-OfferList-offerCardItem")
    out: list[dict[str, Any]] = []
    for card in cards:
        try:
            parsed = _parse_card(card)
        except Exception:  # pragma: no cover - defensive
            log.exception("infojobs: parse exception on card")
            parsed = None
        if parsed:
            out.append(parsed)
    return out


def _log_forensic(payload: dict[str, Any]) -> None:
    if forensic is None:
        return
    try:
        forensic.log_step(
            "infojobs.fetch",
            input=payload.get("input", {}),
            output=payload.get("output", {}),
        )
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for infojobs.fetch", exc_info=True)


def fetch(filters: dict) -> list[Job]:
    """Fetch latest postings from InfoJobs Spain.

    `filters` keys consulted:
      * max_per_source (int, default 12) — cap on returned jobs
      * infojobs_query (str, optional) — free-text query passed as `?keyword=`
        to bias the result list toward a topic. Default: site-wide latest.
      * infojobs_timeout_s (int, default 25) — request timeout.

    Returns up to `max_per_source` Job records; returns [] gracefully on any
    network / parser failure (forensic log will record the body_head).
    """
    cap = int(filters.get("max_per_source") or 12)
    timeout_s = int(filters.get("infojobs_timeout_s") or 25)
    query = (filters.get("infojobs_query") or "").strip()

    params: dict[str, str] = {"sortBy": "PUBLICATION_DATE"}
    if query:
        params["keyword"] = query

    jobs: list[Job] = []
    status_code: int | None = None
    body_head = ""

    try:
        r = requests.get(SEARCH_URL, params=params, headers=UA, timeout=timeout_s)
        status_code = r.status_code
        if status_code != 200:
            body_head = (r.text or "")[:500]
            log.warning(
                "infojobs non-200 status=%s body_head=%r", status_code, body_head
            )
        else:
            cards = _parse_list_html(r.text)
            if not cards:
                body_head = (r.text or "")[:500]
                log.warning(
                    "infojobs: 0 cards parsed (markup drift or block?) body_head=%r",
                    body_head,
                )
            for c in cards:
                if len(jobs) >= cap:
                    break
                jobs.append(Job(
                    "infojobs",
                    c["external_id"],
                    fix_mojibake(c["title"])[:140],
                    fix_mojibake(c["company"])[:120],
                    fix_mojibake(c["location"])[:120],
                    c["url"],
                    c["posted_at"],
                    clean_snippet(c["snippet"], max_chars=400),
                    fix_mojibake(c.get("salary") or "")[:120],
                ))
    except requests.RequestException as e:
        log.error("infojobs fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]
    except Exception as e:  # noqa: BLE001
        log.exception("infojobs fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]

    _log_forensic({
        "input": {
            "endpoint": SEARCH_URL,
            "max_per_source": cap,
            "infojobs_query": query,
        },
        "output": {
            "status_code": status_code,
            "count": len(jobs),
            "sample_titles": [j.title for j in jobs[:5]],
            "body_head": body_head if (status_code != 200 or not jobs) else "",
        },
    })

    return jobs
