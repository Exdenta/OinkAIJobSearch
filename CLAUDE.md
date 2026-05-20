# FindJobs — Claude instructions

Telegram job-alert bot. See `README.md` for architecture, setup, and command reference.
Always use /caveman skill.

## Knowledge wiki

Project knowledge wiki at `claude-docs/` — see `claude-docs/CLAUDE.md` for the schema.

- Catalog: `claude-docs/index.md`
- Activity: `claude-docs/log.md`
- Sources (immutable): `claude-docs/raw/`
- Wiki pages (LLM-maintained): `claude-docs/wiki/`

Routing: this-codebase knowledge → `claude-docs/`. Cross-project Claude Code / Agent SDK / Anthropic API knowledge → global wiki at `~/.claude/claude-docs/`.

## Design principle — prefer AI, avoid hardcoded heuristics

This codebase deliberately leans on LLM scoring + LLM prompts rather than hardcoded heuristics. When adding a new matching rule, false-positive filter, or quality gate, **prefer a prompt instruction over a Python regex / allow-list / score cap**.

Acceptable:
- Tightening the scoring prompt in `skill/job-search/scripts/job_enrich.py` (`_PROMPT`) with new rules, examples, or veto signals — the model interprets them in context.
- Tightening the liveness verifier prompt in `skill/job-search/scripts/telegram_client.py` (`_web_search_listing_still_open`).
- Tightening the profile-builder prompts in `prompts/profile_builder.txt` and `prompts/profile_seeds.txt`.
- Adding a `reanalyze_scoring_ai` audit pass (second-opinion model).

Avoid:
- Hardcoded `_GENERIC_TOKENS = frozenset({...})` lists that "filter spammy words".
- Per-source regex patterns that try to detect "data labeling gigs", "LLM eval", etc.
- Heuristic score caps ("cap at 2 for staffing companies").
- Per-company allow-lists / block-lists in Python.
- Brittle string-matching gates against a frozenset.

Exceptions where hardcoded logic IS justified (do not extend casually):
- HTTP transport invariants (User-Agent strings, ATS-domain allowlist for `_ALLOWED_ATS` in profile_builder).
- Schema enums (`_VALID_REMOTE`, `_ALLOWED_LEVELS`) where the wire format must match a closed set.
- Macro-region expansions in the scoring prompt (EU = list of countries) — these are facts about LinkedIn / geography, not heuristics about postings.

Pattern: when tempted to write `if "data labeling" in body.lower(): score = 0`, instead write that signal into the prompt as a rule for the scorer. The model can then weigh it in context against legitimate exceptions.
