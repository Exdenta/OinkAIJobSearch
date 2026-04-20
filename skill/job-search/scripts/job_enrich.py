"""AI-driven job matching (the sole matching gate).

For every new posting from every source we ask the `claude` CLI (smallest
model — Haiku) to decide fit against the user's resume AND their stated
preferences, then emit three things:

  - match_score:   integer 0-5
                     0 = clearly wrong fit (wrong stack, seniority, location,
                         language, or something the user explicitly excluded).
                         Callers drop score 0.
                     1 = poor
                     2 = weak
                     3 = OK / acceptable stretch
                     4 = strong fit
                     5 = perfect fit
  - why_match:     1-2 sentences, resume-aware (mentions overlapping skills /
                    experience, not generic "great frontend role")
  - key_details:   {stack, seniority, remote_policy, location, salary,
                    visa_support, language, standout}

Design decisions:
  - This is now the ONLY matching gate. The old keyword/regex post_filter in
    search_jobs.py has been neutered — Claude holistically decides using the
    resume + the user's preference dict. filters.yaml's title_must_match /
    title_exclude / keywords / exclude_keywords fields are NOT consulted.
  - Smallest model (Haiku) per operator instruction. Cheap + fast enough to
    run on every fetched posting from every source (LinkedIn, HN, remoteok,
    remotive, weworkremotely, curated boards, web_search).
  - ONE batched CLI call per user per run. Cheaper, faster, and avoids the
    `claude -p` startup cost being multiplied by N.
  - Strict JSON-only output. We re-key by `external_id` (stable across runs)
    so partial responses still associate correctly.
  - Per-field length caps in the prompt — Telegram messages are tight on space.
  - Graceful degradation: if the CLI is missing, times out, or returns junk,
    `enrich_jobs_ai` returns {} and the caller renders without enrichment.
    In that degraded mode nothing filters by score, so postings still flow —
    but the operator sees a WARN in logs.

This module intentionally has no Telegram dependency — it only knows about
`Job` objects and returns plain dicts.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from claude_cli import run_p, extract_assistant_text, parse_json_block, SMALLEST_MODEL
from dedupe import Job
from text_utils import fix_mojibake

log = logging.getLogger(__name__)


_PROMPT = """You are a careful job-match analyst working for ONE candidate.

You are the SOLE gate deciding whether a posting should be shown to this
candidate. There are no keyword pre-filters upstream — every posting that
reaches you came straight from a public source (LinkedIn, HackerNews "Who is
hiring", remoteok, remotive, weworkremotely, curated remote boards, and
open-web search results). So you must:

  - REJECT postings whose role, stack, seniority, location, language, or
    work arrangement clearly contradict the candidate's preferences.
    Signal this by scoring 0.
  - Actively evaluate fit against BOTH the resume AND the preferences block.
    The preferences block is how the candidate told the bot what they want;
    it is authoritative. Resume describes what they CAN do; preferences
    describe what they WANT.

For each posting, you must:

  1. Score how well it matches THIS candidate, on an integer 0-5 scale:
       0 = clearly wrong fit — reject. Use this when the title/role, stack,
           seniority, location, language, or remote policy directly
           contradicts the candidate's stated preferences.
       1 = poor
       2 = weak
       3 = OK / acceptable stretch
       4 = strong fit
       5 = perfect fit
     Be honest — most postings should land at 2-4. Reserve 5 for postings
     where role, stack, seniority, location/remote, AND language all line up.
     Do not inflate scores to be nice.

  2. Write `why_match`: ONE or TWO sentences, max 240 chars, that name
     specific overlaps with this candidate's resume AND preferences (e.g.
     "React + TS + Storybook overlap; Bilbao remote-friendly, matches
     user's EU remote ask"). DO NOT write generic filler like "great
     frontend role". For score-0 rejects, `why_match` should state WHY
     it's a reject ("backend role; user wants frontend only").

  3. Extract `key_details` from the posting (use "" for fields not stated):
       - stack:          comma-separated tech mentioned (e.g. "React, TS, Vue")
       - seniority:      one of "junior" | "middle" | "senior" | "lead" | "any" | ""
       - remote_policy:  "remote" | "hybrid" | "onsite" | ""
       - location:       city/country if onsite/hybrid, else ""
       - salary:         as stated (with currency), else ""
       - visa_support:   "yes" | "no" | "" if not stated
       - language:       primary working language if stated, else ""
       - standout:       at most 80 chars naming the single most distinctive
                          aspect of the posting (perk, product, scale, etc.)

