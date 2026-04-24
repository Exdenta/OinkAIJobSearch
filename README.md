# Hryu - Job Alert Bot in Telegram

![Welcome screen — orange-hat pig greeting the user in Telegram](assets/screenshots/welcome.png)

A daily job-posting digest + interactive Telegram bot. Scrapes LinkedIn, Indeed,
HackerNews "Who is Hiring", and remote-focused boards; filters by your criteria;
sends each new posting as its own Telegram message with inline buttons; tracks
which roles you've applied to; and can produce a "tailored resume note" for any
posting on demand.

**Try the live bot:** [@job_search_everyday_bot](https://t.me/job_search_everyday_bot) —
send `/start` to onboard.

## Architecture

```
┌────────────────────────┐     ┌──────────────────────────┐
│  cron / launchd /      │────▶│  search_jobs.py          │
│  systemd timer (daily) │     │   scrape → filter → DB   │
└────────────────────────┘     │   send digest + buttons  │
                               └───────────┬──────────────┘
                                           ▼
                               ┌──────────────────────────┐
                               │   Telegram chats         │
                               │   (per registered user)  │
                               └───────────┬──────────────┘
                                           │ button press
                                           ▼
┌────────────────────────┐     ┌──────────────────────────┐
│   Long-running bot.py  │◀────│  getUpdates long-poll    │
│   /start · CV upload   │     │  (callback_query events) │
│   Applied / Skip / ✍️  │     └──────────────────────────┘
└───────────┬────────────┘
            │ read/write
            ▼
┌────────────────────────┐
│  SQLite: state/jobs.db │
│   users, jobs,         │
│   applications,        │
│   sent_messages        │
└────────────────────────┘

Per-user files:   state/users/<chat_id>/resume.pdf
                  state/users/<chat_id>/resume.txt
                  state/users/<chat_id>/tailored/<job_id>.md
```

## Directory layout

```
FindJobs/
├── README.md
├── .env                      ← TELEGRAM_BOT_TOKEN, OPERATOR_CONTACT (gitignored)
├── .env.example
├── config/
│   └── filters.yaml          ← job-search criteria
├── docs/
│   ├── PRIVACY.md            ← privacy policy published with the bot
│   ├── per-user-profile-plan.md
│   └── telegram_listing.md
├── state/                    ← runtime data (gitignored)
│   ├── jobs.db               ← SQLite — auto-created
│   └── users/<chat_id>/      ← per-user resumes, tailored notes, research
├── tools/
│   ├── get_chat_id.py        ← helper (mostly obsolete now; use /start)
│   ├── demo_ui_to_user.py    ← one-shot UI demo sender
│   ├── capture_sticker_ids.py
│   └── fetch_fat_roll_pig.py
└── skill/
    └── job-search/
        ├── SKILL.md          ← skill definition Claude reads
        ├── scripts/
        │   ├── search_jobs.py         ← scheduled digest orchestrator
        │   ├── bot.py                 ← long-running Telegram bot
        │   ├── onboarding.py          ← /start wizard + resume intake
        │   ├── db.py                  ← SQLite layer
        │   ├── dedupe.py              ← Job dataclass + per-user dedupe
        │   ├── telegram_client.py
        │   ├── resume_tailor.py       ← skill-matching + markdown note
        │   ├── fit_analyzer.py        ← per-job fit scoring
        │   ├── pig_stickers.py        ← sticker cache + sender
        │   ├── profile_builder.py     ← Opus profile rebuild
        │   ├── market_research.py     ← /marketresearch orchestrator (10 Opus workers + manager)
        │   ├── market_research_render.py  ← DOCX renderer for ResearchRun
        │   ├── safety_check.py        ← prompt-injection gate for user input
        │   ├── prompts/               ← fit_analysis.txt, market_research_{demand,history,...,manager}.txt, profile_builder.txt
        │   ├── sources/
        │   │   ├── hackernews.py
        │   │   ├── remote_boards.py
        │   │   ├── curated_boards.py
        │   │   ├── web_search.py
        │   │   ├── indeed.py
        │   │   └── linkedin.py
        │   └── tools/
        │       └── reset_user.py      ← per-user history wipe
        └── references/
            └── source_notes.md
```

## Setup

### 1. Install dependencies

```bash
pip install --break-system-packages \
    requests pyyaml feedparser beautifulsoup4 python-dotenv pdfplumber
```

### 2. Put your bot token in `.env`

```bash
TELEGRAM_BOT_TOKEN=123456789:AAE-your-bot-token-here
```

No `TELEGRAM_CHAT_ID` needed anymore — users register themselves via `/start`.

### 3. Start the bot (long-running process)

```bash
python skill/job-search/scripts/bot.py
```

Leave this running. It handles `/start`, resume uploads, and button presses.
Stop with Ctrl-C. Re-run after code changes.

For production, wrap it in systemd / nohup / a Docker container:
```bash
nohup python skill/job-search/scripts/bot.py > bot.log 2>&1 &
```

### 4. Onboard each user

In Telegram, send `/start` to your bot, then upload your CV (PDF). The bot
saves it to `state/users/<chat_id>/resume.pdf`, extracts text, and you're in.
From here on, the daily digest will post to this chat.

### 5. Smoke-test the scheduled digest

```bash
python skill/job-search/scripts/search_jobs.py --dry-run
```

Prints what would be posted without actually sending or recording anything.

### 6. Schedule the daily digest

Pick any scheduler you like and point it at `search_jobs.py`. Examples:

**cron** (Linux / macOS):
```cron
0 8 * * *  cd /path/to/FindJobs && /usr/bin/python3 skill/job-search/scripts/search_jobs.py >> bot.log 2>&1
```

**launchd** (macOS): drop a `.plist` in `~/Library/LaunchAgents/` that runs
`python skill/job-search/scripts/search_jobs.py` on a `StartCalendarInterval`.

**systemd timer** (Linux): pair a `findjobs.service` (`ExecStart=python
skill/job-search/scripts/search_jobs.py`) with a `findjobs.timer`
(`OnCalendar=*-*-* 08:00:00`).

The long-running `bot.py` process is separate from the digest — keep it up
under systemd / nohup / Docker so `/start`, uploads, and button presses keep
working between digests.

## User experience

1. User sends `/start` → bot replies with instructions.
2. User uploads resume.pdf → bot saves and confirms word-count.
3. Every morning, the scheduled task posts each new matching job as its own
   message:

   ```
   Senior Frontend Developer (Remote EU)
   Acme Inc · Remote · $80k–$120k
   We need a React + TypeScript engineer to build our design system…
   linkedin

   [✅ Applied]  [🚫 Not applied]
   [✍️ Tailor my resume]
   ```

4. Clicking **✅ Applied** — records the application and this job will never
   reappear in any future digest.
5. Clicking **🚫 Not applied** — hides it (same dedupe effect but tracked
   separately so you can audit).
6. Clicking **✍️ Tailor my resume** — the bot compares your resume's skills
   against the posting, saves a Markdown note at
   `state/users/<chat_id>/tailored/<job_id>.md`, and sends it back as a file
   attachment. The note only rearranges emphasis — it does not invent
   experience.

### Bot commands

| Command                | Action |
|------------------------|--------|
| `/start`               | Onboarding wizard + register your chat_id |
| `/help`                | Full command reference |
| `/jobs`                | Run a search now (on-demand, doesn't wait for the daily digest) |
| `/prefs`               | Send/update free-text preferences (triggers an Opus profile rebuild) |
| `/clearprefs`          | Wipe stored free-text preferences |
| `/minscore`            | Set the minimum match-score filter |
| `/myprofile`           | Show the current AI-built profile summary |
| `/rebuildprofile`      | Force-rebuild the profile from the current resume + free-text |
| `/applied`             | List every role you've marked applied |
| `/marketresearch`      | Deep market scan for your role + location — 10 Opus sub-agents with WebSearch/WebFetch run in parallel, a manager agent synthesizes, delivered as a polished `.docx` (~25–40 min) |
| `/cleardata`           | Scoped deletion menu (resume / history / tailored / profile / research / everything) |
| `/privacy`             | In-chat privacy summary + link to the full policy |

## /marketresearch — deep market research

Requires: resume uploaded + profile built (`/prefs`).

The bot asks for a target location (send `.` to reuse the location from your
profile, or type a market like `Berlin, Germany` / `Remote EU`). A
per-user lock ensures only one run at a time. Behind the scenes:

1. 10 Opus sub-agents run in parallel, each with WebSearch + WebFetch and a
   narrow topic:
   - current demand & volume
   - 24-month historical context
   - current industry trends
   - your resume skills vs. the market (skill table)
   - 12-18 month projections
   - salary in your home market
   - salary in neighboring markets
   - company landscape (top employers)
   - interview & hiring bar
   - recommended upskilling plan
2. A manager agent synthesizes the ten JSON outputs, dedups sources, and
   renumbers citations globally.
3. `market_research_render.py` renders the result to a polished `.docx`
   (cover page, auto-populating Word TOC field, numbered references, clickable
   URLs, skill + salary tables).
4. The bot sends the DOCX as an attachment plus a short Telegram summary
   (executive-summary bullets). Each run is logged to `research_runs` with
   status / elapsed_ms / worker ok+fail counts / input hashes / docx path.

Failure policy: ≥5 worker failures → report aborted; 1-4 → partial report
with a notice listing failed topics; manager crash on an otherwise OK run
demotes it to partial. Saved runs and generated .docx files live under
`state/users/<chat_id>/research/` and are wiped by `/cleardata → 🔬 Research`.

Prompt injection hardening: every sub-agent prompt wraps the candidate's
inputs in opaque-data blocks with an instruction-ignore preamble; the user's
location input passes through the same `safety_check.check_user_input` gate
as `/prefs` before it reaches any Claude call.

## Data model (SQLite)

- **users** — chat_id, resume_path, resume_text, prefs_free_text (raw /prefs
  input), user_profile (Opus-built JSON profile)
- **jobs** — every posting ever seen (stable job_id = sha1 of source+url)
- **applications** — (chat_id, job_id) → status ∈ {applied, skipped, interested}
- **sent_messages** — (chat_id, message_id) → job_id, so callbacks can resolve
- **profile_builds** — audit log (status, error, elapsed_ms, input hashes) for
  every Opus profile build
- **research_runs** — audit log (status, elapsed_ms, workers_ok/workers_failed,
  docx_path, input hashes) for every `/marketresearch` run

Deletion: to reset a user, `DELETE FROM users WHERE chat_id=?` and drop their
`state/users/<chat_id>/` folder. To wipe job history, delete `state/jobs.db`.

## Extending

- **Add a source**: drop `my_source.py` into `skill/job-search/scripts/sources/`,
  expose `fetch(filters) -> list[Job]`, register in `search_jobs.py:SOURCES`,
  add a toggle in `filters.yaml:sources`.
- **Change button behavior**: edit `telegram_client.py::job_keyboard` and the
  callback dispatcher in `bot.py::handle_callback`.
- **Upgrade resume tailoring to a real LLM rewrite**: replace
  `resume_tailor.py::build_tailor_note` with an API call (Claude, GPT, etc.).

## Security notes

- `.env` contains your bot token — gitignored, don't commit it.
- LinkedIn and Indeed adapters scrape public search endpoints; both services
  disallow automated access in their TOS. Use low volume and accept the risk.
  Read `skill/job-search/references/source_notes.md`.
- Resume PDFs live on disk in `state/users/<chat_id>/resume.pdf`. If multiple
  people use this instance, secure the filesystem accordingly.
- If the bot token leaks: talk to [@BotFather](https://t.me/BotFather),
  `/revoke`, and paste the new one into `.env` then restart `bot.py`.
