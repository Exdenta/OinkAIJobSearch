"""AI-powered adapter for curated boards that lack public APIs.

Covers:
  - remocate.app          (global remote + relocation jobs)
  - wantapply.com         (curated AI-screened postings)
  - remoterocketship.com  (free public listings; paid API skipped on purpose)

None of these publishes a free JSON or RSS feed, and their HTML layouts drift
often enough that maintaining BeautifulSoup selectors is not worth it. Instead,
we delegate the fetch-and-extract step to a Claude agent: at run time this
module shells out to the `claude` CLI with a strict JSON-only prompt, a
target URL, and an explicit `--allowed-tools WebFetch` grant, and Claude
uses its own WebFetch tool to pull the current listings.

The `--allowed-tools` grant is load-bearing: without it the CLI prompts the
user interactively for tool permission, and in a non-interactive subprocess
that prompt becomes the textual response ("I need permission to use
WebFetch…") instead of the JSON envelope this adapter expects. See the
2026-05 fix in `sources/un_careers.py` for the same class of bug.

Requirements (on the machine that runs the cron):
  1. The `claude` CLI on PATH, already logged in (Anthropic account).
  2. Network egress to the three domains (checked by Claude, not by us).

Fallback behavior:
  - If the CLI is missing → log a warning and return [].
  - If the CLI times out or returns non-JSON → log and return [].
  - If a board is toggled off in defaults.DEFAULTS["sources"] → skip.

All three boards are DISABLED BY DEFAULT in defaults.DEFAULTS. Enable after
a `python search_jobs.py --dry-run` sanity check.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from claude_cli import (
    TOOLS_DENY_SHELL_FS,
    TOOLS_WEB_FETCH_ONLY,
    extract_assistant_text,
    parse_json_block,
    run_p_with_tools,
)
from instrumentation.wrappers import wrapped_run_p, wrapped_run_p_with_tools
import forensic
from dedupe import Job
from text_utils import fix_mojibake

log = logging.getLogger(__name__)

# Backend toggle — "claude" (agentic WebFetch subagent, the default) or
# "orchestrated" (plain-Python fetch via sources._detail_fetch + ONE
# single-shot LLM extract call via the "curated_boards_extract" stage).
# Mirrors sources/web_search.py's WEB_SEARCH_BACKEND toggle. Default "claude" — new control-flow path ships
# OFF until a manual smoke check confirms parity.
_VALID_CURATED_BOARDS_BACKENDS = frozenset({"claude", "orchestrated"})


def _curated_boards_backend() -> str:
    raw = (os.environ.get("CURATED_BOARDS_BACKEND") or "").strip().lower()
    return raw if raw in _VALID_CURATED_BOARDS_BACKENDS else "claude"

# Start URLs — each one is frontend-focused where possible.
BOARDS: dict[str, str] = {
    "remocate":          "https://www.remocate.app/job-categories/frontend-development",
    "wantapply":         "https://wantapply.com/",
    "remoterocketship":  "https://www.remoterocketship.com/jobs/category/software-engineer",
}


# Tool grants for the curated-boards sub-agent. Each board's prompt instructs
# Claude to ``Use the WebFetch tool to load the URL below`` — without an
# explicit ``--allowed-tools`` grant the CLI prompts interactively, and in a
# non-interactive subprocess that prompt becomes a textual "I need
# permission…" response instead of JSON. We do NOT grant WebSearch: the
# prompt only ever asks Claude to WebFetch the target URL (and at most one
# "next page" link). Keeping the allow list minimal narrows the attack
# surface. See sources/un_careers.py / sources/devex.py for the same fix.
_ALLOWED_TOOLS = TOOLS_WEB_FETCH_ONLY
# Belt-and-suspenders: forbid filesystem/shell so a successful prompt-injection
# in a fetched board page can't escalate.
_DISALLOWED_TOOLS = TOOLS_DENY_SHELL_FS

# The prompt is deliberately strict: it asks for JSON only, gives a clear schema,
# and embeds the title/location gates so Claude drops obvious noise on its side.
_PROMPT = """You are a job-scraping assistant. Use the WebFetch tool to load the URL below,
follow up to 1 "next page" link if the first page is a category index, and return the
open frontend-engineering roles as STRICT JSON (no commentary, no code fences).

