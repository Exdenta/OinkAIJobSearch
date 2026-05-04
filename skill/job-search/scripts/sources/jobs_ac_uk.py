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

Integration choice: RSS (per subject area)
------------------------------------------
jobs.ac.uk publishes RSS feeds for every subject area, type/role, and region
under the public discovery page at https://www.jobs.ac.uk/feeds. Each feed
follows the URL pattern:

    https://www.jobs.ac.uk/jobs/<slug>/?format=rss

Feeds are well-formed RSS 2.0 (Content-Type: application/rss+xml), each item
gives `<title>`, `<link>`, `<guid>`, and a `<description>` CDATA blob with
employer + salary + body HTML. They return up to 20 of the freshest postings
per category with no auth required.

  Why RSS instead of the search page?
    The HTML search results page is heavily JS-rendered and filter-shaped;
    the RSS feeds expose the same listings as a single, server-rendered,
    UTF-8-clean XML document with stable URLs. Cheaper, simpler, lower
    fragility surface than HTML scraping.

  Why a *fan-in* across multiple categories?
    A single category feed (e.g. social-sciences) misses cross-listed roles
    that are tagged only under humanities, psychology, or the "academic or
    research" type/role bucket. We fetch a small set of research-leaning
    categories, dedupe by URL, then cap at `max_per_source`. The category
    list biases toward qualitative-research-friendly disciplines but is
    overridable via the `jobs_ac_uk_categories` filter.

Caveats / fragility notes
-------------------------
  * Per-item RSS entries do NOT carry a `<pubDate>` (only the channel does),
    so `posted_at` is left empty. The downstream age-filter is permissive
    when posted_at is empty, and the title/description usually still convey
    recency. If they ever add per-item pubDate, feedparser will pick it up
    automatically via `entry.published`.
  * The description CDATA is structured as
      "{Employer} - {Department}<br />Salary: {amount}<br />..."
    so we split on `<br />` to extract the employer line and a `Salary:`
    regex to surface the salary. These are conventions, not guarantees;
    if the template drifts the parsers fall back gracefully (employer/
    salary become empty, the snippet still renders).
  * The job ID lives in the URL path: `/job/<EXTID>/<slug>/` where
    `<EXTID>` is an alphanumeric short code (e.g. `DRJ953`). It's stable
    across re-fetches and suitable for `external_id`.
  * The portal serves with charset=utf-8 and well-encoded glyphs (£, é,
    ü, etc.); we still pipe titles through `fix_mojibake` for safety
    in case of upstream encoding regressions.
  * No server-side full-text filter is exposed on the RSS feeds. Topic
    biasing is done by *which categories* we fan in, and downstream AI
    scoring handles the rest.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import feedparser
import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

BASE = "https://www.jobs.ac.uk"

# Default research-leaning category fan-in. Biases toward qualitative
# researcher / social-science / humanities profiles. Overridable via
# `filters["jobs_ac_uk_categories"]`. Slugs match the public feed paths
# enumerated at https://www.jobs.ac.uk/feeds/subject-areas and
# https://www.jobs.ac.uk/feeds/type-roles.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "social-sciences-and-social-care",
    "historical-and-philosophical-studies",
    "languages-literature-and-culture",
    "psychology",
    "politics-and-government",
    "academic-or-research",
)

# /job/<EXTID>/<slug>/ — EXTID is alphanumeric (e.g. DRJ953, ABC1234).
_JOB_ID_RE = re.compile(r"/job/([A-Za-z0-9]+)/")
# Salary blob inside CDATA description (we want the human-readable line).
_SALARY_RE = re.compile(
    r"Salary:\s*([^<&\r\n]+?)(?:&lt;|<|$)", re.IGNORECASE
)

# Optional: forensic logger is wired separately; degrade gracefully if absent.
try:  # pragma: no cover - thin shim
    import forensic  # type: ignore
except Exception:  # noqa: BLE001
    forensic = None  # type: ignore[assignment]


def _extract_id(url: str) -> str:
    """Pull the alphanumeric job id out of a jobs.ac.uk job URL."""
    m = _JOB_ID_RE.search(url or "")
    return m.group(1) if m else (url or "")


def _build_feed_urls(filters: dict) -> list[str]:
    cats = filters.get("jobs_ac_uk_categories")
    if cats:
        slugs = [str(c).strip().strip("/") for c in cats if str(c).strip()]
    else:
        slugs = list(DEFAULT_CATEGORIES)
    return [f"{BASE}/jobs/{slug}/?format=rss" for slug in slugs]


