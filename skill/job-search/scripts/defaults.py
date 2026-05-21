"""Operational defaults for the job-search pipeline.

Replaces the deleted `config/filters.yaml`. All matching is now per-user via
the Opus-built profile (`user_profile.py`); the only dict-shaped state the
pipeline still needs is OPERATIONAL — which sources to run, timeouts, caps,
score floor, message rendering. Those live here as code defaults so a fresh
clone is runnable with no config file.

Per-user override: `effective_filters(DEFAULTS, profile)` (see user_profile.py)
layers the user's profile on top — list/seniority/remote/salary fields the
user stated win; sentinel values inherit these defaults. The `sources` toggles
and timeouts are operator-controlled and are NOT overridden by the profile.

Operators tuning a deployment should edit this file (or set env vars in
`search_jobs.py` if a knob ever needs ops-time control), not ship a YAML.
"""
from __future__ import annotations

DEFAULTS: dict = {
    # Source toggles. `linkedin` and `web_search` run PER-USER only — they
    # need profile.search_seeds to know what to fetch. The others run once
    # globally and the per-user filter happens downstream in the AI score.
    "sources": {
        "hackernews":       True,
        "remoteok":         True,
        "remotive":         True,
        "weworkremotely":   True,
        "linkedin":         True,    # per-user via profile.search_seeds.linkedin
        "remocate":         True,    # curated boards — Claude CLI required
        "wantapply":        True,
        "remoterocketship": True,
        "web_search":       True,    # per-user, requires Claude CLI
        # Humanitarian + research/academic sources (added 2026-04-30).
        "reliefweb":        True,    # public API — humanitarian sector
        "euraxess":         True,    # EU researcher mobility portal — public API
        "un_careers":       True,    # SPA — Claude CLI delegation
        "math_ku_phd":      True,    # KU employment RSS — math department
        "ub_doctoral":      True,    # Univ Barcelona — Claude CLI
        # Wave 2 sources (added 2026-05-01). 10 live + 3 blocked-stubs.
        # All flipped ON 2026-05-01 per operator request — blocked stubs
        # return [] gracefully so they're cheap to keep enabled (forensic
        # logs surface their failure reason every run for visibility).
        "eures":             True,   # BLOCKED: EU Login (stub returns [])
        "infojobs":          True,   # Spain HTML scrape
        "tecnoempleo":       True,   # Spain tech RSS
        "ai_jobs_net":       True,   # Curated AI/ML HTML scrape
        "jobs_ac_uk":        True,   # UK/EU academic RSS (multi-category)
        "academicpositions": True,   # BLOCKED: Cloudflare BFM (stub returns [])
        "ikerbasque":        True,   # Basque research foundation
        "wellfound":         True,   # BLOCKED: DataDome (stub returns [])
        "ycombinator_was":   True,   # YC startup JSON API
        "wttj":              True,   # Welcome to the Jungle — Algolia
        "builtin":           True,   # US-heavy tech HTML scrape
        "impactpool":        True,   # UN/NGO HTML — researcher-friendly
        "devex":             True,   # International dev — Claude CLI (paywall workaround)
        "justjoinit":        True,   # Polish/CEE tech JSON API — frontend-heavy
        "nofluffjobs":       True,   # Polish/CEE tech JSON API — salary-mandatory
    },

    # How many hours back to look. HN threads are softened ×30 internally.
    "max_age_hours":  48,
    "max_per_source": 36,
    # Headroom: 23 SOURCES × 36 + 90 linkedin + 36 web_search ≈ 954 ceiling.
    # 1200 leaves room for adding 1-2 more sources before this needs another bump.
    "max_total":      1200,

    # AI enrichment — single Haiku call per user per run scores every fetched
    # posting against resume + preferences. The sole matching gate.
    "ai_enrich":            True,
    # v2.1: bumped 240→1200 because Sonnet at batch=10 with full resume +
    # prefs + 10 jobs (~37k chars prompt) routinely hit the old 240s
    # ceiling, forcing split-retries. 1200 (~20 min) gives Sonnet breathing
    # room without inviting indefinite hangs.
    "ai_enrich_timeout_s":  1200,

    # Two-pass scoring. Default ON as of 2026-05-19 — single-pass Sonnet
    # was 63% of monthly AI spend. Two-pass: Haiku triages every posting
    # cheap, Sonnet re-scores only postings >= triage_floor. Cost cut
    # ~5-8× vs single-pass Sonnet; Sonnet still arbitrates the borderline
    # 3-vs-4 boundary where Haiku alone was noisy.
    #   ai_two_pass:        master toggle. False → single-pass Sonnet.
    #                       True  → Haiku triage + Sonnet rescore.
    #   ai_triage_floor:    Haiku score threshold for promotion to Sonnet.
    #                       2 keeps anything plausibly relevant; raise to
    #                       3 for an even tighter funnel if cost still high.
    "ai_two_pass":        True,
    "ai_triage_floor":    2,

    # Per-batch chunk size (algorithm v2.1). Bumped from 5 → 10 once we
    # promoted the scorer to Sonnet — Sonnet handles the bigger prompt
    # comfortably and the larger batch halves the per-pool wall time.
    "ai_max_jobs_per_call": 10,
    # Parallel batch dispatch (v2.1). Each batch is one `claude -p` sub-
    # process; firing N at once roughly divides wall time by N. Anthropic
    # API + claude CLI handle their own rate limits; the OS subprocess
    # pool is the practical bound. 4 workers cuts a 60-min serial run to
    # ~15-20 min while staying well under the per-key rate ceiling.
    "ai_enrich_workers": 4,

    # Parallel source-fetch dispatch (v2.2). The global-pass `fetch_all`
    # runs 23 adapters in a thread pool of this size. Adapters are
    # network-IO bound; threading scales them well. Six is a safe
    # middle ground — each adapter has its own internal concurrency
    # (LinkedIn paces 1.5s/req, impactpool runs 8 detail-page workers),
    # and 6 simultaneous adapters means roughly 6× outbound HTTP +
    # 1-2× claude subprocesses at peak.
    "ai_source_workers": 6,

    # v2.5 scoring-audit stage. AFTER the digest cards ship, re-grade
    # the score-≥1 verdicts with a second-opinion model (Opus). Catches
    # scoring drift without blocking the user-facing send. Output lands
    # in forensic as `scoring_audit.review` lines — operators can grep
    # disagreements for patterns. ai_scoring_audit_model can be
    # overridden to sonnet/haiku for cost trade-offs.
    "ai_scoring_audit":          True,
    "ai_scoring_audit_model":    "opus",
    "ai_scoring_audit_timeout_s": 480,
    # v2.6 batched-audit-with-critic. Split the audit pool into
    # small batches dispatched in parallel; each batch is paired with
    # a runtime Opus "critic" that re-verifies the scorer's output
    # for FORMAT + INTERNAL CONSISTENCY (score in [0,5], verdict
    # matches the score delta, exactly one review per id, etc.).
    # Scorer + critic loop until the critic approves OR the round
    # cap fires — never blocks the audit on a stalemate.
    #   batch_size:    items per scorer call. 10 keeps Opus prompts
    #                  comfortable and matches the existing
    #                  enrich_jobs_ai chunk size.
    #   workers:       parallel worker pool over batches. Mirrors
    #                  ai_enrich_workers.
    #   critic_rounds: max scorer↔critic rounds per batch before
    #                  falling back to the last scorer output.
    #   critic_model:  may differ from the scorer model in future
    #                  (e.g. critic on cheaper sonnet). Defaults to
    #                  opus today so format checks share the scorer's
    #                  judgement class.
    "ai_scoring_audit_batch_size":    10,
    "ai_scoring_audit_workers":       4,
    "ai_scoring_audit_critic_rounds": 3,
    "ai_scoring_audit_critic_model":  "opus",

    # Liveness verifier moved to send-time (telegram_client) in v2.1 —
    # only the postings that survived scoring + ⭐ floor get a Haiku
    # WebFetch verification. Per-pool pre-enrich verification was 90+ min
    # of wasted Haiku calls. The toggle below is left as a no-op for
    # back-compat with operators who set it; remove on next major bump.
    "ai_pre_enrich_liveness": False,

    # Default match-score floor when a user hasn't set their own ⭐ via the
    # bot button. 0 disables; clamps to [0, 5]. Bumped 2 → 4 in v2.1 —
    # at 2, users who never tapped ⭐ were getting 30-50 borderline
    # postings per day. 4 keeps the bar high enough that the default
    # output feels curated.
    "ai_min_match_score": 4,

    # Per-scrape timeout for Claude-CLI-backed adapters (curated_boards).
    "ai_scrape_timeout_s":     180,
    # Web-search agent runs multiple WebSearch+WebFetch calls — needs more.
    "ai_web_search_timeout_s": 300,

    "message": {
        "parse_mode":      "MarkdownV2",
        "group_by_source": True,
        "include_snippet": True,
        "snippet_chars":   260,
        # Send a "nothing new" ping on empty days so the chat stays alive.
        "quiet_if_empty":  False,
    },
}
