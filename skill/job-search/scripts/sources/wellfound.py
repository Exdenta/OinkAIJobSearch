"""Wellfound (https://wellfound.com, formerly AngelList Talent) job source.

Status: STUB — default OFF. This adapter is intentionally non-functional and
returns an empty list. Reasoning is documented below so future operators don't
waste time re-investigating.

Why we ship a stub instead of a working scraper
-----------------------------------------------

Wellfound sits behind DataDome (a CAPTCHA / anti-bot vendor that fronts the
Cloudflare edge). Every listing-bearing endpoint we tried answers HTTP 403
to unauthenticated requests, regardless of User-Agent:

    GET /                     -> 200   (marketing page only, no listings)
    GET /robots.txt           -> 200   (no Sitemap: directive)
    GET /jobs                 -> 403   (DataDome challenge)
    GET /role/r/<role>        -> 403   (DataDome challenge)
    GET /role/l/<role>/<loc>  -> 403   (DataDome challenge)
    GET /sitemap.xml          -> 403   (DataDome challenge)
    GET /jobs.rss             -> 403   (DataDome challenge)
    GET /feed                 -> 403   (DataDome challenge)
    GET /feed.rss             -> 403   (DataDome challenge)

Response headers consistently include `x-datadome: protected` and a
`set-cookie: datadome=...` challenge token. There is no documented public
RSS, no public sitemap, and no documented public REST/GraphQL endpoint;
the GraphQL gateway under `/graphql` requires authenticated session
cookies obtained via the login flow.

Bypassing DataDome requires either (a) a real browser with a solved JS
challenge (Playwright + stealth, or a residential-proxy + browser farm),
or (b) carrying a logged-in session cookie minted from a real user
account. Both options are out of scope for a personal job-alert bot.

What an operator could try if they really want this source
----------------------------------------------------------

1. Sign in to Wellfound in a regular browser, copy the `_b3_session`
   cookie + the solved `datadome` cookie, and persist them (env var or
   secrets file). Re-issue every few weeks. Fragile and TOS-grey.
2. Use the indirect path: Google site-search `site:wellfound.com/jobs`
   via the existing `web_search` adapter — Wellfound *does* allow
   Googlebot, so individual job pages may already surface there. No
   change needed in this file; just enable a `web_search` query.
3. Subscribe to Wellfound's email job alerts and ingest via the IMAP /
   Gmail path used elsewhere in this skill.

Until one of those is wired up, this adapter is a polite no-op so that
`sources/__init__.py` can register `wellfound` as a known module key
without breaking the orchestrator.

Module key: wellfound  (default OFF — do not enable in filters.yaml)
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from dedupe import Job

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

# Probe endpoints (in order) so the forensic log records *which* gate fired.
# We intentionally hit only the listing endpoints — never the homepage —
# so the log truthfully reflects the blocked-listing situation.
_PROBE_URLS: tuple[str, ...] = (
    "https://wellfound.com/jobs.rss",
    "https://wellfound.com/jobs",
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
        forensic.log_step(
            "wellfound.fetch",
            input=payload.get("input", {}),
            output=payload.get("output", {}),
        )
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for wellfound.fetch", exc_info=True)


def _probe_block_reason() -> tuple[str, dict[str, Any]]:
    """Issue a single lightweight probe so the forensic log records the
    actual gate that fired today. Returns (reason, debug_dict).

    Never raises; returns ('probe_error', {...}) on any exception.
    """
    debug: dict[str, Any] = {}
    for url in _PROBE_URLS:
        try:
            r = requests.get(url, headers=UA, timeout=10, allow_redirects=False)
            debug[url] = {
                "status": r.status_code,
                "x_datadome": r.headers.get("x-datadome", ""),
                "server": r.headers.get("server", ""),
                "cf_ray": r.headers.get("cf-ray", ""),
            }
            if r.status_code == 200:
                # Unexpected — Wellfound let us through. Return a marker
                # the operator can grep for; we still don't parse, since
                # the HTML structure isn't validated against a real spec
                # and would silently rot. Treat it as a signal to revisit.
                return "unexpected_200_revisit_adapter", debug
            if r.headers.get("x-datadome", "").lower() == "protected":
                return "datadome_403", debug
            if r.status_code == 403:
                return "http_403", debug
        except requests.RequestException as e:
            debug[url] = {"error": repr(e)[:200]}
    return "all_probes_failed", debug


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point.

    Always returns an empty list. Performs one cheap probe so the forensic
    log records the block reason for the day's run; this is the *only*
    side effect.

    `filters` keys consulted:
      * max_per_source (int, default 12) — accepted for API parity, ignored
        because this adapter never produces jobs.
    """
    cap = int(filters.get("max_per_source") or 12)
    reason, debug = _probe_block_reason()

    _log_forensic({
        "input": {
            "endpoints_tried": list(_PROBE_URLS),
            "max_per_source": cap,
        },
        "output": {
            "status": "blocked",
            "reason": reason,
            "count": 0,
            "probe_debug": debug,
            "note": (
                "Wellfound is gated by DataDome; no public RSS / sitemap / "
                "GraphQL is reachable without a logged-in browser session. "
                "See module docstring for operator workarounds."
            ),
        },
    })

    log.info("wellfound: skipped (reason=%s)", reason)
    return []