Return STRICT JSON (no markdown, no fences, no commentary) of this shape:

{{"results": [
  {{"id": "<external_id verbatim>",
    "match_score": <0-5>,
    "why_match": "...",
    "key_details": {{
      "stack": "...", "seniority": "...", "remote_policy": "...",
      "location": "...", "salary": "...", "visa_support": "...",
      "language": "...", "standout": "..."
    }}
  }}
]}}

Rules:
- The `id` MUST match the posting's external_id exactly.
- Output MUST be parseable by json.loads().
- No newlines inside any string field.
- Return one entry per input posting — do not drop postings from the
  response. Rejects are scored 0, not omitted.

=== CANDIDATE RESUME (plaintext) ===
{resume}

=== CANDIDATE PREFERENCES (JSON; "" / [] / "any" = "no opinion, be lenient") ===
{prefs_json}

=== JOBS (JSON array) ===
{jobs_json}
""".strip()


def _job_to_brief(j: Job) -> dict[str, str]:
    """Compact representation we hand to the model. Truncated for token economy."""
    return {
        "external_id": j.external_id,
        "title":   (j.title or "")[:200],
        "company": (j.company or "")[:120],
        "location": (j.location or "")[:120],
        "salary":  (j.salary or "")[:120],
        "url":     (j.url or "")[:400],
        "snippet": (j.snippet or "").replace("\n", " ")[:1200],
    }


def _normalize_score(v: Any) -> int:
    """Coerce to int in [0, 5]. Returns 0 if it can't be parsed."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, n))


def _normalize_details(d: Any) -> dict[str, str]:
    """Make sure every expected key exists as a clean string."""
    keys = ("stack", "seniority", "remote_policy", "location",
            "salary", "visa_support", "language", "standout")
    if not isinstance(d, dict):
        d = {}
    out: dict[str, str] = {}
    for k in keys:
        val = d.get(k)
        if val is None:
            out[k] = ""
        else:
            out[k] = fix_mojibake(str(val).strip())[:160]
    return out


def enrich_jobs_ai(
    jobs: list[Job],
    resume_text: str,
    timeout_s: int = 240,
    max_jobs_per_call: int = 25,
    projected_prefs: dict | None = None,
) -> dict[str, dict]:
    """Return a {external_id → enrichment dict} map.

    enrichment dict has shape:
        {"match_score": int 0-5, "why_match": str, "key_details": {...}}

    Args:
      jobs:            Postings to score.
      resume_text:     Raw resume text the user uploaded via /start.
      timeout_s:       Per-CLI-call timeout. The caller passes the full
                       batch timeout; individual chunks share it.
      max_jobs_per_call: Chunk size. Kept low so the Haiku prompt stays
                       within a fast-response window.
      projected_prefs: The projected per-user preference dict (produced by
                       `user_profile.project_to_prefs` from the Opus-built
                       profile). Claude uses it as the "what the user wants"
                       companion to the resume's "what the user can do".

    Returns an empty dict on any failure. Caller MUST tolerate missing entries
    (e.g. the model dropped some IDs).

    Batching: if `jobs` is longer than `max_jobs_per_call`, we send multiple
    chunks. Each chunk gets its own CLI invocation.
    """
    if not jobs:
        return {}
    if not resume_text or not resume_text.strip():
        log.info("enrich_jobs_ai: empty resume — skipping enrichment")
        return {}

    prefs_for_prompt = _prefs_for_prompt(projected_prefs)

    out: dict[str, dict] = {}
    # Build chunks
    for start in range(0, len(jobs), max_jobs_per_call):
        chunk = jobs[start:start + max_jobs_per_call]
        out.update(_enrich_one_chunk(chunk, resume_text, timeout_s, prefs_for_prompt))
    return out