Target URL: {url}

Return shape:
{{"jobs": [
  {{"title": "...", "company": "...", "location": "...", "url": "...",
    "posted_at": "", "snippet": ""}}
]}}

Rules for what to include:
- Frontend roles ONLY: React, Vue, Angular, Svelte, TypeScript, JavaScript, UI
  engineer, UI developer, web developer, or fullstack-with-frontend-lean.
- EXCLUDE: backend-only, data engineer, devops/SRE, iOS/Android, mobile,
  manager/director/head/principal/staff/VP, content/marketing/compliance roles.
- Include both remote and on-site roles in Spain, Basque Country, EU, or Europe.
  Skip roles that are US-only or APAC-only.
- Cap at 15 jobs. Prefer the freshest postings.
- `url` must be the direct job-detail URL (absolute, https).
- If you can't find anything, return {{"jobs": []}}.

Output MUST be parseable by json.loads(). Do not add any text before or after the JSON.
""".strip()


def _parse_jobs_json(text: str) -> list[dict[str, Any]]:
    """Extract the `jobs` array from the agent's JSON response."""
    data = parse_json_block(text)
    if not isinstance(data, dict):
        return []
    jobs = data.get("jobs") or []
    return [j for j in jobs if isinstance(j, dict)]


def _jobs_from_raw(raw: list[dict], board_key: str, url: str, cap: int) -> list[Job]:
    out: list[Job] = []
    for r in raw[:cap]:
        job_url = (r.get("url") or "").strip() or url
        out.append(Job(
            source=board_key,
            external_id=job_url,
            title=fix_mojibake(str(r.get("title") or ""))[:140],
            company=fix_mojibake(str(r.get("company") or ""))[:80],
            location=fix_mojibake(str(r.get("location") or "Remote"))[:80],
            url=job_url,
            posted_at=str(r.get("posted_at") or ""),
            snippet=fix_mojibake(str(r.get("snippet") or ""))[:400],
        ))
    return out


def _record_fetch_tier4(board_key: str, out: list[Job], db) -> None:
    if db is None:
        return
    try:
        seen_ids = [j.job_id for j in out]
        existing = db.count_existing_jobs(seen_ids) if seen_ids else 0
        db.record_fetch(
            board_key, board_key, 1, "",
            jobs_seen=len(seen_ids),
            jobs_new=max(0, len(seen_ids) - existing),
        )
    except Exception:
        log.debug("%s: db.record_fetch raised; continuing", board_key, exc_info=True)


# Orchestrated (non-agentic) prompt: same rules/schema as `_PROMPT`, but the
# page body is fetched by our own code (sources._detail_fetch) and handed to
# the model, plus an added `next_page_url` field so the caller can decide
# whether to fetch a second page — replicating the old agent's "follow up to
# 1 next-page link" behavior as an explicit, bounded, deterministic 2nd
# fetch+extract rather than agentic browsing.
_ORCHESTRATED_PROMPT = """You are a job-scraping assistant. Below is the fetched content of
a job-board page. Extract the open frontend-engineering roles as STRICT JSON (no commentary,
no code fences).

Page URL: {url}

Return shape:
{{"jobs": [
  {{"title": "...", "company": "...", "location": "...", "url": "...",
    "posted_at": "", "snippet": ""}}
], "next_page_url": ""}}

Rules for what to include:
- Frontend roles ONLY: React, Vue, Angular, Svelte, TypeScript, JavaScript, UI
  engineer, UI developer, web developer, or fullstack-with-frontend-lean.
- EXCLUDE: backend-only, data engineer, devops/SRE, iOS/Android, mobile,
  manager/director/head/principal/staff/VP, content/marketing/compliance roles.
- Include both remote and on-site roles in Spain, Basque Country, EU, or Europe.
  Skip roles that are US-only or APAC-only.
- Cap at 15 jobs. Prefer the freshest postings.
- `url` must be the direct job-detail URL (absolute, https) — look for it as
  the "(https://...)" link target right after a posting's title/label below.
  If the page body doesn't give you one, use the Page URL above.
- If this page is a category index rather than a listing of roles, and you can
  see a "next page"/"see more" link (an absolute "(https://...)" URL) for the
  SAME category/listing, put it in `next_page_url`. Otherwise leave
  `next_page_url` as "".
- If you can't find anything, return {{"jobs": [], "next_page_url": ""}}.

Page content — link targets are inlined as "label (https://...)":
{body}

Output MUST be parseable by json.loads(). Do not add any text before or after the JSON.
""".strip()


