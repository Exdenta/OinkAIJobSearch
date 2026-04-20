# Telegram bot listing copy

Paste-ready text for the three description slots BotFather exposes, plus the
/commands list. Each section is under its Telegram character cap.

---

## 1. Description — shown in the empty chat before /start

Set via `@BotFather` → `/setdescription`. **512 character max.**

```
🐷  Your pocket job-search assistant.

Every morning I scan LinkedIn, Indeed, Hacker News, and curated remote boards — then deliver a shortlist ranked 0-5 against your CV so you only see roles worth your attention.

Tap any posting for:
•  Fit analysis — strengths, gaps, honest verdict
•  Tailored resume — rewritten for that specific role
•  Deep market research — demand, salary, hiring bar

Upload your CV and send /start to begin.
```

---

## 2. About text — shown on the bot's profile page

Set via `@BotFather` → `/setabouttext`. **120 character max.**

```
🐷 Daily job digest ranked to your CV. Per-role fit analysis, tailored resumes & deep market research.
```

---

## 3. Short description (catalog / search listings)

Some Telegram clients show a short bio in search results. **160 character max.**

```
Daily shortlist of jobs from LinkedIn, Indeed, HN & remote boards, ranked to your CV. Per-role fit analysis, tailored resumes, and deep market research.
```

---

## 4. Commands menu

Set via `@BotFather` → `/setcommands`. Paste this block as-is — each line is
`command - description`, no slash prefix. Keep it tight (Telegram shows these
in a popup the user can scroll, but shorter is more usable).

```
start - Set up or re-run the onboarding wizard
help - Show command reference
jobs - Scan job boards now
applied - List roles you've marked applied
prefs - Describe what you're looking for in plain English
myprofile - Show your current profile
minscore - Set the minimum match score (0-5)
rebuildprofile - Force a profile rebuild now
marketresearch - Deep market scan (25-40 min, DOCX report)
cleardata - Delete resume, history, profile, or everything
privacy - What data is stored and who sees it
```

---

## Notes on tone

All copy is written to be honest and concrete — no "AI-powered revolutionary"
hype, no emoji storms. Single pig keeps the personality consistent with the
in-bot mascot without feeling childish. The description leads with *what the
bot does for the user today* and only then names the AI features, because
users who land from a link don't know what "fit analysis" is until they see
the daily-digest context.

If you're planning to list this in a public catalog or run ads, consider
dropping the pig from the About and Short description (keep it in the full
Description where the longer format absorbs the whimsy) — catalog readers
scan at speed and a single emoji at the front of a 100-char line can read
as noise rather than personality.

## Setup steps in BotFather

1. `/mybots` → pick your bot
2. `Edit Bot` → `Edit Description` → paste section 1
3. `Edit Bot` → `Edit About` → paste section 2
4. `Edit Bot` → `Edit Commands` → paste section 4
5. (Optional) `Edit Bot` → `Edit Botpic` → upload a pig PNG if you want a
   matching avatar

Descriptions and commands take effect immediately for new users; existing
chats see the commands update within a minute or two.

## Privacy policy link

The full privacy policy lives at `docs/PRIVACY.md`. You'll want to:

1. Host it somewhere publicly readable — the simplest options are:
   - **GitHub**: push to the repo and link to the raw `docs/PRIVACY.md`
     at `https://github.com/<you>/<repo>/blob/main/docs/PRIVACY.md`
   - **Gist**: paste the Markdown into a public gist
   - **A static page** on any site you already run
2. Export the URL to the bot so `/privacy` can link to it:
   ```bash
   echo 'PRIVACY_POLICY_URL=https://<your-host>/PRIVACY.md' >> .env
   ```
   Restart the bot. `/privacy` in chat will now include a link to the
   full text. If the env var is unset, `/privacy` still returns the
   in-chat summary — the link line just drops to "ask the operator by
   email".
3. Set the same URL as the bot's external privacy link in BotFather
   (some Telegram catalogs surface this separately from the description).
   At the time of writing BotFather doesn't have a dedicated privacy URL
   field for regular bots — but you can include the URL in the About
   text if you shorten the other copy, or mention it at the end of the
   Description text.
