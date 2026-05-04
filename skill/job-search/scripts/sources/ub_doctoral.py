"""Universitat de Barcelona — Doctoral Programmes adapter (slug: `ub_doctoral`).

Surfaces PhD-position openings tied to UB. The canonical landing page,

    https://web.ub.edu/en/web/estudis/doctoral-programmes

lists *programmes* (multi-year structured PhDs) rather than directly-funded
job openings. Real openings live in three messy places:

  1. UB's HR / convocatories portal
     (https://www.ub.edu/web/portal/en/convocatories.html → redirects to
     https://web.ub.edu/web/ub/ — the listings sit under "Working at the UB")
  2. Per-doctoral-programme pages where PIs sometimes list openings inline.
  3. Mirrored EURAXESS postings (covered by a separate adapter; we don't
     import or depend on it — keep this self-contained per the brief).

Why AI delegation (option 4)
----------------------------
RSS/feed probes against the doctoral-programmes URL all fail:

    GET .../doctoral-programmes/feed         -> 404
    GET .../doctoral-programmes/rss          -> 404
    GET .../doctoral-programmes?format=rss   -> 200 but HTML (no feed)

The site is Liferay (sets JSESSIONID + GUEST_LANGUAGE_ID cookies on every
hit), the per-page markup is irregular Drupal/Liferay layout, and content
shifts between the EN/ES/CA locales. Hand-maintained selectors would break
constantly.

Volume is genuinely low — at any given time UB rarely has more than a
handful of open PhD positions advertised on their public pages — so paying
~$0.05 per run for a Claude CLI call that uses WebFetch is the right
trade-off. The adapter shells out via `claude_cli.run_p_with_tools` with a
strict JSON-only prompt and lets the model crawl up to two pages
(landing + one HR/convocatories link) on its side.

Filters / config
----------------
  - `max_per_source` (default 12)         — cap on returned Jobs
  - `ai_scrape_timeout_s` (default 180)   — passed to the CLI
  - `sources.ub_doctoral` toggle          — wired separately in defaults.py;
                                            this module is opt-in.

Fallback behavior matches the curated_boards adapter:
  - CLI missing  -> warn + return []
  - timeout      -> log + return []
  - non-JSON     -> log + return []
  - empty result -> return [] (totally normal — UB often has 0 active posts)
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from claude_cli import (
    extract_assistant_text,
    parse_json_block,
    run_p_with_tools,
)
from dedupe import Job
from text_utils import clean_snippet, fix_mojibake

log = logging.getLogger(__name__)

SOURCE_KEY = "ub_doctoral"

# Primary URL the LLM is told to start from. The HR/convocatories link is
# given as a secondary hint inside the prompt rather than a separate fetch
# (we want exactly one CLI invocation per run).
LANDING_URL = "https://web.ub.edu/en/web/estudis/doctoral-programmes"
HR_URL = "https://www.ub.edu/web/ub/en/menu_eines/treballar.html"

_PROMPT = """You are a careful PhD-position scraper for the University of Barcelona (UB).

Your job: surface CURRENTLY-OPEN, FUNDED PhD positions or doctoral-research vacancies tied to UB.
This is NOT a request for the list of doctoral programmes — those are multi-year curricula, not jobs.
We want concrete openings a candidate can apply to right now (a specific thesis topic, a named
supervisor or department, a deadline, or an "apply here" link).

