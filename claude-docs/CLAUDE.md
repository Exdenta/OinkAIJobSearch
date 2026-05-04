# FindJobs Project Wiki — Schema

A Karpathy-style LLM Wiki for **this project's** knowledge that compounds across sessions.
Pattern source: https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

This is the **project-local** wiki at `FindJobs/claude-docs/`. The global wiki at `~/.claude/claude-docs/` covers Claude Code / Agent SDK / Anthropic API patterns. Cross-project knowledge → global. This-codebase knowledge → here.

## Layers

- `raw/` — immutable source documents (Telegram API docs, scraping notes, LinkedIn/Indeed TOS snapshots, resume samples, market-research transcripts, screenshots, prompt dumps). Read-only. Never edit.
- `wiki/` — LLM-maintained markdown pages. Entity, concept, source-summary, comparison, synthesis pages. **You own this entirely.**
- `index.md` — content-oriented catalog. One line per wiki page. Updated on every ingest.
- `log.md` — chronological append-only record of ingests, queries, and lint passes.
- `CLAUDE.md` — this file. The schema.

## Scope (project wiki only)

Knowledge specific to **the FindJobs Telegram job-alert bot**:

- Architecture (search_jobs.py orchestrator, long-running bot.py, SQLite layer, per-user file layout)
- Source scrapers (LinkedIn, Indeed, HackerNews "Who is Hiring", remote boards, curated boards) — selectors, rate limits, breakage notes
- Telegram bot UX (commands, inline keyboards, callback dispatch, sticker cache)
- Claude Opus orchestration (`/marketresearch` 10-worker + manager pattern, profile_builder, fit_analyzer, resume_tailor)
- Prompt-injection hardening (`safety_check`, opaque-data wrapping)
- DOCX rendering (market_research_render.py)
- Data model (users, jobs, applications, sent_messages, profile_builds, research_runs)
- Deployment (cron, launchd, systemd, nohup) and operational gotchas
- Per-user state lifecycle and `/cleardata` semantics
- Domain knowledge: hiring-market trends, salary benchmarks, job-board behaviors

If a discovery would help in *any* project (Claude API patterns, hooks, prompt-caching tactics) → put it in the global wiki.

## Operations

### Ingest
When a source lands in `raw/` and the user asks to ingest it:
1. Read the source.
2. Briefly discuss key takeaways with the user.
3. Write a summary page in `wiki/` (filename: `<slug>.md`).
4. Update `wiki/` entity/concept pages the source touches. Add cross-references (relative markdown links).
5. Update `index.md` — add the new page, revise touched pages' summaries if they shifted.
6. Append to `log.md`: `## [YYYY-MM-DD] ingest | <source title>` + 1–3 lines on what changed.
7. Flag contradictions with existing pages in the log entry.

A single ingest commonly touches 5–15 wiki pages. Expected.

### Query
When the user asks a question against the wiki:
1. Read `index.md` first to find candidate pages.
2. Read those pages, synthesize an answer with citations to wiki pages (and through them, `raw/` sources).
3. If the answer is novel and valuable (a comparison, an analysis, a discovered connection), **offer to file it back as a new wiki page**. Don't let good answers vanish into chat history.
4. Append to `log.md`: `## [YYYY-MM-DD] query | <short question>` + pages consulted.

### Lint
On health-check request, look for:
- Contradictions between pages
- Stale claims newer sources superseded (selector breakage, deprecated commands, schema drift)
- Orphan pages (no inbound links)
- Important concepts mentioned but lacking their own page
- Missing cross-references
- Data gaps a web search / repo read could fill

Append findings to `log.md`: `## [YYYY-MM-DD] lint | <summary>`.

## Page conventions

- Filenames are kebab-case slugs.
- Each page starts with a one-line summary suitable for `index.md`.
- Use frontmatter when useful:
  ```yaml
  ---
  type: entity | concept | source | synthesis
  sources: [<raw-file>, ...]
  updated: YYYY-MM-DD
  ---
  ```
- Cross-link with relative paths: `[Market research](./market-research.md)`.
- Cite sources with relative paths to `raw/`: `(see raw/linkedin-search-html-2026-04.html)`.
- Cite repo code with `path:line` so the user can jump: `skill/job-search/scripts/search_jobs.py:42`.

## Log entry format

Consistent prefix so `grep "^## \[" log.md | tail -10` works:
```
## [YYYY-MM-DD] <ingest|query|lint> | <short title>
- bullet of what changed / what was asked / what was found
```
