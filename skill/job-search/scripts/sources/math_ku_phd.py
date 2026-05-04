"""University of Copenhagen — Mathematical Sciences PhD source.

The Department of Mathematical Sciences page
    https://www.math.ku.dk/english/programmes/ph.d/apply_for_a_phd/
is an *application instructions* page, not a listings feed: the math
department doesn't run its own ATS. All open KU PhD positions are
aggregated on the central HR portal at https://employment.ku.dk/phd/.

Probe results (2026-04-29):
  - https://employment.ku.dk/phd/?get_rss=1     -> HTTP 200, valid RSS 2.0
    (~25 items, with <title>, <description> (HTML), <pubDate>, and a
     <link> that contains a placeholder bug — but the show=NNNN id is
     extractable, and we rebuild the canonical URL from it).
  - https://employment.ku.dk/phd/?feed=rss2     -> HTTP 200 but text/html
    (param ignored; not a real feed).
  - https://employment.ku.dk/feed                -> 404.
  - https://employment.ku.dk/sitemap.xml         -> 404.
  - https://employment.ku.dk/all-vacancies/?department=Mathematical+Sciences
    -> HTTP 200 but the `department=` query param is NOT honored — every
    department comes back. Useless for server-side filtering.

Chosen approach: RSS at /phd/?get_rss=1.
  - Stable, structured, Apache-served XML (no JS).
  - Includes pubDate.
  - Volume: ~25 listings at any given time (across the *whole* university);
    typically 0–5 are math/CS/stats/data-science adjacent. Don't pre-filter:
    let downstream AI scoring decide relevance against the user's profile.

Caveats:
  - The <link> field in the RSS is broken (`https://cms.ku.dk Obvius::
    Document=HASH(0x...)?show=NNNN`). We discard it and rebuild the URL as
    `https://employment.ku.dk/phd/?show=NNNN`, which is the live, working
    posting URL.
  - <description> is HTML; we strip + truncate to 400 chars for the snippet.
  - Department/sub-faculty is NOT in any RSS field; we leave `company` as
    "University of Copenhagen — Mathematical Sciences" because that's the
    adapter slug's intent, even though some postings will actually be from
    sister departments (Niels Bohr Institute, Computer Science, etc.). The
    title and snippet make the actual home department obvious downstream.
  - All KU PhD positions are on-site in Copenhagen — `location` hard-coded.

Volume expectation: 0–5 net new listings/week, with the cap defaulting to
`max_per_source` (10).
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

# Optional forensic instrumentation. The `forensic` module is referenced in
# the project contract but may not be wired up yet — fall back to a no-op
# rather than fail-closed, mirroring the soft-import pattern used elsewhere
# (e.g. ftfy in text_utils).
try:
    import forensic  # type: ignore
    _HAVE_FORENSIC = True
except ImportError:  # pragma: no cover
    forensic = None  # type: ignore
    _HAVE_FORENSIC = False


SOURCE_SLUG = "math_ku_phd"
RSS_URL = "https://employment.ku.dk/phd/?get_rss=1"
POSTING_URL_TMPL = "https://employment.ku.dk/phd/?show={show_id}"
COMPANY = "University of Copenhagen — Mathematical Sciences"
LOCATION = "Copenhagen, Denmark"

_UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}
_SHOW_RE = re.compile(r"show=(\d+)")


def _log_step(name: str, *, input: dict, output: dict) -> None:
    """Dispatch to forensic.log_step if available, else stdlib logging."""
    if _HAVE_FORENSIC:
        try:
            forensic.log_step(name, input=input, output=output)  # type: ignore[attr-defined]
            return
        except Exception:  # pragma: no cover — never let logging break a fetch
            log.exception("forensic.log_step failed; falling back to logger")
    log.info("step=%s input=%s output=%s", name, input, output)


def _extract_show_id(entry: Any) -> str:
    """Pull the stable `show=NNNN` id out of an RSS entry.

    The RSS <link> is corrupted by KU's CMS (it contains a Perl `HASH(0x...)`
    debug string), but the show-id query param survives. As a fallback we
    also look at the entry id and the raw element string.
    """
    for candidate in (entry.get("link"), entry.get("id"), str(entry)):
        if not candidate:
            continue
        m = _SHOW_RE.search(candidate)
        if m:
            return m.group(1)
    return ""


def fetch(filters: dict) -> list[Job]:
    """Pull the current list of KU PhD positions.

    Returns an unfiltered list (capped at `max_per_source`) of every PhD-track
    posting on the KU HR portal. Math / CS / stats relevance is left to the
    downstream AI scorer.
    """
    cap = int(filters.get("max_per_source") or 10)
    out: list[Job] = []
    status_code: int | str = "n/a"
    error: str | None = None

    try:
        # KU's HR portal serves an incomplete TLS chain (leaf-only, no
        # intermediate from Let's Encrypt R13). Verified via openssl s_client:
        #   verify error:num=21:unable to verify the first certificate
        # `requests` (and Python's stdlib SSL) reject the connection. The RSS
        # is public, unauthenticated data — falling back to verify=False on
        # SSLError is pragmatic. We try strict first so a server fix is
        # automatically picked up.
        try:
            resp = requests.get(RSS_URL, headers=_UA, timeout=20)
        except requests.exceptions.SSLError:
            log.warning(
                "%s: TLS chain incomplete on %s; falling back to verify=False",
                SOURCE_SLUG, RSS_URL,
            )
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(RSS_URL, headers=_UA, timeout=20, verify=False)
        status_code = resp.status_code
        resp.raise_for_status()

        # Hand the bytes to feedparser directly so it can detect encoding.
        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            # Truly broken — bail with empty result.
            error = f"feedparser.bozo={parsed.bozo_exception!r}"
            log.warning("%s: RSS parse failed: %s", SOURCE_SLUG, error)
        else:
            for entry in parsed.entries:
                show_id = _extract_show_id(entry)
                if not show_id:
                    # Without a stable id we'd dedupe poorly — skip.
                    continue

                title_raw = entry.get("title") or ""
                desc_raw = entry.get("summary") or entry.get("description") or ""
                posted_at = (
                    entry.get("published")
                    or entry.get("updated")
                    or ""
                )
                url = POSTING_URL_TMPL.format(show_id=show_id)

                out.append(Job(
                    source=SOURCE_SLUG,
                    external_id=show_id,
                    title=fix_mojibake(title_raw)[:200],
                    company=COMPANY,
                    location=LOCATION,
                    url=url,
                    posted_at=posted_at,
                    snippet=clean_snippet(desc_raw, max_chars=400),
                ))

                if len(out) >= cap:
                    break
    except requests.RequestException as e:
        error = f"{type(e).__name__}: {e}"
        log.error("%s fetch failed: %s", SOURCE_SLUG, error)

    _log_step(
        f"{SOURCE_SLUG}.fetch",
        input={
            "url": RSS_URL,
            "max_per_source": cap,
        },
        output={
            "status_code": status_code,
            "count": len(out),
            "sample_titles": [j.title for j in out[:3]],
            "error": error,
        },
    )
    return out
