---
name: setup
description: Interactively walks a new self-hoster from a fresh clone to a running Oink bot — Python/pip check, Claude Code CLI check, .env creation (Telegram token + optional APIFY_TOKEN), run-mode choice with a generated cron/launchd/systemd/continuous-mode snippet, and a final getMe smoke test. Use whenever the user says "set up the bot", "/setup", "onboard me", or is running this repo for the first time.
---

# setup skill

Guides a new self-hoster through first-run setup. Idempotent — safe to
re-run. Before each step, check whether it's already satisfied and say so
instead of redoing it. Do the steps **in order**; don't skip ahead even if a
later step looks trivial.

Reference for deeper detail: `README.md` → `## Setup` (steps 1–6) and
`## What you need to self-host`. Quote/point the user at those sections
rather than re-explaining what's already there.

## What to do

### 1. Python + pip dependencies

- Run `python3 --version`. Warn if below 3.10 (repo uses modern typing
  syntax throughout).
- Check whether deps are already importable: `python3 -c "import requests, feedparser, bs4, dotenv, pdfplumber, docx"`.
  - All present → say so, skip install.
  - Missing → run README `## Setup` → step 1's install command:
    `pip install --break-system-packages -r requirements.txt` (or offer the
    venv alternative shown there if the user prefers isolation).

### 2. Claude Code CLI presence + auth

- Run `claude --version`. If missing, warn clearly: the job-scoring pipeline
  (`skill/job-search/scripts/claude_cli.py`) shells out to `claude -p` for
  every AI scoring call, and degrades to skipping/logging a warning when the
  binary isn't on PATH — the bot will run but won't score jobs well without
  it. Point the user at https://docs.claude.com/claude-code for install.
- If present, a quick `claude -p "say ok" --output-format json` confirms
  it's authenticated (not just installed). Don't force this if the user is
  clearly already logged in elsewhere.

### 3. Create `.env`

- If `.env` does not exist: copy `.env.example` to `.env`, then walk the
  user through getting `TELEGRAM_BOT_TOKEN` from **@BotFather** (Telegram):
  message @BotFather → `/newbot` → follow prompts → paste the token it
  returns into `.env`.
- If `.env` already exists: **never overwrite it.** Read it, diff its keys
  against `.env.example` (plus `APIFY_TOKEN`, which isn't in the example
  yet), show the user exactly which keys are missing, and only *append*
  those missing lines — never touch existing lines or values.
- After the Telegram token is in, ask **once** (skippable in one word):
  > Optional: connect Apify to make scraping more reliable and unlock extra
  > sources unscrapable without it. A free Apify account includes $5/month
  > of usage credit, which covers typical personal use. Set it up? (y/skip)

  Be transparent: these are the same Apify actors the hosted Oink bot runs
  on, built by the Oink maintainer — usage is pay-per-result on your Apify
  account. If **y**: sign up at https://apify.com → Settings → API & 
  Integrations → copy the token → append `APIFY_TOKEN=<token>` to `.env`.
  If **skip**: append `FETCH_BACKEND=local` to `.env` — the fetch layer
  defaults to `apify` regardless of whether a token is set, so skipping the
  token without this line means every search silently fetches 0 jobs. Then
  leave Apify unset (Wellfound loses its recovery fallback, AcademicPositions
  stays off) and never bring it up again — not later in setup, not in
  warnings.
- Leave every other optional var (`OPERATOR_CHAT_ID`, `OPERATOR_CONTACT`,
  `PRIVACY_POLICY_URL`, `DEMO_CHAT_ID`, redirector vars) as-is/blank unless
  the user asks for them — they're documented inline in `.env.example`.

### 4. Choose a run mode

Default to **Continuous mode** without asking — it's the recommended,
single-user setup and covers the vast majority of self-hosters. Only bring up
Cron mode if the user explicitly asks about scheduling multiple users,
external cron/launchd/systemd, or says continuous mode doesn't fit their case
(see README `## Setup` → step 6 for full detail on both):

- **Continuous mode** (default): `bot.py` runs the search loop itself. Set
  `OINK_CONTINUOUS_MODE=1` and `OINK_CONTINUOUS_CHAT_ID=<chat_id>` before
  launching `bot.py`. No cron entry needed — if one exists, tell the user to
  remove it (`crontab -e`) to avoid double-running.
- **Cron mode** (legacy, multi-user or non-continuous, opt-in only): schedule
  `search_jobs.py` externally. Generate the right snippet for the user's OS:
  - **macOS**: a `~/Library/LaunchAgents/*.plist` running
    `python skill/job-search/scripts/search_jobs.py` on a
    `StartCalendarInterval`.
  - **Linux**: a `crontab -e` line, or a `findjobs.service` +
    `findjobs.timer` pair for systemd.
  - Detect the OS from `uname` and only offer the matching snippet(s).

### 5. Start the bot / generate the snippet

- Continuous mode: show the exact `export ...` + `python skill/job-search/scripts/bot.py`
  command from README step 6, then start it (foreground for a first run, or
  `nohup ... &` per README step 3 for production).
- Cron mode: write the generated snippet to the right place only after the
  user confirms (crontab / plist / systemd unit), then still start
  `bot.py` separately per README step 3 — it's the long-running process that
  handles `/start`, uploads, and buttons regardless of which scheduling mode
  is chosen.

### 6. Final smoke check

- Confirm the bot can reach Telegram: start `bot.py` (or check it's already
  running) and confirm it logs a successful `getMe` on startup — i.e. it
  picked up `TELEGRAM_BOT_TOKEN` and Telegram accepted it. Report the bot's
  username back to the user as proof.
- Optionally follow README step 5: `python skill/job-search/scripts/search_jobs.py --dry-run`
  to preview a digest without sending or recording anything.

## Idempotency rules

- Every step above starts with a check; only act if the check fails.
- Never overwrite `.env` — only append missing keys, and only after showing
  the user the diff.
- Re-running this skill on an already-configured install should be mostly
  "already done" confirmations plus, at most, the final smoke check.