def _parse_company(desc_html: str) -> str:
    """Description starts with '{Employer} - {Dept}<br />Salary: ...' — grab
    the employer-and-department head before the first <br /> (escaped or not).
    """
    if not desc_html:
        return ""
    head = re.split(r"&lt;br|<br", desc_html, maxsplit=1)[0]
    return fix_mojibake(head).strip()


def _parse_salary(desc_html: str) -> str:
    if not desc_html:
        return ""
    m = _SALARY_RE.search(desc_html)
    if not m:
        return ""
    return fix_mojibake(m.group(1)).strip()


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
      * jobs_ac_uk_categories (list[str], optional) — override the default
        category fan-in. Each entry should be a slug from the public feed
        index (https://www.jobs.ac.uk/feeds/subject-areas), e.g.
        "social-sciences-and-social-care" or "academic-or-research".
    """
    cap = int(filters.get("max_per_source") or 12)
    feed_urls = _build_feed_urls(filters)

    jobs: list[Job] = []
    seen_urls: set[str] = set()
    per_feed_status: list[dict[str, Any]] = []
    body_head = ""

    try:
        for feed_url in feed_urls:
            if len(jobs) >= cap:
                break
            status_code: int | None = None
            entry_count = 0
            try:
                # Fetch via `requests` (uses certifi) and hand bytes to
                # feedparser; matches the reliefweb adapter pattern and
                # avoids macOS Python urllib SSL issues.
                r = requests.get(feed_url, headers=UA, timeout=20)
                status_code = r.status_code
                if status_code != 200:
                    body_head = (r.text or "")[:300] or body_head
                    log.warning(
                        "jobs_ac_uk RSS non-200 status=%s url=%s body_head=%r",
                        status_code,
                        feed_url,
                        body_head,
                    )
                    per_feed_status.append(
                        {"url": feed_url, "status": status_code, "entries": 0}
                    )
                    continue

                parsed = feedparser.parse(r.content)
                if parsed.bozo:
                    log.warning(
                        "jobs_ac_uk feedparser bozo on %s: %s",
                        feed_url,
                        getattr(parsed, "bozo_exception", ""),
                    )
                entries = list(parsed.entries or [])
                entry_count = len(entries)

                for entry in entries:
                    if len(jobs) >= cap:
                        break
                    url = (entry.get("link") or "").strip()
                    if not url or url in seen_urls:
                        continue
                    external_id = _extract_id(url)
                    title = fix_mojibake(entry.get("title") or "").strip()
                    if not (title and external_id):
                        continue

                    desc_html = (
                        entry.get("summary")
                        or entry.get("description")
                        or ""
                    )
                    company = _parse_company(desc_html)
                    salary = _parse_salary(desc_html)

                    # No structured location field on the feed; UK is the
                    # dominant footprint and the company string usually
                    # carries "University of X" which downstream geo
                    # filters can resolve. Leave a sensible default.
                    location = "United Kingdom"

                    seen_urls.add(url)
                    jobs.append(
                        Job(
                            "jobs_ac_uk",
                            external_id,
                            title[:140],
                            company[:120],
                            location[:120],
                            url,
                            # No per-item pubDate in jobs.ac.uk RSS; if
                            # they ever add it, feedparser exposes it as
                            # `published`. Channel-level pubDate is not
                            # useful as a per-item posted_at.
                            entry.get("published") or entry.get("updated") or "",
                            clean_snippet(desc_html, max_chars=400),
                            salary[:120],
                        )
                    )

                per_feed_status.append(
                    {"url": feed_url, "status": status_code, "entries": entry_count}
                )
            except requests.RequestException as e:
                log.error("jobs_ac_uk fetch failed for %s: %s", feed_url, e)
                body_head = body_head or repr(e)[:300]
                per_feed_status.append(
                    {"url": feed_url, "status": status_code, "error": repr(e)[:200]}
                )
                continue

    except Exception as e:  # noqa: BLE001
        log.exception("jobs_ac_uk fetch failed: %s", e)
        body_head = body_head or repr(e)[:300]

    sample_titles = [j.title for j in jobs[:5]]

    _log_forensic(
        {
            "input": {
                "feeds": feed_urls,
                "max_per_source": cap,
                "jobs_ac_uk_categories": filters.get("jobs_ac_uk_categories") or [],
            },
            "output": {
                "count": len(jobs),
                "sample_titles": sample_titles,
                "per_feed": per_feed_status,
                "body_head": body_head,
            },
        }
    )

    return jobs
