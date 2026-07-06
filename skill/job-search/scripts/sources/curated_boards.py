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
WebFetch…") instead of the JSON envelope this adapter expects (2026-05 fix).

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
from typing import Any

from claude_cli import (
    TOOLS_DENY_SHELL_FS,
    TOOLS_WEB_FETCH_ONLY,
    extract_assistant_text,
    parse_json_block,
    run_p_with_tools,
)
from instrumentation.wrappers import wrapped_run_p_with_tools
import forensic
from dedupe import Job
from text_utils import fix_mojibake

log = logging.getLogger(__name__)

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
# surface. See sources/devex.py for the same fix.
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


def _scrape_one(board_key: str, url: str, filters: dict, *, db=None) -> list[Job]:
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
        # user interactively (textual response, never JSON) — 2026-05 fix.
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
