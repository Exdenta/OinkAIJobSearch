# Monitoring + Reporting System — Plan

> Three-slice design: parallel-implementable storage / capture / delivery layers
> with zero file overlap. Wiring into existing modules is done by hand after
> the slices land. The seam between slices is the `MonitorStore` API in slice A.

---

## 0. Goals & Non-Goals

**In scope** — six pillars: source-health, run telemetry, Claude-call cost surrogate, error capture + delivery, operator commands, daily summary.

**Out of scope** — token-exact billing (requires SDK migration off `claude` CLI); log rotation; Prometheus/external sinks (stdlib only, plus `requests`).

**Hard constraint** — three slices A/B/C have **zero file overlap**.

---

## 1. Architecture

```
                                           ┌─────────────────────────────┐
 [search_jobs.run]─────┐                   │ telemetry/                  │
 [bot._dispatch ]──────┤                   │  ├ pipeline_runs            │
 [bot.handle_callback]─┼── instrumented ──►│  ├ source_runs              │
 [bot._run_market_research_work]           │  ├ claude_calls             │
 [claude_cli.run_p / run_p_with_tools]     │  ├ error_events (rate-lim)  │
                                           │  └ ops_toggles              │
                                           └─────────────────────────────┘
                                                      │
                                                      ▼
                                           ┌─────────────────────────────┐
                                           │ ops/                        │
                                           │  ├ alerts.py  (deliver)     │
                                           │  ├ summary.py (digest)      │
                                           │  └ commands.py (/health…)   │
                                           └─────────────────────────────┘
```

All new tables live in `state/jobs.db`. Slice C imports nothing from slice B; both depend only on slice A.

---

## 2. Data model — full DDL

```sql
-- One row per orchestrator invocation.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL,             -- daily_digest | market_research | manual_check
    triggered_by    INTEGER,                      -- chat_id when user-initiated; NULL for cron
    status          TEXT    NOT NULL,             -- ok | partial | failed | exception
    exit_code       INTEGER,                      -- search_jobs.run exit code (0/1/2/3); NULL for bot-side runs
    users_total     INTEGER NOT NULL DEFAULT 0,
    jobs_raw        INTEGER NOT NULL DEFAULT 0,
    jobs_sent       INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    extra_json      TEXT,                         -- web_hits, linkedin_user_hits, anomalies[]
    started_at      REAL    NOT NULL,
    finished_at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_kind ON pipeline_runs(kind, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_when ON pipeline_runs(finished_at DESC);

-- Per-source per-run health.
CREATE TABLE IF NOT EXISTS source_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_run_id INTEGER NOT NULL,             -- FK → pipeline_runs.id
    source_key      TEXT    NOT NULL,             -- hackernews | linkedin | …
    user_chat_id    INTEGER,                      -- NULL global; set for per-user (linkedin/web_search)
    status          TEXT    NOT NULL,             -- ok | partial | failed | suspicious_zero
    raw_count       INTEGER NOT NULL DEFAULT 0,
    elapsed_ms      INTEGER,
    error_class     TEXT,
    error_head      TEXT,                         -- first 200 chars of err
    started_at      REAL    NOT NULL,
    finished_at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_runs_run    ON source_runs(pipeline_run_id);
CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source_key, finished_at DESC);

-- One row per Claude CLI subprocess invocation. Cost is a SURROGATE — see §6.
CREATE TABLE IF NOT EXISTS claude_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_run_id  INTEGER,
    chat_id          INTEGER,
    caller           TEXT    NOT NULL,            -- 'job_enrich' | 'profile_builder' | 'market_research:worker' | …
    model            TEXT,                        -- 'haiku' | 'opus' | 'sonnet' | NULL (CLI default)
    prompt_chars     INTEGER NOT NULL DEFAULT 0,
    output_chars     INTEGER NOT NULL DEFAULT 0,
    elapsed_ms       INTEGER NOT NULL DEFAULT 0,
    exit_code        INTEGER,
    status           TEXT    NOT NULL,            -- ok | timeout | non_zero | cli_missing | exception
    cost_estimate_us INTEGER NOT NULL DEFAULT 0,  -- micro-USD; see §6 formula
    started_at       REAL    NOT NULL,
    finished_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claude_calls_when   ON claude_calls(finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_claude_calls_caller ON claude_calls(caller, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_claude_calls_run    ON claude_calls(pipeline_run_id);

-- Captured exceptions; UNIQUE enforces per-fingerprint per-hour rate limit.
CREATE TABLE IF NOT EXISTS error_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT    NOT NULL,             -- sha1(error_class + last-frame file:line + first 80 chars of msg)
    hour_bucket     INTEGER NOT NULL,             -- floor(unixtime / 3600)
    where_          TEXT    NOT NULL,             -- 'search_jobs.run' | 'bot._dispatch' | …
    error_class     TEXT    NOT NULL,
    message_head    TEXT,                         -- first 200 chars of str(exc)
    stack_tail      TEXT,                         -- last 8 frames
    chat_id         INTEGER,
    delivered_at    REAL,                         -- when alert sent; NULL = suppressed by rate-limit
    occurred_at     REAL    NOT NULL,
    UNIQUE (fingerprint, hour_bucket)
);
CREATE INDEX IF NOT EXISTS idx_error_events_when ON error_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_error_events_fp   ON error_events(fingerprint, hour_bucket);

-- Tiny KV store for op-toggles (alerts on/off, quiet_alerts).
CREATE TABLE IF NOT EXISTS ops_toggles (
    key         TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL,
    updated_at  REAL    NOT NULL
);
```

