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
    },

    # How many hours back to look. HN threads are softened ×30 internally.
    "max_age_hours":  48,
    "max_per_source": 12,
    # Bumped 40 → 120 after adding 5 new sources (reliefweb, euraxess,
    # un_careers, math_ku_phd, ub_doctoral). Old 40-cap truncated user_pool
    # before reliefweb/euraxess/linkedin reached the enrich step. With 8
    # global + 1 per-user adapters at 12 each, 120 leaves headroom + the
    # AI score gate is the actual filter.
    "max_total":      120,

    # AI enrichment — single Haiku call per user per run scores every fetched
    # posting against resume + preferences. The sole matching gate.
    "ai_enrich":            True,
    "ai_enrich_timeout_s":  240,

    # Default match-score floor when a user hasn't set their own ⭐ via the
    # bot button. 0 disables; clamps to [0, 5].
    "ai_min_match_score": 2,

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
