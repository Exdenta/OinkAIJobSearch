"""aijobs.net (a.k.a. ai-jobs.net) source adapter — curated AI/ML/MLOps board.

Portal: https://aijobs.net (https://ai-jobs.net redirects here)
Module key: ``ai_jobs_net``

Default OFF — opt-in per user via filters.yaml. Useful for the senior MLOps
persona (e.g. user 169016071) because the board is *exclusively* AI / ML /
data engineering / MLOps with high signal density (~50k active listings).

Integration choice: HTML scrape (homepage listing)
--------------------------------------------------
We probed the obvious structured options on 2026-05-01 and none work:

  - ``/feed/`` , ``/rss/`` , ``/feed.xml`` , ``/rss.xml`` , ``/atom`` → 404
  - ``/jobs/feed/`` , ``/jobs/rss/`` → 200 but redirect/render the regular
    HTML listing page, not RSS
  - ``/sitemap.xml`` , ``/api/jobs`` , ``/api/v1/jobs`` → 404
  - ``?region=Europe`` (or any GET query param) → silently ignored; results
    are identical to the bare URL
  - ``/alerts/`` exists but is gated behind a $17/mo PRO subscription
  - The site filters via htmx POST to ``/`` with a CSRF token + tom-select
    multi-value form (regions, countries, topics, roles, skills, etc.).
    Replicating the POST flow is brittle and requires a CSRF round-trip;
    not worth it for ~50 cards on a single GET.

So we GET ``https://aijobs.net/`` and parse the listing cards with
BeautifulSoup. The homepage returns ~50 ``<li class="d-flex justify-between
position-relative ...">`` cards. Each card includes:

  - ``<a class="font-monospace fw-bold stretched-link" href="/job/{slug-id}/">``
    title + slug link. The trailing numeric ``id`` is stable across runs.
  - A ``<div class="text-end">`` block with experience badge, location text
    (e.g. ``Sioux Falls, SD, United States`` , ``Remote`` , ``北京``), and
    a relative date ``<div class="text-muted">3h ago</div>``.
  - Skills, perks, and (optionally) salary as colored badges — folded into
    the snippet so downstream AI scoring can use them.

Caveat: the listing card does **not** show the company name. The detail
page does (``<a href="/company/{slug-id}/">@ {company}</a>``). To avoid
N extra HTTP requests we leave ``company=""`` and let downstream enrichers
or the AI scorer infer it from the title/url. This matches the precedent
set by ``hackernews.py`` (Show HN posts have no employer field either).

EU bias
-------
The portal does not honour ``?region=`` query params, so we cannot push the
filter server-side. Instead we expose ``ai_jobs_net_eu_bias`` (default
False — adapter is default-OFF anyway) which, when True, skews ranking
toward listings whose location text or URL slug names a European country
or "Remote (Europe)". Non-EU jobs are still returned (we have no reliable
way to infer locale for slugs like ``/job/ai-111021/``); they just sort
last. This is a soft preference, not a hard filter — the senior MLOps
profile cares more about role quality than geography.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

LIST_URL = "https://aijobs.net/"
BASE = "https://aijobs.net"

try:  # pragma: no cover - optional forensic logger
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]

# ``/job/<slug>-<numeric-id>/`` — id is the trailing integer in the slug.
_JOB_HREF_RE = re.compile(r"^/job/(?P<slug>[a-z0-9\-]+?)-(?P<id>\d+)/?$")

# Soft EU bias keywords; used only when ai_jobs_net_eu_bias is True.
_EU_HINTS = (
    "europe", "european", "eu remote", "remote eu", "remote, eu",
    "uk", "united kingdom", "england", "scotland", "wales", "ireland",
    "germany", "deutschland", "berlin", "munich", "munchen", "hamburg",
    "france", "paris", "lyon", "spain", "espana", "madrid", "barcelona",
    "italy", "italia", "rome", "milan", "milano", "netherlands", "amsterdam",
    "belgium", "brussels", "portugal", "lisbon", "lisboa", "porto",
    "switzerland", "zurich", "geneva", "austria", "vienna", "wien",
    "denmark", "copenhagen", "kobenhavn", "sweden", "stockholm",
    "finland", "helsinki", "norway", "oslo", "iceland", "reykjavik",
    "poland", "warsaw", "warszawa", "krakow", "czechia", "czech republic", "prague",
    "hungary", "budapest", "romania", "bucharest", "greece", "athens",
    "estonia", "tallinn", "latvia", "riga", "lithuania", "vilnius",
    "luxembourg", "slovakia", "slovenia", "croatia", "bulgaria", "cyprus",
    "malta",
)


def _looks_european(blob: str) -> bool:
    s = (blob or "").lower()
    return any(h in s for h in _EU_HINTS)


def _log_forensic(payload: dict[str, Any]) -> None:
    if forensic is None:
        return
    try:
        forensic.log_step(
            "ai_jobs_net.fetch",
            input=payload.get("input", {}),
            output=payload.get("output", {}),
        )
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for ai_jobs_net.fetch", exc_info=True)


def _card_text_compact(node) -> str:
    """Inline text of a card subtree, collapsing whitespace."""
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True))


def _extract_cards(html: str) -> Iterable[Any]:
    soup = BeautifulSoup(html, "html.parser")
    # Listing cards: <li class="d-flex justify-content-between position-relative ...">
    for li in soup.select('li.d-flex.justify-content-between.position-relative'):
        # Skip nav / footer items: real cards always have a <a href="/job/...">
        link = li.find("a", href=_JOB_HREF_RE)
        if link is None:
            continue
        yield li


def _parse_card(li) -> dict | None:
    link = li.find("a", href=_JOB_HREF_RE)
    if link is None:
        return None
    href = link.get("href", "")
    m = _JOB_HREF_RE.match(href)
    if not m:
        return None
    external_id = m.group("id")
    slug = m.group("slug")
    url = BASE + href

    # Title: the link's text minus any "Featured" badge spans.
    for badge in link.find_all("span"):
        badge.extract()
    title = fix_mojibake(link.get_text(" ", strip=True))
    if not title:
        return None

    # Right-hand metadata column: experience, location, "Nh ago".
    right = li.find("div", class_="text-end")
    location = ""
    posted_at = ""
    if right is not None:
        # The relative date sits in <div class="text-muted">…</div>.
        posted_div = right.find("div", class_="text-muted")
        if posted_div is not None:
            posted_at = posted_div.get_text(" ", strip=True)
            posted_div.extract()
        # Drop badge spans (experience level, "R" remote dot) so the
        # remaining text is just the location.
        for badge in right.find_all("span"):
            badge.extract()
        location = _card_text_compact(right)

    # Body: skills + perks + salary live as siblings of the link inside the
    # left column. We use the whole <li> compact text (minus link/right) as
    # the snippet for downstream AI matching.
    snippet_html = str(li)
    snippet = clean_snippet(snippet_html, max_chars=400)

    return {
        "external_id": external_id,
        "slug": slug,
        "title": title[:140],
        "location": (location or "")[:120],
        "url": url,
        "posted_at": posted_at,
        "snippet": snippet,
    }


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point. Returns up to ``max_per_source`` Jobs.

    ``filters`` keys consulted:
      * ``max_per_source`` (int, default 12) — cap on returned jobs
      * ``ai_jobs_net_eu_bias`` (bool, default False) — when True, sort EU-
        coded listings ahead of the rest; does NOT drop non-EU jobs.
    """
    cap = int(filters.get("max_per_source") or 12)
    eu_bias = bool(filters.get("ai_jobs_net_eu_bias"))

    jobs: list[Job] = []
    status_code: int | None = None
    body_head = ""
    sample_titles: list[str] = []

    try:
        r = requests.get(LIST_URL, headers=UA, timeout=20)
        status_code = r.status_code
        if status_code != 200:
            body_head = (r.text or "")[:500]
            log.error("ai_jobs_net non-200 status=%s body_head=%r",
                      status_code, body_head)
            r.raise_for_status()

        cards = [c for c in (_parse_card(li) for li in _extract_cards(r.text)) if c]
        if eu_bias:
            cards.sort(
                key=lambda c: 0 if _looks_european(
                    f"{c['location']} {c['slug']} {c['title']}") else 1
            )

        for c in cards:
            if len(jobs) >= cap:
                break
            jobs.append(Job(
                "ai_jobs_net",                # source
                c["external_id"],             # external_id
                c["title"],                   # title
                "",                           # company (not in listing markup)
                c["location"] or "Unknown",   # location
                c["url"],                     # url
                c["posted_at"],               # posted_at (relative, e.g. "3h ago")
                c["snippet"],                 # snippet
            ))

        sample_titles = [j.title for j in jobs[:5]]

    except requests.RequestException as e:
        log.error("ai_jobs_net fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]
    except Exception as e:  # noqa: BLE001
        log.exception("ai_jobs_net fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]

    _log_forensic({
        "input": {
            "endpoint": LIST_URL,
            "max_per_source": cap,
            "ai_jobs_net_eu_bias": eu_bias,
        },
        "output": {
            "status_code": status_code,
            "count": len(jobs),
            "sample_titles": sample_titles,
            "body_head": body_head if status_code != 200 else "",
        },
    })

    return jobs
