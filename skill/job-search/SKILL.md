---
name: job-search
description: Runs the daily job-alert pipeline. Scrapes LinkedIn (per-user), HackerNews "Who is Hiring", and remote-focused boards; matches each posting against each user's Opus-built profile via a Haiku score gate; deduplicates via SQLite; posts new postings to Telegram with inline buttons (Applied / Not applied / Tailor my resume). Use whenever the user says "run job search", "check for new jobs", or the scheduled task fires.
---

# job-search skill

Entrypoint for the daily Telegram job-alert bot. The system has two moving parts:

1. **Scheduled digest** (this skill) — runs once a day via Cowork's `schedule` skill. Scrapes sources, writes to `state/jobs.db`, posts per-job messages with inline buttons to every registered user.
2. **Long-running bot** (`skill/job-search/scripts/bot.py`) — handles `/start`, CV uploads, and button presses. Must be running continuously elsewhere (local machine, VPS, etc.).

This skill is only responsible for the **scheduled digest**. Don't touch the bot process from here.

## When to use

- A Cowork scheduled task fires with a prompt like "run the job-search skill".
- The user says "check for new jobs", "run my digest", etc.
- Manual runs: "preview what would be posted today".

## What to do

1. **Sanity checks.** Confirm these files exist relative to the project root:
   - `.env` with `TELEGRAM_BOT_TOKEN` set (chat IDs come from the DB now)
   - `skill/job-search/scripts/search_jobs.py`
   - `skill/job-search/scripts/defaults.py` (operational defaults — sources, timeouts, score floor)

2. **Check there's a registered user.** Query the DB:
   ```bash
   sqlite3 state/jobs.db "SELECT chat_id, resume_path IS NOT NULL AS has_resume FROM users;"
   ```
   If no rows, stop and tell the user to `/start` the bot and upload their CV first.

3. **Run the pipeline.**
   ```bash
   python skill/job-search/scripts/search_jobs.py
   ```
   - `--dry-run` prints results without posting or touching `sent_messages`.
   - `--chat-id <id>` scopes the run to a single user (useful for testing).

4. **Report back.** After the script exits, summarize:
   - Total postings found (and per source).
   - How many were new *for each user* after dedupe.
   - How many Telegram messages were sent.
   - Any source errors (e.g. LinkedIn 429) — mention but don't fail the run.

5. **Don't spam on empty days.** If `message.quiet_if_empty: true` is set in `defaults.DEFAULTS` and a user has 0 new postings, skip sending them anything.

## Common failure modes

- **`ModuleNotFoundError`** — run `pip install --break-system-packages requests feedparser beautifulsoup4 python-dotenv pdfplumber`.
- **`no such table: users`** — DB schema hasn't been created. The first bot or search run auto-creates it; if not, ensure `state/` is writable.
- **401 from Telegram** — token revoked; stop and tell the user to update `.env`.
- **400 "chat not found"** — user is in the DB but has blocked the bot. Either delete that user row or ignore that exit code on their message.
- **429 from LinkedIn** — skip that user's per-user LinkedIn batch for today, mention in report.

## Files you may edit

- `skill/job-search/scripts/defaults.py` when the user asks to change operator-level toggles (sources, timeouts, default score floor, message format). Per-user matching criteria live on the user's profile (`/prefs` and the Opus profile builder) — don't bake them in here.

## Files you must NOT modify during a run

- `.env` (credentials)
- anything under `skill/job-search/scripts/` other than `defaults.py`
- `state/jobs.db` except via the scripts themselves

## Related docs

- `README.md` — full setup walkthrough and architecture diagram
- `skill/job-search/references/source_notes.md` — per-source quirks and TOS caveats
