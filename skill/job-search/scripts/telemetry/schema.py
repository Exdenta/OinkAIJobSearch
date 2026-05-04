"""Telemetry DDL — single source of truth for the monitoring tables.

The DDL is the canonical text from docs/monitoring-plan.md §2. Every
statement is `CREATE TABLE/INDEX IF NOT EXISTS`, so `migrate()` is safe
to call multiple times against any DB state (fresh, partially migrated,
fully migrated). Bootstrap-time invocation is wired into `db.DB._init`
post-merge (see plan §4.1) — keeping it idempotent means we don't have
to gate the call.
"""
from __future__ import annotations

import sqlite3


# Full DDL block. Mirrors docs/monitoring-plan.md §2 verbatim — change
# both together. The trailing `where_` column is intentionally suffixed
# with an underscore: `where` is a SQL reserved word.
MONITOR_SCHEMA = """
-- One row per orchestrator invocation.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT    NOT NULL,
    triggered_by    INTEGER,
    status          TEXT    NOT NULL,
    exit_code       INTEGER,
    users_total     INTEGER NOT NULL DEFAULT 0,
    jobs_raw        INTEGER NOT NULL DEFAULT 0,
    jobs_sent       INTEGER NOT NULL DEFAULT 0,
    error_count     INTEGER NOT NULL DEFAULT 0,
    extra_json      TEXT,
    started_at      REAL    NOT NULL,
    finished_at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_kind ON pipeline_runs(kind, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_when ON pipeline_runs(finished_at DESC);

-- Per-source per-run health.
CREATE TABLE IF NOT EXISTS source_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_run_id INTEGER NOT NULL,
    source_key      TEXT    NOT NULL,
    user_chat_id    INTEGER,
    status          TEXT    NOT NULL,
    raw_count       INTEGER NOT NULL DEFAULT 0,
    elapsed_ms      INTEGER,
    error_class     TEXT,
    error_head      TEXT,
    started_at      REAL    NOT NULL,
    finished_at     REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_runs_run    ON source_runs(pipeline_run_id);
CREATE INDEX IF NOT EXISTS idx_source_runs_source ON source_runs(source_key, finished_at DESC);

-- One row per Claude CLI subprocess invocation. Cost is a SURROGATE — see plan §6/§8.
CREATE TABLE IF NOT EXISTS claude_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_run_id  INTEGER,
    chat_id          INTEGER,
    caller           TEXT    NOT NULL,
    model            TEXT,
    prompt_chars     INTEGER NOT NULL DEFAULT 0,
    output_chars     INTEGER NOT NULL DEFAULT 0,
    elapsed_ms       INTEGER NOT NULL DEFAULT 0,
    exit_code        INTEGER,
    status           TEXT    NOT NULL,
    cost_estimate_us INTEGER NOT NULL DEFAULT 0,
    started_at       REAL    NOT NULL,
    finished_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claude_calls_when   ON claude_calls(finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_claude_calls_caller ON claude_calls(caller, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_claude_calls_run    ON claude_calls(pipeline_run_id);

-- Captured exceptions; UNIQUE enforces per-fingerprint per-hour rate limit.
CREATE TABLE IF NOT EXISTS error_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT    NOT NULL,
    hour_bucket     INTEGER NOT NULL,
    where_          TEXT    NOT NULL,
    error_class     TEXT    NOT NULL,
    message_head    TEXT,
    stack_tail      TEXT,
    chat_id         INTEGER,
    delivered_at    REAL,
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
"""


def migrate(conn: sqlite3.Connection) -> None:
    """Apply the monitoring DDL. Safe to call repeatedly.

    Caller is responsible for committing — `MonitorStore` always opens
    its own short-lived connection via `db.DB._conn()` (which commits on
    success), so passing that connection here lands the schema in the
    same transaction as whatever else the caller is doing.
    """
    conn.executescript(MONITOR_SCHEMA)
