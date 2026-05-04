"""DevEx (https://www.devex.com) source adapter — international development jobs.

Slug: ``devex``. Landing page: https://www.devex.com/jobs/search

Why we delegate to the Claude CLI
---------------------------------
DevEx serves every public surface (homepage, ``/jobs``, ``/jobs/search``,
individual ``/jobs/<slug>-<id>`` detail pages, ``/sitemap.xml``,
``/robots.txt``) behind DataDome bot protection. Probed on 2026-05-01:

  * Plain ``requests.get`` with a generic UA  -> HTTP 403 + DataDome
    JS challenge body (``X-DataDome: protected``, redirect to
    ``geo.captcha-delivery.com``).
  * Browser-class UA (Chrome/121 on macOS)    -> HTTP 302 -> /jobs/search,
    then 403 with the same DataDome interstitial.
  * Googlebot UA                               -> HTTP 403, same wall.
  * Candidate feed paths (``/jobs.rss``, ``/jobs.xml``, ``/jobs/feed``,
    ``/jobs/feed.xml``, ``/api/v1/jobs``, ``/api/jobs``,
    ``/career-center/feed``)                  -> HTTP 403 (DataDome) on every
    one.
  * The documented public API at
    ``/api/public_secure/job_uploads/job_upload.xml`` is upload-only
    (HTTP 405 on GET; intended for partner employers POSTing jobs INTO
    DevEx, not consumers fetching them out).
  * No cookieless RSS/Atom feed advertised in any HTML ``<link rel>`` we
    were able to retrieve (the HTML is JS-blocked too).

Paywall scope (separate from the bot wall):
  * Job titles, employer names, locations, and detail URLs ARE publicly
    indexed by Google (verified via ``site:devex.com/jobs <query>``).
  * Full job descriptions and "apply" buttons typically gate behind a Pro
    subscription on the DevEx site itself, but title + employer + URL is
    enough to surface a posting and let the user click through to evaluate.

Maintaining a custom DataDome bypass is not viable (that's the entire point
of DataDome), so we follow the same delegation pattern as ``un_careers.py``
and ``curated_boards.py``: shell out to the ``claude`` CLI with WebSearch +
WebFetch granted, and let it use Google site-search to discover the latest
DevEx job URLs. WebFetch on individual ``devex.com/jobs/...`` pages is
ALSO DataDome-blocked, so the prompt tells Claude to extract whatever is
visible in the Google snippet (title, employer, location) and skip
postings whose detail URL it can't recover.

This means the adapter ships **deliberately partial**: we get titles +
employer + URL, and (best-effort) location and snippet. The downstream fit
analyzer + the user clicking through fills in the description.

Failure modes (all return ``[]`` and never raise out of ``fetch``):
  * ``claude`` CLI missing or login expired           -> warning, ``[]``
  * Network failure / WebSearch + WebFetch blocked    -> warning, ``[]``
  * CLI returned non-JSON / unparseable response      -> warning, ``[]``
  * CLI returned ``{"jobs": []}`` (legitimate zero)   -> info,    ``[]``

DISABLED BY DEFAULT — wiring toggle lives in ``defaults.py``; enable per-user
in their profile after a ``--dry-run`` sanity check. Module key: ``devex``.

Target audience: qualitative researchers in peace / migration / policy /
international development (the bot's primary qualitative-research user
profile). The discovery prompt biases toward those domains but the actual
filtering happens downstream in the per-user fit analyzer.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

from claude_cli import extract_assistant_text, parse_json_block, run_p, run_p_with_tools
from dedupe import Job
from text_utils import fix_mojibake

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}


# ---------------------------------------------------------------------------
# Optional integrations: forensic + instrumented Claude CLI wrapper.
#
# Mirrors the un_careers.py guarding so this module also works in lean
# checkouts (older clones, isolated test envs) without those modules.
# ---------------------------------------------------------------------------

try:  # forensic.step / forensic.log_step
    import forensic as _forensic  # type: ignore
    _HAS_FORENSIC = True
except Exception:  # ImportError or transitive failure
    _forensic = None  # type: ignore[assignment]
    _HAS_FORENSIC = False

try:  # wrapped_run_p_with_tools (claude_calls + forensic prompt-head capture)
    from instrumentation.wrappers import wrapped_run_p_with_tools as _wrapped_run_p_with_tools  # type: ignore
    _HAS_WRAPPED = True
except Exception:
    _wrapped_run_p_with_tools = None  # type: ignore[assignment]
    _HAS_WRAPPED = False


# Tool grants for the DevEx discovery sub-agent. WebSearch is REQUIRED (the
# whole point — DataDome blocks direct page loads, so we discover URLs via
# Google ``site:`` queries). WebFetch is granted as a best-effort fallback in
# case Claude's fetch path bypasses DataDome on a given run; the prompt tells
# the agent to back off after one WebFetch failure.
_ALLOWED_TOOLS = "WebSearch,WebFetch"
# Belt-and-suspenders: forbid filesystem/shell so a successful prompt-injection
# in a future search-result snippet can't escalate.
_DISALLOWED_TOOLS = "Bash,Edit,Write,Read"


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


def _log_forensic(payload: dict[str, Any]) -> None:
    """Best-effort one-shot log_step for parity with reliefweb-style adapters.

    Used in addition to ``_step`` so the adapter still emits a
    ``devex.fetch`` event even on early-return paths where the context-manager
    wasn't entered.
    """
    if not _HAS_FORENSIC or _forensic is None:
        return
    try:
        _forensic.log_step(  # type: ignore[union-attr]
            "devex.fetch",
            input=payload.get("input", {}),
            output=payload.get("output", {}),
        )
    except Exception:  # noqa: BLE001
        log.debug("forensic.log_step failed for devex.fetch", exc_info=True)


def _run_claude(prompt: str, *, timeout_s: int) -> str | None:
    """Use the instrumented wrapper when available, else plain run_p_with_tools.

    DevEx requires explicit ``WebSearch,WebFetch`` tool grants — without them
    the sub-agent has no way to discover postings (the DataDome wall blocks
    every direct request) and just returns ``{"jobs": []}`` after burning
    context. See ``sources/web_search.py`` and the 2026-04 zero-runs incident
    on chat 169016071 for the same class of bug.
    """
    if _HAS_WRAPPED and _wrapped_run_p_with_tools is not None:
        try:
            return _wrapped_run_p_with_tools(  # type: ignore[misc]
                None, "devex", prompt,
                allowed_tools=_ALLOWED_TOOLS,
                disallowed_tools=_DISALLOWED_TOOLS,
                timeout_s=timeout_s,
            )
        except Exception:
            log.exception("devex: wrapped_run_p_with_tools failed; falling back to run_p_with_tools")
    return run_p_with_tools(
        prompt,
        allowed_tools=_ALLOWED_TOOLS,
        disallowed_tools=_DISALLOWED_TOOLS,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# Prompt
#
# DevEx detail pages and the search page are DataDome-walled. We instruct the
# sub-agent to discover postings via Google site-search and to extract whatever
# is visible in the search-result title/snippet itself, since opening the
# detail page will (most likely) fail. The site-search query is biased toward
# qualitative-research / peace / migration / policy roles to match the
# primary user profile, but downstream fit scoring is the actual filter.
#
# Cap is 12 (passed in from filters.max_per_source).
# ---------------------------------------------------------------------------

LANDING = "https://www.devex.com/jobs/search"

_PROMPT = """You are a job-discovery assistant.

