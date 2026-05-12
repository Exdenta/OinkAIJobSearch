"""AI-driven job matching (the sole matching gate, algorithm v2).

For every new posting from every source we ask the `claude` CLI (Haiku)
to decide fit against the candidate's RESUME and PREFS. v2 changes vs v1:

  * inputs are RAW TEXT — both `resume.txt` (verbatim resume body) and
    `prefs.txt` (user's verbatim preferences plus any 'not a fit'
    comments appended after they pressed the skip button). No projected
    prefs JSON, no structured profile fields. The model extracts
    constraints (locations, language, seniority, exclusions) from the
    natural-language prefs text directly.
  * outputs add an explicit `why_mismatch` field alongside `why_match`,
    so the digest can render BOTH "what aligns" and "what doesn't" — the
    user has been asking for this signal since the algorithm-v1 reviews.
  * single-pass Haiku by default (no Sonnet two-pass). Operator can flip
    `defaults.ai_two_pass=True` if/when triage proves noisy again.

Per-batch verdict shape:

  - match_score:   integer 0-5
                     0 = clearly wrong fit (wrong stack, seniority, location,
                         language, or something the user explicitly excluded).
                         Callers drop score 0.
                     1 = poor
                     2 = weak
                     3 = OK / acceptable stretch
                     4 = strong fit
                     5 = perfect fit
  - why_match:     1-2 sentences naming SPECIFIC overlaps (resume tech
                    that the posting names, location matches, etc.)
  - why_mismatch:  1-2 sentences naming SPECIFIC misalignments. Empty
                    string only when the posting is a clear 5/5 fit.
                    For score=0, this is where the rejection reason goes.
  - key_details:   {stack, seniority, remote_policy, location, salary,
                    visa_support, language, standout}

Design decisions:
  - This is now the ONLY matching gate. The old keyword/regex post_filter in
    search_jobs.py has been neutered — Claude holistically decides using the
    resume + the user's preference dict. The legacy keyword/title/locations
    fields (formerly in `config/filters.yaml`, now removed) are NOT consulted.
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

from claude_cli import (
    extract_assistant_text, parse_json_block, SMALLEST_MODEL, MID_MODEL,
)
from instrumentation.wrappers import wrapped_run_p
from dedupe import Job
from text_utils import fix_mojibake

log = logging.getLogger(__name__)


# Per-batch failure reasons surfaced in forensic logs and at the call site.
# Kept as plain string sentinels (not Enum) so they appear verbatim in the
# JSONL forensic stream and are grep-friendly.
_BATCH_OK = "ok"
_BATCH_CLI_MISSING = "cli_missing"      # wrapped_run_p returned None
_BATCH_EMPTY_RESULT = "empty_result"    # CLI envelope had result="" — Haiku produced no JSON
_BATCH_PARSE_ERROR = "parse_error"      # body wasn't a JSON object / no `results` list
_BATCH_PARTIAL = "partial"              # results parsed but fewer verdicts than postings sent


_PROMPT = """You are a careful job-match analyst working for ONE candidate.

You are the SOLE gate deciding whether a posting should be shown to this
candidate. There are no keyword pre-filters upstream — every posting that
reaches you came straight from a public source (LinkedIn, HackerNews "Who is
hiring", remoteok, remotive, weworkremotely, curated remote boards, and
open-web search results). So you must:

  - REJECT postings whose role, stack, seniority, location, language, or
    work arrangement clearly contradict the candidate's preferences.
    Signal this by scoring 0.
  - Actively evaluate fit against BOTH the RESUME and the PREFS text.
    PREFS is how the candidate told the bot what they want; it is
    authoritative. RESUME describes what they CAN do; PREFS describes
    what they WANT.