# Fields we surface to the prompt. The projector (`project_to_prefs`) emits
# exactly these keys so the model sees a stable layout every run — helps
# cache hits under the hood.
_PREFS_PROMPT_KEYS = (
    "keywords",
    "title_must_match",
    "title_exclude",
    "exclude_keywords",
    "exclude_companies",
    "locations",
    "remote",
    "seniority",
    "salary_min_usd",
    "drop_if_salary_unknown",
    "language",
    "max_age_hours",
    "free_text",
)


def _prefs_for_prompt(projected_prefs: dict | None) -> dict:
    """Reduce a projected prefs dict to the keys the prompt actually uses.

    Missing/empty slots are preserved as their canonical sentinels so the
    model can see "no opinion" explicitly. An empty list is not the same
    signal as the field being absent.
    """
    p = projected_prefs or {}
    out: dict = {}
    for k in _PREFS_PROMPT_KEYS:
        v = p.get(k)
        if k in {"remote", "seniority"}:
            out[k] = (v or "any")
        elif k == "language":
            out[k] = (v or "")
        elif k == "free_text":
            out[k] = (v or "")[:500]
        elif k in {"salary_min_usd", "max_age_hours"}:
            try:
                out[k] = int(v or 0)
            except (TypeError, ValueError):
                out[k] = 0
        elif k == "drop_if_salary_unknown":
            out[k] = bool(v)
        else:
            out[k] = list(v) if isinstance(v, list) else []
    return out


def _enrich_one_chunk(
    chunk: list[Job],
    resume_text: str,
    timeout_s: int,
    prefs_for_prompt: dict,
) -> dict[str, dict]:
    briefs = [_job_to_brief(j) for j in chunk]
    prompt = _PROMPT.format(
        resume=(resume_text or "")[:12000],
        prefs_json=json.dumps(prefs_for_prompt, ensure_ascii=False),
        jobs_json=json.dumps(briefs, ensure_ascii=False),
    )
    # Smallest Claude model for matching — operator instruction. Every source
    # (LinkedIn, HN, remote boards, curated boards, web_search) flows through
    # this one call, so cheapest tier is the whole point.
    stdout = run_p(prompt, timeout_s=timeout_s, model=SMALLEST_MODEL)
    if not stdout:
        log.warning("enrich_jobs_ai: CLI unavailable — skipping enrichment for %d jobs", len(chunk))
        return {}
    body = extract_assistant_text(stdout)
    data = parse_json_block(body)
    if not isinstance(data, dict):
        log.error("enrich_jobs_ai: response wasn't a JSON object (head=%r)", body[:200])
        return {}
    results = data.get("results")
    if not isinstance(results, list):
        log.error("enrich_jobs_ai: response missing `results` list")
        return {}

    valid_ids = {j.external_id for j in chunk}
    out: dict[str, dict] = {}
    for r in results:
        if not isinstance(r, dict):
            continue
        ext_id = str(r.get("id") or "").strip()
        if not ext_id or ext_id not in valid_ids:
            continue
        out[ext_id] = {
            "match_score": _normalize_score(r.get("match_score")),
            "why_match": fix_mojibake(str(r.get("why_match") or "").strip())[:280],
            "key_details": _normalize_details(r.get("key_details")),
        }
    log.info("enrich_jobs_ai: enriched %d/%d jobs", len(out), len(chunk))
    return out


# ---------------------------------------------------------------------------
# Convenience: convert a job-id → enrichment map keyed by Job.job_id (sha)
# ---------------------------------------------------------------------------

def by_job_id(enrichments: dict[str, dict], jobs: list[Job]) -> dict[str, dict]:
    """Re-key an `enrichments` map (keyed by external_id) by Job.job_id.

    Useful when callers store the enrichment alongside data already keyed by
    the dedupe hash.
    """
    ext_to_job = {j.external_id: j for j in jobs}
    out: dict[str, dict] = {}
    for ext_id, enr in enrichments.items():
        j = ext_to_job.get(ext_id)
        if j:
            out[j.job_id] = enr
    return out