---

## 3. Module boundaries — three parallel slices

All new modules under `skill/job-search/scripts/`. Each new sub-package has a `__init__.py` re-exporting its public surface.

### Slice A — Storage layer (Agent A)

**Owns**: schema + DAL + fingerprinting + cost helper. No imports from B or C. No Telegram, no claude_cli.

| File | Spec |
|------|------|
| `telemetry/__init__.py` | Re-exports `MonitorStore`, `MONITOR_SCHEMA`, status string enums (`RunStatus`, `SourceStatus`, `CallStatus`). |
| `telemetry/schema.py` | `MONITOR_SCHEMA` (full DDL above as triple-quoted string) and `migrate(conn: sqlite3.Connection)` running `executescript(MONITOR_SCHEMA)` — idempotent. |
| `telemetry/store.py` | `MonitorStore` — single typed surface over `state/jobs.db`. Wraps a `db.DB` instance (does NOT open its own connection — uses `db._conn()` context manager pattern). Public methods: `start_pipeline_run(kind, triggered_by) -> int`, `finish_pipeline_run(run_id, status, exit_code, users_total, jobs_raw, jobs_sent, error_count, extra)`, `record_source_run(run_id, source_key, status, raw_count, elapsed_ms, *, user_chat_id=None, error_class=None, error_head=None, started_at, finished_at)`, `record_claude_call(...)`, `try_record_error(fingerprint, hour_bucket, where, error_class, message_head, stack_tail, chat_id) -> int | None` (returns inserted row id or None if rate-limited via UNIQUE constraint), `mark_alert_delivered(event_id)`, `last_source_run_per_source() -> list[Row]`, `recent_pipeline_runs(limit) -> list[Row]`, `claude_call_window_summary(since_ts) -> dict`, `recent_errors(since_ts) -> list[Row]`, `consecutive_zero_runs(source_key, n=3) -> bool`, `get_toggle(key, default)`, `set_toggle(key, value)`, `pipeline_run_with_sources(run_id) -> tuple[Row, list[Row]]`. |
| `telemetry/fingerprint.py` | Stateless: `error_fingerprint(exc) -> str` (sha1 of `error_class + last-frame "file:line" + first 80 chars of msg`), `format_stack_tail(tb, n=8) -> str` (last n frames `"<file>:<line> in <fn>"` newline-joined), `hour_bucket(ts=None) -> int` (`int(time.time() // 3600)`). |
| `telemetry/cost.py` | `estimate_cost_us(model, prompt_chars, output_chars) -> int` (micro-USD). Constants `_PRICES` keyed by alias. Char→token ratio `_CHARS_PER_TOKEN = 4.0`. `_PRICES` placeholder values (haiku 1/5, sonnet 3/15, opus 15/75 per Mtok in/out — replace with current Anthropic public rates). `None` model alias falls back to opus (pessimistic). |
| `telemetry/tests/smoke_store.py` | Smoke test (script style like `tests/smoke_profile_builder.py`). Temp DB. Exercises every method, the rate-limit on `try_record_error`, cost math per model alias. |

