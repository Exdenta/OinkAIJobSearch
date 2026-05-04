"""ReliefWeb (https://reliefweb.int) job source — UN OCHA's humanitarian portal.

Used to surface jobs in the humanitarian / development / UN agency / NGO space
(WFP, UNHCR, IRC, Save the Children, MSF, etc.). Roles span field operations,
programme management, finance, M&E — and increasingly MLOps / data / digital /
IT / engineering positions as humanitarian agencies modernize.

Integration choice: RSS (https://reliefweb.int/jobs/rss.xml).

  Why not the public API?
    The documented JSON API at https://api.reliefweb.int/v2/jobs requires a
    pre-approved `appname` since 1 November 2025 (see
    https://apidoc.reliefweb.int/parameters#appname). Without one the service
    returns HTTP 403:

        {"status":403,"error":{"type":"AccessDeniedHttpException",
         "message":"You are not using an approved appname. ..."}}

    The v1 API has been decommissioned (HTTP 410). Until we register an
    appname, we use the public RSS feed instead.

  Why not HTML scrape?
    Not necessary — the RSS feed is officially published, stable, well-formed,
    and gives us everything we need (title, link, pubDate, embedded
    organization + country tags inside the description HTML).

Caveats / limitations of the RSS path:
  * The feed returns the latest ~20 postings globally, no server-side
    filtering. ReliefWeb's `?advanced-search=(TY...)` theme filter works on
    the HTML page but is silently ignored on `rss.xml`. The `?search=...`
    free-text query DOES work on RSS, so we expose `reliefweb_search` in
    filters.yaml as a soft knob to bias the feed toward IT/engineering roles
    when desired. By default we leave it empty and let downstream AI scoring
    sort the noise.
  * Country / organization metadata is embedded inside the RSS description
    as HTML tags (`<div class="tag country">Country: ...</div>`,
    `<div class="tag source">Organization: ...</div>`); we parse them out
    with a simple regex.
  * The numeric job id is in the URL path: /job/<id>/<slug> — stable across
    runs, suitable for `external_id`.

When ReliefWeb approves an appname for FindJobs, we should switch to the v2
API (richer fields: country.iso3, theme[], career_categories[], experience,
type[]) and add proper server-side theme filtering.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlencode

import feedparser
import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

RSS_URL = "https://reliefweb.int/jobs/rss.xml"

# Optional: forensic logger is wired separately; degrade gracefully if absent.
try:  # pragma: no cover - thin shim
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]

# Match the per-tag <div> blobs ReliefWeb embeds inside the RSS description.
_TAG_COUNTRY_RE = re.compile(
    r'<div class="tag country">\s*Country:\s*(?P<v>[^<]+?)\s*</div>',
    re.IGNORECASE,
)
_TAG_SOURCE_RE = re.compile(
    r'<div class="tag source">\s*Organization:\s*(?P<v>[^<]+?)\s*</div>',
    re.IGNORECASE,
)
_TAG_CITY_RE = re.compile(
    r'<div class="tag city">\s*City:\s*(?P<v>[^<]+?)\s*</div>',
    re.IGNORECASE,
)
_JOB_ID_RE = re.compile(r"/job/(\d+)/")


def _extract_id(url: str) -> str:
    """Pull the stable numeric job id out of a ReliefWeb job URL."""
    m = _JOB_ID_RE.search(url or "")
    return m.group(1) if m else (url or "")


def _extract_tag(pat: re.Pattern[str], blob: str) -> str:
    m = pat.search(blob or "")
    if not m:
        return ""
    return fix_mojibake(m.group("v")).strip()


def _build_feed_url(filters: dict) -> str:
    """Optionally add a `search=` query to bias toward MLOps / engineering
    / data / IT / digital roles. Does NOT hardcode the bias — only applied
    when `reliefweb_search` is set in filters.yaml.
    """
    search = (filters.get("reliefweb_search") or "").strip()
    if not search:
        return RSS_URL
    return f"{RSS_URL}?{urlencode({'search': search})}"


def _log_forensic(payload: dict[str, Any]) -> None:
    if forensic is None:
        return
    try:
        forensic.log_step("reliefweb.fetch", input=payload.get("input", {}),
                          output=payload.get("output", {}))
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for reliefweb.fetch", exc_info=True)


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point. Returns up to `max_per_source` Jobs.

    `filters` keys consulted:
      * max_per_source (int, default 12) — cap on returned jobs
      * reliefweb_search (str, optional) — free-text query to pass to the
        feed; useful for biasing toward "software OR engineer OR data OR IT"
        without hardcoding that filter into the adapter.
    """
    cap = int(filters.get("max_per_source") or 12)
    feed_url = _build_feed_url(filters)

    jobs: list[Job] = []
    status_code: int | None = None
    body_head = ""
    sample_titles: list[str] = []

    try:
        # We fetch via `requests` (which uses `certifi`) and hand the bytes to
        # feedparser. Calling `feedparser.parse(url, ...)` directly relies on
        # urllib's SSL bundle, which is unreliable on macOS Python builds and
        # triggers CERTIFICATE_VERIFY_FAILED in some environments.
        r = requests.get(feed_url, headers=UA, timeout=20)
        status_code = r.status_code
        if status_code != 200:
            body_head = (r.text or "")[:500]
            log.error("reliefweb RSS non-200 status=%s body_head=%r",
                      status_code, body_head)
            r.raise_for_status()

        parsed = feedparser.parse(r.content)
        if parsed.bozo:
            log.warning("reliefweb feedparser bozo: %s",
                        getattr(parsed, "bozo_exception", ""))
        entries = list(parsed.entries or [])
        for entry in entries:
            if len(jobs) >= cap:
                break
            url = (entry.get("link") or "").strip()
            if not url:
                continue
            external_id = _extract_id(url)
            title = fix_mojibake(entry.get("title") or "").strip()
            if not (title and external_id):
                continue

            # Description holds both the metadata tags AND the body HTML.
            desc_html = entry.get("summary") or entry.get("description") or ""
            company = _extract_tag(_TAG_SOURCE_RE, desc_html)
            country = _extract_tag(_TAG_COUNTRY_RE, desc_html)
            city = _extract_tag(_TAG_CITY_RE, desc_html)
            location = ", ".join([p for p in (city, country) if p]) or country or "Worldwide"
            # ReliefWeb postings without a country are typically
            # global / remote-allowed roles.
            if not country and "remote" in (title.lower() + " " + desc_html.lower()):
                location = "Remote / Worldwide"

            jobs.append(Job(
                source="reliefweb",
                external_id=external_id,
                title=title[:140],
                company=company[:120],
                location=location[:120],
                url=url,
                posted_at=entry.get("published") or entry.get("updated") or "",
                snippet=clean_snippet(desc_html, max_chars=400),
            ))

        sample_titles = [j.title for j in jobs[:5]]

    except requests.RequestException as e:
        log.error("reliefweb fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]
    except Exception as e:  # noqa: BLE001
        log.exception("reliefweb fetch failed: %s", e)
        body_head = body_head or repr(e)[:500]

    _log_forensic({
        "input": {
            "endpoint": feed_url,
            "max_per_source": cap,
            "reliefweb_search": filters.get("reliefweb_search") or "",
        },
        "output": {
            "status_code": status_code,
            "count": len(jobs),
            "sample_titles": sample_titles,
            "body_head": body_head if status_code != 200 else "",
        },
    })

    return jobs
