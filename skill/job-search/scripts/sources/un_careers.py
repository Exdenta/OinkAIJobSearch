"""UN Careers (United Nations Inspira) source adapter.

Slug: ``un_careers``. Public portal: https://careers.un.org/jobSearch?language=en

Design: direct HTTP-JSON first, Chrome-agent fallback
-----------------------------------------------------
UN Careers is a JS-only single-page app served by Oracle's Inspira HR system,
but the SPA is backed by a **public JSON API** that needs no cookies and no
auth — only a browser-like set of request headers. We talk to that API
directly instead of delegating to a WebFetch sub-agent:

  POST https://careers.un.org/api/public/opening/jo/list/filteredV2/en
    body  {"filterConfig":{}, "pagination":{"page":N, "itemPerPage":25,
           "sortBy":"startDate","sortDirection":-1}}
    -> {"status":1, "data":{"list":[ITEMS], "count":TOTAL}}

An empty ``filterConfig`` means "all current openings, newest first", which is
exactly what we want from this GLOBAL source — the per-user scorer filters
downstream. We paginate ``page=0,1,2,…`` until the returned list is empty or we
hit ``max_per_source``.

Header note (load-bearing): a generic User-Agent with no ``Origin``/``Referer``
gets a CloudFront 403; the desktop-Chrome UA plus ``Origin``/``Referer`` below
returns 200. We send the POST through ``safe_url.safe_request`` so the outbound
fetch goes through the project's SSRF guard like every other scraped-URL fetch.

Fallback: if the API path raises OR yields zero jobs, we ask the shared
``chrome_agent_fetch.fetch_listings_via_chrome`` helper to drive the operator's
real desktop Chrome past any anti-bot wall and extract the listing grid. That
helper is GATED behind ``DEFAULTS["chrome_agent_fallback_enabled"]`` (default
False), so with the flag OFF — the default — the fallback returns ``[]`` and
this adapter is effectively API-only, with behavior identical to a plain HTTP
adapter (zero new side effects).

Failure modes (all return ``[]`` and never raise out of ``fetch``):
  * API POST raised / SSRF-blocked / non-2xx  -> try chrome fallback, else []
  * API returned a non-JSON / unexpected body -> try chrome fallback, else []
  * API returned zero openings                -> try chrome fallback, else []
  * chrome fallback disabled (the DEFAULT)    -> []

``db`` (Tier 4): when supplied, the adapter records one ``search_fetches`` row
per fetch so the adaptive source-cooldown FSM
(``search_jobs.should_run_source``) gets a novelty signal for ``un_careers``.

DISABLED BY DEFAULT in filters.yaml. The wiring toggle is owned by
``defaults.py`` (don't edit here).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

import requests

import safe_url
from dedupe import Job
from text_utils import fix_mojibake, strip_html
from chrome_agent_fetch import fetch_listings_via_chrome

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# UN Careers public API constants. The header set is verified-working: a
# generic UA without Origin/Referer returns 403; with these it returns 200.
# These are transport invariants (User-Agent / required browser headers),
# not scoring heuristics — the allowed kind of hardcoding per CLAUDE.md.
# ---------------------------------------------------------------------------

API_URL = "https://careers.un.org/api/public/opening/jo/list/filteredV2/en"
SEARCH_URL = "https://careers.un.org/jobSearch?language=en"

_API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://careers.un.org",
    "Referer": "https://careers.un.org/jobSearch?language=en",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}

_ITEMS_PER_PAGE = 25
# Belt-and-suspenders page ceiling so a misbehaving API (e.g. one that keeps
# returning a non-empty list) can never spin us forever. cap/itemsPerPage + 2.
_MAX_PAGES = 8


# ---------------------------------------------------------------------------
# Optional integration: per-step JSONL forensic logs. Present in the broader
# project; absent in isolated test environments. Guarded so the adapter works
# in both worlds.
# ---------------------------------------------------------------------------

try:  # forensic.step / forensic.log_step
    import forensic as _forensic  # type: ignore
    _HAS_FORENSIC = True
except Exception:  # ImportError or transitive failure
    _forensic = None  # type: ignore[assignment]
    _HAS_FORENSIC = False


class _NoopStepCtx:
    """Mimics the slice of forensic._StepCtx the adapter uses."""

    __slots__ = ()

    def set_output(self, output: Any) -> None:  # noqa: D401 — trivial
        return None

    def set_intermediate(self, intermediate: Any) -> None:
        return None


@contextmanager
def _step(op: str, *, input: Any | None = None) -> Iterator[Any]:
    """Forward to ``forensic.step`` when available, no-op otherwise."""
    if _HAS_FORENSIC:
        with _forensic.step(op, input=input) as ctx:  # type: ignore[union-attr]
            yield ctx
    else:
        yield _NoopStepCtx()


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _detail_url(job_id: Any) -> str:
    """Build the absolute job-detail URL from an item's ``jobId``."""
    return f"https://careers.un.org/jobSearchDescription/{job_id}?language=en"


