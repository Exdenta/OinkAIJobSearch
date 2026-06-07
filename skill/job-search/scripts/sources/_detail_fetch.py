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

# HTTP status codes that indicate an anti-bot block rather than a genuine
# "page gone" — these are the cases a headless browser can recover (it
# executes the JS/TLS challenge that plain `requests` fails). 404/410 etc.
# are NOT here: a real browser wouldn't recover a deleted page, so we don't
# waste a launch on them.
_ANTIBOT_STATUSES = frozenset({403, 429, 503})


def _browser_fallback_config() -> tuple[bool, float]:
    """Read the (enabled, timeout_s) fallback config from defaults, lazily.

    Lazy + defensive so this module never gains a hard import dependency on
    defaults' shape: any import/lookup problem → fallback disabled, which is
    exactly today's behavior.
    """
    try:
        from defaults import DEFAULTS
        enabled = bool(DEFAULTS.get("browser_fetch_fallback_enabled", False))
        timeout_s = float(DEFAULTS.get("browser_fetch_timeout_s", 30) or 30)
        return enabled, timeout_s
    except Exception:
        return False, 30.0


def _try_browser_fallback(url: str, *, max_chars: int) -> str:
    """Best-effort headless-browser retry for an anti-bot-blocked URL.

    Returns the rendered+cleaned body text, or "" if the fallback is
    disabled, playwright is unavailable, the URL is SSRF-blocked, or the
    render produced nothing. NEVER raises — a failure here must degrade to
    today's "" result.
    """
    enabled, timeout_s = _browser_fallback_config()
    if not enabled:
        return ""
    try:
        from browser_fetch import fetch_rendered
        rendered = fetch_rendered(url, timeout_s=timeout_s, max_chars=max_chars)
    except Exception as e:
        log.debug("detail_fetch: browser fallback raised for %s (%s)", url, e)
        return ""
    if rendered:
        log.debug("detail_fetch: browser fallback recovered %s (%d chars)",
                  url, len(rendered))
        return rendered
    return ""


def _try_chrome_agent_fallback(url: str) -> str:
    """Last-resort recovery via the OPERATOR's real desktop Chrome.

    Tier 3: when the headless Playwright fallback ALSO came back empty on an
    anti-bot status, hand the URL to ``chrome_agent_fetch.fetch_page_text_via_
    chrome``, which drives the operator's logged-in desktop Chrome (via
    ``claude -p --chrome`` + the claude-in-chrome MCP). That browser carries
    real cookies / a real fingerprint and can clear challenges that a fresh
    headless chromium cannot.

    Returns the recovered cleaned body text, or "" when the chrome-agent
    fallback is disabled (the DEFAULT — ``chrome_agent_fallback_enabled`` is
    False, so NO subprocess is spawned and behavior is identical to today),
    the URL is SSRF-blocked, or nothing came back. NEVER raises — any failure
    here degrades to today's "" result.
    """
    try:
        from chrome_agent_fetch import fetch_page_text_via_chrome
        recovered = fetch_page_text_via_chrome(url=url)
    except Exception as e:
        log.debug("detail_fetch: chrome-agent fallback raised for %s (%s)", url, e)
        return ""
    if recovered:
        log.info("detail_fetch: chrome-agent fallback recovered %s (%d chars)",
                 url, len(recovered))
        return recovered
    return ""


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
        # Anti-bot block (403/429/503): the page likely exists but `requests`
        # was fingerprinted. If the operator enabled the headless-browser
        # fallback AND playwright is installed, retry ONCE through a real
        # browser. Any failure there → fall back to today's behavior ("").
        if resp.status_code in _ANTIBOT_STATUSES:
            rendered = _try_browser_fallback(url, max_chars=max_chars)
            if rendered:
                _BODY_CACHE[url] = rendered
                return rendered
            # Tier 3: headless Playwright also came back empty (still blocked).
            # As a last resort, drive the operator's real desktop Chrome. This
            # is gated OFF by default (chrome_agent_fallback_enabled), so when
            # disabled it returns "" with no subprocess and behavior is exactly
            # today's. max_chars is NOT enforced here — the chrome helper owns
            # its own cleaning/contract.
            recovered = _try_chrome_agent_fallback(url)
            if recovered:
                _BODY_CACHE[url] = recovered
                return recovered
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