def _extract_orchestrated(board_key: str, url: str, body: str, *, timeout_s: int) -> tuple[list[dict], str]:
    """One single-shot extract call over an already-fetched, link-preserving
    page body (see `text_utils.html_links_to_text`).

    Returns (raw_jobs, next_page_url). Empty list / "" on any failure.
    """
    prompt = _ORCHESTRATED_PROMPT.format(url=url, body=body[:8000])
    stdout = wrapped_run_p(
        None, f"curated_boards_extract:{board_key}", prompt, timeout_s=timeout_s,
    )
    if not stdout:
        return [], ""
    data = parse_json_block(extract_assistant_text(stdout))
    if not isinstance(data, dict):
        return [], ""
    jobs = [j for j in (data.get("jobs") or []) if isinstance(j, dict)]
    next_page_url = str(data.get("next_page_url") or "").strip()
    return jobs, next_page_url


def _fetch_listing_text(url: str) -> str:
    """Fetch a listing/index page and return link-preserving text.

    Unlike job-DETAIL pages (`sources._detail_fetch.fetch_body_text`, which
    strips all markup including hrefs — fine when only description text
    matters), a listing page's per-posting URLs and "next page" link live in
    `<a href>` attributes the model needs to recover. Fetch raw HTML, then
    inline link targets as "label (URL)" text via `text_utils`.
    """
    from sources._detail_fetch import fetch_raw_html
    from text_utils import html_links_to_text

    raw_html = fetch_raw_html(url, timeout_s=15.0)
    return html_links_to_text(raw_html, base_url=url, max_chars=8000)


def _scrape_one_orchestrated(board_key: str, url: str, filters: dict, *, db=None) -> list[Job]:
    """Non-agentic equivalent of `_scrape_one_claude`: fetch via
    sources._detail_fetch, extract via ONE single-shot LLM call (the
    "curated_boards_extract" stage). Bounded 2nd
    fetch+extract if the model reports a `next_page_url` — deterministic, not
    agentic browsing.
    """
    timeout_s = int(filters.get("ai_scrape_timeout_s") or 180)
    cap = int(filters.get("max_per_source") or 10)

    with forensic.step(
        f"curated_boards.{board_key}",
        input={"board_key": board_key, "url": url, "timeout_s": timeout_s, "cap": cap,
               "backend": "orchestrated"},
    ) as fctx:
        body = _fetch_listing_text(url)
        if not body:
            fctx.set_output({"raw_count": 0, "reason": "fetch_empty"})
            return []

        raw, next_page_url = _extract_orchestrated(board_key, url, body, timeout_s=timeout_s)
        if next_page_url and next_page_url != url and len(raw) < cap:
            body2 = _fetch_listing_text(next_page_url)
            if body2:
                raw2, _ = _extract_orchestrated(board_key, next_page_url, body2, timeout_s=timeout_s)
                raw = raw + raw2

        log.info("%s (orchestrated): %d raw postings", board_key, len(raw))
        out = _jobs_from_raw(raw, board_key, url, cap)
        _record_fetch_tier4(board_key, out, db)

        fctx.set_output({
            "raw_count": len(raw),
            "kept": len(out),
            "sample_titles": [j.title[:80] for j in out[:5]],
            "next_page_url": next_page_url,
        })
        return out