Goal: surface the latest job postings on DevEx (https://www.devex.com/jobs)
with a focus on roles relevant to a QUALITATIVE RESEARCHER working in
peace, migration, policy, and international development.

IMPORTANT: DevEx is protected by DataDome and most direct WebFetch attempts
on www.devex.com URLs will return a 403 / "Please enable JS" interstitial.
Do NOT spend more than ONE attempt on direct WebFetch of a devex.com URL.
Instead, use WebSearch with Google ``site:`` queries to discover postings.
The Google search-result TITLE and SNIPPET will give you enough metadata
(role title, employer, sometimes location) to populate a Job entry even
when the detail page itself is blocked.

Suggested search strategy (run 2-4 of these, dedupe URLs):
  - site:devex.com/jobs "research"
  - site:devex.com/jobs "migration" OR "displacement" OR "refugee"
  - site:devex.com/jobs "peace" OR "conflict"
  - site:devex.com/jobs "policy analyst" OR "qualitative"
  - site:devex.com/jobs "M&E" OR "evaluation"

For each unique posting URL, extract:
  - title: from the Google result title (strip the trailing " | Devex")
  - company: the employer/organization name; if the search snippet names
    a UN agency, NGO, think-tank, or contractor, use that. If unclear,
    use "DevEx (employer not specified in snippet)".
  - location: from the snippet if visible (city, country, "Remote", etc.);
    otherwise "" (empty string).
  - url: the absolute https://www.devex.com/jobs/<slug>-<id> URL exactly
    as Google indexed it.
  - posted_at: from the snippet if it shows a date ("Posted ...",
    "Published ..."), else "".
  - snippet: a one-sentence trimmed version of the Google search snippet.

Return STRICT JSON (no commentary, no markdown fences, no prose before or
after) with this shape:

{{"jobs": [
  {{"title": "...",
    "company": "...",
    "location": "...",
    "url": "absolute https://www.devex.com/jobs/... URL",
    "posted_at": "",
    "snippet": ""}}
]}}

Rules:
- Cap at {cap} unique results. Prefer the freshest (look for high job-id
  numbers in the URL — DevEx ids are monotonic-ish — and dates in snippets).
- ``url`` MUST be an absolute https URL on www.devex.com/jobs/. Skip any
  posting whose URL you can't recover.
- Skip duplicates by URL.
- If WebSearch returns nothing relevant or the searches all fail, return
  ``{{"jobs": []}}`` — do not invent results.

Output MUST be parseable by json.loads(). Do not include any text before or
after the JSON object.
""".strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch(filters: dict) -> list[Job]:
    """Aggregate DevEx postings via the Claude CLI delegation pattern.

    Parameters mirrored from filters / defaults:
      * ``max_per_source`` — hard cap on returned jobs (default 12).
      * ``ai_scrape_timeout_s`` — subprocess timeout for the CLI invocation
        (default 180s; multiple WebSearch calls add up).

    Returns a (possibly empty) list of ``Job`` instances. Never raises:
    every failure path logs and returns ``[]`` so the orchestrator can move
    on to the next source.

    Caveats (see module docstring for the full picture):
      * DevEx is DataDome-walled; we discover URLs via Google site-search,
        not direct fetch. Title/employer/location come from the search
        snippet, not the rendered job page.
      * Full job descriptions remain behind DevEx's Pro paywall — the user
        clicks through to read them.
    """
    timeout_s = int(filters.get("ai_scrape_timeout_s") or 180)
    cap = int(filters.get("max_per_source") or 12)

    sample_titles: list[str] = []
    body_head_for_forensic: str = ""
    out: list[Job] = []

    with _step(
        "devex.fetch",
        input={"cap": cap, "timeout_s": timeout_s, "landing": LANDING},
    ) as fctx:
        prompt = _PROMPT.format(cap=cap)

        try:
            stdout = _run_claude(prompt, timeout_s=timeout_s)
        except Exception as e:
            log.exception("devex: CLI invocation failed: %s", e)
            fctx.set_output({"raw_count": 0, "kept": 0, "reason": "cli_exception"})
            _log_forensic({
                "input": {"cap": cap, "landing": LANDING},
                "output": {"count": 0, "reason": "cli_exception", "error": repr(e)[:300]},
            })
            return []

        if stdout is None:
            log.warning("devex: claude CLI missing or returned None")
            fctx.set_output({"raw_count": 0, "kept": 0, "reason": "cli_missing_or_none"})
            _log_forensic({
                "input": {"cap": cap, "landing": LANDING},
                "output": {"count": 0, "reason": "cli_missing_or_none"},
            })
            return []
        if not stdout.strip():
            log.warning("devex: claude CLI returned empty stdout")
            fctx.set_output({"raw_count": 0, "kept": 0, "reason": "cli_empty"})
            _log_forensic({
                "input": {"cap": cap, "landing": LANDING},
                "output": {"count": 0, "reason": "cli_empty"},
            })
            return []

        body = extract_assistant_text(stdout)
        body_head_for_forensic = (body or "")[:300]
        data = parse_json_block(body)

        if not isinstance(data, dict):
            log.warning("devex: response was not a JSON object; head=%r", body_head_for_forensic)
            fctx.set_output({
                "raw_count": 0, "kept": 0,
                "reason": "non_object_response",
                "body_head": body_head_for_forensic,
            })
            _log_forensic({
                "input": {"cap": cap, "landing": LANDING},
                "output": {"count": 0, "reason": "non_object_response",
                           "body_head": body_head_for_forensic},
            })
            return []

        raw = data.get("jobs") or []
        if not isinstance(raw, list):
            log.warning("devex: 'jobs' field was not a list (%s)", type(raw).__name__)
            fctx.set_output({
                "raw_count": 0, "kept": 0,
                "reason": "jobs_not_list",
                "body_head": body_head_for_forensic,
            })
            _log_forensic({
                "input": {"cap": cap, "landing": LANDING},
                "output": {"count": 0, "reason": "jobs_not_list",
                           "body_head": body_head_for_forensic},
            })
            return []

        log.info("devex (AI): %d raw postings", len(raw))

        seen_urls: set[str] = set()
        for r in raw[:cap * 2]:  # over-iterate to allow url-dedupe down to cap
            if len(out) >= cap:
                break
            if not isinstance(r, dict):
                continue
            url = (r.get("url") or "").strip()
            if not url or not url.startswith("https://www.devex.com/jobs/"):
                # Without a valid devex.com job URL we have no stable
                # external_id (job id is in the URL path) and no link to
                # send the user. Skip.
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            title = fix_mojibake(str(r.get("title") or "")).strip()
            # Strip trailing " | Devex" if Claude forgot to.
            for suffix in (" | Devex", " | DevEx", " - Devex", " - DevEx"):
                if title.endswith(suffix):
                    title = title[: -len(suffix)].strip()
            if not title:
                continue

            # The job-id (numeric tail of the URL path) is our stable
            # external_id. If we can't recover one we fall back to the URL
            # itself — still unique, just longer. dedupe.Job hashing handles
            # both shapes.
            external_id = url.rstrip("/").rsplit("-", 1)[-1]
            if not external_id.isdigit():
                external_id = url

            out.append(Job(
                "devex",                         # source
                external_id,                     # external_id
                title[:140],                     # title
                fix_mojibake(str(r.get("company") or "")).strip()[:120],  # company
                fix_mojibake(str(r.get("location") or "")).strip()[:120], # location
                url,                             # url
                str(r.get("posted_at") or ""),   # posted_at
                fix_mojibake(str(r.get("snippet") or "")).strip()[:400],  # snippet
                "",                              # salary (DevEx rarely surfaces this)
            ))

        sample_titles = [j.title[:80] for j in out[:5]]

        fctx.set_output({
            "raw_count": len(raw),
            "kept": len(out),
            "sample_titles": sample_titles,
            # Only keep the body head when we got nothing — for diagnostics.
            "body_head": body_head_for_forensic if not out else None,
        })

    _log_forensic({
        "input": {"cap": cap, "landing": LANDING},
        "output": {
            "count": len(out),
            "raw_count": len(raw) if isinstance(raw, list) else 0,
            "sample_titles": sample_titles,
            "body_head": body_head_for_forensic if not out else "",
        },
    })

    return out