Use the WebFetch tool to load these URLs. You may follow at most ONE additional link per page if it
clearly leads to current vacancies (e.g. "Open positions", "Vacancies", "Convocatories", "PhD
positions", "Predoctoral contracts"):

  Primary:   {landing_url}
  Secondary: {hr_url}

If the primary page only lists programmes (no openings), check the secondary page and any obvious
"working at UB" / "predoctoral" / "convocatories" sub-page. Don't crawl deeper than that.

Return STRICT JSON (no commentary, no code fences) with this exact shape:

{{"jobs": [
  {{
    "title": "Programme + thesis topic if available, else just the position title",
    "url": "absolute https URL of the position detail page",
    "posted_at": "YYYY-MM-DD if visible, else empty string",
    "snippet": "first ~400 chars summarising the role / requirements"
  }}
], "reasoning": "one short sentence explaining what you found or why the result is empty"}}

Rules:
- ONLY include genuine open vacancies. Skip the programme catalogue entries.
- Skip purely administrative / non-research roles.
- Cap at 15 jobs.
- `url` MUST be absolute https. Prefer https://web.ub.edu/... or https://www.ub.edu/... ;
  external mirrors (euraxess.ec.europa.eu) are also fine if that's the canonical apply link.
- If you find nothing, return {{"jobs": [], "reasoning": "no open PhD positions visible today"}}.
- Output MUST be parseable by json.loads(). Do not add any text before or after the JSON.
""".strip()


def _stable_external_id(url: str, title: str) -> str:
    """Stable external_id even if UB rewrites the URL with a new session token."""
    if url and url.startswith("http"):
        return url
    raw = f"{url}::{title}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def _parse_response(text: str) -> tuple[list[dict[str, Any]], str]:
    """Return (jobs_list, reasoning). reasoning is "" if the model didn't supply one."""
    data = parse_json_block(text)
    if not isinstance(data, dict):
        return [], ""
    jobs = data.get("jobs") or []
    if not isinstance(jobs, list):
        jobs = []
    reasoning = str(data.get("reasoning") or "")
    return [j for j in jobs if isinstance(j, dict)], reasoning


def fetch(filters: dict | None = None) -> list[Job]:
    """Fetch current UB doctoral PhD-position openings via Claude CLI + WebFetch.

    Returns an empty list on any failure (CLI missing, timeout, parse error,
    or genuinely zero open positions). Never raises.
    """
    f = filters or {}
    cap = int(f.get("max_per_source") or 12)
    timeout_s = int(f.get("ai_scrape_timeout_s") or 180)

    prompt = _PROMPT.format(landing_url=LANDING_URL, hr_url=HR_URL)

    log.info(
        "%s.fetch: invoking claude CLI (cap=%d timeout=%ds landing=%s)",
        SOURCE_KEY, cap, timeout_s, LANDING_URL,
    )

    stdout = run_p_with_tools(
        prompt,
        allowed_tools="WebFetch WebSearch",
        timeout_s=timeout_s,
    )
    if stdout is None:
        log.warning("%s.fetch: CLI unavailable or failed; returning []", SOURCE_KEY)
        return []

    body = extract_assistant_text(stdout)
    raw, reasoning = _parse_response(body)

    if not raw:
        body_head = (body or "").strip().replace("\n", " ")[:300]
        log.info(
            "%s.fetch: 0 raw postings (reasoning=%r body_head=%r)",
            SOURCE_KEY, reasoning, body_head,
        )
        return []

    log.info(
        "%s.fetch: %d raw postings; reasoning=%r",
        SOURCE_KEY, len(raw), reasoning,
    )

    out: list[Job] = []
    sample_titles: list[str] = []
    for r in raw[:cap]:
        url = (r.get("url") or "").strip()
        title = fix_mojibake(str(r.get("title") or "")).strip()
        if not title:
            continue
        if url and not url.startswith("http"):
            # Best-effort absolute-ization for relative paths.
            url = f"https://web.ub.edu{url}" if url.startswith("/") else ""
        if not url:
            url = LANDING_URL  # last resort so the message link is at least valid
        ext_id = _stable_external_id(url, title)
        snippet = clean_snippet(str(r.get("snippet") or ""), max_chars=400)
        out.append(Job(
            source=SOURCE_KEY,
            external_id=ext_id,
            title=title[:140],
            company="Universitat de Barcelona",
            location="Barcelona, Spain",
            url=url,
            posted_at=str(r.get("posted_at") or "").strip()[:32],
            snippet=snippet,
        ))
        if len(sample_titles) < 3:
            sample_titles.append(title[:80])

    log.info(
        "%s.fetch: kept=%d sample_titles=%s",
        SOURCE_KEY, len(out), sample_titles,
    )
    return out
