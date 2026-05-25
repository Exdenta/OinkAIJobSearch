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
import re
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
    # Pin to haiku (2026-05-25): without an explicit --model the CLI falls
    # back to the default (Opus on this account) and a single devex run
    # routinely exceeds 240s + ARG/IO ceiling, blowing through the 180s
    # bot timeout. Haiku handles the WebSearch+JSON extraction in ~90s.
    from claude_cli import SMALLEST_MODEL as _MODEL
    if _HAS_WRAPPED and _wrapped_run_p_with_tools is not None:
        try:
            return _wrapped_run_p_with_tools(  # type: ignore[misc]
                None, "devex", prompt,
                allowed_tools=_ALLOWED_TOOLS,
                disallowed_tools=_DISALLOWED_TOOLS,
                timeout_s=timeout_s,
                model=_MODEL,
            )
        except Exception:
            log.exception("devex: wrapped_run_p_with_tools failed; falling back to run_p_with_tools")
    return run_p_with_tools(
        prompt,
        allowed_tools=_ALLOWED_TOOLS,
        disallowed_tools=_DISALLOWED_TOOLS,
        timeout_s=timeout_s,
        model=_MODEL,
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


# DevEx slug patterns observed across postings:
#   .../jobs/<role-slug>-at-<org-slug>-<id>      ← preferred (org tail)
#   .../jobs/<role-slug>-<id>                    ← no org in slug
#   .../jobs/<role-slug>-<m-f-d>-<id>            ← diversity suffix
_DEVEX_URL_AT_RE = re.compile(r"/jobs/[^/?#]*?-at-([a-z0-9-]+?)-\d+/?$", re.I)


def _extract_employer_from_url(url: str) -> str:
    """Pull the employer name out of the DevEx URL slug.

    Returns the title-cased org name on success, "" on failure. We only
    parse the canonical `-at-<org>-<id>` shape; other shapes are too
    ambiguous to risk a false extraction.
    """
    if not url:
        return ""
    m = _DEVEX_URL_AT_RE.search(url.split("?", 1)[0])
    if not m:
        return ""
    raw = m.group(1).replace("-", " ").strip()
    if not raw:
        return ""
    # Title-case each token. Acronyms (UNHCR, IRC, etc.) will render as
    # "Unhcr" / "Irc"; that's a cosmetic miss but still strictly better
    # than the placeholder "DevEx (employer not specified in snippet)".
    # The scorer doesn't care about case; the user-facing card uses
    # whatever this returns plus whatever the scorer wrote into
    # `key_details`.
    return " ".join(p.capitalize() for p in raw.split())

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
  - company: the employer/organization name. EXTRACT IT — never write
    "DevEx (employer not specified in snippet)" or any placeholder.
    Priority:
      (i)   If snippet starts with "<Org> is hiring", "<Org> is
            seeking", "<Org> is looking for", "Position at <Org>",
            "<Org> | Devex" → "<Org>" is the employer.
      (ii)  If the URL slug contains the employer hint (e.g.
            ".../jobs/research-officer-at-international-rescue-
            committee-518185" → "International Rescue Committee";
            URL pattern `-at-<org-slug>-<id>` is common on DevEx),
            extract from the slug.
      (iii) If snippet names a UN agency (UNHCR, UNDP, UNESCO, IOM,
            FAO, WFP, WHO, UNICEF), NGO, think-tank, donor agency
            (USAID, GIZ, DFAT, FCDO, SIDA), university, or
            consultancy → use that name.
      (iv)  If snippet says "posted by <Org>" or "Organization:
            <Org>" → extract.
      (v)   If absolutely nothing identifies the employer → use the
            string "Unknown employer" (NOT a "DevEx" placeholder).
            But this should be RARE — DevEx postings almost always
            name the org in the title slug or snippet head.
  - location: from the snippet if visible (city, country, "Remote", etc.);
    otherwise "" (empty string).
  - url: the absolute https://www.devex.com/jobs/<slug>-<id> URL exactly
    as Google indexed it.
  - posted_at: aggressive extraction REQUIRED. DevEx postings frequently
    have visible dates in Google's search snippet. Priority:
      (i)   Explicit date in snippet: "Posted on May 10, 2026",
            "Published 2 weeks ago", "Posted Jan 15, 2020" — convert
            to ISO ("YYYY-MM-DD"). "X weeks ago" → today - 7*X days.
            "X months ago" → today - 30*X days.
      (ii)  Relative: "Yesterday" → today-1; "Today" → today;
            "Hours ago" / "Just now" → today.
      (iii) Deadline mention in snippet ("Deadline: 18 May 2026" /
            "Apply by 30/06/2026") — use that as a freshness proxy
            ONLY if no posted date is visible; assume posted_at =
            deadline - 30 days.
      (iv)  If none of the above and the URL contains a high job-id
            (>1000000) → guess "today". If the URL has a LOW job-id
            (<700000) → guess a date 12+ months ago (these are
            historical postings DevEx never closed; the downstream
            age gate should drop them).
    Always return SOMETHING for posted_at, even if best-guess. NEVER
    return empty string for DevEx postings — the age gate's
    missing_policy will let them through and stale 2020 postings
    will leak. Be conservative: if uncertain, guess OLD, not new.
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

            company = fix_mojibake(str(r.get("company") or "")).strip()
            # Reject placeholder employer strings the subagent might still
            # emit despite the prompt update — fall back to URL slug
            # extraction so user sees a real org name, not "DevEx (...)".
            if (
                not company
                or company.lower().startswith("devex (")
                or company.lower() == "devex"
                or "employer not specified" in company.lower()
            ):
                company = _extract_employer_from_url(url) or "Unknown employer"

            out.append(Job(
                "devex",                         # source
                external_id,                     # external_id
                title[:140],                     # title
                company[:120],                   # company
                fix_mojibake(str(r.get("location") or "")).strip()[:120], # location
                url,                             # url
                str(r.get("posted_at") or ""),   # posted_at
                fix_mojibake(str(r.get("snippet") or "")).strip()[:400],  # snippet
                "",                              # salary (DevEx rarely surfaces this)
            ))

        # Algorithm v2.2 — Option 4: DevEx Google-search snippets stop
        # at ~150 chars and routinely omit the "Required qualifications"
        # block, which is exactly the signal Sonnet needs to rule out
        # senior-only / US-only / Arabic-required postings. Fetch each
        # detail URL inline. (DevEx HTML is DataDome-gated for the
        # Anthropic WebFetch service, but a plain requests.get with a
        # browser UA gets through.)
        if out:
            try:
                from sources._detail_fetch import fetch_many_bodies
                body_map = fetch_many_bodies(
                    [j.url for j in out], max_chars=4000, workers=8,
                )
                enriched = 0
                for i, j in enumerate(out):
                    body = body_map.get(j.url, "")
                    if body and len(body) > len(j.snippet):
                        out[i] = Job(
                            j.source, j.external_id, j.title, j.company,
                            j.location, j.url, j.posted_at, body,
                            getattr(j, "salary", ""),
                        )
                        enriched += 1
                log.info(
                    "devex: detail-page bodies fetched for %d/%d postings",
                    enriched, len(out),
                )
            except Exception:
                log.exception("devex: detail-page fetch raised; continuing")

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
