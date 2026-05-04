"""UN Careers (United Nations Inspira) source adapter.

Slug: ``un_careers``. Landing page: https://careers.un.org/home?language=en

Why we delegate to the Claude CLI
---------------------------------
UN Careers is a JS-only single-page app served by Oracle's Inspira HR system.
The public landing page is a 50-line HTML shell that loads its own bundle and
populates listings client-side from XHR endpoints that are not announced
publicly:

  * Direct probes of plausible REST paths (``/api/public/jobsearch``,
    ``/api/public/v2/jobs``, ``/api/jobs/search``, ``/RestService/...``,
    ``/jobs.rss``, ``/sitemap.xml``) all return either the SPA shell catch-all
    (HTTP 200, identical 116 KB body for every path) or a CloudFront 403 when
    the User-Agent is recognized as a bot.
  * The HTML carries no inline ``__INITIAL_STATE__`` / ``window.__APP_STATE__``
    blob — listings are populated only after the JS bundle runs.
  * No public RSS / sitemap is exposed.

Maintaining a custom DOM scraper for a target like this is not worth it: the
underlying Inspira UI changes shape with every quarterly release, and the
front-end is built specifically to make scraping painful. Instead we follow
the same Claude-CLI delegation pattern as ``curated_boards.py``: shell out to
the ``claude`` CLI with a strict JSON-only prompt, let it use its own
``WebFetch`` tool to render the page through a real browser-like fetcher, and
parse whatever JSON comes back into ``Job`` objects.

This keeps the adapter robust to layout drift (the prompt describes the
*shape* of what we want, not which CSS selector to read) and isolates the
maintenance burden to a single prompt rewrite if the UN ever rebuilds the
portal entirely.

Failure modes (all return ``[]`` and never raise out of ``fetch``):
  * ``claude`` CLI missing or login expired         -> warning, ``[]``
  * Network failure / WebFetch blocked              -> warning, ``[]``
  * CLI returned non-JSON / unparseable response    -> warning, ``[]``
  * CLI returned ``{"jobs": []}`` (legitimate zero) -> info,    ``[]``

DISABLED BY DEFAULT in filters.yaml. Enable after a ``--dry-run`` sanity check;
the wiring toggle is owned by ``defaults.py`` (don't edit here).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from claude_cli import extract_assistant_text, parse_json_block, run_p
from dedupe import Job
from text_utils import fix_mojibake

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Optional integrations: forensic + instrumented Claude CLI wrapper.
#
# These live in the broader project (per-step JSONL forensic logs and a
# Claude-cost-tracking wrapper). When they're available we use them; when they
# aren't (older checkouts, isolated test environments) we degrade silently to
# plain ``run_p`` + a no-op forensic context. The adapter must keep working in
# both worlds — that's why every reference is guarded.
# ---------------------------------------------------------------------------

try:  # forensic.step / forensic.log_step
    import forensic as _forensic  # type: ignore
    _HAS_FORENSIC = True
except Exception:  # ImportError or transitive failure
    _forensic = None  # type: ignore[assignment]
    _HAS_FORENSIC = False

try:  # wrapped_run_p (claude_calls + forensic prompt-head capture)
    from instrumentation.wrappers import wrapped_run_p as _wrapped_run_p  # type: ignore
    _HAS_WRAPPED = True
except Exception:
    _wrapped_run_p = None  # type: ignore[assignment]
    _HAS_WRAPPED = False


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


def _run_claude(prompt: str, *, timeout_s: int) -> str | None:
    """Use the instrumented wrapper when available, else plain run_p."""
    if _HAS_WRAPPED:
        try:
            return _wrapped_run_p(None, "un_careers", prompt, timeout_s=timeout_s)  # type: ignore[misc]
        except Exception:
            log.exception("un_careers: wrapped_run_p failed; falling back to run_p")
    return run_p(prompt, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# Prompt
#
# Strict JSON only, no commentary, no markdown fences. We deliberately give
# Claude permission to follow up to one "next page" / "see all jobs" link
# because the landing page is a marketing splash on most days; the actual
# listing grid lives one click in. The dotted "absolute https://..." note is
# a hard requirement — the Job dedupe key is built from `external_id`/`url`,
# and a relative link would silently collide across postings.
# ---------------------------------------------------------------------------

LANDING = "https://careers.un.org/home?language=en"

_PROMPT = """You are a job-scraping assistant. Use the WebFetch tool to load:
{url}

