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


_PROMPT = """OUTPUT STYLE — caveman english
==============================
For every natural-language field you emit (especially `why_match` and
`key_details.standout`): drop articles (a/an/the), filler ("just",
"really", "actually", "simply", "basically", "essentially"),
pleasantries, hedging. Fragments OK. Use short synonyms ("fix" not
"implement a solution for"; "big" not "extensive"; "use" not
"leverage"; "show" not "demonstrate"). Technical terms stay exact.
DO NOT apply this style to: numbers, code, URLs, JSON keys, schema
field names, enum values, IDs, raw extracted titles, company names,
or location strings — those pass through verbatim.

You are a careful job-match analyst working for ONE candidate.

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

═══ TOP-LEVEL SCORING DOCTRINE — read before applying any rule ═══

These three doctrines override anything below if they conflict.

DOCTRINE A — NEVER penalize "overqualification". Anywhere.
  Forbidden why_mismatch phrasings (drop them; do not subtract):
    × "candidate is overqualified for this junior role"
    × "X years experience exceeds Y-year requirement"
    × "salary undervalues the candidate"
    × "below market", "compensation too low"
    × "senior candidate vs mid posting"
    × "candidate's tenure exceeds the role bar"
  Good example — Senior candidate sees a Junior React role:
    why_match: "React stack matches; remote EU; English."
    why_mismatch: ""        ← LEAVE EMPTY when only delta is downward
    score: base score, no penalty. NO subtraction.

DOCTRINE B — NEVER stack penalties on the SAME underlying fact.
  Common stacked-penalty bugs (caught by audit 2026-05-15):
    × "Senior title → -1 (seniority)  AND  5+ years → -1 (years)"
       Both penalize the same upward gap. Apply ONLY ONE.
    × "Junior title → -1 below-target  AND  2y required → -1 years
       gap below candidate." Apply NEITHER (Doctrine A).
  Rule: if SENIORITY PENALTY fires from the title, the PER-SKILL
  YEARS PENALTY for the same skill/role does NOT also fire. The two
  penalties exist to catch DIFFERENT signals (title vs body), not to
  double-tax the same gap.

DOCTRINE C — Generic "<Stack> Developer (Remote)" titles whose BODY
describes data labeling / LLM evaluation / AI rating tasks are NOT
frontend / backend / engineering postings. They are PROMPT-rating
gigs (BairesDev / Hire Feed / Outlier-style aggregators). Veto with
score=0 when the body shows ANY of:
    "evaluate AI/LLM responses", "rate model outputs",
    "data labeling", "training data review",
    "prompt engineering" as the JOB (not as a tool you use),
    "rate model quality", "provide human feedback for AI",
    "review LLM outputs", "rank responses", "score completions"
why_mismatch must cite: "AI-rating / data-labeling gig — not real
engineering" (with the matched phrase).

═════════════════════════════════════════════════════════════════════

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
            * RECLASSIFY-AS-HYBRID rule: if the posting describes
              itself as "remote within <one country>" AND ALSO
              imposes MANDATORY periodic in-person attendance at a
              named place (e.g. "Home based in Poland with 1-2
              in-person meetings per quarter", "Remote Germany,
              monthly team day in Berlin HQ", "Fully remote in the
              UK with quarterly on-site weeks in London", "remote
              with occasional travel to <city>"), treat the posting
              as `remote_policy = hybrid` at THAT country/city for
              V4 purposes. Apply the onsite branch of V4 — country/
              city MUST match `onsite_locations` or SCORE=0. The
              user committed to in-person at their `onsite_locations`
              cities only; cross-border travel for "occasional"
              meetings is exactly what they ruled out.
              Signals that trigger reclassification:
                * "X in-person <events> per <period>" (any frequency).
                * "monthly / quarterly / bi-annual team day(s)".
                * "occasional travel to <named place>" as a job
                  requirement, not a perk.
                * "home based in <country>, ideally <city>".
                * "remote within <country>" coupled with any of the
                  above.
              Signals that DO NOT trigger reclassification:
                * "Optional offsites", "annual company retreat
                  (travel covered, attendance encouraged)" — perk,
                  not requirement.
                * "may include occasional travel" with no fixed
                  cadence and no named hub.
                * "We are a fully distributed team" with no in-
                  person requirement.
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
         * Posting: "Home based in Poland (ideally Warsaw, flexible
           nationwide) with occasional in-person meetings (1-2 per
           quarter)" → reclassify as hybrid Poland → Poland ∉
           onsite_locations → SCORE=0. (Even though Poland ∈ EU,
           the mandatory quarterly in-person at Poland breaks the
           remote-EU branch.)
         * Posting: "Remote within Germany, monthly team day in
           Berlin office" → reclassify hybrid Berlin → Berlin ∉
           onsite_locations → SCORE=0.
         * Posting: "Fully distributed, optional annual retreat" →
           NO reclassification → stays remote · (no country) →
           V4 OK.

     V4 EXTENSIONS — also reach SCORE=0 via the LOCATION axis:

       (V4a) KNOWN-COUNTRY-RESTRICTED COMPANIES. Some companies are
       infamous for hiring in a single country/region only despite
       calling roles "remote". Treat these as `remote · <their hiring
       country>` for V4 purposes, REGARDLESS of how the posting tags
       location. The posting body / careers FAQ usually confirms this
       in a sentence like "we are unable to sponsor visas", "you must
       have authorization to work in <country>", "we only hire within
       <country/region>".
       Signals to recognise (not exhaustive — judge per posting):
         * Body explicitly says "we hire only in <country>" or
           equivalent.
         * Body explicitly says "no sponsorship", "no visa support",
           "no relocation assistance" alongside a single country tag.
         * Company is one of the well-known US-only hirers for SWE
           roles (Linear, Vanta, Stripe, Brex, Ramp, Mercury, Coda,
           Notion, Figma, Plaid, Airtable, Anthropic, OpenAI etc. —
           confirm against current posting; companies grow into new
           geos over time).
       Apply: reclassify posting as `remote · <hiring country>`.
       Country must be in `remote_regions` macro-expanded, else
       SCORE=0.

       (V4b) NO-SPONSORSHIP COUNTRY-LOCKED VETO. A posting that says
       "Remote · <country X>" + ANY of:
           "no sponsorship", "no visa sponsorship",
           "no visa support", "must have existing work authorization
           in <X>", "no relocation assistance", "we cannot relocate",
           "must reside in <X>"
       is effectively hiring ONLY people who already live + have
       work authorization in country X. Treat as effectively onsite
       country X for V4. Country X must be in `remote_regions`
       (macro-expanded) AND the candidate must plausibly reside in X
       (assume candidate resides in any of their `onsite_locations`
       countries OR Spain if onsite_locations is Spain-only). If X
       is not the candidate's residence country → SCORE=0.
       Example: candidate in Bilbao, Spain. Posting "Frontend
       Developer · Remote · Poland · no sponsorship" → X=Poland,
       candidate not resident in Poland → SCORE=0. Even though
       Poland ∈ EU, the no-sponsorship clause means the role is
       only open to existing Polish residents.

       (V4c) US-ONSITE / US-HUB-RADIUS VETO. Postings tagged
       "Remote · USA" / "US-based" / listing US cities only / "hub-
       radius hybrid" across US cities → treat as remote · USA.
       USA must be in `remote_regions` (or macro `north america`)
       OR SCORE=0.
       Additional US-onsite signals to catch:
         * "<N> US hub locations" / "must live within 50 miles of
           one of: <US cities>".
         * US state-university research centers (UCLA, Harvard,
           Stanford, NYU, MIT, USC, U-Michigan, U-Penn etc.) →
           default to US-onsite unless posting explicitly says
           "fully remote, global" or "remote within EU".
         * US federal / state agencies → US-onsite.
         * US-based non-profits with no remote callout → assume
           US-onsite.

     V4 PRECEDENCE (geography-first weighting): apply V4 (and V4a,
     V4b, V4c) BEFORE topical / stack / domain fit. NEVER let a
     strong topical match override a V4 veto. The candidate cannot
     teleport. A "perfect topical fit" in a country the candidate
     cannot work in is a 0, not a 4 or 5. Re-read the candidate's
     `onsite_locations` and `remote_regions` BEFORE deciding the
     posting's location is acceptable. When in doubt about location,
     default to VETO rather than pass — the candidate ranks 50
     fewer false-positives over 1 missed real positive.

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

     The CEFR penalty fires ONLY when the posting names a language AND
     specifies an EXPLICIT high-bar level. Vague phrasing
     ("proficient", "professional working", "advanced", "good
     communicator", "comfortable in English") is NOT enough — too many
     postings boilerplate-include those without meaning a true C1.

     Identifying the REQUIRED language + level for a posting:
       - HIGH-CONFIDENCE TRIGGERS (penalty fires):
           * Explicit CEFR letter: "C1", "C2", "B2"
             (e.g. "C2-level Spanish", "C1 German required").
           * "fluent" / "fluency" / "near-native"   → C2
           * "native" / "native speaker" / "native-level"   → C2
           * "must be native-level <lang>"          → C2
           * "all <activities> taught in <lang>"    → C2 (working
             language is that language end-to-end).
           * Posting body language is non-English AND the role
             explicitly demands the body language → C1 minimum.
       - DO NOT TRIGGER on weak signals — these stay at "no penalty":
           * "professional", "professional working proficiency",
             "advanced", "working knowledge", "conversational",
             "comfortable in", "good <lang> skills", "<lang> a plus".
           * "English required" with no level qualifier — assume B2,
             which is below C1 so no auto-C1 bar.
           * Posting NAMES a language but doesn't tie it to a
             requirement ("we work across English / Spanish teams").
       - If posting is multilingual ("English OR French"): pick the
         language with the SMALLEST gap for the candidate (best of).
       - If genuinely ambiguous: NO penalty.

     Identifying the candidate's LEVEL for that language:
       - Read the resume's languages section / inline language mentions
         AND the PREFS file (which may explicitly list levels:
         "Languages: English C1, Russian native").
       - Recognise CEFR markers (A1/A2/B1/B2/C1/C2), "native",
         "bilingual", "fluent" (= C2), "intermediate" (= B1),
         "basic" (= A2), explicit certificate names (DELF C1, Goethe
         B2, IELTS 7.0 ≈ C1, TOEFL 110 ≈ C2).
       - Soft self-descriptors ("professional", "working proficiency",
         "advanced") map to B2 UNLESS paired with an explicit CEFR
         qualifier — they're aspirational on resumes too.
       - The candidate's `language` field in preferences, if non-empty,
         lists the language(s) they prefer to work in — assume C2 for
         each unless the resume/PREFS says otherwise.
       - If the candidate did NOT list the required language at all,
         treat their level as A1 (numeric 1).
       - English: if the resume is written in English OR lists English
         anywhere, assume at least B2 unless the resume/PREFS gives a
         higher explicit level.

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
     PREFS text contains an EXPLICIT VETO PHRASE for lower seniority.

     Default rule for lower-than-target postings:
       Treat intern / junior / mid / associate / entry-level postings
       as a PERFECTLY acceptable seniority fit at no penalty. Many
       strong candidates intentionally apply downward to switch
       domain / company / stack. The bot does NOT second-guess that.

     SOFT phrasing that DOES NOT trigger the below-target penalty
     (these are aspirational, not vetoes):
       * "I am a mid-level engineer" / "looking for a mid-level role"
       * "Mid-level frontend engineer role working primarily in …"
       * "Senior backend engineer" (candidate self-describing)
       * "Targeting mid → senior"
       * The user's stated TARGET being mid/senior is NOT, by itself,
         a veto of junior postings. Aspirational target ≠ exclusion.

     OVERQUALIFICATION IS NEVER A SCORING PENALTY. Phrases like
     "candidate is overqualified for this junior role", "X years
     experience exceeds Y-year requirement", "salary undervalues the
     candidate" must NOT subtract any points. The candidate decides
     whether to apply downward — your job is to surface the role,
     not gate it on their behalf. NEVER write a `why_mismatch` that
     reads "overqualified", "above the role's bar", "candidate has
     more experience than required", etc. Skip the rule entirely.

     HARD veto phrases that DO trigger −1 per level below target:
       * "no junior", "no intern", "no entry-level", "no associate"
       * "NOT a fit: junior", "NOT a fit: mid level"
       * "must be senior", "senior or above only", "minimum senior"
       * `title_exclude` list containing "junior" / "intern" / "entry"
       * Skip-feedback comments (under `[Recent 'not a fit' comments]`)
         like "too junior", "not a fit: too junior"
     Only these explicit forms count — anything weaker is aspirational
     and gets no penalty.

     SALARY is NEVER a scoring penalty. Salary is informational only,
     surfaced in `key_details.salary` for the user to see. Do NOT
     subtract for "salary undervalues the candidate", "below market",
     or "compensation range too low" — that is the user's decision to
     make from the card, not yours.

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
          exclude, V4 LOCATION + V4a country-restricted company,
          V4b no-sponsorship country-locked, V4c US-onsite/hub-
          radius) — if ANY hits → score = 0, return. APPLY V4
          BEFORE ANY TOPICAL FIT — never let topical strength
          override a location veto.
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
    db=None,
    chat_id: int | None = None,
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
      db, chat_id:     Optional persistent score-cache wiring. When BOTH
                       are provided, we look up cached verdicts (keyed
                       by Job.job_id + the current profile_hash) before
                       calling the model, and persist fresh verdicts
                       back after scoring. Either being None falls back
                       to the un-cached path so unit tests and ad-hoc
                       callers keep working.

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

    # ----- Persistent score cache (lookup) ----------------------------
    # When db+chat_id are both threaded through, fetch cached verdicts
    # for this user at the CURRENT profile_hash (resume + prefs). Any
    # edit to either input flips the hash → automatic invalidation.
    # Cache is keyed by Job.job_id (stable sha1 of source+url); we
    # rebuild the external_id→enrichment surface at the boundary so
    # the function's public return shape is unchanged.
    cache_enabled = db is not None and chat_id is not None
    cached_by_ext: dict[str, dict] = {}
    to_score: list[Job] = list(jobs)
    phash: str | None = None
    if cache_enabled:
        try:
            from db import profile_hash as _profile_hash
            phash = _profile_hash(resume_text, prefs_text)
            job_ids = [j.job_id for j in jobs]
            cached_by_job_id = db.get_cached_scores(int(chat_id), job_ids, phash)
        except Exception:
            log.exception("enrich_jobs_ai: cache lookup failed; "
                          "falling back to full scoring")
            cached_by_job_id = {}
            phash = None
        if cached_by_job_id:
            still_needs: list[Job] = []
            for j in jobs:
                hit = cached_by_job_id.get(j.job_id)
                if hit is None:
                    still_needs.append(j)
                else:
                    cached_by_ext[j.external_id] = hit
            to_score = still_needs
            log.info(
                "enrich_jobs_ai: cache hit %d/%d, scoring %d new",
                len(cached_by_ext), len(jobs), len(to_score),
            )
        else:
            log.info(
                "enrich_jobs_ai: cache hit 0/%d, scoring %d new",
                len(jobs), len(to_score),
            )

    if not to_score:
        # Everyone was cached.
        return cached_by_ext

    # Model selection:
    #   * single-pass (two_pass=False): Sonnet for every job. v2.1 default
    #     because Haiku was noisy at the 4-vs-3 boundary on its own.
    #   * two-pass (two_pass=True): Haiku triages every job cheap, then
    #     Sonnet re-scores only survivors at `triage_floor`. Cost cut
    #     ~5-8× vs single-pass Sonnet; Sonnet still arbitrates the
    #     borderline cases where Haiku is unreliable.
    if two_pass:
        primary_model = SMALLEST_MODEL  # haiku
        primary_label = "haiku"
    else:
        primary_model = MID_MODEL       # sonnet
        primary_label = "sonnet"

    first_out = _enrich_pool(
        to_score, resume_text, prefs_text, timeout_s,
        max_jobs_per_call, model=primary_model, pass_label=primary_label,
        workers=workers,
    )

    if not two_pass:
        # Persist freshly-scored verdicts before merging back with cache.
        if cache_enabled and phash and first_out:
            _persist_scores(db, int(chat_id), phash, first_out,
                            to_score, primary_label)
        # Merge cached hits + new verdicts. Cached entries never collide
        # with fresh ones because to_score == jobs - cached.
        merged_out = dict(cached_by_ext)
        merged_out.update(first_out)
        return merged_out

    survivors: list[Job] = [
        j for j in to_score
        if int((first_out.get(j.external_id) or {}).get("match_score") or 0)
           >= triage_floor
    ]
    if not survivors:
        log.info("enrich_jobs_ai: two-pass — 0 survivors at triage_floor=%d",
                 triage_floor)
        # No Sonnet pass: the only verdicts produced were Haiku's. Persist
        # them under the haiku label.
        if cache_enabled and phash and first_out:
            _persist_scores(db, int(chat_id), phash, first_out,
                            to_score, primary_label)
        merged_out = dict(cached_by_ext)
        merged_out.update(first_out)
        return merged_out

    log.info(
        "enrich_jobs_ai: two-pass — %d/%d jobs survived Haiku triage "
        "(floor=%d), re-scoring with %s",
        len(survivors), len(to_score), triage_floor, MID_MODEL,
    )
    sonnet_out = _enrich_pool(
        survivors, resume_text, prefs_text, timeout_s,
        max_jobs_per_call, model=MID_MODEL, pass_label="sonnet",
        workers=workers,
    )

    # Merge: Sonnet overwrites Haiku where present, else Haiku stands.
    merged = dict(first_out)
    upgrades = downgrades = unchanged = 0
    for ext_id, sonnet_v in sonnet_out.items():
        prev = (first_out.get(ext_id) or {}).get("match_score") or 0
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

    # ----- Persist freshly-scored verdicts back to the cache ----------
    # Each row's `model` reflects the final pass that produced its
    # verdict: "sonnet" for entries Sonnet re-scored, else "haiku".
    if cache_enabled and phash and merged:
        sonnet_ext_ids = set(sonnet_out.keys())
        _persist_scores(
            db, int(chat_id), phash, merged, to_score,
            primary_label,  # default ("haiku")
            sonnet_ext_ids=sonnet_ext_ids,
        )

    # Merge cached hits into the final return (cached entries are
    # disjoint from to_score by construction).
    merged_out = dict(cached_by_ext)
    merged_out.update(merged)
    return merged_out


def _persist_scores(
    db,
    chat_id: int,
    profile_hash_value: str,
    verdicts_by_ext: dict[str, dict],
    scored_jobs: list[Job],
    default_label: str,
    *,
    sonnet_ext_ids: set[str] | None = None,
) -> None:
    """Translate the {external_id → enrichment} dict produced this run
    into {job_id → enrichment} and bulk-upsert into job_scores.

    Cache rows are keyed by Job.job_id (stable sha1 of source+url) so
    a job is reusable across re-runs even if its external_id rotates.
    `sonnet_ext_ids`, when provided, marks the entries that survived
    a Sonnet re-score so they're tagged "sonnet" instead of the
    default model label.
    """
    ext_to_job_id = {j.external_id: j.job_id for j in scored_jobs}
    by_job_id_local: dict[str, dict] = {}
    for ext_id, enr in verdicts_by_ext.items():
        if not isinstance(enr, dict):
            continue
        jid = ext_to_job_id.get(ext_id)
        if not jid:
            # Verdicts the model emitted for IDs we didn't ask about;
            # `_enrich_one_chunk` already filters these but belt-and-
            # braces here so a future regression can't corrupt the cache.
            continue
        row = dict(enr)
        if sonnet_ext_ids is not None:
            row["model"] = "sonnet" if ext_id in sonnet_ext_ids else default_label
        else:
            row["model"] = default_label
        by_job_id_local[jid] = row
    if not by_job_id_local:
        return
    try:
        db.upsert_scores(chat_id, profile_hash_value, by_job_id_local,
                         default_label)
    except Exception:
        log.exception("enrich_jobs_ai: cache upsert failed; "
                      "verdicts still returned to caller")


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


def pick_most_relevant_ai(
    candidates: list[Job],
    resume_text: str,
    prefs_text: str,
    *,
    timeout_s: int = 240,
) -> Job | None:
    """Tie-breaker for the "no matches at floor" fallback path.

    Given >=2 candidates at the same top score, call Sonnet once with
    the full resume + prefs + each candidate's brief and ask it to
    pick the SINGLE most relevant one. Returns the chosen Job (or the
    first candidate as a deterministic fallback when the call fails).
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    if not resume_text or not resume_text.strip():
        return candidates[0]

    briefs = [_job_to_brief(j) for j in candidates[:10]]
    prompt = (
        "You are picking ONE job posting out of several tied on score "
        "for ONE candidate. The candidate's daily digest currently has "
        "NO postings clearing their ⭐ floor; we want to surface the "
        "single most relevant 'closest' so they don't get an empty "
        "digest. Be DECISIVE — return the one external_id that best "
        "matches the candidate's resume + PREFS.\n\n"
        "Selection rules, in order:\n"
        "  1. Strongest stack/tooling overlap with the resume.\n"
        "  2. Best location fit (PREFS onsite/remote constraints).\n"
        "  3. Closest seniority to the candidate's target.\n"
        "  4. Most concrete description (avoid generic snippets).\n\n"
        f"=== CANDIDATE RESUME ===\n{(resume_text or '')[:_MAX_RESUME_PROMPT_CHARS]}\n\n"
        f"=== CANDIDATE PREFS ===\n{(prefs_text or '')[:_MAX_PREFS_PROMPT_CHARS]}\n\n"
        f"=== TIED CANDIDATES (JSON) ===\n{json.dumps(briefs, ensure_ascii=False)}\n\n"
        "Respond with ONE JSON object only — no prose, no fence:\n"
        "{\"chosen_id\": \"<external_id verbatim>\", "
        "\"reason\": \"<one short sentence>\"}"
    )
    stdout = wrapped_run_p(None, "pick_most_relevant", prompt,
                          timeout_s=timeout_s, model=MID_MODEL)
    if not stdout:
        log.warning("pick_most_relevant_ai: CLI returned None; "
                    "falling back to first candidate")
        return candidates[0]
    body = extract_assistant_text(stdout)
    data = parse_json_block(body)
    if not isinstance(data, dict):
        return candidates[0]
    chosen = str(data.get("chosen_id") or "").strip()
    for j in candidates:
        if j.external_id == chosen:
            log.info("pick_most_relevant_ai: chose %s (reason=%r)",
                     j.external_id[:60], str(data.get("reason"))[:120])
            return j
    return candidates[0]