def _scrape_one_claude(board_key: str, url: str, filters: dict, *, db=None) -> list[Job]:
    timeout_s = int(filters.get("ai_scrape_timeout_s") or 180)
    cap = int(filters.get("max_per_source") or 10)

    with forensic.step(
        f"curated_boards.{board_key}",
        input={"board_key": board_key, "url": url, "timeout_s": timeout_s, "cap": cap},
    ) as fctx:
        prompt = _PROMPT.format(url=url)
        # Tag the underlying CLI call with the specific board so claude_calls
        # rolls up per-board cost/elapsed.
        # Pin to haiku (2026-05-25) — see same fix in devex/web_search.
        # Use the tool-aware wrapper: the prompt asks for WebFetch, and
        # without an explicit --allowed-tools grant the CLI prompts the
        # user interactively (textual response, never JSON). See the
        # 2026-05 fix in sources/un_careers.py for the same class of bug.
        from claude_cli import SMALLEST_MODEL as _MODEL
        stdout = wrapped_run_p_with_tools(
            None,
            f"curated_boards:{board_key}",
            prompt,
            allowed_tools=_ALLOWED_TOOLS,
            disallowed_tools=_DISALLOWED_TOOLS,
            timeout_s=timeout_s,
            model=_MODEL,
        )
        if not stdout:
            fctx.set_output({"raw_count": 0, "reason": "cli_missing_or_empty"})
            return []
        body = extract_assistant_text(stdout)
        raw = _parse_jobs_json(body)
        log.info("%s (AI): %d raw postings", board_key, len(raw))

        out: list[Job] = []
        for r in raw[:cap]:
            job_url = (r.get("url") or "").strip() or url
            out.append(Job(
                source=board_key,
                external_id=job_url,
                title=fix_mojibake(str(r.get("title") or ""))[:140],
                company=fix_mojibake(str(r.get("company") or ""))[:80],
                location=fix_mojibake(str(r.get("location") or "Remote"))[:80],
                url=job_url,
                posted_at=str(r.get("posted_at") or ""),
                snippet=fix_mojibake(str(r.get("snippet") or ""))[:400],
            ))

        # Tier 4: feed the adaptive source-cooldown FSM PER SUB-BOARD. Each
        # board (remocate / wantapply / remoterocketship) records under its
        # OWN source key, so `should_run_source` can demote a chronically
        # quiet board independently of its siblings. jobs_new counts how many
        # of this fetch's postings aren't yet in the `jobs` table, keyed on
        # the real Job.job_id (sha1 of source+external_id) that `upsert_job`
        # stores — recorded before the downstream upsert, so a repeat posting
        # reads as "not new". These boards don't paginate, so we use a single
        # fixed (query=board_key, page=1, location="") cell. Best-effort: a DB
        # error never breaks the scrape.
        if db is not None:
            try:
                seen_ids = [j.job_id for j in out]
                existing = db.count_existing_jobs(seen_ids) if seen_ids else 0
                db.record_fetch(
                    board_key, board_key, 1, "",
                    jobs_seen=len(seen_ids),
                    jobs_new=max(0, len(seen_ids) - existing),
                )
            except Exception:
                log.debug("%s: db.record_fetch raised; continuing",
                          board_key, exc_info=True)

        fctx.set_output({
            "raw_count": len(raw),
            "kept": len(out),
            "sample_titles": [j.title[:80] for j in out[:5]],
            "body_head": (body or "")[:300] if not raw else None,
        })
        return out


def _scrape_one(board_key: str, url: str, filters: dict, *, db=None) -> list[Job]:
    if _curated_boards_backend() == "orchestrated":
        return _scrape_one_orchestrated(board_key, url, filters, db=db)
    return _scrape_one_claude(board_key, url, filters, db=db)


def fetch(filters: dict, *, db=None) -> list[Job]:
    """Aggregate AI-scraped postings from the enabled curated boards.

    A board is scraped when it is enabled in ``filters["sources"]``. The
    caller (`search_jobs.fetch_all`) already applies the per-sub-board
    adaptive-cooldown gate (`should_run_source`) and drops a board's toggle
    for the OFF half of its alternation, so a sub-board the FSM has demoted
    simply isn't enabled here this cycle. ``db`` is forwarded to
    ``_scrape_one`` so each board records its own ``search_fetches`` novelty
    row (Tier 4). When ``db`` is None (dry-run preview) recording is skipped.
    """
    srcs = filters.get("sources") or {}
    all_jobs: list[Job] = []
    enabled_keys = [k for k in BOARDS if srcs.get(k, False)]
    for key, url in BOARDS.items():
        if not srcs.get(key, False):
            continue
        try:
            all_jobs.extend(_scrape_one(key, url, filters, db=db))
        except Exception as e:
            log.exception("%s: AI scrape failed: %s", key, e)
    forensic.log_step(
        "curated_boards.fetch",
        input={"enabled": enabled_keys},
        output={"total": len(all_jobs)},
    )
    return all_jobs
