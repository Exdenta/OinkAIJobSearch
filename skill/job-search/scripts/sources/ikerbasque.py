"""Ikerbasque (https://www.ikerbasque.net) source adapter (slug: `ikerbasque`).

Ikerbasque is the Basque Foundation for Science — a small Bilbao-based research
foundation that runs a handful of recurring calls per year (Research Fellows,
Permanent Positions, ERC Fast Track, plus DIPC-partnered quantum positions).
For a qualitative researcher targeting Bilbao + research, this is a direct hit.

Default: OFF. Enable via filters.yaml `enabled_sources` list when desired.

Integration choice: HTML scrape of /en/calls.

  Why not RSS?
    The site does publish a sitewide RSS feed at
    https://www.ikerbasque.net/en/rss.xml, but it is mixed-purpose (annual
    reports, news items, test posts) and is NOT the calls listing — there
    is no per-section feed. We probed:
        /en/rss/calls.xml         -> 404
        /en/calls/rss.xml         -> 404
        /en/convocatorias/rss     -> 404
    so HTML scraping is the only viable path.

  Why HTML is fine here:
    The /en/calls page is a tidy Drupal Views render — each call sits in
    a `<div class="views-row">` containing exactly one `<h3><a>title</a></h3>`,
    one or two `<h4>` status badges (Open / Closed), and a paragraph blurb
    inside `<div class="columns large-8">`. Volume is genuinely tiny
    (typically 3–8 calls live at once), the markup has been stable for years,
    and the site is small enough that a single GET with default UA is
    appropriate. We cap at `max_per_source` (default 12).

Filters / config keys consulted:
  * max_per_source (int, default 12) — cap on returned Jobs
  * ikerbasque_include_closed (bool, default False) — when True, also include
    rows whose status label reads "Closed". By default we surface only Open
    calls, since closed listings would only add noise to alerts.

Output Job fields:
  * source        = "ikerbasque"
  * external_id   = the URL slug (e.g. "ikerbasque-erc-fast-track-2026")
  * title         = the <h3> text, mojibake-fixed
  * company       = "Ikerbasque" (the foundation; some calls are co-run with
                    DIPC, but the foundation is always the public host)
  * location      = "Bilbao, Spain" (Ikerbasque only places researchers at
                    Basque-region host institutions; Bilbao is the canonical
                    foundation seat. Specific host varies per programme but
                    is rarely listed on the index page.)
  * url           = absolute https://www.ikerbasque.net/en/calls/<slug>
  * posted_at     = "" (the index has no per-row date and the detail pages
                    do not expose a stable publish timestamp; we leave it
                    blank rather than fabricate one)
  * snippet       = status label + paragraph blurb, cleaned to <=400 chars
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

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

LISTING_URL = "https://www.ikerbasque.net/en/calls"
BASE = "https://www.ikerbasque.net"

# Optional: forensic logger is wired separately; degrade gracefully if absent.
try:  # pragma: no cover - thin shim
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]


_SLUG_RE = re.compile(r"^/en/calls/(?P<slug>[^/?#]+)/?$")


def _slug_from_href(href: str) -> str:
    m = _SLUG_RE.match(href or "")
    return m.group("slug") if m else ""


def _log_forensic(payload: dict[str, Any]) -> None:
    if forensic is None:
        return
    try:
        forensic.log_step("ikerbasque.fetch", input=payload.get("input", {}),
                          output=payload.get("output", {}))
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for ikerbasque.fetch", exc_info=True)


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point. Returns up to `max_per_source` Jobs.

    Returns an empty list (without raising) on transport / parse failure or
    when the foundation simply has no current openings — this is a small
    foundation and a legitimately empty index is normal.
    """
    cap = int(filters.get("max_per_source") or 12)
    include_closed = bool(filters.get("ikerbasque_include_closed") or False)

    jobs: list[Job] = []
    status_code: int | None = None
    body_head = ""
    sample_titles: list[str] = []
    rows_seen = 0

    try:
        r = requests.get(LISTING_URL, headers=UA, timeout=20)
        status_code = r.status_code
        if status_code != 200:
            body_head = (r.text or "")[:500]
            log.error("ikerbasque listing non-200 status=%s body_head=%r",
                      status_code, body_head)
            r.raise_for_status()

        # Drupal Views serves UTF-8; requests guesses encoding from headers.
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("div.views-row")
        rows_seen = len(rows)

        for row in rows:
            if len(jobs) >= cap:
                break

            h3 = row.find("h3")
            anchor = h3.find("a", href=True) if h3 else None
            if not anchor:
                continue
            href = anchor.get("href", "").strip()
            slug = _slug_from_href(href)
            if not slug:
                # Skip rows that link somewhere unexpected (e.g. /en/calls/evaluation).
                continue

            title = fix_mojibake(anchor.get_text(strip=True))
            if not title:
                continue

            # Status label: <h4>Open</h4> / <h4>Closed</h4>. There may be
            # multiple <h4> blocks (one empty); take the first non-empty.
            status_label = ""
            for h4 in row.find_all("h4"):
                txt = h4.get_text(strip=True)
                if txt:
                    status_label = txt
                    break
            is_closed = status_label.lower() == "closed"
            if is_closed and not include_closed:
                continue

            # Body blurb: the right-hand column.
            blurb_node = row.select_one("div.columns.large-8") or row
            # Drop the "Read more" paragraph from the snippet source.
            for p in blurb_node.select("p.boton"):
                p.decompose()
            blurb_html = blurb_node.decode_contents()

            # Prepend the status label so downstream scoring sees Open/Closed.
            if status_label:
                snippet_src = f"[{status_label}] {blurb_html}"
            else:
                snippet_src = blurb_html

            url = urljoin(BASE, href)

            jobs.append(Job(
                "ikerbasque",
                slug,
                title[:140],
                "Ikerbasque",
                "Bilbao, Spain",
                url,
                "",  # posted_at: not exposed by the index; leave blank
                clean_snippet(snippet_src, max_chars=400),
            ))

        sample_titles = [j.title for j in jobs[:5]]

    except requests.RequestException as e:
        log.error("ikerbasque fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]
    except Exception as e:  # noqa: BLE001
        log.exception("ikerbasque fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]

    _log_forensic({
        "input": {
            "endpoint": LISTING_URL,
            "max_per_source": cap,
            "ikerbasque_include_closed": include_closed,
        },
        "output": {
            "status_code": status_code,
            "rows_seen": rows_seen,
            "count": len(jobs),
            "sample_titles": sample_titles,
            "body_head": body_head if status_code != 200 else "",
        },
    })

    return jobs
