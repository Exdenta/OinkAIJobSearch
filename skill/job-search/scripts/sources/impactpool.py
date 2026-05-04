"""ImpactPool (https://www.impactpool.org) job source — UN / INGO / NGO portal.

Surfaces jobs across the international-impact ecosystem: UN agencies (UNHCR,
UNICEF, WFP, IOM, OHCHR), development banks (EBRD, EIB, IDB), INGOs, EU
institutions, and bilateral / academic mission organisations. Useful for a
qualitative researcher targeting peace / migration / policy / programme /
consultant roles where openings cluster outside the ReliefWeb/UN-Careers feeds.

Integration choice: HTML scrape of the public ``/search`` page.

  Why not RSS / API?
    Probed extensively (2026-05). ImpactPool exposes no public RSS, Atom, or
    JSON endpoint:

      GET /rss            -> 404
      GET /jobs/rss       -> 404
      GET /jobs.rss       -> HTTP 200 but redirects to /search returning HTML
      GET /jobs.json      -> HTTP 200 but redirects to /search returning HTML
      GET /jobs/feed      -> 404
      GET /atom           -> 404
      GET /api/jobs       -> 404

    The Rails-Turbo front-end serves listings exclusively as server-rendered
    HTML at ``/search`` (the ``/jobs`` path 302s to ``/search``). There is no
    XHR for listings — Turbo-Frames swap server-rendered partials.

  Why HTML scrape is safe enough here:
    * The markup is consistent: each card sits inside ``<div class='job'>``,
      the link is the first ``<a href='/jobs/<numeric_id>'>``, the title is
      tagged ``type='cardTitle'``, and the next two ``type='bodyEmphasis'``
      blocks are organisation and location respectively. This shape has been
      stable across multiple checks of the same listing page.
    * Numeric ids in the URL path (``/jobs/1209848``) are stable — perfect for
      ``external_id``.
    * No login, no cookies, no JS execution required for the listing page.
      Premium ImpactPool features (alerts, fellow status, salary insights)
      are paywalled, but the job listings themselves render server-side
      without authentication.

Caveats:
  * The listing HTML carries NO posting date — only "Senior - Senior level"
    seniority text. ``posted_at`` therefore comes back as an empty string;
    downstream age-filtering treats this the same as other sources that omit
    a date (the dedupe layer keys off ``external_id``, not ``posted_at``).
  * Search supports ``?q=<keywords>`` for a soft topical bias. We expose this
    via ``filters['impactpool_search']`` so a caller can lean toward
    qualitative-research / policy / programme roles without hardcoding a
    hard-coded filter into the adapter (downstream AI scoring is the real
    relevance gate).
  * The public listing returns 40 jobs per page in newest-first id order.
    We never paginate — ``max_per_source`` (default 12) is well under that.
  * No salary on the listing — left as empty string.

DISABLED BY DEFAULT in defaults.py / filters.yaml. Enable opt-in.
Module key: ``impactpool``.
"""
from __future__ import annotations

import html as _html
import logging
import re
from typing import Any
from urllib.parse import urlencode

import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

BASE_URL = "https://www.impactpool.org"
SEARCH_URL = f"{BASE_URL}/search"

# Optional: forensic logger is wired separately; degrade gracefully if absent.
try:  # pragma: no cover - thin shim
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Markup shape (as of 2026-05):
#
#   <div class='job'>
#     <div class='extra'>...</div>
#     <a data-turbo-frame="_top" href="/jobs/1209848">
#       <img alt="EBRD - ..." ... />
#       <div class='ip-typography' ... type='cardTitle'>Principal, Business IT ...</div>
#       <div class='ip-layout' gap='1' wrap='wrap'>
#         <div class='ip-typography' ... type='bodyEmphasis'>
#           EBRD - European Bank for Reconstruction and Development
#           <img src="/assets/ellipse-...svg" loading="lazy" />
#         </div>
#         <div class='ip-layout' gap='1'>
#           <div class='ip-typography' ... type='bodyEmphasis'>London</div>
#           <img ... />
#           <div class='ip-typography' ... type='bodyEmphasis'>Senior - Senior level</div>
#         </div>
#       </div>
#     </a>
#   </div>
#
# Three ``bodyEmphasis`` blocks per card in order: company, location, seniority.
# ---------------------------------------------------------------------------