### Slice B — Capture / instrumentation primitives (Agent B)

**Owns**: reusable wrappers used by hand at existing call sites. **Imports `telemetry.MonitorStore` only.** No Telegram. Slice C imports nothing from B.

| File | Spec |
|------|------|
| `instrumentation/__init__.py` | Re-exports `pipeline_run`, `source_run`, `claude_call`, `error_capture`, `wrapped_run_p`, `wrapped_run_p_with_tools`. |
| `instrumentation/contexts.py` | Four context managers: (1) `pipeline_run(store, kind, triggered_by=None)` — yields `PipelineCtx` with `set_users_total(n)`, `set_jobs_raw(n)`, `set_jobs_sent(n)`, `incr_errors(n)`, `record_extra(k, v)`, `set_exit_code(code)`. On exit writes `pipeline_runs` and propagates exceptions. Status: `ok` if no error and `error_count==0`; `partial` if `error_count>0`; `exception` if exception bubbled. (2) `source_run(store, run_id, source_key, *, user_chat_id=None)` — yields `SourceRunCtx` with `.set_count(n)`. On exception: status `failed`. On success: `ok`, OR `suspicious_zero` if `raw_count==0` AND `store.consecutive_zero_runs(source_key, 3)`. (3) `claude_call(store, caller, *, chat_id=None, pipeline_run_id=None, model=None)` — yields `CallCtx` whose `.record(prompt_chars, output_chars, exit_code, status)` is invoked exactly once. Never raises. (4) `error_capture(store, where, *, chat_id=None, alert_sink=None)` — catches `Exception` (NOT `BaseException`), fingerprints, calls `store.try_record_error`. If newly-recorded AND `alert_sink` provided, invokes `alert_sink(envelope)` (`AlertEnvelope` dataclass defined here). Always re-raises. |
| `instrumentation/wrappers.py` | `wrapped_run_p(store, caller, prompt, *, pipeline_run_id=None, chat_id=None, model=None, **kwargs)` and `wrapped_run_p_with_tools(...)`. 6-line passthroughs over `claude_cli.run_p` / `run_p_with_tools` — time, infer status from return (`None` → `cli_missing` or `non_zero`; non-empty → `ok`), measure `len(prompt)` and `len(stdout or '')`, write via `store.record_claude_call`. Drop-in replacement for existing call sites. |
| `instrumentation/tests/smoke_contexts.py` | Smoke test against temp `MonitorStore`. Verifies happy path, exception path (re-raises + records), suspicious_zero classification, claude_call status mapping, error_capture rate-limit short-circuits the alert_sink. |

### Slice C — Delivery / Operator surface (Agent C)

**Owns**: alert envelope rendering, daily summary, operator commands. **Imports `telemetry.MonitorStore` and `telegram_client.TelegramClient`.** Knows nothing about how telemetry got recorded.

