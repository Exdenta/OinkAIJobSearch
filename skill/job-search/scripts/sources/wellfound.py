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

Chrome-agent fallback (opt-in, default OFF)
-------------------------------------------

Option (a) above — drive a REAL browser that has already solved the
DataDome JS challenge — is now wired through the shared
``chrome_agent_fetch`` helper. When the operator enables
``chrome_agent_fallback_enabled`` in ``defaults.py`` the adapter asks the
operator's desktop Chrome (via ``claude -p --chrome`` + the
``claude-in-chrome`` MCP) to load ``/remote`` and extract the listing
cards. With the flag OFF (the DEFAULT) the helper returns ``[]`` WITHOUT
spawning anything, so this adapter's observable behavior is byte-for-byte
identical to the historic DataDome stub — it still probes, logs the block
reason, and returns ``[]``. Zero regression.

Module key: wellfound  (default OFF — do not enable in filters.yaml)

Apify fallback (opt-in, default OFF)
-------------------------------------
When the operator sets the `APIFY_TOKEN` env var, `fetch()` tries the
confirmed `nomad-agent/wellfound-scraper` Apify Actor first (Playwright +
residential proxy, already clears DataDome — see
https://apify.com/nomad-agent/wellfound-scraper). Its dataset items are
mapped straight to `Job`. With `APIFY_TOKEN` unset, this path is never
invoked and behavior is byte-for-byte the historic stub.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

from dedupe import Job

log = logging.getLogger(__name__)

_APIFY_ACTOR = "nomad-agent/wellfound-scraper"

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


def _coerce_str(value: Any) -> str:
    """Best-effort str coercion for an extracted listing field; never raises."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return str(value).strip()
    except Exception:  # noqa: BLE001
        return ""


def _try_apify(filters: dict, *, cap: int, token: str) -> list[Job]:
    """Best-effort recovery via the confirmed Apify Actor. Never raises:
    any transport or mapping problem degrades to `[]`, same contract as the
    Chrome fallback below.
    """
    from sources._apify import run_actor

    run_input = {
        "keyword": (filters.get("wellfound_search") or "").strip(),
        "maxItems": cap,
        "remoteOnly": bool(filters.get("wellfound_remote_only", False)),
        "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"]},
    }
    try:
        items = run_actor(_APIFY_ACTOR, run_input, token=token)
    except Exception as e:  # noqa: BLE001 - belt-and-suspenders, run_actor already no-raise
        log.debug("wellfound: apify actor raised (%s); returning []", e)
        return []

    jobs: list[Job] = []
    for item in items:
        try:
            job_url = _coerce_str(item.get("url"))
            jobs.append(Job(
                source="wellfound",
                external_id=_coerce_str(item.get("id")) or job_url,
                title=_coerce_str(item.get("title")),
                company=_coerce_str(item.get("company")),
                location=_coerce_str(item.get("location")),
                url=job_url,
                posted_at=_coerce_str(item.get("postedAt")),
                snippet=_coerce_str(item.get("snippet")),
                salary=_coerce_str(item.get("salary")),
            ))
        except Exception as e:  # noqa: BLE001
            log.debug("wellfound: skipped unmappable apify item (%s)", e)
            continue

    if jobs:
        log.info("wellfound: apify actor recovered %d listing(s)", len(jobs))
    return jobs


def _try_chrome_fallback(*, max_items: int) -> list[Job]:
    """Best-effort agentic-Chrome recovery of Wellfound's DataDome-walled
    ``/remote`` listing page.

    Returns a list of mapped :class:`Job` rows, or ``[]`` when the
    ``chrome_agent_fallback_enabled`` flag is OFF (the DEFAULT — the helper
    returns ``[]`` with no subprocess spawned), the browser is unavailable,
    the page is still blocked, or nothing parseable came back. NEVER raises:
    any import / mapping problem degrades to ``[]`` so the adapter's contract
    (return an empty list) is preserved exactly.
    """
    url = "https://wellfound.com/remote"
    try:
        from chrome_agent_fetch import fetch_listings_via_chrome
        rows = fetch_listings_via_chrome(
            url=url,
            instruction=(
                "remote tech and startup job listings: role title, company, "
                "location, posting URL, short description"
            ),
            max_items=max_items,
        )
    except Exception as e:  # noqa: BLE001
        log.debug("wellfound: chrome fallback raised (%s); returning []", e)
        return []

    if not rows:
        # Disabled / blocked / empty — identical to the historic stub.
        log.debug("wellfound: chrome fallback returned no listings")
        return []

    jobs: list[Job] = []
    for item in rows:
        try:
            job_url = _coerce_str(item.get("url")) or url
            jobs.append(
                Job(
                    source="wellfound",
                    external_id=job_url,
                    title=_coerce_str(item.get("title")),
                    company=_coerce_str(item.get("company")),
                    location=_coerce_str(item.get("location")),
                    url=job_url,
                    posted_at=_coerce_str(item.get("posted_at")),
                    snippet=_coerce_str(item.get("snippet")),
                )
            )
        except Exception as e:  # noqa: BLE001
            log.debug("wellfound: skipped unmappable listing (%s)", e)
            continue

    if jobs:
        log.info("wellfound: chrome fallback recovered %d listing(s)", len(jobs))
    return jobs


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point.

    Default behavior is an empty list: Wellfound is DataDome-walled, so the
    plain-``requests`` path cannot reach listings. When the operator has
    enabled ``chrome_agent_fallback_enabled`` the adapter first tries to
    recover listings through the operator's desktop Chrome; on success it
    returns the mapped :class:`Job` rows. On ANY failure — including the
    default-OFF flag, which makes the helper return ``[]`` with no
    subprocess — it falls through to the historic stub: one cheap probe so
    the forensic log records the day's block reason, then ``[]``.

    `filters` keys consulted:
      * max_per_source (int, default 12) — caps both the chrome extraction
        and (for API parity) the probe log.
    """
    cap = int(filters.get("max_per_source") or 12)

    # Opt-in Apify recovery. With APIFY_TOKEN unset (default) this branch
    # never runs, so behavior below is identical to the historic stub.
    apify_token = os.environ.get("APIFY_TOKEN")
    if apify_token:
        apify_jobs = _try_apify(filters, cap=cap, token=apify_token)
        if apify_jobs:
            return apify_jobs

    # Opt-in agentic-Chrome recovery. With the flag OFF (default) this
    # returns [] WITHOUT spawning anything, so the behavior below is
    # identical to the historic DataDome stub.
    jobs = _try_chrome_fallback(max_items=int(filters.get("max_per_source") or 36))
    if jobs:
        return jobs

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
