"""Built In (https://builtin.com) job source — US-curated tech / startup jobs.

Built In aggregates roles from US tech companies and startups, organized by
metro area and category (dev/engineering, data/analytics, design, etc.).
Heavy bias toward US-based roles; we constrain to *remote* category pages so
EU candidates have a reasonable shot. Downstream AI fit-scoring is what
ultimately gates relevance for non-US users — this adapter just supplies the
candidate pool.

Module key: ``builtin``. **Default OFF** — opt in by adding ``builtin`` to a
profile's ``sources_enabled`` list. Consider pairing with strict location /
visa filters in the user profile when enabled from the EU.

Integration choice: HTML scrape of category landing pages.

  Why HTML?
    Built In does NOT publish a real RSS or Atom feed. Despite paths like
    ``/jobs/rss``, ``/jobs/feed``, ``/jobs/rss.xml`` returning HTTP 200, all
    of them serve ``text/html`` (the regular jobs page). The XML namespace
    isn't there. We confirmed against ``builtin.com/robots.txt`` — only
    sitemaps are published, no syndication feeds.

    The internal ``api.builtin.com`` host exists (it's referenced from page
    ``data-external-api`` attributes) but its endpoints are gated:
    ``/v1/jobs`` returns HTTP 405 to GET/POST without the right auth or
    method, and ``/jobs`` returns a Go ``database/sql`` error
    (``mysql select many: missing destination name active_in_feed``)
    suggesting it's not a public REST surface.

    Saved-search IDs of the form ``/jobs/saved-search/<id>`` and
    ``/jobs?f=<id>`` resolve but require the user's session cookie to apply
    the filter — without cookies they all return the same default jobs
    page. So we don't use them; we navigate to the public category pages
    instead.

  Why category pages?
    Each ``/jobs/remote/<bucket>/<category>`` page renders ~25 server-side
    job cards with stable structure: ``data-job-id="<id>"``,
    ``data-id="job-card-title"``, ``data-id="company-title"``, plus icon
    rows for location / remote / posted-time. That's everything we need.

Caveats / limitations:
  * 25 cards per page; we cap at ``max_per_source`` (default 12). Pagination
    on these pages is JS-driven (``?page=2`` does not reliably return new
    server-rendered cards under bot UAs), so we do NOT paginate. If yield
    needs to grow we add more category pages, not deeper pages.
  * Built In is *US-heavy*. Even on ``/jobs/remote/...`` pages most listings
    are "Remote (US)". The fit analyzer / location filter further down the
    pipeline is responsible for filtering for EU candidates; this adapter
    intentionally doesn't try to second-guess location strings.
  * ``posted_at`` is rendered as relative text ("3 Days Ago", "10 Hours
    Ago"). We pass it through verbatim — downstream uses ``first_seen_at``
    for age cutoffs anyway.
  * No salary normalization — Built In sometimes shows "130K-160K Annually",
    sometimes nothing. We surface it as-is when present.
  * external_id is the numeric ``data-job-id`` attribute (Built In's own
    stable id; visible in the canonical job URL ``/job/<slug>/<id>``).
"""
from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any
from urllib.parse import urljoin

import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

BASE_URL = "https://builtin.com"

# Curated entry-points. We bias to remote-flagged category pages so the noise
# floor for EU users is at least livable. Order matters — we round-robin so
# that no single category dominates when ``max_per_source`` is low.
DEFAULT_PAGES: tuple[str, ...] = (
    # Front-end / JavaScript
    "/jobs/remote/dev-engineering/front-end",
    "/jobs/remote/dev-engineering/javascript",
    # MLOps / data engineering / ML
    "/jobs/remote/data-analytics/machine-learning",
    "/jobs/remote/dev-engineering/devops",
    "/jobs/remote/data-analytics/data-engineering",
)

# Allow user/profile override via ``filters["builtin_pages"]`` — list of
# absolute or relative URL paths to scrape. Useful for switching to non-remote
# (e.g. ``/jobs/new-york/dev-engineering/front-end``) when a profile is
# explicitly US-based.

