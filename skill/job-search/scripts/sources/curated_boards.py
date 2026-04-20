"""AI-powered adapter for curated boards that lack public APIs.

Covers:
  - remocate.app          (global remote + relocation jobs)
  - wantapply.com         (curated AI-screened postings)
  - remoterocketship.com  (free public listings; paid API skipped on purpose)

None of these publishes a free JSON or RSS feed, and their HTML layouts drift
often enough that maintaining BeautifulSoup selectors is not worth it. Instead,
we delegate the fetch-and-extract step to a Claude agent: at run time this
module shells out to the `claude` CLI with a strict JSON-only prompt and a
target URL, and Claude uses its own WebFetch / WebSearch tools to pull the
current listings.

Requirements (on the machine that runs the cron):
  1. The `claude` CLI on PATH, already logged in (Anthropic account).
  2. Network egress to the three domains (checked by Claude, not by us).

Fallback behavior:
  - If the CLI is missing → log a warning and return [].
  - If the CLI times out or returns non-JSON → log and return [].
  - If a board is toggled off in filters.yaml → skip.

All three boards are DISABLED BY DEFAULT in filters.yaml. Enable after a
`python search_jobs.py --dry-run` sanity check.
"""
from __future__ import annotations

import logging
from typing import Any

from claude_cli import run_p, extract_assistant_text, parse_json_block
from dedupe import Job
from text_utils import fix_mojibake

log = logging.getLogger(__name__)

# Start URLs — each one is frontend-focused where possible.
BOARDS: dict[str, str] = {
    "remocate":          "https://www.remocate.app/job-categories/frontend-development",
    "wantapply":         "https://wantapply.com/",
    "remoterocketship":  "https://www.remoterocketship.com/jobs/category/software-engineer",
}

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


def _scrape_one(board_key: str, url: str, filters: dict) -> list[Job]:
    timeout_s = int(filters.get("ai_scrape_timeout_s") or 180)
    cap = int(filters.get("max_per_source") or 10)

    prompt = _PROMPT.format(url=url)
    stdout = run_p(prompt, timeout_s=timeout_s)
    if not stdout:
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
    return out


def fetch(filters: dict) -> list[Job]:
    """Aggregate AI-scraped postings from the enabled curated boards."""
    srcs = filters.get("sources") or {}
    all_jobs: list[Job] = []
    for key, url in BOARDS.items():
        if not srcs.get(key, False):
            continue
        try:
            all_jobs.extend(_scrape_one(key, url, filters))
        except Exception as e:
            log.exception("%s: AI scrape failed: %s", key, e)
    return all_jobs
