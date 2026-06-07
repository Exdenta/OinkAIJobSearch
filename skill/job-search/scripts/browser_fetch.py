"""Headless-browser fallback tier for anti-bot-blocked job-detail pages.

Some org sites (migrationpolicy.org, undp.org, iom.int, devex.com, …) return
HTTP 403/429/503 to plain ``requests`` even with a realistic Firefox/Chrome
User-Agent — genuine anti-bot defences (TLS/JS fingerprinting, challenge
pages). A real (headless) browser executes the JS challenge and gets the
rendered page; plain ``requests`` never can.

This module exposes a single entry point, :func:`fetch_rendered`, used as a
LAST-RESORT fallback by ``sources/_detail_fetch.fetch_body_text`` when the
primary ``safe_request`` GET is anti-bot-blocked AND the operator has enabled
``browser_fetch_fallback_enabled`` in ``defaults.py`` (default OFF).

Hard safety / robustness contract (see TASK B):
  * **SSRF first.** Call ``safe_url.is_safe_url(url)`` BEFORE doing anything
    else; on an unsafe URL return ``None`` and NEVER launch the browser. The
    browser bypasses the ``safe_request`` per-hop guard, so this is the only
    SSRF check on this path — it must run first.
  * **Lazy + optional.** Import ``playwright`` INSIDE the function. If the
    package isn't installed OR the chromium binary isn't downloaded, log at
    debug and return ``None`` — never raise. So a server without playwright
    behaves exactly as today.
  * **Never raises.** Any exception (launch failure, navigation timeout,
    SSRF block) is swallowed → ``None``. The caller treats ``None`` /``""``
    identically to "primary fetch failed" and keeps its original result.
  * **Always cleans up.** Browser + context are closed in ``finally``.

DEPLOY NOTE (one-time, NOT done here): installing the Python package is not
enough — the chromium binary must also be downloaded once on the host:

    pip install playwright        # already in requirements.txt
    playwright install chromium   # one-time, downloads the browser binary

Until BOTH are done (and the ``browser_fetch_fallback_enabled`` flag is True)
this tier stays dormant and the pipeline behaves exactly as before.
"""
from __future__ import annotations

import logging
import re

from safe_url import is_safe_url
from text_utils import strip_html, fix_mojibake

log = logging.getLogger(__name__)

# Mirror _detail_fetch's pre-strip so browser-rendered HTML is cleaned the
# same way as the requests path (script/style/comment/doctype blocks out).
_BLOCK_STRIP_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_DOCTYPE_RE = re.compile(r"<!DOCTYPE[^>]*>", re.IGNORECASE)

# Realistic desktop-Chrome UA — matches _detail_fetch.DEFAULT_HEADERS so the
# browser presents the same identity as the primary fetch.
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _clean(raw: str, max_chars: int) -> str:
    """Strip HTML/scripts and fix mojibake — identical pipeline to _detail_fetch."""
    raw = _BLOCK_STRIP_RE.sub(" ", raw or "")
    raw = _COMMENT_RE.sub(" ", raw)
    raw = _DOCTYPE_RE.sub(" ", raw)
    text = fix_mojibake(strip_html(raw))
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


# Anti-bot challenge / denial pages that even a headless browser may receive
# (Akamai/Cloudflare serve a block or JS-challenge page in place of the real
# content — e.g. undp.org returns an edgesuite "Access Denied"). Treat these
# as a MISS so the junk text never becomes a job snippet; the caller then keeps
# its original listing-card snippet. Transport-quality guard (sibling of
# soft-404 detection), NOT a scoring rule.
_BLOCK_PAGE_MARKERS = (
    "you don't have permission to access",
    "you don’t have permission to access",
    "errors.edgesuite.net",
    "attention required! | cloudflare",
    "verify you are human",
    "checking your browser before accessing",
    "sorry, you have been blocked",
    "enable javascript and cookies to continue",
)


def _looks_like_block_page(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _BLOCK_PAGE_MARKERS)


def fetch_rendered(
    url: str,
    *,
    timeout_s: float = 30.0,
    max_chars: int = 4000,
) -> str | None:
    """Render ``url`` in a headless chromium and return cleaned body text.

    Returns ``None`` (never raises) when:
      * the URL fails the SSRF guard (browser is NOT launched),
      * playwright / the chromium binary is unavailable,
      * navigation or rendering fails for any reason.

    Returns cleaned plain-text body (HTML stripped, mojibake fixed, truncated
    to ``max_chars``) on success. An empty rendered page yields ``""`` — the
    caller distinguishes ``None``/``""`` from useful text and keeps its
    original result if nothing better came back.
    """
    if not url:
        return None

    # SSRF guard FIRST — must run before any browser launch. The headless
    # browser does not go through safe_request, so this is the sole guard on
    # this path.
    ok, reason = is_safe_url(url)
    if not ok:
        log.debug("browser_fetch: SSRF-blocked %s (%s); not launching", url, reason)
        return None

    # Lazy import — a host without playwright must degrade to today's behavior.
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # ImportError or anything else
        log.debug("browser_fetch: playwright unavailable (%s); skipping", e)
        return None

    timeout_ms = max(1, int(timeout_s * 1000))
    try:
        with sync_playwright() as p:
            browser = None
            try:
                # launch() raises if the chromium binary isn't installed —
                # caught below and turned into a graceful None.
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=_UA)
                page = context.new_page()
                # wait_until="load" mirrors the requests path's "page loaded";
                # the per-call timeout caps total navigation time.
                page.goto(url, timeout=timeout_ms, wait_until="load")
                html = page.content()
            finally:
                # Always tear down the browser, even on navigation failure.
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except Exception as e:
        log.debug("browser_fetch: render failed for %s (%s)", url, e)
        return None

    if not html:
        return ""
    text = _clean(html, max_chars)
    if _looks_like_block_page(text):
        log.debug("browser_fetch: %s rendered a block/challenge page — treating as miss", url)
        return None
    return text