STEP 0 — extract constraints from PREFS (and RESUME where PREFS is silent).
The PREFS block is plain text — it may be a paragraph, a bullet list, or
a paragraph followed by a `[Recent 'not a fit' comments]` block of
appended skip-reasons. Read it ALL and infer:

  * `onsite_locations` — cities / regions where the candidate accepts
    ONSITE or HYBRID work. STRICT: only what the user named or what
    obviously contains their residence. NO macro-region expansion at
    this step ("EU", "Europe", "EMEA" are NOT valid onsite locations).
  * `remote_regions` — countries / macro-regions where the candidate
    accepts FULLY-REMOTE work. Macro tokens "EU", "Europe", "EMEA",
    "North America", "LATAM", "APAC" are valid here.
  * `time_zone_band` — the UTC band the candidate works in (e.g.
    "UTC-1..UTC+3" for an EU-based candidate). Infer from residence
    when the prefs text doesn't state one.
  * `target_levels` — junior / mid / senior / lead / staff / principal.
    Infer from prefs phrasing ("mid-level", "no senior", etc.).
  * `years_experience` — count from the resume (most recent N years).
  * `language` — working language(s) named or inferred from resume.
    Default English when the resume is in English and prefs are silent.
  * `title_exclude` — title tokens the candidate explicitly rejected
    in PREFS (e.g. "no senior", "no manager", "rather than senior or
    managerial tracks"). Be literal: include ONLY tokens the user
    actually named. Do not pad.
  * `body_exclude` — body tokens the user vetoed (stacks, language
    requirements like "no German", topics).
  * `company_exclude` — company names the user explicitly excluded.

If the candidate's `[Recent 'not a fit' comments]` mention specific
patterns ("not a fit: senior", "not a fit: requires German", "remote
US only", "no Solidity"), fold those into the relevant veto / location
list. Treat skip-reasons as authoritative additions to the user's
stated prefs.

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

     HARD VETO RULES (apply BEFORE the penalty math; if any fire, score=0
     and skip the rest of the penalty pipeline):
       (V1) If posting title contains, case-insensitive whole-word, ANY
            entry from `title_exclude` → score=0.
       (V2) If posting body OR title contains, case-insensitive whole-
            word, ANY entry from `exclude_keywords` → score=0.
       (V3) If posting company name (case-insensitive) is in
            `exclude_companies` → score=0.
       (V4) LOCATION HARD VETO — see two-axis logic below. ANY location
            mismatch is score=0, no penalty math, no second chance.
       For these vetoes, `why_mismatch` MUST cite the matched reason,
       e.g. "title-exclude hit: 'staff'", "body-exclude hit: 'german'",
       "off-onsite: Madrid not in [bilbao, basque, euskadi]".

     LOCATION HARD VETO (V4) — TWO geo lists in PREFS, each with
     different match semantics:
       * `onsite_locations`: STRICT, narrow expansion only.
            "bilbao" ↔ "basque country", "euskadi", "bizkaia",
            "greater bilbao", "bilbao metropolitan area".
            "london" ↔ "greater london".
            DO NOT expand to country / macro-region. Bilbao does NOT
            match Madrid; Madrid does NOT match Spain (it IS in Spain
            but Madrid is not Bilbao).
       * `remote_regions`: macro-region-aware, country/macro expansion:
            "europe" / "eu" → all EU member states + UK + Norway +
                              Switzerland + Iceland + Liechtenstein.
            "emea"          → Europe + Middle East + Africa.
            "north america" → US + Canada + Mexico.
            "latam"         → Mexico + Central + South America.
            "apac"          → Asia-Pacific.
            "anywhere" / "global" / "worldwide" → any country.

     Apply by `remote_policy`:
       - posting `remote_policy` = onsite OR hybrid:
            * Posting MUST name a city/region matching `onsite_locations`
              (verbatim or via the narrow expansion above).
            * If it does NOT match → SCORE = 0. NEVER fall back to
              `remote_regions` for hybrid/onsite postings. Madrid,
              Barcelona, Berlin, Paris all VETO when onsite_locations
              is ["bilbao","basque","euskadi"] — even though some of
              them sit inside the candidate's `remote_regions`.
       - posting `remote_policy` = remote (fully):
            * If posting location is country-tagged (e.g. "Remote ·
              Spain", "Remote — Germany"): country MUST be in
              `remote_regions` verbatim or via macro expansion. If
              not → SCORE = 0.
            * Fully un-tagged remote ("Remote", "Anywhere") → assume
              the candidate's TZ band; pass the V4 check; let the TZ
              penalty handle further filtering.
       - `remote_policy` unknown / not stated:
            * If posting names a city/country, treat as onsite for V4.
            * If posting offers no geo signal at all → pass V4.

     If `onsite_locations` AND `remote_regions` are both empty in PREFS
     (the candidate stated no geographical preference) → V4 does not
     fire.

     Examples:
       - Candidate onsite_locations=["bilbao","basque","euskadi"],
         remote_regions=["spain","europe","eu","emea"].
         * Posting: onsite Madrid → Madrid ∉ onsite_locations → SCORE=0.
         * Posting: hybrid Madrid → Madrid ∉ onsite_locations → SCORE=0.
           DO NOT pass on the basis of "Madrid is in Spain, Spain is in
           remote_regions" — those rules are for fully-remote postings,
           not hybrid.
         * Posting: hybrid Bilbao → match → V4 passes.
         * Posting: hybrid Berlin → Berlin ∉ onsite_locations → SCORE=0.
         * Posting: remote · Germany → Germany ∈ europe (macro) → V4 OK.
         * Posting: remote · USA → USA ∉ remote_regions → SCORE=0.
         * Posting: fully remote, no country tag → V4 OK.

     TIME-ZONE PENALTY (remote postings only):
       - Inputs: candidate `time_zone_band` (e.g. "UTC-1..UTC+3"),
         posting TZ requirement.
       - Identify posting TZ requirement from body: explicit ranges
         ("UTC+0 to UTC+4", "Eastern Time", "PST hours"), country tags
         on remote ("Remote · Germany" → CET ≈ UTC+1; "Remote · USA"
         → UTC-8..UTC-5), or "must overlap N hours with X timezone".
       - If candidate band and posting band have ZERO overlap →
         SUBTRACT 2.
       - If overlap < 4 hours → SUBTRACT 1.
       - If overlap ≥ 4 hours OR posting accepts any TZ OR posting TZ
         is unstated → no penalty.
       - Skip entirely if `time_zone_band` is empty.

     LANGUAGE PENALTY (per-level CEFR gap): subtract 1 point per CEFR
     level the candidate is BELOW the posting's required level for the
     working language. Floor at 0.

     CEFR ordering (numeric, for the gap math):
       A1=1, A2=2, B1=3, B2=4, C1=5, C2=6, Native=7
     gap = max(0, required_level - candidate_level)
     subtract = gap   (so each level below the bar costs 1 point)

     Identifying the REQUIRED language + level for a posting:
       - Look at posting body/title: "all classes taught in French",
         "fluency in German required", "C2-level Spanish", "must be
         native-level Polish speaker", "Spanish-language working
         environment", or the posting is itself written entirely in a
         non-English language as the working language of the role.
       - If the posting names a language but no explicit level:
           * "fluent" / "fluency" / "native" / "near-native"  → C2
           * "professional" / "professional working"          → C1
           * "advanced"                                       → C1
           * "working knowledge" / "conversational"           → B2
           * "basic"                                          → A2
           * If the posting is BODY-LANGUAGE in non-English (e.g. a
             Spanish-language listing) → treat as C1 required for that
             language.
           * If posting only says "English required" with no level → C1.
       - If posting is multilingual ("English OR French"): pick the
         language with the SMALLEST gap for the candidate (best of).
       - If posting language requirement is genuinely ambiguous, do not
         apply the language penalty.

     Identifying the candidate's LEVEL for that language:
       - Read the resume's languages section / inline language mentions.
         Recognise CEFR markers (A1/A2/B1/B2/C1/C2), as well as
         "native", "bilingual", "fluent" (= C2), "professional"/"working
         proficiency" (= C1), "intermediate" (= B1), "basic" (= A2),
         and explicit certificate names (DELF C1, Goethe B2, etc.).
       - The candidate's `language` field in preferences, if non-empty,
         lists the language(s) they prefer to work in — assume C2 for
         each unless the resume says otherwise.
       - If the candidate did NOT list the required language at all,
         treat their level as A1 (numeric 1).
       - English: if the resume is written in English OR lists English
         anywhere, assume at least B2 unless the resume's languages
         section gives a higher explicit level.

     Examples:
       - Posting "All classes taught in French (C2 required)". Resume:
         "French — A2". A2=2, C2=6 → gap=4 → SUBTRACT 4.
       - Posting "fluent German required". Resume: "German — B1". Implied
         level C2=6, candidate B1=3 → gap=3 → SUBTRACT 3.
       - Posting "C1 English required". Resume: "English — C2".
         C2≥C1 → gap=0 → no penalty.
       - Spanish-language posting (no English mentioned). Resume lists
         no Spanish at all → A1=1 vs implied C1=5 → gap=4 → SUBTRACT 4.
       - Posting "English OR French at professional level". Candidate:
         "English — C2, French — A2". English path: C2 vs C1 → gap=0;
         French path: A2 vs C1 → gap=3. Take min = 0 → no penalty.
       - Posting "native-level Polish". Resume has no Polish → A1=1
         vs Native=7 → gap=6 → SUBTRACT 6 (floor at 0 still applies).

     SENIORITY PENALTY (per-level gap above target): subtract 1 point
     per seniority step the posting sits ABOVE the candidate's target
     level. Floor at 0. STRICTLY ONE-DIRECTIONAL — postings BELOW the
     candidate's target are NOT penalized here UNLESS the candidate's
     PREFS text explicitly excludes lower-seniority roles.

     Default rule for lower-than-target postings:
       Treat junior / mid / associate / entry-level postings as a
       perfectly acceptable seniority fit for a senior candidate (no
       penalty). Many strong candidates intentionally apply downward
       to switch domain / company / stack — the bot does not second-
       guess that.

     ONLY downgrade lower-than-target postings when PREFS explicitly
     rejects them. Signals to look for inside the PREFS block:
       * `title_exclude` tokens like "junior", "entry-level", "intern".
       * Free-text vetoes: "NOT a fit: junior / mid level", "senior or
         above only", "no associate roles", "must be senior".
       * Skip-feedback comments (under `[Recent 'not a fit' comments]`)
         that name the same — "not a fit: too junior".
     When such a signal IS present, subtract 1 point per level BELOW
     target (mirrors the above-target rule, but conditional). When NO
     such signal is present, lower-than-target postings stay at the
     stack/role base score.

     Seniority ordering (numeric, for the gap math):
       intern/internship       = 0
       junior / entry-level    = 1
       associate               = 2
       mid / middle / regular  = 3
       senior                  = 4
       lead / staff            = 5
       principal               = 6
       director                = 7
       vp / head / chief / c-level = 8
     gap = max(0, posting_level - candidate_target_level)
     subtract = gap

     Identifying the candidate's TARGET level:
       - Read `target_levels` from the preferences block. If it lists
         multiple, take the HIGHEST listed (e.g. ["mid","middle"] → 3).
       - If empty or "any" → no penalty.

     Identifying the posting's seniority level:
       - Title prefix is the strongest signal: "Senior Frontend" → 4,
         "Staff Engineer" → 5, "Principal Engineer" → 6, "Lead Frontend"
         → 5, "Director of Engineering" → 7, "VP Engineering" → 8.
       - "Junior" / "Entry" / "Associate" titles map per the table.
       - If title has no seniority prefix and the body says "5+ years
         required" → senior (4); "3-5 years" → mid (3); "8+" → staff/
         lead (5); "10+" / "principal" → 6.
       - If posting genuinely doesn't signal level (no title prefix,
         no years requirement, no level callout) → assume mid (3) and
         apply no penalty if candidate target ≥ mid.

     Examples:
       - Candidate target=mid (3). Posting "Senior Frontend Engineer"
         → senior=4 → gap=1 → SUBTRACT 1.
       - Candidate target=mid (3). Posting "Staff Software Engineer"
         → staff=5 → gap=2 → SUBTRACT 2.
       - Candidate target=mid (3). Posting "Principal Engineer"
         → principal=6 → gap=3 → SUBTRACT 3.
       - Candidate target=mid (3). Posting "Frontend Developer
         (3-5 years)" → mid=3 → gap=0 → no penalty.
       - Candidate target=mid (3). Posting "Junior React Developer"
         → junior=1 → gap=0 (one-directional, below target ignored).
       - Candidate target=senior (4). Posting "Staff Engineer"
         → staff=5 → gap=1 → SUBTRACT 1.

     PER-SKILL YEARS-EXPERIENCE PENALTY: subtract 1 point per FULL year
     of experience the candidate is MISSING on whatever specific skill
     the posting demands the most. STRICTLY ONE-DIRECTIONAL — postings
     demanding LESS experience than the candidate has are NEVER
     penalized here. A senior candidate scoring against a "2+ years
     React" posting gets zero penalty from this rule. Apply the
     SENIORITY PENALTY's "lower-than-target only if PREFS excludes it"
     guard as a tiebreaker if you're tempted to downgrade.

     The PENALTY IS PER-SKILL, not per-role. Read the posting's
     requirements carefully and identify the largest individual skill
     gap. Examples of what counts as a "skill" for this rule: a
     programming language ("5+ years Python"), a framework ("4+ years
     React"), a tool ("3+ years Kubernetes"), a domain ("5+ years
     financial services"). Total years-of-experience requirements
     ("8+ years software engineering experience") also count as a
     skill — the skill is "software engineering" itself.

     For each skill the posting names with a years requirement:
       1. Pull the posting's MINIMUM required years for that skill
          (lower bound on a range; the bare number on "X+ years").
       2. Find the candidate's years on that SAME skill from the
          RESUME — count distinct role-years that explicitly list the
          skill. If the resume doesn't list the skill at all, treat
          the candidate's count as 0.
       3. gap = max(0, required_years - candidate_skill_years).

     Take the BIGGEST gap across all stated skills, and SUBTRACT that
     many points. Floor at 0.

     Examples:
       - Candidate resume shows 3 years React, 3 years TypeScript.
         Posting "5+ years frontend JavaScript developer" → JS skill
         requires 5y, candidate has ~3y JS → gap=2 → SUBTRACT 2.
       - Candidate has 3 years React, 1 year Vue.
         Posting "3+ years React, 5+ years Vue" → React gap=0,
         Vue gap=4 → biggest gap=4 → SUBTRACT 4.
       - Candidate has 8 years Python, 0 years Rust.
         Posting "5+ years Python, 2+ years Rust" → Python gap=0,
         Rust gap=2 → biggest gap=2 → SUBTRACT 2.
       - Candidate has 3y total experience.
         Posting "8+ years software engineering" → gap=5 → SUBTRACT 5.
         (Floor at 0 still applies.)
       - Posting names no specific years requirements → skip the rule.

     The `why_mismatch` field MUST cite the gap explicitly when this
     rule fires, e.g. "5+ years React required vs candidate 3y → -2".

     Order of application:
       1. HARD VETOES (V1 title-exclude, V2 body-exclude, V3 company-
          exclude, V4 LOCATION) — if ANY hits → score = 0, return.
       2. Role/stack base score (1-5).
       3. TIME-ZONE PENALTY (-2 zero overlap, -1 < 4h overlap).
       4. LANGUAGE PENALTY (-1 per CEFR level the candidate is below
          the posting's required level for the working language).
       5. SENIORITY PENALTY (-1 per level the posting is above the
          candidate's target levels).
       6. PER-SKILL YEARS PENALTY (-1 per year of the BIGGEST gap on
          any specific skill the posting demands).
       7. Final score: floor at 0, ceiling at 5.

  2. Write `why_match`: ONE or TWO sentences, max 240 chars, that name
     specific overlaps with this candidate's resume AND preferences (e.g.
     "React + TS + Storybook overlap; Bilbao remote-friendly, matches
     user's EU remote ask"). DO NOT write generic filler like "great
     frontend role". For score-0 rejects this should be empty or terse.

  3. Write `why_mismatch`: ONE or TWO sentences, max 240 chars, naming
     the SPECIFIC misalignments — penalties that fired, constraints that
     missed, gaps the candidate would have to bridge. Examples:
       "Senior level (1 above mid target); office in Munich is outside
        candidate's onsite cities (-3)."
       "Working language German; resume shows English/Russian only,
        CEFR gap 4."
     For score=0 (HARD VETO), this MUST cite the matched token, e.g.
     "title-exclude hit: 'staff'", "body-exclude hit: 'german'",
     "company-exclude hit: 'Acme Corp'".
     For a clean 5/5 fit, may be empty string ("").

  4. Extract `key_details` from the posting (use "" for fields not stated):
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
    "why_mismatch": "...",
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

=== CANDIDATE RESUME (plain text, verbatim) ===
{resume}

=== CANDIDATE PREFS (plain text, verbatim — may include a [Recent 'not a fit' comments] block) ===
{prefs_text}

=== JOBS (JSON array) ===
{jobs_json}
""".strip()


def _job_to_brief(j: Job) -> dict[str, str]:
    """Compact representation we hand to the model.

    `snippet` is the full posting body when the source adapter fetched
    a detail page (algorithm v2.2 — Option 4). Cap raised to 4000 chars
    to actually surface what the adapter captured; Sonnet handles the
    larger prompt comfortably at batch=10. Older sources that only emit
    listing-card snippets stay well under this cap, so it's a no-op for
    them.
    """
    return {
        "external_id": j.external_id,
        "title":   (j.title or "")[:200],
        "company": (j.company or "")[:120],
        "location": (j.location or "")[:120],
        "salary":  (j.salary or "")[:120],
        "url":     (j.url or "")[:400],
        "snippet": (j.snippet or "").replace("\n", " ")[:4000],
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
    prefs_text: str = "",
    timeout_s: int = 240,
    max_jobs_per_call: int = 10,
    *,
    two_pass: bool = False,
    triage_floor: int = 2,
    workers: int = 4,
) -> dict[str, dict]:
    """Return a {external_id → enrichment dict} map.

    enrichment dict has shape:
        {"match_score": int 0-5, "why_match": str, "why_mismatch": str,
         "key_details": {...}}

    Algorithm-v2 inputs are plain text — `resume_text` is the verbatim
    resume body, `prefs_text` is the user's verbatim PREFS file
    (preferences plus appended skip-reasons). The model extracts
    structured constraints (locations, language, seniority, exclusions)
    from those blobs directly.

    Args:
      jobs:            Postings to score.
      resume_text:     Verbatim resume body (`state/users/<id>/resume.txt`).
      prefs_text:      Verbatim PREFS body (`state/users/<id>/prefs.txt`),
                       including any `[Recent 'not a fit' comments]` block.
                       May be empty — the model then scores against the
                       resume alone.
      timeout_s:       Per-CLI-call timeout. The caller passes the full
                       batch timeout; individual chunks share it.
      max_jobs_per_call: Chunk size. v2 default is 5 — small chunks keep
                       Haiku's prompt window comfortable and let split-
                       retry recover individual misses cheaply.
      two_pass:        Off by default in v2. When True, runs a Sonnet
                       re-score on Haiku survivors at `triage_floor`.
                       Operator can flip this on if Haiku noise returns.
      triage_floor:    Only meaningful when `two_pass=True`.

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

    prefs_text = (prefs_text or "").strip()

    # Algorithm v2.1: scoring runs on Sonnet by default. Haiku was too
    # noisy at the 4-vs-3 boundary (missed years-experience + onsite-
    # location vetoes consistently). Sonnet's prompt-following is the
    # quality gate. Operators can fall back to Haiku via env override on
    # CLAUDE_MID_MODEL/SMALLEST_MODEL.
    primary_model = MID_MODEL
    primary_label = "sonnet"
    haiku_out = _enrich_pool(
        jobs, resume_text, prefs_text, timeout_s,
        max_jobs_per_call, model=primary_model, pass_label=primary_label,
        workers=workers,
    )

    if not two_pass:
        return haiku_out

    survivors: list[Job] = [
        j for j in jobs
        if int((haiku_out.get(j.external_id) or {}).get("match_score") or 0)
           >= triage_floor
    ]
    if not survivors:
        log.info("enrich_jobs_ai: two-pass — 0 survivors at triage_floor=%d",
                 triage_floor)
        return haiku_out

    log.info(
        "enrich_jobs_ai: two-pass — %d/%d jobs survived Haiku triage "
        "(floor=%d), re-scoring with %s",
        len(survivors), len(jobs), triage_floor, MID_MODEL,
    )
    sonnet_out = _enrich_pool(
        survivors, resume_text, prefs_text, timeout_s,
        max_jobs_per_call, model=MID_MODEL, pass_label="sonnet",
        workers=workers,
    )

    # Merge: Sonnet overwrites Haiku where present, else Haiku stands.
    merged = dict(haiku_out)
    upgrades = downgrades = unchanged = 0
    for ext_id, sonnet_v in sonnet_out.items():
        prev = (haiku_out.get(ext_id) or {}).get("match_score") or 0
        new = (sonnet_v or {}).get("match_score") or 0
        merged[ext_id] = sonnet_v
        if new > prev:
            upgrades += 1
        elif new < prev:
            downgrades += 1
        else:
            unchanged += 1
    log.info(
        "enrich_jobs_ai: two-pass merge — upgrades=%d downgrades=%d unchanged=%d "
        "(survivors not re-scored: %d)",
        upgrades, downgrades, unchanged, len(survivors) - len(sonnet_out),
    )
    return merged


def _enrich_pool(
    jobs: list[Job],
    resume_text: str,
    prefs_text: str,
    timeout_s: int,
    max_jobs_per_call: int,
    *,
    model: str,
    pass_label: str,
    workers: int = 4,
) -> dict[str, dict]:
    """Run one model over a job pool, batching as needed. Returns the
    {external_id → enrichment} map. Used for both single-pass Haiku and
    the optional two-pass Sonnet re-score.

    Batches dispatch concurrently via a thread pool (`workers` knob).
    `wrapped_run_p` records each call into `claude_calls` via fresh
    short-lived sqlite3 connections (DB._conn opens a new connection per
    call) and `forensic.log_step` is thread-safe, so concurrent dispatch
    is safe. Pass `workers=1` to fall back to serial dispatch.
    """
    chunks: list[list[Job]] = [
        jobs[start:start + max_jobs_per_call]
        for start in range(0, len(jobs), max_jobs_per_call)
    ]
    total_batches = len(chunks)

    def _run(idx_chunk):
        idx, chunk = idx_chunk
        return _enrich_one_chunk(
            chunk, resume_text, prefs_text, timeout_s,
            batch_idx=idx, total_batches=total_batches,
            allow_split_retry=True,
            model=model, pass_label=pass_label,
        )

    out: dict[str, dict] = {}
    failed_batches = 0
    if workers <= 1 or total_batches <= 1:
        for ic in enumerate(chunks, start=1):
            verdicts, reason = _run(ic)
            out.update(verdicts)
            if reason != _BATCH_OK:
                failed_batches += 1
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        log.info(
            "enrich_jobs_ai[%s]: dispatching %d batches across %d workers",
            pass_label, total_batches, workers,
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_run, ic)
                       for ic in enumerate(chunks, start=1)]
            for fut in as_completed(futures):
                try:
                    verdicts, reason = fut.result()
                except Exception:
                    log.exception("enrich_jobs_ai[%s]: worker raised", pass_label)
                    failed_batches += 1
                    continue
                out.update(verdicts)
                if reason != _BATCH_OK:
                    failed_batches += 1
    if failed_batches:
        log.warning(
            "enrich_jobs_ai[%s]: %d/%d batch(es) failed silently — verdicts only "
            "from %d/%d batches",
            pass_label, failed_batches, total_batches,
            total_batches - failed_batches, total_batches,
        )
    return out


# Cap on raw resume / prefs blobs the prompt receives. Resume can be
# multi-page; cap at 12 KiB. PREFS is shorter (verbatim user description
# plus appended skip-reasons) — 4 KiB matches the storage cap in
# `user_files.py`.
_MAX_RESUME_PROMPT_CHARS = 12000
_MAX_PREFS_PROMPT_CHARS = 4000


def _enrich_one_chunk(
    chunk: list[Job],
    resume_text: str,
    prefs_text: str,
    timeout_s: int,
    *,
    batch_idx: int = 1,
    total_batches: int = 1,
    allow_split_retry: bool = True,
    _retry_depth: int = 0,
    model: str = SMALLEST_MODEL,
    pass_label: str = "haiku",
) -> tuple[dict[str, dict], str]:
    """Enrich one chunk. Returns (verdicts_by_external_id, failure_reason).

    failure_reason is one of:
      _BATCH_OK            — all postings got a verdict back
      _BATCH_CLI_MISSING   — CLI unavailable / wrapped_run_p returned None
      _BATCH_EMPTY_RESULT  — CLI succeeded but result text was empty (Haiku
                             produced 0 chars of assistant text — the exact
                             failure mode that lost 25 jobs in pipeline_run #12)
      _BATCH_PARSE_ERROR   — response didn't parse as a JSON object with `results`
      _BATCH_PARTIAL       — results parsed but fewer verdicts than postings

    On failure (_CLI_MISSING / _EMPTY_RESULT / _PARSE_ERROR), if
    `allow_split_retry` is True and the chunk has >=2 jobs, we split it in
    half and retry each half once. This gives us a cheap second chance at
    the model: a 25-job prompt is most likely to hit Haiku's empty-output
    glitch, two 12-job prompts are far less so.

    On _BATCH_PARTIAL (Haiku returned valid JSON but skipped some IDs —
    seen consistently on full 25-job batches in the 2026-05-02 cron run,
    one missing verdict per user per batch 4/5), we do a TARGETED re-ask:
    we build a fresh prompt containing only the missing jobs and call
    once more. Cheaper than splitting (we don't re-score the 24 we got)
    and recovers what's actually a flaky-LLM omission rather than a
    prompt-size problem.

    All retries are bounded to a single attempt (_retry_depth==0 → 1) so
    a systematic failure can't fan out forever.
    """
    briefs = [_job_to_brief(j) for j in chunk]
    prompt = _PROMPT.format(
        resume=(resume_text or "")[:_MAX_RESUME_PROMPT_CHARS],
        prefs_text=(prefs_text or "")[:_MAX_PREFS_PROMPT_CHARS],
        jobs_json=json.dumps(briefs, ensure_ascii=False),
    )
    # Two-pass scoring: Haiku triage by default, Sonnet for the re-score on
    # survivors. Caller threads `model` + `pass_label` so each batch's
    # forensic line carries the model used (handy when comparing pass
    # outcomes post-hoc).
    caller = f"job_enrich:{pass_label}"
    stdout = wrapped_run_p(None, caller, prompt, timeout_s=timeout_s, model=model)

    reason = _BATCH_OK
    out: dict[str, dict] = {}
    body_head = ""

    if stdout is None:
        reason = _BATCH_CLI_MISSING
        log.warning(
            "enrich_jobs_ai: batch %d/%d CLI unavailable — %d jobs at risk",
            batch_idx, total_batches, len(chunk),
        )
    else:
        body = extract_assistant_text(stdout)
        body_head = (body or "")[:200]
        # extract_assistant_text falls back to the raw envelope when no
        # non-empty result/content/text/message field is present, so we
        # also need the explicit "envelope.result was empty" check.
        # That is the failure mode observed in claude_calls #23 / run #12.
        is_envelope_empty = _is_empty_result_envelope(stdout)
        if is_envelope_empty:
            reason = _BATCH_EMPTY_RESULT
            log.error(
                "enrich_jobs_ai: batch %d/%d returned empty result text — "
                "%d jobs at risk (head=%r)",
                batch_idx, total_batches, len(chunk), body_head,
            )
        else:
            data = parse_json_block(body)
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                reason = _BATCH_PARSE_ERROR
                log.error(
                    "enrich_jobs_ai: batch %d/%d response missing `results` list "
                    "(head=%r)",
                    batch_idx, total_batches, body_head,
                )
            else:
                valid_ids = {j.external_id for j in chunk}
                for r in data["results"]:
                    if not isinstance(r, dict):
                        continue
                    ext_id = str(r.get("id") or "").strip()
                    if not ext_id or ext_id not in valid_ids:
                        continue
                    out[ext_id] = {
                        "match_score": _normalize_score(r.get("match_score")),
                        "why_match": fix_mojibake(str(r.get("why_match") or "").strip())[:280],
                        "why_mismatch": fix_mojibake(str(r.get("why_mismatch") or "").strip())[:280],
                        "key_details": _normalize_details(r.get("key_details")),
                    }
                if len(out) < len(chunk):
                    reason = _BATCH_PARTIAL

    missing_count = len(chunk) - len(out)

    # Per-batch forensic line. One line per batch keeps the JSONL log
    # easy to scan: `grep enrich_jobs_ai.batch` shows the run's batch
    # outcomes at a glance.
    try:
        from forensic import log_step as _flog
        _flog(
            "enrich_jobs_ai.batch",
            input={
                "batch_idx": batch_idx,
                "total_batches": total_batches,
                "batch_size": len(chunk),
                "retry_depth": _retry_depth,
                "model": model,
                "pass": pass_label,
            },
            output={
                "verdicts_returned": len(out),
                "missing_count": missing_count,
                "failure_reason": reason,
                "body_head": body_head,
            },
        )
    except Exception:
        log.debug("enrich_jobs_ai.batch forensic emit failed; continuing",
                  exc_info=True)

    # Retry path: split the chunk in half and try each half once. We only
    # split-retry on failure modes that suggest a transient model/CLI issue
    # (empty / parse-error / CLI missing) — for these, a smaller prompt is
    # most likely to succeed.
    retryable = {_BATCH_CLI_MISSING, _BATCH_EMPTY_RESULT, _BATCH_PARSE_ERROR}
    if reason in retryable and allow_split_retry and len(chunk) >= 2 and _retry_depth == 0:
        mid = len(chunk) // 2
        left, right = chunk[:mid], chunk[mid:]
        log.info(
            "enrich_jobs_ai: retrying batch %d/%d (reason=%s) by splitting "
            "%d jobs into %d + %d",
            batch_idx, total_batches, reason, len(chunk), len(left), len(right),
        )
        for sub_chunk in (left, right):
            sub_out, _ = _enrich_one_chunk(
                sub_chunk, resume_text, prefs_text, timeout_s,
                batch_idx=batch_idx, total_batches=total_batches,
                allow_split_retry=False,
                _retry_depth=_retry_depth + 1,
                model=model, pass_label=pass_label,
            )
            out.update(sub_out)
        # Re-classify after retry: if the splits recovered every job we
        # promote to OK; if some are still missing we leave the original
        # reason so the orchestrator counts this batch as failed but the
        # caller still sees whatever verdicts came back.
        if len(out) >= len(chunk):
            reason = _BATCH_OK

    # Targeted re-ask path for partial batches. Empirically (forensic logs
    # 2026-05-02 cron run), Haiku consistently drops exactly 1 verdict
    # when handed full 25-job batches — every user, every batch-of-25 with
    # full payload. Splitting the whole chunk would re-spend tokens on the
    # 24 jobs we already scored; instead we re-ask ONLY for the missing
    # external_ids. Capped at 1 retry (allow_split_retry==True &&
    # _retry_depth==0) so a deterministic poison pill can't fan out.
    if (
        reason == _BATCH_PARTIAL
        and allow_split_retry
        and _retry_depth == 0
        and len(out) < len(chunk)
    ):
        missing_jobs = [j for j in chunk if j.external_id not in out]
        log.info(
            "enrich_jobs_ai: re-asking batch %d/%d for %d missing verdict(s) "
            "(targeted retry, partial batch)",
            batch_idx, total_batches, len(missing_jobs),
        )
        recovered, _ = _enrich_one_chunk(
            missing_jobs, resume_text, prefs_text, timeout_s,
            batch_idx=batch_idx, total_batches=total_batches,
            allow_split_retry=False,
            _retry_depth=_retry_depth + 1,
            model=model, pass_label=pass_label,
        )
        out.update(recovered)
        if len(out) >= len(chunk):
            reason = _BATCH_OK

    log.info(
        "enrich_jobs_ai: enriched %d/%d jobs (batch %d/%d, reason=%s)",
        len(out), len(chunk), batch_idx, total_batches, reason,
    )
    return out, reason


def _is_empty_result_envelope(stdout: str) -> bool:
    """True iff the CLI envelope JSON has `result=""` (and no fallback text).

    Mirrors `extract_assistant_text` but explicitly returns True when every
    candidate field is absent or empty — the case where the assistant
    produced no usable text. Conservative: any parse failure returns False
    so callers fall through to the parse_json_block path.
    """
    s = (stdout or "").strip()
    if not s:
        return False
    try:
        envelope = json.loads(s)
    except json.JSONDecodeError:
        return False
    if not isinstance(envelope, dict):
        return False
    for key in ("result", "content", "text", "message"):
        val = envelope.get(key)
        if isinstance(val, str) and val.strip():
            return False
    return True


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
