"""`MonitorStore` — single typed surface over the monitoring tables.

Wraps a `db.DB` instance and uses its `_conn()` context manager for
every connection (so SQLite locking, row-factory, and commit/rollback
behavior stay consistent with the rest of the bot's persistence). This
class deliberately does NOT open its own sqlite3 connections.

Surface shape is frozen by docs/monitoring-plan.md §3 — slices B and C
import these method signatures by name. If you need a new method, add
one; do not rename or repurpose an existing one.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional, Tuple

import db as _db_mod  # `db.DB` — only used for type hints.
from .cost import estimate_cost_us
from .schema import migrate as _migrate_schema


# Status string enums. Plain str-subclasses so they round-trip through
# sqlite without a converter — but Slice B can also pass raw strings
# and we will not validate (these are documentation, not gates).

class RunStatus:
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    EXCEPTION = "exception"


class SourceStatus:
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    SUSPICIOUS_ZERO = "suspicious_zero"


class CallStatus:
    OK = "ok"
    TIMEOUT = "timeout"
    NON_ZERO = "non_zero"
    CLI_MISSING = "cli_missing"
    EXCEPTION = "exception"


class MonitorStore:
    """DAL for the five monitoring tables.

    Construction runs the schema migration once against the wrapped DB,
    so callers don't need to remember to. (Idempotent — safe to
    re-instantiate against an already-migrated DB.)
    """

    def __init__(self, db: "_db_mod.DB"):
        self._db = db
        # Run the migration on construction so a freshly-built DB is
        # immediately usable. `db._conn()` commits on success.
        with self._db._conn() as c:
            _migrate_schema(c)

    # ---------- pipeline_runs ----------

    def start_pipeline_run(
        self,
        kind: str,
        triggered_by: Optional[int] = None,
    ) -> int:
        """Insert a `pipeline_runs` row in its initial state and return its id.

        Status is recorded as 'running' until `finish_pipeline_run` updates
        it. (Plan §2 lists `ok|partial|failed|exception` as terminal
        statuses — 'running' is a transient marker, never queried by the
        UI which filters on `finished_at`-bound terminal states.)
        """
        now = time.time()
        with self._db._conn() as c:
            cur = c.execute(
                """
                INSERT INTO pipeline_runs (kind, triggered_by, status, started_at, finished_at)
                VALUES (?, ?, 'running', ?, ?)
                """,
                (kind, triggered_by, now, now),
            )
            return int(cur.lastrowid or 0)

    def finish_pipeline_run(
        self,
        run_id: int,
        status: str,
        exit_code: Optional[int] = None,
        users_total: int = 0,
        jobs_raw: int = 0,
        jobs_sent: int = 0,
        error_count: int = 0,
        extra: Optional[dict] = None,
    ) -> None:
        """Stamp the terminal state on a pipeline_runs row.

        `extra` is JSON-serialized into `extra_json`; pass None to leave
        the column NULL.
        """
        extra_json = None
        if extra is not None:
            try:
                extra_json = json.dumps(extra, ensure_ascii=False)
            except (TypeError, ValueError):
                extra_json = None
        with self._db._conn() as c:
            c.execute(
                """
                UPDATE pipeline_runs
                SET status       = ?,
                    exit_code    = ?,
                    users_total  = ?,
                    jobs_raw     = ?,
                    jobs_sent    = ?,
                    error_count  = ?,
                    extra_json   = ?,
                    finished_at  = ?
                WHERE id = ?
                """,
                (
                    status, exit_code,
                    int(users_total), int(jobs_raw), int(jobs_sent), int(error_count),
                    extra_json, time.time(), int(run_id),
                ),
            )

    # ---------- source_runs ----------

    def record_source_run(
        self,
        run_id: int,
        source_key: str,
        status: str,
        raw_count: int,
        elapsed_ms: Optional[int],
        *,
        user_chat_id: Optional[int] = None,
        error_class: Optional[str] = None,
        error_head: Optional[str] = None,
        started_at: float,
        finished_at: float,
    ) -> int:
        """Append a source_runs row. Returns the new row id."""
        with self._db._conn() as c:
            cur = c.execute(
                """
                INSERT INTO source_runs
                  (pipeline_run_id, source_key, user_chat_id, status, raw_count,
                   elapsed_ms, error_class, error_head, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(run_id), source_key, user_chat_id, status, int(raw_count),
                    int(elapsed_ms) if elapsed_ms is not None else None,
                    error_class,
                    (error_head or "")[:200] if error_head else None,
                    float(started_at), float(finished_at),
                ),
            )
            return int(cur.lastrowid or 0)

    # ---------- claude_calls ----------

    def record_claude_call(
        self,
        caller: str,
        prompt_chars: int,
        output_chars: int,
        elapsed_ms: int,
        status: str,
        *,
        pipeline_run_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        model: Optional[str] = None,
        exit_code: Optional[int] = None,
        started_at: float,
        finished_at: float,
    ) -> int:
        """Append a claude_calls row with computed cost surrogate.

        Cost is calculated from `model + prompt_chars + output_chars`
        via `cost.estimate_cost_us` and stored in micro-USD.
        """
        cost_us = estimate_cost_us(model, prompt_chars, output_chars)
        with self._db._conn() as c:
            cur = c.execute(
                """
                INSERT INTO claude_calls
                  (pipeline_run_id, chat_id, caller, model, prompt_chars,
                   output_chars, elapsed_ms, exit_code, status, cost_estimate_us,
                   started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pipeline_run_id, chat_id, caller, model,
                    int(prompt_chars), int(output_chars),
                    int(elapsed_ms), exit_code, status, int(cost_us),
                    float(started_at), float(finished_at),
                ),
            )
            return int(cur.lastrowid or 0)

    # ---------- error_events ----------

    def try_record_error(
        self,
        fingerprint: str,
        hour_bucket: int,
        where: str,
        error_class: str,
        message_head: Optional[str],
        stack_tail: Optional[str],
        chat_id: Optional[int] = None,
    ) -> Optional[int]:
        """INSERT an error_events row, deduped by (fingerprint, hour_bucket).

        Returns the new row id on insert, or None when the UNIQUE
        constraint short-circuits (caller treats None as "rate-limited,
        do not alert").
        """
        now = time.time()
        with self._db._conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO error_events
                  (fingerprint, hour_bucket, where_, error_class, message_head,
                   stack_tail, chat_id, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fingerprint, int(hour_bucket), where, error_class,
                    (message_head or "")[:200] if message_head else None,
                    stack_tail, chat_id, now,
                ),
            )
            if cur.rowcount == 0:
                return None
            return int(cur.lastrowid or 0)

    def mark_alert_delivered(self, event_id: int) -> None:
        """Stamp `delivered_at` on an error_events row.

        Called by `ops.alerts.deliver_alert` after a successful Telegram
        send so the audit log distinguishes delivered alerts from
        rate-limit-suppressed ones.
        """
        with self._db._conn() as c:
            c.execute(
                "UPDATE error_events SET delivered_at = ? WHERE id = ?",
                (time.time(), int(event_id)),
            )

    # ---------- read paths ----------

    def last_source_run_per_source(self) -> list[sqlite3.Row]:
        """Most-recent source_runs row for each `source_key`.

        Drives `/health`. Cross-user — for per-user sources (linkedin,
        web_search) this returns the latest-by-`finished_at` row across
        all users.
        """
        with self._db._conn() as c:
            return list(c.execute(
                """
                SELECT sr.*
                FROM source_runs sr
                JOIN (
                    SELECT source_key, MAX(finished_at) AS max_ts
                    FROM source_runs
                    GROUP BY source_key
                ) latest
                  ON latest.source_key = sr.source_key
                 AND latest.max_ts     = sr.finished_at
                ORDER BY sr.source_key
                """
            ))

    def recent_pipeline_runs(self, limit: int) -> list[sqlite3.Row]:
        """Last N pipeline_runs rows, newest first. Drives `/runlog`."""
        with self._db._conn() as c:
            return list(c.execute(
                "SELECT * FROM pipeline_runs ORDER BY finished_at DESC LIMIT ?",
                (int(limit),),
            ))

    def claude_call_window_summary(self, since_ts: float) -> dict:
        """Aggregate claude_calls in [since_ts, now). Drives `/stats`.

        Returns:
            {
              'count':         total calls,
              'cost_us':       summed micro-USD,
              'prompt_chars':  summed,
              'output_chars':  summed,
              'by_model':      {model_or_'default': count, ...},
              'by_caller':     {caller: count, ...},
              'by_status':     {status: count, ...},
            }
        """
        with self._db._conn() as c:
            tot = c.execute(
                """
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(cost_estimate_us), 0) AS cost_us,
                       COALESCE(SUM(prompt_chars), 0)     AS prompt_chars,
                       COALESCE(SUM(output_chars), 0)     AS output_chars
                FROM claude_calls
                WHERE finished_at >= ?
                """,
                (float(since_ts),),
            ).fetchone()
            by_model = {
                (r["model"] if r["model"] is not None else "default"): int(r["n"])
                for r in c.execute(
                    """
                    SELECT model, COUNT(*) AS n
                    FROM claude_calls
                    WHERE finished_at >= ?
                    GROUP BY model
                    """,
                    (float(since_ts),),
                )
            }
            by_caller = {
                r["caller"]: int(r["n"])
                for r in c.execute(
                    """
                    SELECT caller, COUNT(*) AS n
                    FROM claude_calls
                    WHERE finished_at >= ?
                    GROUP BY caller
                    """,
                    (float(since_ts),),
                )
            }
            by_status = {
                r["status"]: int(r["n"])
                for r in c.execute(
                    """
                    SELECT status, COUNT(*) AS n
                    FROM claude_calls
                    WHERE finished_at >= ?
                    GROUP BY status
                    """,
                    (float(since_ts),),
                )
            }
        return {
            "count": int(tot["n"] or 0) if tot else 0,
            "cost_us": int(tot["cost_us"] or 0) if tot else 0,
            "prompt_chars": int(tot["prompt_chars"] or 0) if tot else 0,
            "output_chars": int(tot["output_chars"] or 0) if tot else 0,
            "by_model": by_model,
            "by_caller": by_caller,
            "by_status": by_status,
        }

    def recent_errors(self, since_ts: float) -> list[sqlite3.Row]:
        """error_events with `occurred_at >= since_ts`, newest first."""
        with self._db._conn() as c:
            return list(c.execute(
                """
                SELECT * FROM error_events
                WHERE occurred_at >= ?
                ORDER BY occurred_at DESC
                """,
                (float(since_ts),),
            ))

    def consecutive_zero_runs(self, source_key: str, n: int = 3) -> bool:
        """True iff the most recent N source_runs for `source_key` all have raw_count == 0.

        Returns False when fewer than N rows exist (not enough signal to
        cry suspicious yet — first run of a brand-new source shouldn't
        trigger an alert).
        """
        if n <= 0:
            return False
        with self._db._conn() as c:
            rows = list(c.execute(
                """
                SELECT raw_count FROM source_runs
                WHERE source_key = ?
                ORDER BY finished_at DESC
                LIMIT ?
                """,
                (source_key, int(n)),
            ))
        if len(rows) < n:
            return False
        return all(int(r["raw_count"] or 0) == 0 for r in rows)

    # ---------- ops_toggles ----------

    def get_toggle(self, key: str, default: str) -> str:
        """Read a toggle value, returning `default` when unset."""
        with self._db._conn() as c:
            row = c.execute(
                "SELECT value FROM ops_toggles WHERE key = ?", (key,),
            ).fetchone()
            if row is None:
                return default
            return row["value"]

    def set_toggle(self, key: str, value: str) -> None:
        """Upsert a toggle. Stamps updated_at."""
        with self._db._conn() as c:
            c.execute(
                """
                INSERT INTO ops_toggles (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value      = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, value, time.time()),
            )

    # ---------- composite reads ----------

    def pipeline_run_with_sources(
        self,
        run_id: int,
    ) -> Tuple[Optional[sqlite3.Row], list[sqlite3.Row]]:
        """Return (pipeline_runs row, [source_runs rows]) for one run.

        Used by `ops.summary.build_daily_summary`. Returns (None, [])
        when the run id doesn't exist.
        """
        with self._db._conn() as c:
            head = c.execute(
                "SELECT * FROM pipeline_runs WHERE id = ?", (int(run_id),),
            ).fetchone()
            if head is None:
                return (None, [])
            sources = list(c.execute(
                """
                SELECT * FROM source_runs
                WHERE pipeline_run_id = ?
                ORDER BY started_at ASC
                """,
                (int(run_id),),
            ))
        return (head, sources)
