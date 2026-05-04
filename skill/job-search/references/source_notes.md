# Source notes and caveats

Reference material for understanding / debugging each source adapter.

## HackerNews "Who is Hiring?"

- **Endpoint**: Algolia search + Firebase item API. Both are free, unauthenticated, rate-friendly.
- **Cadence**: The thread is posted on the 1st of each month around 11:00 AM PT by user `whoishiring`. Posts stay relevant for the whole month.
- **Quality**: High signal, mostly technical roles. ONSITE / REMOTE / VISA tags are a community convention — use the `remote` filter in config to leverage them.
- **Gotchas**:
  - The "latest" thread might be a few days old right after the month rolls over — the adapter's `max_age_hours` is softened (×30) for HN so it keeps working.
  - Posts aren't structured; the adapter takes the first 80 chars of the comment as the "title" and the substring before `|` as the "company". Good enough for a digest.

## RemoteOK (`https://remoteok.com/api`)

- **Endpoint**: Single JSON array at the root URL. The first element is a disclaimer — skip it.
- **Auth**: None needed.
- **Rate limits**: Be kind; once per day is fine.
- **Fields**: `position`, `company`, `location`, `url`, `tags`, `salary_min`, `salary_max`, `description`.
- **Gotchas**: Occasionally returns duplicates. Our dedupe handles that.

## Remotive (`https://remotive.com/api/remote-jobs`)

- **Endpoint**: JSON `{ jobs: [...] }`. Free, no auth.
- **Fields**: `title`, `company_name`, `candidate_required_location`, `url`, `salary`, `publication_date`, `description`, `tags`.

## WeWorkRemotely

- **Endpoint**: Per-category RSS. Standard RSS 2.0.
- **Fields**: `title` ("Company: Role Title"), `link`, `summary`, `published`.
- **Gotchas**: `summary` is HTML — the adapter takes the first 400 chars raw; Telegram formatter strips it further.

## Indeed

Removed. The legacy adapter relied on a hardcoded global query (`q="frontend
developer", l="Bilbao, Spain"`) baked into `config/filters.yaml`. With matching
now per-user-only, there's no equivalent default — Indeed RSS support has also
been quietly deprecated in many regions. To revive: ship a per-user adapter
that reads `profile.search_seeds.indeed` (currently unset by the profile
builder) and add an entry under `defaults.DEFAULTS["sources"]`.

## LinkedIn

**Enable at your own risk.** LinkedIn's TOS explicitly prohibits scraping, and
they litigate.

LinkedIn now runs **per-user only** — `linkedin.fetch_for_user(filters, seeds)`
consumes `profile.search_seeds.linkedin.queries` (up to 3) and returns
deduplicated results. There is no global LinkedIn query; users without an
Opus-built profile get no LinkedIn results.

- **Endpoint**: `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search` — the HTML fragment LinkedIn's own page uses for infinite-scroll. No auth required.
- **Useful params**:
  - `keywords` — free-text query
  - `location` — e.g. "Netherlands" or "Greater Amsterdam Area"
  - `f_TPR=r86400` — posted in the last 24h (`r604800` for last week, `r2592000` for last month)
  - `f_WT=2` — remote only
  - `f_E=2,3,4` — experience level (2=entry, 3=associate, 4=mid-senior, 5=director, 6=executive)
  - `start=0` — pagination offset (cards per page ≈ 25)
- **Rate limits**: Very aggressive. 429s appear after ~10 rapid requests. Adapter sleeps 1.5s between calls and skips the rest of the user's batch on 429.
- **HTML fragility**: The CSS selectors `li`, `.base-card`, `h3`, `h4`, `.job-search-card__location` are the current (2025) structure. If LinkedIn changes their markup, parsing breaks — fix selectors in `sources/linkedin.py`.

## Curated boards (AI-delegated): remocate.app / wantapply.com / remoterocketship.com

These three boards publish listings only via HTML — no RSS, no public JSON, and
Remote Rocketship's real feed is paid. Rather than writing brittle BeautifulSoup
selectors, `sources/curated_boards.py` shells out to the `claude` CLI once per
enabled board and asks Claude to fetch the page (via its built-in WebFetch /
WebSearch tools) and return a strict JSON payload. The result is fed back into
the normal pipeline, so downstream filters (`title_must_match`, `title_exclude`,
dedupe, per-user seen-list) apply as usual.

**Requirements on the machine that runs the cron:**
- The `claude` CLI is on PATH and already logged in.
- Network egress to the three domains.

**Cost:** ~1 Claude prompt per enabled board per run — three at most per day.

**Toggles:** `remocate`, `wantapply`, `remoterocketship` under
`defaults.DEFAULTS["sources"]`. All three are OFF by default.
`ai_scrape_timeout_s` (default 180) caps each sub-process.

**Failure modes (all silent — logged at WARN/ERROR, pipeline continues):**
- `claude` CLI missing → board skipped.
- CLI timed out → board skipped for this run.
- JSON parse failure → board skipped; raw output head logged.

**Why not direct HTTP scraping?** Tried it; the three boards change markup
often enough (and ship heavy client-side rendering) that static selectors
rot within weeks. Delegating to an LLM is more resilient at the cost of
a few cents per run.

## Open-web discovery (AI sub-agent with WebSearch + WebFetch)

`sources/web_search.py` runs a single `claude` CLI invocation per
orchestrator run that has both WebSearch and WebFetch available. The agent
forms up to four different queries, picks the most-promising results, and
uses WebFetch to open each posting's canonical detail page to extract title,
company, location, URL, and snippet. Results flow through the same dedupe +
post-filter + enrichment pipeline as every other source.

This adapter fills the gap the static-source adapters can't cover:
one-off postings on company career pages, ATS systems (greenhouse.io,
lever.co, ashbyhq.com, workable.com, personio.jobs, recruitee.com, workday,
smartrecruiters.com), regional job boards, engineering-team blogs, and
anywhere else a web search can reach.

**Requirements:**
- `claude` CLI on PATH, logged in.
- Network egress the agent can use for WebSearch + WebFetch.

**Cost:** one `claude -p` invocation per run, which internally issues
several WebSearch and WebFetch calls. Plan for ~60-120 seconds of wall clock.

**Toggle:** `sources.web_search` in `defaults.DEFAULTS`. ON by default —
gated per-user by whether the profile has `search_seeds.web_search` populated
or the user typed `/prefs`.

**Timeout:** `ai_web_search_timeout_s` (default 300). Falls back to
`ai_scrape_timeout_s` when unset.

**Domain exclusion:** The prompt tells the agent to skip the domains
already covered by dedicated adapters (RemoteOK, Remotive, WeWorkRemotely,
LinkedIn, HackerNews, remocate, wantapply, remoterocketship) so
we don't pay tokens to re-find what other adapters surface for free.

**Failure modes (silent, logged, pipeline continues):**
- CLI missing → returns [].
- CLI timeout / non-zero exit → returns [].
- Non-JSON response → returns [].

## Adding a new source

1. Drop `my_source.py` in `skill/job-search/scripts/sources/`.
2. Export `def fetch(filters: dict) -> list[Job]`.
3. Import it in `search_jobs.py` and add to the `SOURCES` dict.
4. Add a toggle under `sources:` in `defaults.DEFAULTS`.
5. Document quirks here.

## Debugging a bad run

- `python skill/job-search/scripts/search_jobs.py --dry-run` prints every job it finds without posting.
- Set `LOG_LEVEL=DEBUG` in the environment for verbose logs.
- Delete `state/seen.json` to force the next run to treat everything as new (useful for testing).
