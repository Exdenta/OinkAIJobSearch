"""Universitat de Barcelona — PhD / predoctoral vacancy adapter (slug: `ub_doctoral`).

Reads UB's official public-vacancy board (the *seu electrònica* → *Ofertes de
feina*), filtered server-side to currently-OPEN vacancies:

    https://seu.ub.edu/ofertaPublicaCategoriaPublic/categories

REWRITTEN 2026-07-13 — this adapter used to shell out to the Claude CLI and ask
it to browse `web.ub.edu` / `www.ub.edu`. That was broken twice over:

  1. Those hosts sit behind a WAF that 403s every datacenter IP and every
     non-Spanish residential IP, so the model never saw a page at all.
  2. Even unblocked, the pages it was pointed at are navigation hubs that carry
     no vacancies whatsoever.

The old module header argued an LLM was unavoidable because "the site is Liferay
and hand-maintained selectors would break constantly". That reasoning was about
the WRONG HOST. `seu.ub.edu` is a different system: a plain, stable HTML table
with `data-label` cells, a server-side open-state filter (`estat=Oberta`) and
full-text search (`text=`) — no JS, no cookies, no WAF, no AI needed. So this
adapter is now a plain HTTP fetch: faster, free, and no longer dependent on the
Claude CLI (one less failure surface in the bot's per-cycle hot path).

Filters / config
----------------
  - `max_per_source` (default 12)  — cap on returned Jobs
  - `sources.ub_doctoral` toggle   — wired in defaults.py; this module is opt-in.

Never raises: any network/parse failure logs and returns what it already has.

PARALLEL IMPLEMENTATION — keep in sync with apify/ub-doctoral-scraper/src/main.py,
which feeds the SAME downstream scoring pipeline via apify_fetch.record_to_job.
The actor emits a dedicated `deadline` column; `Job` has no such field — and UB
publishes deadlines only inside each vacancy's attached PDF anyway, so neither
side ever invents one.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

SOURCE_KEY = "ub_doctoral"

BOARD_URL = "https://seu.ub.edu/ofertaPublicaCategoriaPublic/categories"
DETAIL_URL = "https://seu.ub.edu/ofertaPublicaCategoriaPublic/listPublicacionsAmbCategoria"

# UB's board ignores `max` above 10 and pages via `offset`.
PAGE_SIZE = 10
MAX_PAGES = 5

# UB writes its vacancy titles in Catalan; "predoctoral" is the term it uses for
# funded PhD contracts, and it is what the board's own full-text search matches.
DEFAULT_QUERY = "predoctoral"

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ca;q=0.8,es;q=0.7",
}

_CID_RE = re.compile(r"categoria\.id=(\d+)")


def _params(offset: int) -> dict[str, str]:
    return {
        "lang": "en",  # forces the English `data-label` column names we parse
        "tipus": "totes",
        "estat": "Oberta",  # currently-open vacancies only — filtered by UB
        "text": DEFAULT_QUERY,
        "dataOfertaPublicaFilter": "dataPublicacio",
        "max": str(PAGE_SIZE),
        "offset": str(offset),
    }


def _posted_at(raw: str) -> str:
    """UB prints dd-mm-yyyy. Return YYYY-MM-DD, or "" when unreadable — never guessed."""
    try:
        return datetime.strptime((raw or "").strip(), "%d-%m-%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _jobs_from_page(html: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Job] = []
    for tr in soup.find_all("tr"):
        link = tr.find("a", href=_CID_RE)
        if not link:
            continue
        cid = _CID_RE.search(link["href"]).group(1)
        cells = {
            (td.get("data-label") or "").strip(): td.get_text(" ", strip=True)
            for td in tr.find_all("td")
        }
        # The full convocatòria text is in the link's title attribute; the visible
        # cell text is the same string, so take whichever is longer.
        title = max((link.get("title") or "").strip(), link.get_text(" ", strip=True), key=len)
        title = fix_mojibake(re.sub(r"\s+", " ", title)).strip()
        if not title:
            continue
        out.append(Job(
            source=SOURCE_KEY,
            external_id=cid,  # UB's own stable numeric vacancy id
            title=title[:140],
            company="Universitat de Barcelona",
            location="Barcelona, Spain",
            url=f"{DETAIL_URL}?categoria.id={cid}",
            posted_at=_posted_at(cells.get("Publication date", "")),
            snippet=clean_snippet(title, max_chars=400),
        ))
    return out


def fetch(filters: dict | None = None) -> list[Job]:
    """Fetch currently-open UB PhD / predoctoral vacancies. Never raises."""
    cap = int((filters or {}).get("max_per_source") or 12)

    jobs: list[Job] = []
    seen: set[str] = set()
    for page in range(MAX_PAGES):
        try:
            resp = requests.get(
                BOARD_URL, params=_params(page * PAGE_SIZE), headers=UA, timeout=30
            )
            resp.raise_for_status()
        except Exception as exc:  # network / DNS / HTTP error — keep what we have
            log.warning(
                "ub_doctoral: board fetch failed (%s); returning %d job(s)", exc, len(jobs)
            )
            break

        page_jobs = _jobs_from_page(resp.text)
        if not page_jobs:
            break
        for job in page_jobs:
            if job.external_id in seen:
                continue
            seen.add(job.external_id)
            jobs.append(job)

        # Cap met, or a short page means this was the last one.
        if len(jobs) >= cap or len(page_jobs) < PAGE_SIZE:
            break

    jobs = jobs[:cap]
    log.info("ub_doctoral: %d open vacancy(ies) from UB's board", len(jobs))
    return jobs