def _post_page(page: int, *, timeout_s: float) -> list[dict]:
    """POST one page of openings; return the raw ``data.list`` items.

    Sends the body through ``safe_url.safe_request`` so the outbound fetch is
    SSRF-guarded like every other scraped-URL fetch. Raises on transport
    failure / non-2xx / unexpected body — the caller turns any exception into
    the chrome fallback.
    """
    body = {
        "filterConfig": {},
        "pagination": {
            "page": page,
            "itemPerPage": _ITEMS_PER_PAGE,
            "sortBy": "startDate",
            "sortDirection": -1,
        },
    }
    try:
        resp = safe_url.safe_request(
            "POST",
            API_URL,
            timeout=timeout_s,
            headers=_API_HEADERS,
            json=body,
        )
    except TypeError:
        # The deployed ``safe_request`` predates JSON-body passthrough (its
        # signature has no ``json``/``data`` kwarg). The UN API needs the body,
        # so re-create the same SSRF guarantee inline: validate the URL via the
        # module's own ``is_safe_url`` BEFORE connecting, then POST directly.
        # API_URL is a fixed https constant, never attacker-controlled, so the
        # per-hop redirect revalidation that ``safe_request`` adds is not needed
        # here. When Foundation extends ``safe_request`` with a body, the call
        # above just works and this branch goes cold.
        ok, reason = safe_url.is_safe_url(API_URL)
        if not ok:
            raise RuntimeError(f"un_careers API SSRF-blocked ({reason})")
        resp = requests.post(
            API_URL, timeout=timeout_s, headers=_API_HEADERS, json=body,
        )
    if getattr(resp, "status_code", 0) >= 400:
        raise RuntimeError(f"un_careers API status={resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("un_careers API returned non-object body")
    payload = data.get("data") or {}
    items = payload.get("list") if isinstance(payload, dict) else None
    return items if isinstance(items, list) else []


def _map_item(item: dict) -> Job | None:
    """Map one API item to a ``Job`` (or None when it lacks a usable id)."""
    if not isinstance(item, dict):
        return None
    job_id = item.get("jobId")
    if job_id in (None, ""):
        return None
    url = _detail_url(job_id)

    title = item.get("postingTitle") or item.get("jobTitle") or ""

    company = "United Nations"
    dept = item.get("dept")
    if isinstance(dept, str) and dept.strip():
        company = f"United Nations — {dept.strip()}"

    location = ""
    duty = item.get("dutyStation")
    if isinstance(duty, list) and duty:
        first = duty[0]
        if isinstance(first, dict):
            location = first.get("description") or ""

    return Job(
        source="un_careers",
        external_id=url,
        title=fix_mojibake(str(title))[:140],
        company=fix_mojibake(str(company))[:80],
        location=fix_mojibake(str(location))[:80],
        url=url,
        posted_at=str(item.get("startDate") or ""),
        # jobDescription comes back as raw HTML — strip tags so the scorer
        # sees readable prose, not ``<div class='jobPostingDetail'>`` noise.
        snippet=fix_mojibake(strip_html(str(item.get("jobDescription") or "")))[:400],
    )


def _fetch_via_api(cap: int, *, timeout_s: float) -> list[Job]:
    """Paginate the public API into a capped list of ``Job``.

    Raises on the first transport / parse failure so the caller can fall back
    to the chrome tier. Stops at the first empty page or when ``cap`` is hit.
    """
    out: list[Job] = []
    for page in range(_MAX_PAGES):
        items = _post_page(page, timeout_s=timeout_s)
        if not items:
            break
        for item in items:
            job = _map_item(item)
            if job is not None:
                out.append(job)
                if len(out) >= cap:
                    return out
    return out


def _fetch_via_chrome(cap: int) -> list[Job]:
    """Last-resort fallback: drive the operator's Chrome to read the grid.

    Returns ``[]`` when the chrome tier is disabled (the default) — so this
    path is a no-op unless an operator opts in. Never raises.
    """
    dicts = fetch_listings_via_chrome(
        url=SEARCH_URL,
        instruction=(
            "every current UN job opening, newest first: title, department, "
            "duty station, posting URL"
        ),
        max_items=cap,
    )
    out: list[Job] = []
    for r in dicts:
        url = (r.get("url") or "").strip()
        if not url:
            continue
        out.append(Job(
            source="un_careers",
            external_id=url,
            title=fix_mojibake(str(r.get("title") or ""))[:140],
            company=fix_mojibake(str(r.get("company") or "United Nations"))[:80],
            location=fix_mojibake(str(r.get("location") or ""))[:80],
            url=url,
            posted_at=str(r.get("posted_at") or ""),
            snippet=fix_mojibake(str(r.get("snippet") or ""))[:400],
        ))
        if len(out) >= cap:
            break
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch(filters: dict, *, db=None) -> list[Job]:
    """Aggregate UN Careers postings via the public JSON API.

    Parameters mirrored from filters.yaml:
      * ``max_per_source`` — hard cap on returned jobs (default 36).
      * ``ai_scrape_timeout_s`` — per-request HTTP timeout (default 30s).

    Primary path: paginate the public ``filteredV2`` API (newest first).
    Fallback: if the API raises OR yields zero jobs, ask the shared
    chrome-agent helper to drive the operator's desktop Chrome. With the
    ``chrome_agent_fallback_enabled`` flag OFF (the default) that helper
    returns ``[]``, so this adapter is effectively API-only.

    ``db`` (Tier 4): when supplied, records one ``search_fetches`` row per
    fetch (single fixed cell query="un_careers", page=1, location="") so the
    adaptive source-cooldown FSM gets a novelty signal. Best-effort: a DB
    hiccup never breaks the fetch. When ``db`` is None we skip recording.

    Returns a (possibly empty) list of ``Job``. Never raises: every failure
    path logs and returns ``[]`` so the orchestrator moves on.
    """
    timeout_s = float(filters.get("ai_scrape_timeout_s") or 30)
    cap = int(filters.get("max_per_source") or 36)

    with _step(
        "un_careers.fetch",
        input={"cap": cap, "timeout_s": timeout_s, "url": API_URL},
    ) as fctx:
        out: list[Job] = []
        path = "api"
        try:
            out = _fetch_via_api(cap, timeout_s=timeout_s)
        except Exception as e:
            log.warning("un_careers: API path failed (%s); trying chrome fallback", e)
            out = []

        if not out:
            # API raised or returned zero — try the operator-Chrome tier. This
            # is a no-op ([]) unless chrome_agent_fallback_enabled is True.
            path = "chrome"
            try:
                out = _fetch_via_chrome(cap)
            except Exception:
                log.debug("un_careers: chrome fallback raised; continuing", exc_info=True)
                out = []

        log.info("un_careers (%s): %d postings", path, len(out))

        # Tier 4: feed the adaptive source-cooldown FSM. jobs_new counts how
        # many of THIS fetch's postings are not yet in the `jobs` table, keyed
        # on the real Job.job_id (sha1 of source+external_id). Recorded before
        # the downstream upsert. Best-effort: a DB error never breaks the fetch.
        if db is not None:
            try:
                seen_ids = [j.job_id for j in out]
                existing = db.count_existing_jobs(seen_ids) if seen_ids else 0
                db.record_fetch(
                    "un_careers", "un_careers", 1, "",
                    jobs_seen=len(seen_ids),
                    jobs_new=max(0, len(seen_ids) - existing),
                )
            except Exception:
                log.debug("un_careers: db.record_fetch raised; continuing",
                          exc_info=True)

        fctx.set_output({
            "path": path,
            "kept": len(out),
            "sample_titles": [j.title[:80] for j in out[:5]],
        })
        return out
