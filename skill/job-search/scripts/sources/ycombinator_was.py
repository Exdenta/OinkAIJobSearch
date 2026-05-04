"""Y Combinator Work at a Startup (https://www.workatastartup.com).

YC's portfolio job board — surfaces engineering / ML / data / frontend roles
across the YC company graph (Stripe, Brex, Retool, Anthropic-era startups,
plus current-batch teams). Strong signal for early-stage tech roles.

Integration choice: public Inertia search endpoint at `/jobs/search?q=...`.

  Why not Algolia?
    The HTML page exposes an Algolia app id + public search key in
    `window.AlgoliaOpts` (app=`45BWZJ1SGC`), but the production index name
    is not stable in the bundled JS, and the public search-key restricts
    indices to a tag-filtered subset. Probing `JobPosting_production` and
    similar names returns 404. The JSON endpoint at
    `https://www.workatastartup.com/jobs/search?q=<query>` is the same
    backend the JobsPage uses for in-page search (see
    `JobsPage-CLd82dvV.js` -> `fetch('/jobs/search?q=...')`); it is
    anonymous-readable, returns clean JSON, and gives us all the fields we
    need (id, title, location, roleType, salary, companyName,
    companySlug, companyBatch, companyOneLiner, applyUrl).

  Why not RSS / sitemap?
    `/sitemap.xml` 404s. There is no public RSS feed.

The endpoint takes a free-text `q` and returns up to ~16 jobs per query.
We fan out across a small, role-tuned query list (covers MLOps + frontend
target users) and dedupe by `id`. Downstream AI scoring decides relevance
per profile; we don't pre-filter.

Default: OFF. Enable by adding `ycombinator_was` to a profile's
`enabled_sources` list. The module key is `ycombinator_was` (matches
filename, registered downstream by `sources/__init__.py`).
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

SEARCH_URL = "https://www.workatastartup.com/jobs/search"

# Default fan-out queries. Cover both target users:
#   * MLOps / ML eng / data eng     (user 169016071)
#   * Frontend / web / React        (user 433775883)
# Override via `filters["ycombinator_was_queries"]` if a profile wants to
# narrow / broaden the fan-out without code changes.
DEFAULT_QUERIES: tuple[str, ...] = (
    "machine learning",
    "mlops",
    "data engineer",
    "ai engineer",
    "frontend",
    "react",
    "fullstack",
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
            "ycombinator_was.fetch",
            input=payload.get("input", {}),
            output=payload.get("output", {}),
        )
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for ycombinator_was.fetch", exc_info=True)


def _search_one(session: requests.Session, query: str) -> tuple[int | None, list[dict]]:
    """Hit `/jobs/search?q=<query>` once. Returns (status_code, jobs_list)."""
    try:
        r = session.get(
            SEARCH_URL,
            params={"q": query},
            headers={**UA, "Accept": "application/json"},
            timeout=20,
        )
        status = r.status_code
        if status != 200:
            log.warning(
                "ycombinator_was non-200 query=%r status=%s body=%r",
                query, status, (r.text or "")[:300],
            )
            return status, []
        try:
            payload = r.json()
        except ValueError:
            log.warning("ycombinator_was non-JSON response for query=%r", query)
            return status, []
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return status, []
        return status, jobs
    except requests.RequestException as e:
        log.error("ycombinator_was query=%r failed: %s", query, e)
        return None, []


def _build_snippet(raw: dict) -> str:
    """Compose a snippet from the fields the search endpoint exposes.

    The search response does NOT include the job description body; the only
    descriptive text is `companyOneLiner` plus structured metadata. We
    assemble these into a short paragraph so downstream AI scoring has
    enough context to triage.
    """
    parts: list[str] = []
    one_liner = fix_mojibake(raw.get("companyOneLiner") or "").strip()
    role_type = (raw.get("roleType") or "").strip()
    job_type = (raw.get("jobType") or "").strip()
    batch = (raw.get("companyBatch") or "").strip()
    if one_liner:
        parts.append(one_liner)
    meta_bits = [b for b in (role_type, job_type, f"YC {batch}" if batch else "") if b]
    if meta_bits:
        parts.append(" | ".join(meta_bits))
    return clean_snippet(". ".join(parts), max_chars=400)


def _job_url(raw: dict) -> str:
    """Canonical public URL for a posting. `/jobs/<id>` is anonymous-readable.

    The `applyUrl` returned by the API is a YC SSO redirect that requires
    login, so it's unsuitable as the user-facing link. The bare `/jobs/<id>`
    path renders the public job detail page.
    """
    job_id = raw.get("id")
    if job_id is None:
        return ""
    return f"https://www.workatastartup.com/jobs/{job_id}"


def fetch(filters: dict) -> list[Job]:
    """Top-level adapter entry point. Returns up to `max_per_source` Jobs.

    `filters` keys consulted:
      * max_per_source (int, default 12) — cap on returned jobs
      * ycombinator_was_queries (list[str], optional) — override the
        default fan-out query list. Useful per-profile to narrow toward a
        single specialty.
    """
    cap = int(filters.get("max_per_source") or 12)
    queries_override = filters.get("ycombinator_was_queries")
    if isinstance(queries_override, (list, tuple)) and queries_override:
        queries = tuple(str(q).strip() for q in queries_override if str(q).strip())
    else:
        queries = DEFAULT_QUERIES

    jobs: list[Job] = []
    seen_ids: set[str] = set()
    last_status: int | None = None
    queries_run: list[str] = []
    sample_titles: list[str] = []
    body_head = ""

    try:
        with requests.Session() as session:
            for query in queries:
                if len(jobs) >= cap:
                    break
                queries_run.append(query)
                status, raw_jobs = _search_one(session, query)
                if status is not None:
                    last_status = status
                for raw in raw_jobs:
                    if len(jobs) >= cap:
                        break
                    jid = raw.get("id")
                    if jid is None:
                        continue
                    external_id = str(jid)
                    if external_id in seen_ids:
                        continue
                    title = fix_mojibake(raw.get("title") or "").strip()
                    if not title:
                        continue
                    url = _job_url(raw)
                    if not url:
                        continue
                    company = fix_mojibake(raw.get("companyName") or "").strip()
                    location = fix_mojibake(raw.get("location") or "").strip() or "Unknown"
                    salary = (raw.get("salary") or "").strip()
                    snippet = _build_snippet(raw)

                    seen_ids.add(external_id)
                    jobs.append(Job(
                        "ycombinator_was",
                        external_id,
                        title[:140],
                        company[:120],
                        location[:120],
                        url,
                        "",  # search endpoint does not expose posted_at
                        snippet,
                        salary[:60],
                    ))

        sample_titles = [j.title for j in jobs[:5]]

    except Exception as e:  # noqa: BLE001
        log.exception("ycombinator_was fetch failed: %s", e)
        body_head = repr(e)[:500]

    _log_forensic({
        "input": {
            "endpoint": SEARCH_URL,
            "max_per_source": cap,
            "queries": list(queries),
            "queries_run": queries_run,
        },
        "output": {
            "status_code": last_status,
            "count": len(jobs),
            "sample_titles": sample_titles,
            "body_head": body_head,
        },
    })

    return jobs