# Per-card regex anchors. Each card opens with ``<div id="job-card-<id>"``.
_CARD_SPLIT_RE = re.compile(r'<div id="job-card-(\d+)"', re.IGNORECASE)
_TITLE_RE = re.compile(
    r'data-id="job-card-title"[^>]*>([\s\S]*?)</a>', re.IGNORECASE,
)
_TITLE_HREF_RE = re.compile(
    # The job-card-title anchor renders ``href`` BEFORE ``data-id`` in the
    # current Built In templates, so anchoring on ``data-id`` and looking
    # forward for ``href`` misses. We anchor on ``data-alias`` instead —
    # Built In emits it on the same anchor and it carries the same path.
    r'data-id="job-card-title"[^>]*data-alias="([^"]+)"', re.IGNORECASE,
)
# Fallback: pull href from any anchor that links to a /job/<slug>/<id>
# matching the current card's job_id (computed at parse time).
_JOB_HREF_RE = re.compile(r'href="(/job/[^"]+)"', re.IGNORECASE)
_COMPANY_RE = re.compile(
    r'data-id="company-title"[^>]*>\s*<span[^>]*>([^<]+)</span>',
    re.IGNORECASE,
)
_SNIPPET_RE = re.compile(
    r'<div class="fs-sm fw-regular mb-md text-gray-04">([\s\S]*?)</div>',
    re.IGNORECASE,
)
_SALARY_RE = re.compile(
    r'fa-sack-dollar[^"]*"[^>]*></i></div>\s*<span[^>]*>([^<]+)</span>',
    re.IGNORECASE,
)
_LOC_COUNTRY_RE = re.compile(
    r'fa-location-dot[\s\S]{0,400}?<span[^>]*>([^<]+)</span>', re.IGNORECASE,
)
_LOC_REMOTE_RE = re.compile(
    r'fa-house[\s\S]{0,400}?<span[^>]*>([^<]+)</span>', re.IGNORECASE,
)
_POSTED_RE = re.compile(
    r'(\d+\s+(?:Hours?|Days?|Weeks?|Months?)\s+Ago)', re.IGNORECASE,
)

# Optional: forensic logger is wired separately; degrade gracefully if absent.
try:  # pragma: no cover - thin shim
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]


def _log_forensic(payload: dict[str, Any]) -> None:
    if forensic is None:
        return
    try:
        forensic.log_step("builtin.fetch", input=payload.get("input", {}),
                          output=payload.get("output", {}))
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for builtin.fetch", exc_info=True)


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


def _first(pat: re.Pattern[str], blob: str) -> str:
    m = pat.search(blob or "")
    if not m:
        return ""
    return fix_mojibake(unescape(_strip_tags(m.group(1)))).strip()


def _split_cards(html: str) -> list[tuple[str, str]]:
    """Return ``(job_id, card_html)`` for each top-level job card on the page.

    Cards are bounded by the next ``<div id="job-card-<id>"`` opener (or the
    end of the document body). We don't try to balance tags — that's brittle
    and unnecessary because the per-card fields we want are all near the
    top of each card and never span a card boundary.
    """
    cards: list[tuple[str, str]] = []
    matches = list(_CARD_SPLIT_RE.finditer(html))
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        cards.append((m.group(1), html[start:end]))
    return cards


