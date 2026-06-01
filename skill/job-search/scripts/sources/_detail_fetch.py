"""Shared helper: fetch the full body text from a job-posting detail URL.

Algorithm v2.2 — Option 4 (source-side body fetch). Sources that surface
job postings via a list/index page (impactpool, devex, jobs_ac_uk,
reliefweb, etc.) historically only kept the listing snippet — title +
company + location, often <200 chars. The Sonnet scorer couldn't see
hard requirements (years, language, on-site, salary) that live in the
detail page body, so it over-scored.

This helper fetches each detail URL once and returns a cleaned plain-
text body (HTML stripped, mojibake fixed). Adapters call it inside
their main loop to populate `Job.snippet` with the real description.

Design choices:
  * **Concurrent.** Adapters typically have N ≤ 50 list entries; firing
    8 concurrent GETs cuts wall time without provoking source rate
    limits.
  * **Tolerant.** Network failures, timeouts, 4xx/5xx all return an
    empty string — caller falls back to whatever listing-card snippet
    they had before. Never raise.
  * **Cached per-process** to dedupe the rare case where the same URL
    appears twice in a single fetch run. Cache is in-memory only;
    each `search_jobs.py` invocation starts fresh.
  * **Conservative timeout.** 15s per URL. Detail pages on slow CMSes
    (reliefweb, impactpool) often need 5-8s. 15s ceiling avoids
    stalling the whole pipeline behind one slow page.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

import requests

import re

from text_utils import strip_html, fix_mojibake
from safe_url import safe_request  # SSRF guard — detail URLs come from scraped listings

# Pre-strip block patterns: anything inside <script>/<style>/<noscript> is
# JS/CSS, not body content. text_utils.strip_html leaves it as visible
# text which then dominates the captured snippet (we saw `imports`,
# `controllers/...` paths fill a 4 KB cap). Removing these wholesale is
# safer than relying on a downstream BeautifulSoup pass.
_BLOCK_STRIP_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
# Comments + `<!DOCTYPE …>` blocks aren't body either.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)

log = logging.getLogger(__name__)


DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}

# Per-process cache so a duplicate URL within the same run only fetches once.
_BODY_CACHE: dict[str, str] = {}


def fetch_body_text(
    url: str,
    *,
    timeout_s: float = 15.0,
    max_chars: int = 4000,
    headers: dict | None = None,
) -> str:
    """Fetch `url`, strip HTML, return cleaned text up to `max_chars`.

    Empty string on any failure (timeout / 4xx / 5xx / unparseable body).
    Caller decides what to do with the empty — typically fall back to
    the listing-card snippet.
    """
    if not url:
        return ""
    cached = _BODY_CACHE.get(url)
    if cached is not None:
        return cached
    try:
        # safe_request follows redirects with per-hop SSRF revalidation and
        # raises SSRFBlocked (a RequestException) for private/internal targets.
        resp = safe_request(
            "GET",
            url,
            timeout=timeout_s,
            headers={**DEFAULT_HEADERS, **(headers or {})},
        )
    except requests.RequestException as e:
        log.debug("detail_fetch: GET %s raised %s", url, e)
        _BODY_CACHE[url] = ""
        return ""
    if resp.status_code >= 400:
        log.debug("detail_fetch: GET %s → %s", url, resp.status_code)
        _BODY_CACHE[url] = ""
        return ""

    raw = resp.text or ""
    # Drop script/style/noscript blocks BEFORE strip_html so their inner
    # text (JS code, CSS, importmap JSON) doesn't end up in the body.
    raw = _BLOCK_STRIP_RE.sub(" ", raw)
    raw = _COMMENT_RE.sub(" ", raw)
    raw = _DOCTYPE_RE.sub(" ", raw)
    text = fix_mojibake(strip_html(raw))
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    _BODY_CACHE[url] = text
    return text


def fetch_many_bodies(
    urls: Iterable[str],
    *,
    timeout_s: float = 15.0,
    max_chars: int = 4000,
    workers: int = 8,
    headers: dict | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> dict[str, str]:
    """Fetch a batch of URLs concurrently.

    Returns a {url → body_text} map. Failures map to "" rather than
    being dropped from the dict, so callers can do a simple lookup.
    """
    urls = [u for u in urls if u]
    if not urls:
        return {}
    out: dict[str, str] = {}
    done = 0
    total = len(urls)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                fetch_body_text,
                u, timeout_s=timeout_s, max_chars=max_chars, headers=headers,
            ): u
            for u in urls
        }
        for fut in as_completed(futures):
            u = futures[fut]
            try:
                out[u] = fut.result() or ""
            except Exception as e:
                log.debug("detail_fetch: %s raised %s", u, e)
                out[u] = ""
            done += 1
            if progress_cb is not None:
                try:
                    progress_cb(done, total)
                except Exception:
                    pass
    return out


def clear_cache() -> None:
    """Tests / repeat runs that want a fresh per-URL cache state."""
    _BODY_CACHE.clear()
