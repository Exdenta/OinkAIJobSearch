# Per-user profile — as built

**Status:** Implemented. This doc describes the final shipped system (no more
phased rollout / feature flags — the legacy flat-prefs path was removed once
the Opus-built profile became the single default).

## What it does

Every registered user gets a structured JSON profile that drives search and
matching. The profile is built by an Opus sub-agent from two inputs:

1. **Resume text** — extracted from the uploaded CV (PDF → text).
2. **Free-text `/prefs`** — whatever the user typed into `/prefs` describing
   what they want (role, location, stack, anything).

The profile is the authoritative "what this user wants" record. It replaces
the old global `config/filters.yaml` per-user overlay.

## End-to-end flow

```
   user uploads CV  ─┐
   user sends /prefs ┘  → bot.py persists raw inputs + enqueues a rebuild
                           ↓  (debounced 60s for /prefs, immediate for CV)
                     profile_builder.ProfileBuilderQueue
                           ↓
                     Opus sub-agent (claude -p, model=opus)
                           ↓  strict JSON → profile_schema_validate
                     users.user_profile (JSON column)  + profile_builds log
                           ↓
                     search_jobs.py run:
                       - linkedin.fetch_for_user(profile.search_seeds.linkedin)
                       - web_search.fetch(profile.search_seeds.web_search)
                       - post_filter is a pass-through (AI is the only gate)
                       - job_enrich.enrich_jobs_ai(profile-projected prefs)
                       - min_match_score cutoff (set via ⭐ keyboard)
                     ↓
                     digest messages
```

## Storage

Single SQLite table (`users`):

- `user_profile`     — TEXT / JSON — the Opus-built structured profile
- `prefs_free_text`  — TEXT — the raw `/prefs` message body (independent of
                       the profile, so rebuilds can use the latest text even
                       after the profile itself is cleared)
- `resume_path` / `resume_text` — uploaded CV + extracted text

`profile_builds` (audit log table):

- `chat_id, built_at, status, error, elapsed_ms, resume_sha1, prefs_sha1, model`

## Profile shape

Schema version `2`. Validated by `profile_schema_validate` on every build.

```jsonc
{
  "schema_version": 2,
  "ideal_fit_paragraph": "...",
  "primary_role": "frontend engineer",
  "target_levels": ["mid", "middle"],
  "years_experience": 5,
  "stack_primary":       ["vue", "typescript"],
  "stack_secondary":     ["react", "jest"],
  "stack_adjacent":      ["node"],
  "stack_antipatterns":  ["wordpress", "drupal"],
  "title_must_match":    ["frontend", "vue"],
  "title_exclude":       ["senior", "staff"],
  "exclude_keywords":    ["wordpress"],
  "exclude_companies":   ["Crossover"],
  "locations":           ["bilbao", "spain", "europe"],
  "remote":              "remote",            // remote | hybrid | onsite | any
  "time_zone_band":      "UTC-1..UTC+3",
  "salary_min_usd":      0,
  "drop_if_salary_unknown": false,
  "language":            "english",
  "max_age_hours":       0,
  "min_match_score":     0,                   // 0..5 (⭐ keyboard sets this)
  "search_seeds": {
    "linkedin": {
      "queries": [
        {"q": "frontend vue developer", "geo": "Spain",          "f_TPR": "r86400"},
        {"q": "react typescript remote", "geo": "European Union", "f_TPR": "r86400"}
      ]
    },
    "web_search": {
      "seed_phrases": ["remote vue frontend europe", "site:greenhouse.io vue"],
      "ats_domains":  ["greenhouse.io", "lever.co"],
      "focus_notes":  "Prefer EU timezones."
    }
  },
  "free_text":  "remote EU, Vue or React",
  "built_at":   "2026-04-20T10:15:00Z",
  "built_from": {"resume_sha1": "…", "prefs_sha1": "…", "model": "opus"}
}
```

All length caps / allowlists are enforced both in the prompt and by
`profile_builder._clip_profile`.

## Key modules

- `user_profile.py` — load/save/validate + `effective_filters(global, profile)`
  (profile fully overrides globals where set) + `project_to_prefs(profile)`
  (flat dict the AI-enrichment prompt consumes) + `format_profile_summary_mdv2`
  + `set_min_match_score`.
- `profile_builder.py` — Opus call + JSON validate + `ProfileBuilderQueue`
  (debounced per-chat work queue, single-in-flight invariant, coalesces edits).
- `search_jobs.py` — per-user loop reads `db.get_user_profile(chat_id)`, runs
  `effective_filters`, dispatches `linkedin.fetch_for_user` and `web_search.fetch`
  with the profile's `search_seeds`, feeds `enrich_jobs_ai` the projected prefs,
  then applies the ⭐ `min_match_score` cutoff.
- `bot.py` — `/prefs`, `/myprofile`, `/rebuildprofile` commands; ⭐ keyboard
  sets `min_match_score`; resume upload and /prefs change both enqueue a
  rebuild through `ProfileBuilderQueue`.

## Commands (user-visible)

| Command              | Action                                                        |
|----------------------|---------------------------------------------------------------|
| `/start`, `/help`    | Onboarding message                                            |
| `/prefs`             | Send/update free-text preferences (enqueues a rebuild)        |
| `/myprofile`         | Show the current Opus-built profile summary                   |
| `/rebuildprofile`    | Force-rebuild the profile from the current resume + free text |
| `/applied`           | List every role marked applied                                |
| `/cleanmydata`       | Scoped deletion menu (resume / history / profile / everything)|

## Rebuild triggers

- `resume_upload` — fires immediately (no debounce). The user just uploaded
  a new CV; their previous profile is definitively stale.
- `prefs_change` — debounced (60s). User may be typing/editing /prefs text;
  coalesce bursts into one build.
- `manual` — `/rebuildprofile` — runs immediately.

The queue guarantees single-in-flight per chat_id. Any trigger that arrives
while a build is running is coalesced into a single follow-up build that
picks up the latest inputs.

## Failure modes

- **Opus CLI missing / timeout** → `BuildResult(status="cli_missing_or_timeout")`.
  User keeps whatever profile they previously had; no data loss.
- **Schema-invalid profile** → `BuildResult(status="validation_error")`. Same as above.
- **Unparseable JSON** → `BuildResult(status="parse_error")`. Same as above.
- **User has no profile yet** → per-user loop inherits `config/filters.yaml`
  globals; LinkedIn falls back to the single-query global `fetch(filters)` path;
  web_search uses the base candidate-profile rows from filters.yaml.

## Operator knobs

- `config/filters.yaml` — globals (default LinkedIn query, source toggles,
  `max_per_source`, timeouts). Profiles override per-user.
- `ai_build_profile_timeout_s` (filters.yaml) — per-build timeout for the
  Opus call.
- `ai_web_search_timeout_s` / `ai_scrape_timeout_s` — timeouts for the
  web-discovery and curated-boards sub-agents.

No feature flags. No phased rollout. No migration dual-path.