| File | Spec |
|------|------|
| `ops/__init__.py` | Re-exports `is_operator`, `OPERATOR_CHAT_ID_ENV`, `deliver_alert`, `build_daily_summary`, `deliver_daily_summary`, `handle_operator_command`. |
| `ops/operator.py` | `OPERATOR_CHAT_ID_ENV = "OPERATOR_CHAT_ID"`. `is_operator(chat_id) -> bool` reads env, returns False on missing/unparseable (silent). |
| `ops/alerts.py` | `render_alert(envelope) -> str` (MarkdownV2 body per §7). `deliver_alert(tg, store, envelope) -> None` is the callable injected as `alert_sink`. Checks `store.get_toggle('alerts_enabled', '1') == '1'` and `is_operator` env is set; on green calls `tg.send_message(operator_chat_id, ..., parse_mode='MarkdownV2')` then `store.mark_alert_delivered(event_id)`. Catch-all `Exception` swallowed and `log.exception`'d — alert delivery must NEVER throw back into the captured path. |
| `ops/summary.py` | `build_daily_summary(store, run_id) -> str` reads `pipeline_run_with_sources(run_id)` and renders 4–6 line MarkdownV2 digest per §6. `deliver_daily_summary(tg, store, run_id) -> None` checks `quiet_alerts` toggle + `is_operator`, sends. |
| `ops/commands.py` | Four commands: `cmd_health`, `cmd_stats [24h|7d]`, `cmd_alerts [on|off|quiet on|off]`, `cmd_runlog [N]`. Each takes `(tg, store, chat_id, args: list[str]) -> None`. Top-level `handle_operator_command(tg, store, chat_id, text) -> bool` parses + dispatches. Returns False silently for non-operators (mirrors existing `_is_admin` ghosting at bot.py:887). Returns True if a command was handled (so the bot's `handle_command` short-circuits). |
| `ops/tests/smoke_ops.py` | Golden tests: each command's output against a seeded `MonitorStore` (temp DB), alert envelope golden, summary builder edge cases (zero raw, all sources failed, `quiet_alerts` on). Fake `tg` recorder — no live Telegram. |

**No file overlaps**: every path under `telemetry/`, `instrumentation/`, `ops/` is uniquely owned. The only contract between slices is the `MonitorStore` API surface, frozen at the start of slice A.

---

## 4. Integration touchpoints (HUMAN does these, post-merge)

### 4.1 Schema bootstrap

- `skill/job-search/scripts/db.py:158` — after `c.executescript(SCHEMA)` in `_init`, call `telemetry.schema.migrate(c)`. Idempotent.

### 4.2 Daily-digest pipeline (`search_jobs.py`)

- `search_jobs.py:163` — open `MonitorStore(db)` and wrap body in `with pipeline_run(store, 'daily_digest') as pctx:`.
- `search_jobs.py:151–157` — wrap each adapter call in `fetch_all` with `with source_run(store, pctx.run_id, key) as sctx:` + `sctx.set_count(len(fetched))`. Add `store, run_id` params to `fetch_all`.
- `search_jobs.py:304` — per-user LinkedIn: `with source_run(store, pctx.run_id, 'linkedin', user_chat_id=chat_id):`.
- `search_jobs.py:328` — per-user web_search: same pattern, key `'web_search'`.
- `search_jobs.py:439–446` — replace summary block with `pctx.set_users_total(...)`, `pctx.set_jobs_raw(...)`, `pctx.set_jobs_sent(...)`, `pctx.record_extra('web_hits', stats['web_search_hits'])`, `pctx.record_extra('linkedin_user_hits', stats['linkedin_user_hits'])`. Keep the `DIGEST_SUMMARY` log line (greppable telemetry).
- `search_jobs.py:447` — before `return`, call `ops.summary.deliver_daily_summary(tg, store, pctx.run_id)`. Internally suppressed by `quiet_alerts`.
- `search_jobs.py:163` — outermost `with error_capture(store, where='search_jobs.run', alert_sink=lambda env: ops.alerts.deliver_alert(tg, store, env))`. Build `tg` first (move construction up) so the sink has it.

### 4.3 Bot dispatch + callback (`bot.py`)

- `bot.py:2098` (start of `main()`) — `store = MonitorStore(db)`, thread it into `_dispatch` via closure.
- `bot.py:2129` — wrap `_dispatch(...)` call in `with error_capture(store, where='bot._dispatch', chat_id=upd.get('message',{}).get('chat',{}).get('id'), alert_sink=...):`.
- `bot.py:1211` (top of `handle_callback`) — `with error_capture(store, where='handle_callback', chat_id=chat_id, alert_sink=...):`.
- `bot.py:333` (`handle_command` dispatch) — before existing dispatch, `if ops.commands.handle_operator_command(tg, store, chat_id, text): return`. Falls through silently for non-operators.
- `bot.py:1550` (`_run_market_research_work`) — wrap body with `pipeline_run(store, 'market_research', triggered_by=chat_id)` AND `error_capture(store, where='market_research.work', chat_id=chat_id, alert_sink=...)`. Keep existing `db.log_research_run` (per-user audit, complementary).
- `bot.py:1107` (`trigger_job_check`) — `pipeline_run(store, 'manual_check', triggered_by=chat_id)`.

### 4.4 Claude CLI cost capture — call-site replace (no edits to `claude_cli.py`)

Swap each `run_p(...)` / `run_p_with_tools(...)` call with `wrapped_run_p(store, caller='...', ...)`:

- `job_enrich.py:284` — caller `'job_enrich'`. Highest volume.
- `profile_builder.py:387` — caller `'profile_builder'`. Module has `_run_p=` test injection seam — swap default.
- `sources/curated_boards.py:88` — caller `'curated_boards'`.
- `sources/web_search.py:306` — caller `'web_search'`.
- `fit_analyzer.py:179` — caller `'fit_analyzer'`, pass `chat_id`.
- `resume_tailor.py:191` (now `_load_prompt_template`-driven) — caller `'resume_tailor'`.
- `safety_check.py:156` — caller `'safety_check'`.
- `market_research.py:639, 663, 691, 1019` — `wrapped_run_p_with_tools`, caller `'market_research:worker'`. The module already accepts `_run_p_with_tools=` injection — swap default.

---

## 5. Operator-command wire format

All commands gated by `is_operator(chat_id)`. Non-operators silently ghost.

### `/health`

```
🩺 Health  · 2026-04-29 14:02 UTC

Sources (last run per source)
  hackernews      ok          12  · 4s ago
  remote_boards   ok          27  · 4s ago
  curated_boards  ok           8  · 4s ago
  linkedin        suspicious   0  · 22h ago  ⚠ 3rd zero-run
  web_search      failed         · 22h ago  TimeoutExpired

Errors (last 24h): 2
Toggles: alerts=on  quiet_alerts=off
```

Uses `last_source_run_per_source()` + `recent_errors(now-86400)`. MarkdownV2-escaped, monospace via triple-backtick block. Lines ≤ 60 chars.

### `/stats [24h|7d]`

```
📊 Stats  · last 24h | last 7d

Pipelines
  daily_digest        7  | 49     (ok 6 · partial 1)
  market_research     1  |  4
  manual_check        3  | 18

Users
  active (sent any)   5  | 11
  digests sent       31  | 218

Claude CLI
  calls              142  |  974
  by model: haiku 128 / opus 12 / sonnet 2
  by caller: job_enrich 88 · market_research:worker 12 · …
  prompt chars      1.2M | 8.9M
  est. cost (surr.) $0.31 | $2.18    surrogate · ±25%

Errors (delivered)    2 / 0
```

`/stats` defaults to two columns; `/stats 24h` collapses to one.

### `/alerts [on|off|quiet on|off]`

```
/alerts on            →  ✅ alerts ENABLED
/alerts off           →  🔕 alerts DISABLED
/alerts quiet on      →  🔕 daily summary suppressed
/alerts quiet off     →  ✅ daily summary enabled
/alerts               →  current: alerts=on  quiet_alerts=off
```

Persists via `set_toggle('alerts_enabled' | 'quiet_alerts', '1' | '0')`.

### `/runlog [N]` (default 10, max 50)

```
🧾 Runs (last 10)

  #2031  2026-04-29 13:00  daily_digest  ok      u=7 raw=142 sent=31 dur=4m21s
  #2030  2026-04-28 13:00  daily_digest  partial u=7 raw=119 sent=24 dur=4m07s  err=1
  ...
```

`recent_pipeline_runs(N)`. Fixed-column table inside MarkdownV2 code block. Each row ≤ 80 chars.

---

## 6. Daily summary delivery

Fires from `search_jobs.py:447`. Suppressed by `ops_toggles['quiet_alerts']='1'`. Format (MarkdownV2):

```
🐷 Daily digest · 2026-04-29

Users:  7  · raw 142  · sent 31
Sources:  hackernews 12 · remote_boards 27 · curated_boards 8
          linkedin (per-user) 3 · web_search 4 · 2 errors
Anomalies:  linkedin 0 results × 3 runs in a row ⚠
Run #2031 · 4m21s · exit 0
```

"Anomalies" omitted when empty (compresses to 4 lines). Source breakdown: 1 line if it fits, 2 if not.

---

## 7. Alert envelope

```python
@dataclass
class AlertEnvelope:
    where: str           # 'search_jobs.run' | 'bot._dispatch' | 'handle_callback' | 'market_research.work'
    error_class: str     # 'TimeoutExpired'
    message_head: str    # first 200 chars of str(exc)
    stack_tail: str      # last 8 frames, '\n'-joined
    chat_id: int | None
    occurred_at: float
    fingerprint: str
    event_id: int        # row id in error_events for delivery confirmation
```

Rendered Telegram body:

```
🚨 Bot error · 2026-04-29 14:02 UTC

Where:  bot.handle_callback
Class:  TimeoutExpired
Chat:   4567...8901

> claude_cli: timed out after 240s
> while running enrich_jobs_ai for chat 45678901

```stack tail (last 8 frames)
bot.py:1287 in handle_callback
bot.py:1834 in _start_fit_analysis
fit_analyzer.py:179 in analyze_fit
claude_cli.py:75 in run_p
…
```

fp: 7e8a9c… · suppressing dupes for 1h
```

**Truncation**: `message_head` ≤ 200 chars. `stack_tail` exactly the last 8 frames (or fewer); each frame ≤ 100 chars. Whole message ≤ 3500 chars (Telegram limit 4096); overflow trimmed from body, never from stack-tail block. Chat IDs partially redacted in body, full in DB.

**Rate-limit**: `error_events.UNIQUE(fingerprint, hour_bucket)`; `try_record_error` does `INSERT … ON CONFLICT DO NOTHING` and returns row id or None. `error_capture` invokes `alert_sink` only when newly inserted.

---

## 8. Cost surrogate formula

```python
_CHARS_PER_TOKEN = 4.0  # rule-of-thumb; English mix; understates ~15% on code-heavy prompts

# Per-Mtok rates (USD). REPLACE WITH CURRENT Anthropic public pricing at impl time:
_PRICES = {
    'haiku':  {'in': 1.00,  'out': 5.00},   # placeholder
    'sonnet': {'in': 3.00,  'out': 15.00},  # placeholder
    'opus':   {'in': 15.00, 'out': 75.00},  # placeholder
    None:     {'in': 15.00, 'out': 75.00},  # CLI default → assume opus (pessimistic)
}

def estimate_cost_us(model, prompt_chars, output_chars):
    p_tok = prompt_chars / _CHARS_PER_TOKEN
    o_tok = output_chars / _CHARS_PER_TOKEN
    rates = _PRICES.get(model, _PRICES[None])
    return int(round((p_tok * rates['in'] + o_tok * rates['out'])))  # micro-USD
```

Stored as int micro-USD. Disclaimer rendered next to every cost number: `"surrogate · ±25% · until SDK migration"`.

---

## 9. Risks / open questions

1. **`error_capture` flooding** — wrapping `_dispatch` records every callback bug as an alert. Per-fp rate-limit is 1/hr; add a global `10/hr` cap in `deliver_alert` if observed. Possibly `/alerts mute 6h`.
2. **`pipeline_run` invasiveness in `search_jobs.run`** — function is 285 lines, builds `tg` mid-way. Restructure so `tg` is built first, before `error_capture` opens.
3. **In-memory locks** in `/health` "Active locks" — needs runtime registry. Omit in v1; add `runtime_state` later.
4. **`OPERATOR_CHAT_ID` vs existing `ADMIN_CHAT_ID`** — keep separate (operator = monitoring; admin = product). Flag for unification later.
5. **Cost surrogate trust** — disclaim everywhere (`surrogate · ±25%`). `/stats` shows call count alongside dollars so volume anomalies are visible without leaning on the dollar.
