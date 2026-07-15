# Hryu — Job Alert Bot in Telegram

![Welcome screen — orange-hat pig greeting the user in Telegram](assets/screenshots/welcome.png)

An AI-powered job-search agent that lives in Telegram. It scrapes 25+ job
sources, scores every posting against a profile Claude builds from your
resume, and sends only the real matches to your chat — each as its own card
with Applied / Skip / "Tailor my resume" buttons.

**Try the live bot:** [@job_search_everyday_bot](https://t.me/job_search_everyday_bot) —
send `/start` and upload your CV. No install needed.

## Ways to use this

| Path | You get | Cost |
|------|---------|------|
| **[Hosted bot](https://t.me/job_search_everyday_bot)** (recommended) | `/start` in Telegram, upload CV, done — zero setup, sources maintained for you | Free |
| **[oinkjobsearch.com](https://oinkjobsearch.com)** | A managed private instance: uptime, upgrades, and support handled for you | Paid |
| **[Apify actors](#apify-actors--the-scrapers-as-an-api)** | The scrapers behind this bot as clean JSON APIs for your own pipeline — proxies and anti-bot handled | Pay per result, from $0.90/1k |
| **Self-host this repo** | Full control: your keys, your data, your prompts — see [Setup](#setup) | Free, your infra |

Self-hosting is the developer path — expect Python, cron, and prompt
tweaking. The `/setup` Claude Code skill walks you through install, `.env`,
and scheduling end to end. If you just want job matches, use the hosted bot
above and skip the rest of this README.

## Who this is for

Job seekers who want matching roles pushed to Telegram instead of
doom-scrolling boards. The sources are strongest for:

- **Software engineers** (frontend, backend, full-stack, DevOps/SRE) —
  LinkedIn, HN "Who is Hiring", Welcome to the Jungle, Built In, Wellfound
  (startups/YC), plus EU tech boards (JustJoin.it, NoFluffJobs, Tecnoempleo,
  InfoJobs).
- **ML / AI engineers and data scientists** — aijobs.net (curated AI/ML/MLOps
  board) on top of all the general tech sources.
- **Researchers and academics** (PhD positions, postdocs, faculty) — EURAXESS,
  jobs.ac.uk, AcademicPositions, Ikerbasque, university doctoral boards.
- **Humanitarian / international-development professionals** — ReliefWeb,
  ImpactPool, DevEx, UN/INGO portals.
- **Remote-first job hunters in any of the above** — dedicated remote boards
  plus EU-wide vacancies via EURES.

If you're a **self-hoster / developer**: run your own instance (for
yourself, friends, or a community), add sources, tweak the matching
prompts — this README is your setup guide.

**Not for**: recruiters sourcing candidates, bulk scraping, or hosting a
public multi-tenant service — the scrapers are deliberately low-volume and
some sources' TOS restrict use to personal job search.

## What it does

1. **Scrapes 25+ sources** — LinkedIn, HN "Who is Hiring", Wellfound,
   EURES, ReliefWeb, remote-work boards, EU tech boards, academic/research
   boards (full list in `skill/job-search/scripts/sources/`).
2. **Builds your profile with Claude** — from your uploaded resume plus
   free-text preferences (`/prefs`), rebuilt by Opus on demand.
3. **Scores every posting with an LLM** — no keyword filters; a scoring
   prompt weighs each job against your profile and only matches above your
   `/minscore` threshold ship.
4. **Delivers each match as a Telegram card** — with a "who to write to"
   hiring contact found by a web-searching agent, and inline buttons to
   track applications or generate a tailored resume note.
5. **Runs deep market research on request** — `/marketresearch` fans out
   10 Opus agents and returns a polished `.docx` report on demand, salaries,
   and trends for your role + location.
6. **Remembers everything** — applied/skipped roles never reappear; history
   lives in a local SQLite DB you own.

## What you need to self-host

- **Python 3.10+** and the deps in `requirements.txt`.
- **A Telegram bot token** — free, from [@BotFather](https://t.me/BotFather).
- **Claude Code CLI** — the `claude` binary on PATH, authenticated (a
  Claude subscription is enough; no API key required). All AI steps —
  scoring, profile builds, hiring-contact lookup, market research — run
  through `claude -p`. Without it the bot still scrapes but can't score
  or personalize.

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
├── requirements.txt
├── .env                      ← TELEGRAM_BOT_TOKEN, OPERATOR_CONTACT,
│                                APIFY_TOKEN (optional) (gitignored)
├── .env.example
├── deploy/                   ← systemd units, Caddy config, VPS bootstrap scripts
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
        │   ├── sources/               ← 25+ adapters, one file per board
        │   │   ├── hackernews.py, linkedin.py, wellfound.py, eures.py,
        │   │   ├── reliefweb.py, remote_boards.py, curated_boards.py,
        │   │   └── … (see the directory for the full list)
        │   └── tools/
        │       └── reset_user.py      ← per-user history wipe
        └── references/
            └── source_notes.md
```

## Setup

### 1. Install dependencies

```bash
pip install --break-system-packages -r requirements.txt
# or, better, in a venv:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

### 2. Put your bot token in `.env`

```bash
TELEGRAM_BOT_TOKEN=123456789:AAE-your-bot-token-here
```

No `TELEGRAM_CHAT_ID` needed anymore — users register themselves via `/start`.

Optional: set `APIFY_TOKEN` to unblock AcademicPositions via the Apify
actors — see [Apify actors](#apify-actors--the-scrapers-as-an-api) below.

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

### 6. Schedule searches

Two modes — pick one.

**Continuous mode (recommended).** `bot.py` itself runs the search loop
every couple of hours for a single chat. Quality is gated by the per-user
buffer (P1) and pagination by the source-page cursors (P2), so the loop
never re-fetches the same source page within 6h and only flushes ≥4-scored
matches. Enable by setting two env vars before launching `bot.py`:

```bash
export HRYU_CONTINUOUS_MODE=1
export HRYU_CONTINUOUS_CHAT_ID=433775883     # your Telegram chat_id
python skill/job-search/scripts/bot.py
```

With these set, the daily cron entry below is no longer needed — remove
it (`crontab -e`) before flipping continuous mode on, otherwise the same
search will run twice. The interval is tunable in `defaults.py`
(`continuous_interval_seconds`, default 7200).

Continuous mode is single-user — only `HRYU_CONTINUOUS_CHAT_ID` is driven
by the in-process loop. Other users still use on-demand `/jobs` taps.

**Cron mode (legacy).** Point any scheduler at `search_jobs.py`. Examples:

```cron
0 8 * * *  cd /path/to/FindJobs && /usr/bin/python3 skill/job-search/scripts/search_jobs.py >> bot.log 2>&1
```

**launchd** (macOS): drop a `.plist` in `~/Library/LaunchAgents/` that runs
`python skill/job-search/scripts/search_jobs.py` on a `StartCalendarInterval`.

**systemd timer** (Linux): pair a `findjobs.service` (`ExecStart=python
skill/job-search/scripts/search_jobs.py`) with a `findjobs.timer`
(`OnCalendar=*-*-* 08:00:00`).

The long-running `bot.py` process is separate from the cron-fired digest —
keep it up under systemd / nohup / Docker so `/start`, uploads, and button
presses keep working between digests.

## User experience

1. User sends `/start` → bot replies with instructions.
2. User uploads resume.pdf → bot saves and confirms word-count.
3. Every morning, the scheduled task posts each new matching job as its own
   message:

   ```
   Senior Frontend Developer (Remote EU)
   Acme Inc · Remote · $80k–$120k
   We need a React + TypeScript engineer to build our design system…

   👤 Jane Doe — Senior Technical Recruiter
   recruits frontend engineers at Acme, covers EMEA
   linkedin

   [✅ Applied]  [🚫 Not applied]
   [✍️ Tailor my resume]
   ```

   The 👤 line is the **hiring contact** — for every card about to ship, a
   Claude agent (WebSearch + WebFetch, `hiring_contact.py`) hunts for the
   one person who most plausibly owns the opening: the recruiter named on
   the posting, the talent-acquisition partner covering that function and
   region, the hiring manager, or a founder at a tiny startup. The name
   links to their public profile (LinkedIn `/in/…` preferred) and the
   italic line says why this person was picked. Verdicts are cached per
   posting in the `hiring_contacts` table, lookups run only for jobs that
   survived every send gate, and any failure just ships the card without
   the block. Knobs: `HIRING_CONTACT_OFF=1` disables the pass;
   `HIRING_CONTACT_TIMEOUT_S` (default 180), `HIRING_CONTACT_WORKERS`
   (default 3), `HIRING_CONTACT_MODEL` (default sonnet) tune it.

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
- **hiring_contacts** — per-job cache of "who to write to" lookups
  (status ∈ {found, not_found} + the contact dict); transport errors are
  never cached so they retry on the next send

Deletion: to reset a user, `DELETE FROM users WHERE chat_id=?` and drop their
`state/users/<chat_id>/` folder. To wipe job history, delete `state/jobs.db`.

## Extending

- **Add a source**: drop `my_source.py` into `skill/job-search/scripts/sources/`,
  expose `fetch(filters) -> list[Job]`, register in `search_jobs.py:SOURCES`,
  add a toggle in `defaults.py:DEFAULTS["sources"]`.
- **Change button behavior**: edit `telegram_client.py::job_keyboard` and the
  callback dispatcher in `bot.py::handle_callback`.
- **Upgrade resume tailoring to a real LLM rewrite**: replace
  `resume_tailor.py::build_tailor_note` with an API call (Claude, GPT, etc.).

## Apify actors — the scrapers as an API

Every scraper behind Hryu is also published as a standalone Apify actor —
call any source as a clean JSON API from your own pipeline, no bot
required. These are the same actors this bot runs on in production every
day, and they handle the hard stuff for you: residential proxies,
DataDome/Cloudflare bypass, pagination, dedupe, and delta mode so you pay
only for postings you haven't seen. No subscription — pay per result.

| Actor | What it does |
|-------|--------------|
| [linkedin-scraper](https://apify.com/nomad-agent/linkedin-scraper) | LinkedIn Jobs without login — delta mode for scheduled alerts, $0.90/1k results. |
| [all-jobs-scraper](https://apify.com/nomad-agent/all-jobs-scraper) | 19 job boards behind one endpoint — the whole fleet in a single call, from $1.20/1k. |
| [ai-job-search-agent](https://apify.com/nomad-agent/ai-job-search-agent) | Full AI job search as an API — describe the candidate, get scored matches. No API key needed; AI cost included. |
| [company-careers-bundle](https://apify.com/nomad-agent/company-careers-bundle) | Turn a company list into live postings by probing Greenhouse, Lever, Ashby, Workable, SmartRecruiters and Workday. |
| [europe-jobs-bundle](https://apify.com/nomad-agent/europe-jobs-bundle) | 14 Europe-focused sources (EURES, EURAXESS, WTTJ, JustJoin.it, NoFluffJobs, InfoJobs, Tecnoempleo, jobs.ac.uk and more) behind one endpoint. |
| [researcher-bundle](https://apify.com/nomad-agent/researcher-bundle) | 12-source academic/research aggregator (EURAXESS, jobs.ac.uk, EURES, AcademicPositions, ReliefWeb, Impactpool, Devex, Ikerbasque, LinkedIn + 2 university boards). |
| [remote-boards-scraper](https://apify.com/nomad-agent/remote-boards-scraper) | 4 remote-job boards in one run — RemoteOK, Remotive, WeWorkRemotely, Himalayas. |
| [eures-scraper](https://apify.com/nomad-agent/eures-scraper) | EURES — 2M+ live EU vacancies across 31 countries, official EU job-mobility portal. |
| [euraxess-scraper](https://apify.com/nomad-agent/euraxess-scraper) | EURAXESS — the EU's official researcher-mobility portal: PhD, postdoc, fellowship and faculty roles. |
| [jobs-ac-uk-scraper](https://apify.com/nomad-agent/jobs-ac-uk-scraper) | jobs.ac.uk — UK academic, postdoc and research jobs (lectureships, fellowships, PhD studentships). |
| [academicpositions-scraper](https://apify.com/nomad-agent/academicpositions-scraper) | Postdoc, PhD and faculty jobs from academicpositions.com across EU, UK and Switzerland. |
| [impactpool-scraper](https://apify.com/nomad-agent/impactpool-scraper) | Impactpool.org — UN, NGO and international-development careers. |
| [unjobs-scraper](https://apify.com/nomad-agent/unjobs-scraper) | unjobs.org — UN and NGO vacancies across 143 agencies (UNICEF, WFP, UNDP, UNHCR...). |
| [ycombinator-was-scraper](https://apify.com/nomad-agent/ycombinator-was-scraper) | Y Combinator's Work at a Startup board — 1,000+ jobs with parsed salary, equity and visa policy. |

For several of these sources (EURAXESS, EURES, Impactpool, unjobs.org,
jobs.ac.uk, AcademicPositions) these are the only maintained scrapers on
Apify. **Full catalog — 50+ actors** covering jobs, search, app
intelligence and open data: [apify.com/nomad-agent](https://apify.com/nomad-agent).

<!-- TODO: append Apify fair-share affiliate parameter (e.g. ?fpr=<code>) to the links above once assigned -->

AcademicPositions is blocked in the self-hosted scraper (Cloudflare — see
`skill/job-search/references/source_notes.md`); set `APIFY_TOKEN` in
`.env` and it's pulled through the actor above instead. That's also the
easiest way to support this project: source fetches through the actors
are what fund its maintenance.

## Security notes

- `.env` contains your bot token — gitignored, don't commit it.
- LinkedIn and Indeed adapters scrape public search endpoints; both services
  disallow automated access in their TOS. Use low volume and accept the risk.
  Read `skill/job-search/references/source_notes.md`.
- Resume PDFs live on disk in `state/users/<chat_id>/resume.pdf`. If multiple
  people use this instance, secure the filesystem accordingly.
- If the bot token leaks: talk to [@BotFather](https://t.me/BotFather),
  `/revoke`, and paste the new one into `.env` then restart `bot.py`.

## License

[PolyForm Noncommercial 1.0.0](LICENSE.md) — free to use, modify, and share
for any noncommercial purpose (personal job search, research, education,
nonprofits). Commercial use is not permitted.

Required Notice: Copyright Lex Sherman