The page is the United Nations careers portal (Inspira). It is a JavaScript
single-page app and the landing surface may be a marketing splash; if it does
not surface job listings directly, follow up to ONE link that looks like
"jobs", "vacancies", "search jobs", "all jobs", or a "next page"/pagination
link to reach the listings grid.

Return STRICT JSON (no commentary, no markdown fences, no prose before or
after) with this shape:

{{"jobs": [
  {{"title": "...",
    "company": "...",
    "location": "...",
    "url": "absolute https://... direct job-detail URL",
    "posted_at": "",
    "snippet": ""}}
]}}

Rules:
- Cap at 15 results. Prefer the freshest postings (most recently posted).
- ``url`` MUST be an absolute https URL pointing to the individual job
  detail page (not the search results page). Skip postings whose detail URL
  you can't recover.
- ``company`` defaults to "United Nations" when the posting itself doesn't
  name a specific UN agency / department; otherwise use the agency name.
- Trim ``snippet`` to a one or two sentence summary of the role.
- ``posted_at`` is best-effort — leave empty if the page doesn't surface a
  visible date.
- If the page fails to load, blocks WebFetch, or genuinely has no postings,
  return {{"jobs": []}}.

Output MUST be parseable by json.loads(). Do not include any text before or
after the JSON object.
""".strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch(filters: dict) -> list[Job]:
    """Aggregate UN Careers postings via the Claude CLI delegation pattern.

    Parameters mirrored from filters.yaml:
      * ``max_per_source`` — hard cap on returned jobs (default 10).
      * ``ai_scrape_timeout_s`` — subprocess timeout for the CLI invocation
        (default 180s; UN portals can be slow under load).

    Returns a (possibly empty) list of ``Job`` instances. Never raises:
    every failure path logs and returns ``[]`` so the orchestrator can move
    on to the next source.
    """
    timeout_s = int(filters.get("ai_scrape_timeout_s") or 180)
    cap = int(filters.get("max_per_source") or 10)

    with _step(
        "un_careers.fetch",
        input={"cap": cap, "timeout_s": timeout_s, "url": LANDING},
    ) as fctx:
        prompt = _PROMPT.format(url=LANDING)
        try:
            stdout = _run_claude(prompt, timeout_s=timeout_s)
        except Exception as e:
            log.exception("un_careers: CLI invocation failed: %s", e)
            fctx.set_output({"raw_count": 0, "kept": 0, "reason": "cli_exception"})
            return []

        if stdout is None:
            log.warning("un_careers: claude CLI missing or returned None")
            fctx.set_output({"raw_count": 0, "kept": 0, "reason": "cli_missing_or_none"})
            return []
        if not stdout.strip():
            log.warning("un_careers: claude CLI returned empty stdout")
            fctx.set_output({"raw_count": 0, "kept": 0, "reason": "cli_empty"})
            return []

        body = extract_assistant_text(stdout)
        data = parse_json_block(body)

        if not isinstance(data, dict):
            log.warning("un_careers: response was not a JSON object; head=%r", (body or "")[:300])
            fctx.set_output({
                "raw_count": 0,
                "kept": 0,
                "reason": "non_object_response",
                "body_head": (body or "")[:300],
            })
            return []

        raw = data.get("jobs") or []
        if not isinstance(raw, list):
            log.warning("un_careers: 'jobs' field was not a list (%s)", type(raw).__name__)
            fctx.set_output({
                "raw_count": 0,
                "kept": 0,
                "reason": "jobs_not_list",
                "body_head": (body or "")[:300],
            })
            return []

        log.info("un_careers (AI): %d raw postings", len(raw))

        out: list[Job] = []
        for r in raw[:cap]:
            if not isinstance(r, dict):
                continue
            url = (r.get("url") or "").strip()
            if not url:
                # No URL means we can't dedupe / link the user anywhere.
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

        fctx.set_output({
            "raw_count": len(raw),
            "kept": len(out),
            "sample_titles": [j.title[:80] for j in out[:5]],
            # Only keep the body head when we got nothing — for diagnostics.
            "body_head": (body or "")[:300] if not out else None,
        })
        return out