def reanalyze_scoring_ai(
    jobs: list[Job],
    enrichments_by_external_id: dict[str, dict],
    resume_text: str,
    prefs_text: str,
    *,
    timeout_s: int = 240,
    model: str = "opus",
) -> list[dict]:
    """v2.5 audit stage: re-grade the score-≥1 verdicts with a second
    opinion model (Opus by default), surface disagreements.

    Runs AFTER `send_per_job_digest` finishes — it's a quality-control
    stage, not a gating stage. Output goes to forensic + can drive a
    manual top-up later if a big miss is detected.

    Args:
      jobs:        Postings the scorer evaluated (score-≥1 subset is
                   typically passed in; caller decides the cut).
      enrichments_by_external_id: original verdicts keyed by external_id.
      resume_text / prefs_text:   same blobs the scorer saw.
      model:       Defaults to `opus` — different model class than the
                   Sonnet/Haiku scorer, so it brings an independent
                   perspective. Operators can swap.

    Returns a list of audit dicts, one per posting:
        {
          "id":             "<external_id>",
          "original_score": <0-5>,
          "revised_score":  <0-5>,
          "verdict":        "agree" | "raise" | "lower",
          "comment":        "<one short sentence>",
        }

    Empty list on any failure (CLI missing, parse error). Never raises.
    """
    if not jobs:
        return []
    if not resume_text or not resume_text.strip():
        return []

    review_items = []
    for j in jobs:
        enr = enrichments_by_external_id.get(j.external_id) or {}
        review_items.append({
            "id": j.external_id,
            "title": (j.title or "")[:140],
            "company": (j.company or "")[:100],
            "url": (j.url or "")[:200],
            "snippet": (j.snippet or "").replace("\n", " ")[:1200],
            "original_score": int(enr.get("match_score") or 0),
            "original_why_match": (enr.get("why_match") or "")[:240],
            "original_why_mismatch": (enr.get("why_mismatch") or "")[:240],
        })

    prompt = (
        "You are a SCORING AUDITOR reviewing match-score verdicts "
        "produced by another model for ONE candidate. Your job is to "
        "verify each score against the same scoring rules and flag "
        "disagreements.\n\n"
        "Scale (same as the scorer used):\n"
        "  0 = clearly wrong fit · 1 = poor · 2 = weak · 3 = OK / "
        "acceptable stretch · 4 = strong · 5 = perfect.\n\n"
        "Audit each posting. Output for each:\n"
        "  verdict      — 'agree' / 'raise' / 'lower'\n"
        "  revised_score — your independent integer 0-5\n"
        "  comment       — ONE short sentence (≤ 240 chars) explaining\n"
        "                  WHY you raised or lowered. For 'agree' it\n"
        "                  can be empty.\n\n"
        "Doctrines the scorer is required to follow (use these as\n"
        "your audit lens):\n"
        "  A) NEVER penalize 'overqualified'. A Senior candidate vs a\n"
        "     Junior role gets NO subtraction. Raise scores that hit\n"
        "     this drift.\n"
        "  B) NEVER stack seniority + years penalties on the SAME\n"
        "     upward gap. Only ONE may fire. Raise scores that hit\n"
        "     this drift.\n"
        "  C) Generic '<Stack> Developer (Remote)' titles whose body\n"
        "     describes AI rating / data labeling / LLM evaluation /\n"
        "     prompt engineering tasks → score=0. Lower scores that\n"
        "     missed this.\n\n"
        "Other common mis-scores to catch:\n"
        "  • Soft-trigger CEFR penalties on words like 'proficient'\n"
        "    or 'professional working' that don't strictly mean C1.\n"
        "    Raise.\n"
        "  • Missed hard mismatches: posting body shows '5+ years' /\n"
        "    'senior only' / 'must speak German' that the original\n"
        "    why_match didn't notice. Lower.\n"
        "  • Wrong onsite-city vetoes — the posting is hybrid in a\n"
        "    city outside the candidate's onsite list but the\n"
        "    original verdict didn't reject. Lower.\n\n"
        "Output STRICT JSON only, no prose, no fence:\n"
        "{\"reviews\": [\n"
        "  {\"id\": \"<external_id>\", \"verdict\": \"agree|raise|lower\", "
        "\"revised_score\": <0-5>, \"comment\": \"...\"}\n"
        "]}\n\n"
        f"=== CANDIDATE RESUME ===\n{resume_text[:_MAX_RESUME_PROMPT_CHARS]}\n\n"
        f"=== CANDIDATE PREFS ===\n{prefs_text[:_MAX_PREFS_PROMPT_CHARS]}\n\n"
        f"=== VERDICTS TO AUDIT ({len(review_items)} items) ===\n"
        f"{json.dumps(review_items, ensure_ascii=False)}"
    )
    stdout = wrapped_run_p(None, "scoring_audit", prompt,
                          timeout_s=timeout_s, model=model)
    if not stdout:
        log.warning("reanalyze_scoring_ai: CLI returned None — audit skipped")
        return []
    body = extract_assistant_text(stdout)
    data = parse_json_block(body)
    if not isinstance(data, dict) or not isinstance(data.get("reviews"), list):
        log.error("reanalyze_scoring_ai: unparseable audit response (head=%r)",
                  (body or "")[:200])
        return []

    out: list[dict] = []
    valid_ids = {j.external_id for j in jobs}
    for r in data["reviews"]:
        if not isinstance(r, dict):
            continue
        ext_id = str(r.get("id") or "").strip()
        if ext_id not in valid_ids:
            continue
        original = int(enrichments_by_external_id.get(ext_id, {}).get("match_score") or 0)
        try:
            revised = max(0, min(5, int(r.get("revised_score") or original)))
        except (TypeError, ValueError):
            revised = original
        verdict = str(r.get("verdict") or "").strip().lower()
        if verdict not in {"agree", "raise", "lower"}:
            verdict = "agree" if revised == original else (
                "raise" if revised > original else "lower"
            )
        out.append({
            "id": ext_id,
            "original_score": original,
            "revised_score": revised,
            "verdict": verdict,
            "comment": fix_mojibake(str(r.get("comment") or "").strip())[:240],
        })
    return out


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
