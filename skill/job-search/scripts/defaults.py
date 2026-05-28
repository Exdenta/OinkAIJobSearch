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
        "web_search":       True,    # Re-enabled 2026-05-24 after T4 verifier hardening (soft-404 pre-LLM check + fail-safe gate) ran clean for 36h. Was #1 strong-hit source (17-32% hit rate).
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
    # cheap, Sonnet re-scores only postings in the
    # [triage_floor, triage_ceiling) window. Cost cut ~5-8× vs single-
    # pass Sonnet; Sonnet still arbitrates the borderline 3-vs-4 boundary
    # where Haiku alone was noisy.
    #   ai_two_pass:        master toggle. False → single-pass Sonnet.
    #                       True  → Haiku triage + Sonnet rescore.
    #   ai_triage_floor:    Haiku score threshold for promotion to Sonnet.
    #                       2 keeps anything plausibly relevant; raise to
    #                       3 for an even tighter funnel if cost still high.
    #   ai_triage_ceiling:  Haiku score AT OR ABOVE which the Sonnet rescore
    #                       is SKIPPED — Haiku's verdict is trusted as-is.
    #                       Default 5: Haiku's 5/5 verdicts pass through
    #                       untouched. Rationale: Haiku is rarely wrong at
    #                       the top end (forensic 7d window 2026-05-21..28
    #                       showed Sonnet downgrading Haiku 5 → <5 on
    #                       ~3-4% of cases), and Sonnet at best confirms
    #                       a 5 — the marginal quality gain doesn't justify
    #                       the per-batch Sonnet latency (53s/job on the
    #                       worst 7d batches). Set to 6 to send EVERYTHING
    #                       (including 5s) through Sonnet, or 4 for an even
    #                       tighter Sonnet funnel that trusts 4s and 5s.
    #                       This is NOT a heuristic score cap: the prompt's
    #                       full scoring doctrine still applies to every
    #                       Sonnet call; this knob only controls WHICH
    #                       Haiku verdicts are routed to Sonnet at all.
    "ai_two_pass":        True,
    "ai_triage_floor":    2,
    "ai_triage_ceiling":  5,

    # Per-batch chunk size. Separate knobs per pass because Haiku and
    # Sonnet have very different latency profiles on this prompt template
    # (~9.1k fixed-overhead tokens of doctrine + per-job briefs).
    #   ai_max_jobs_per_call:       used for the primary pass (Haiku in
    #                               two-pass mode, Sonnet in single-pass
    #                               mode). Haiku at 10/batch is fast and
    #                               cheap; we keep the historical value.
    #   ai_sonnet_max_jobs_per_call: used for the Sonnet RESCORE pass
    #                                only (two-pass mode). Lowered 10 → 5
    #                                in 2026-05-28 after forensic showed
    #                                Sonnet batches of 10 routinely
    #                                stalling 5-9 min (worst 7d:
    #                                347-530s, 53s/job). At batch=5 the
    #                                doctrine+briefs payload drops from
    #                                ~25k → ~19k input tokens; combined
    #                                with the existing 4-worker pool,
    #                                same total wall time but no single
    #                                batch monopolises a worker > a few
    #                                minutes. Set equal to
    #                                ai_max_jobs_per_call to disable the
    #                                per-pass split.
    "ai_max_jobs_per_call":         10,
    "ai_sonnet_max_jobs_per_call":  5,
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

    # Algorithm v2.7 / P2: minimum age (seconds) before a (source, query,
    # page, location) cell is eligible for re-fetch. Threaded into
    # linkedin / justjoinit / nofluffjobs / builtin / web_search via
    # `db.next_page_for`. 21600s = 6h gives the continuous scheduler
    # (P3, runs every ~2h) three iterations to walk pages 1..N before
    # circling back to page 1 of a given source — long enough that we
    # don't burn calls on stale repetition, short enough that fresh
    # listings re-appear within the day. Per-adapter max_page caps live
    # in each adapter (linkedin=10, justjoinit/nofluffjobs/builtin=5,
    # web_search=3).
    "source_min_revisit_age_s": 21600,

    # P2-cursor periodic reset (added 2026-05-24 after diagnosing yield
    # collapse). Every Nth cycle the continuous searcher nukes
    # `search_fetches` for the P2-instrumented sources, forcing the
    # cursor back to page 1. Strong matches concentrate on pages 1-4;
    # without this the cursor walks 10+ pages deep and hits the long
    # tail. Set to 0 to disable.
    "cursor_reset_every_n_cycles": 4,

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

    # Quality-buffer thresholds (algorithm v2.6, P1 pipeline overhaul).
    # The continuous searcher enqueues each scored-and-live match that
    # clears `ai_min_match_score`. The send-decision path flushes the
    # queue in one batch only when EITHER condition fires:
    #
    #   * depth >= quality_send_threshold (enough good matches accumulated)
    #   * oldest_age >= max_queue_latency_hours (latency budget exceeded)
    #
    # This replaces the v2.4 closest-miss fallback: if nothing scores
    # ≥ floor in a given run, the queue simply doesn't grow — no
    # substitute card is surfaced.
    "quality_send_threshold":     1,
    "max_queue_latency_hours":   24,

    # Night mute: do not flush the quality buffer between these hours in the
    # configured timezone. Applies to BOTH the threshold-met flush AND the
    # 48h age-based force-flush — the operator does not want Telegram pings
    # at night. Set start_hour == end_hour to disable.
    #
    # Wrap-around semantics: `start_hour > end_hour` means the window
    # crosses midnight (the default 23 → 09 case = mute from 23:00 through
    # 08:59:59, resume at 09:00). `start_hour < end_hour` is a same-day
    # window (e.g. 13 → 14 mutes only the 13:00-13:59 hour). When
    # `start_hour == end_hour` the window is empty and night-mute is
    # disabled (no muting ever).
    #
    # `end_hour` is exclusive: 09:00 sharp is "back online". The
    # `night_mute_tz` value must be an IANA zone name (e.g. "Europe/Madrid",
    # "UTC"); unknown zones fail-open (no muting + WARNING log) so a typo
    # can't muzzle the user permanently.
    "night_mute_tz":             "Europe/Madrid",
    "night_mute_start_hour":     23,    # inclusive
    "night_mute_end_hour":       9,     # exclusive (09:00 = "back online")

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
        # NOTE (P5, v2.6 continuous mode): this knob has NO effect on the
        # quality-buffer hold branch in search_jobs.run — empty sends are
        # always suppressed there. Retained for back-compat with operators
        # who may set it for other reasons.
        "quiet_if_empty":  False,
    },

    # Continuous searcher (Phase 3). When bot.py is started with the env var
    # HRYU_CONTINUOUS_MODE=1 it spawns an in-process searcher loop instead of
    # relying on the daily cron. The loop calls `search_jobs.run` every
    # `continuous_interval_seconds` for a single chat_id (set via
    # HRYU_CONTINUOUS_CHAT_ID). Quality is gated by the P1 buffer; pages are
    # gated by the P2 cursor memory; this knob just controls how often the
    # searcher wakes up.
    #
    #   continuous_interval_seconds — target gap between iterations. 28800
    #     (8h) is the operator default; tighter intervals burn API + scrape
    #     quota without finding much.
    #   continuous_min_sleep_seconds — back-pressure floor. If an iteration
    #     takes longer than `continuous_interval_seconds`, the loop still
    #     pauses this long before the next run, so a degraded source can't
    #     pin the searcher in a hot loop.
    "continuous_interval_seconds": 28800,
    "continuous_min_sleep_seconds":  60,

    # Adaptive source cooldown (algorithm v2.8, P4 pipeline overhaul).
    # `fetch_all` reads each source's 24h novelty ratio (jobs_new /
    # jobs_seen across the last 24h of `search_fetches` rows) and, when
    # the ratio has stayed below this threshold for 3 consecutive
    # iterations, demotes the source to half-frequency — runs only on
    # odd cycle_index. Recovery is immediate: one cycle above threshold
    # flips the source back to 'normal'.
    #
    # 0.05 (5%) means "if fewer than 5% of jobs are new per fetch, the
    # source is wasting tokens." Raise to be more aggressive (cool down
    # more sources); lower to be more permissive. 0.0 effectively
    # disables the gate. See `search_jobs.should_run_source` for the
    # full FSM.
    "source_low_novelty_threshold": 0.05,

    # Auto profile rebuild on accumulated skip feedback (algorithm
    # v2.8, P4 pipeline overhaul). Each call to `db.append_skip_note`
    # bumps `users.skip_events_since_rebuild`; after every scoring
    # iteration the searcher checks the counter and triggers a fresh
    # `profile_builder.rebuild_profile` once the count crosses this
    # threshold. Reset to 0 only on a SUCCESSFUL rebuild so transient
    # CLI / parse failures don't silently consume K events.
    "auto_rebuild_skip_threshold": 5,
}