# Note: HTML uses single-quoted attributes throughout. We tolerate either
# quote style defensively in case Rails ever flips its serializer.
_JOB_BLOCK_RE = re.compile(
    r"<div\s+class=['\"]job['\"]\s*>(.*?)</a>\s*</div>",
    re.DOTALL | re.IGNORECASE,
)
_HREF_RE = re.compile(
    r"href=['\"](/jobs/(\d+))['\"]",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r"type=['\"]cardTitle['\"][^>]*>\s*([^<]+?)\s*<",
    re.IGNORECASE,
)
_BODY_EMPH_RE = re.compile(
    r"type=['\"]bodyEmphasis['\"][^>]*>\s*([\s\S]*?)\s*</div>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_inline(raw: str) -> str:
    """Strip inline HTML tags + collapse whitespace + decode entities."""
    if not raw:
        return ""
    no_tags = _TAG_RE.sub(" ", raw)
    decoded = _html.unescape(no_tags)
    return _WS_RE.sub(" ", decoded).strip()


def _build_url(filters: dict) -> str:
    """Build the listing URL, optionally with a ``q=`` topical bias.

    ``filters['impactpool_search']`` is a free-text query passed through to
    the public search box. Empty / unset means "newest 40 globally".
    """
    q = (filters.get("impactpool_search") or "").strip()
    if not q:
        return SEARCH_URL
    return f"{SEARCH_URL}?{urlencode({'q': q})}"


def _log_forensic(payload: dict[str, Any]) -> None:
    if forensic is None:
        return
    try:
        forensic.log_step(
            "impactpool.fetch",
            input=payload.get("input", {}),
            output=payload.get("output", {}),
        )
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for impactpool.fetch", exc_info=True)


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point. Returns up to ``max_per_source`` Jobs.

    ``filters`` keys consulted:
      * ``max_per_source`` (int, default 12) — cap on returned jobs
      * ``impactpool_search`` (str, optional) — free-text query passed via
        ``?q=`` to bias the listing toward research / policy / programme /
        consultant roles. Leave unset to get the latest global feed and let
        downstream AI scoring sort the noise.

    Always returns a list (possibly empty) — never raises.
    """
    cap = int(filters.get("max_per_source") or 12)
    url = _build_url(filters)

    jobs: list[Job] = []
    status_code: int | None = None
    body_head = ""
    sample_titles: list[str] = []

    try:
        r = requests.get(url, headers=UA, timeout=20, allow_redirects=True)
        status_code = r.status_code
        if status_code != 200:
            body_head = (r.text or "")[:500]
            log.error(
                "impactpool listing non-200 status=%s body_head=%r",
                status_code,
                body_head,
            )
            r.raise_for_status()

        html_body = r.text or ""

        for block in _JOB_BLOCK_RE.findall(html_body):
            if len(jobs) >= cap:
                break

            href_m = _HREF_RE.search(block)
            title_m = _TITLE_RE.search(block)
            if not (href_m and title_m):
                continue

            external_id = href_m.group(2)
            href_path = href_m.group(1)
            full_url = f"{BASE_URL}{href_path}"

            title = fix_mojibake(_html.unescape(title_m.group(1))).strip()
            if not (title and external_id):
                continue

            bodies = [
                fix_mojibake(_strip_inline(b))
                for b in _BODY_EMPH_RE.findall(block)
            ]
            bodies = [b for b in bodies if b]

            company = bodies[0] if len(bodies) >= 1 else ""
            location = bodies[1] if len(bodies) >= 2 else ""
            seniority = bodies[2] if len(bodies) >= 3 else ""

            # Snippet: synthesize a short bullet from what we have, since the
            # listing card has no description blob. Keep it humanish and let
            # clean_snippet's HTML stripper / mojibake fixer pass through.
            snippet_bits = [b for b in (company, location, seniority) if b]
            snippet_raw = " — ".join(snippet_bits)

            jobs.append(
                Job(
                    "impactpool",                      # source
                    external_id,                       # external_id
                    title[:140],                       # title
                    company[:120],                     # company
                    (location or "Worldwide")[:120],   # location
                    full_url,                          # url
                    "",                                # posted_at (not on listing)
                    clean_snippet(snippet_raw, max_chars=400),  # snippet
                    "",                                # salary
                )
            )

        sample_titles = [j.title for j in jobs[:5]]

    except requests.RequestException as e:
        log.error("impactpool fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]
    except Exception as e:  # noqa: BLE001
        log.exception("impactpool fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]

    _log_forensic(
        {
            "input": {
                "endpoint": url,
                "max_per_source": cap,
                "impactpool_search": filters.get("impactpool_search") or "",
            },
            "output": {
                "status_code": status_code,
                "count": len(jobs),
                "sample_titles": sample_titles,
                "body_head": body_head if status_code != 200 else "",
            },
        }
    )

    return jobs