def _parse_card(card_html: str, job_id: str) -> Job | None:
    title = _first(_TITLE_RE, card_html)
    href = _first(_TITLE_HREF_RE, card_html)
    if not href:
        # Fallback: any anchor linking to a /job/<slug>/<job_id> within this
        # card. We disambiguate by requiring the trailing /<job_id>.
        for m in _JOB_HREF_RE.finditer(card_html):
            cand = m.group(1)
            if cand.rstrip("/").endswith(f"/{job_id}"):
                href = cand
                break
    if not (title and href):
        return None

    url = urljoin(BASE_URL, href)
    company = _first(_COMPANY_RE, card_html)

    # Location: "<country>" + optional "Remote" badge → join.
    country = _first(_LOC_COUNTRY_RE, card_html)
    remote = _first(_LOC_REMOTE_RE, card_html)
    parts = [p for p in (remote, country) if p]
    location = " · ".join(parts) if parts else "Remote / US"

    snippet_raw = ""
    sm = _SNIPPET_RE.search(card_html)
    if sm:
        snippet_raw = sm.group(1)

    salary = _first(_SALARY_RE, card_html)
    posted = _first(_POSTED_RE, card_html)

    return Job(
        "builtin",
        job_id,
        title[:140],
        company[:120],
        location[:120],
        url,
        posted,
        clean_snippet(snippet_raw, max_chars=400),
        salary[:60],
    )


def _resolve_pages(filters: dict) -> list[str]:
    raw = filters.get("builtin_pages")
    if isinstance(raw, (list, tuple)) and raw:
        out: list[str] = []
        for p in raw:
            if not isinstance(p, str) or not p.strip():
                continue
            out.append(p.strip() if p.startswith("http") else urljoin(BASE_URL, p.strip()))
        if out:
            return out
    return [urljoin(BASE_URL, p) for p in DEFAULT_PAGES]


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point. Returns up to ``max_per_source`` Jobs.

    ``filters`` keys consulted:
      * ``max_per_source`` (int, default 12) — cap on returned jobs across
        all category pages combined.
      * ``builtin_pages`` (list[str], optional) — override the default
        category pages. Each item may be a path (``/jobs/...``) or a full
        ``https://builtin.com/...`` URL.

    Round-robins across pages so a single hot category doesn't crowd out the
    others when ``max_per_source`` is small.
    """
    cap = int(filters.get("max_per_source") or 12)
    pages = _resolve_pages(filters)

    seen_ids: set[str] = set()
    per_page_cards: list[list[tuple[str, str]]] = []
    page_status: list[tuple[str, int | None, int]] = []  # (url, status, raw_card_count)
    body_head = ""

    try:
        for url in pages:
            try:
                r = requests.get(url, headers=UA, timeout=20)
                status = r.status_code
                if status != 200:
                    body_head = body_head or (r.text or "")[:500]
                    log.warning("builtin page non-200 status=%s url=%s", status, url)
                    page_status.append((url, status, 0))
                    per_page_cards.append([])
                    continue
                cards = _split_cards(r.text)
                per_page_cards.append(cards)
                page_status.append((url, status, len(cards)))
            except requests.RequestException as e:
                log.error("builtin page fetch failed url=%s err=%s", url, e)
                page_status.append((url, None, 0))
                per_page_cards.append([])
                body_head = body_head or repr(e)[:500]

        # Round-robin selection across pages.
        jobs: list[Job] = []
        idx = 0
        # Use independent cursors per page.
        cursors = [0] * len(per_page_cards)
        while len(jobs) < cap:
            advanced = False
            for pi, cards in enumerate(per_page_cards):
                if len(jobs) >= cap:
                    break
                ci = cursors[pi]
                if ci >= len(cards):
                    continue
                advanced = True
                cursors[pi] += 1
                job_id, card_html = cards[ci]
                if job_id in seen_ids:
                    continue
                job = _parse_card(card_html, job_id)
                if job is None:
                    continue
                seen_ids.add(job_id)
                jobs.append(job)
            if not advanced:
                break  # all pages exhausted
            idx += 1

    except Exception as e:  # noqa: BLE001
        log.exception("builtin fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]
        jobs = []

    sample_titles = [j.title for j in jobs[:5]]

    _log_forensic({
        "input": {
            "pages": pages,
            "max_per_source": cap,
        },
        "output": {
            "page_status": [
                {"url": u, "status": s, "raw_cards": n}
                for (u, s, n) in page_status
            ],
            "count": len(jobs),
            "sample_titles": sample_titles,
            "body_head": body_head,
        },
    })

    return jobs
