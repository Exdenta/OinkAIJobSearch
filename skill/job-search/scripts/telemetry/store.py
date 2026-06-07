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
    # CLI subprocess succeeded and returned a well-formed JSON envelope, but
    # the envelope's `result` field was the empty string — i.e. the model
    # silently produced nothing. Distinct from CLI_MISSING (which is
    # subprocess-level) so operators can detect quota / safety / corrupted
    # prompt issues without grepping the forensic logs.
    EMPTY_RESULT = "empty_result"


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

    # ---------- query_runs (M2 per-query funnel telemetry) ----------

    @staticmethod
    def normalize_query(q: str) -> str:
        """Canonical key for a query string: lowercased, whitespace-collapsed.

        The same seed phrasing ("React Developer" vs "react  developer")
        must roll up to ONE reward bucket across runs, otherwise the
        optimiser sees two half-strength arms instead of one. This is a
        transport-layer normalisation (closed-set facts about casing /
        spacing), NOT a fit heuristic — it never decides whether a query
        is good, only that two spellings are the SAME query.
        """
        return " ".join(str(q or "").split()).lower()

    def record_query_run(
        self,
        chat_id: int,
        source_key: str,
        query: str,
        *,
        pipeline_run_id: Optional[int] = None,
        fetched: int = 0,
        scored: int = 0,
        matched_ge4: int = 0,
        queued: int = 0,
        sent: int = 0,
        started_at: float,
        finished_at: float,
    ) -> int:
        """Append one per-query funnel row. Returns the new row id.

        `query` is stored both normalised (for roll-up) and raw (for
        display). All five funnel counters default to 0 so a caller that
        only knows the fetched count can still record an attribution row
        (the rest are backfilled on the same run via a single insert with
        the full funnel — see search_jobs' run-end roll-up).
        """
        norm = self.normalize_query(query)
        with self._db._conn() as c:
            cur = c.execute(
                """
                INSERT INTO query_runs
                  (pipeline_run_id, chat_id, source_key, query, query_raw,
                   fetched, scored, matched_ge4, queued, sent,
                   started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pipeline_run_id, int(chat_id), source_key, norm,
                    (query or "")[:300],
                    int(fetched), int(scored), int(matched_ge4),
                    int(queued), int(sent),
                    float(started_at), float(finished_at),
                ),
            )
            return int(cur.lastrowid or 0)

    def query_yield_window(
        self,
        chat_id: int,
        *,
        since_ts: float,
        half_life_s: float = 7 * 86400.0,
        now: Optional[float] = None,
    ) -> list[dict]:
        """Per-query reward aggregation over a recent, time-decayed window.

        Reward = the funnel counts summed across every `query_runs` row for
        this user with `finished_at >= since_ts`, where each row is weighted
        by an EXPONENTIAL DECAY on its age: ``weight = 0.5 ** (age / half_life)``.
        A run from one half-life ago counts half as much as a run from now,
        so a query that was productive last month but dead this week sinks
        in the ranking without ever being hard-cut.

        Returns one dict PER (source_key, normalised query), sorted by the
        decayed `sent` then `matched_ge4` then `scored` descending (the
        productivity order the optimiser reads):

            [
              {source_key, query, query_raw, runs,
               fetched, scored, matched_ge4, queued, sent,   # decayed floats
               raw_sent, raw_matched_ge4, raw_scored,        # undecayed ints
               last_finished_at},
              ...
            ]

        The decayed counters are floats (a sum of weighted integers); the
        `raw_*` mirrors are the plain integer sums so the optimiser prompt
        can show "0 sent over N runs" cold-start signal honestly. `runs` is
        the undecayed row count for this query.
        """
        if now is None:
            now = time.time()
        hl = float(half_life_s) if half_life_s and half_life_s > 0 else 0.0
        with self._db._conn() as c:
            rows = list(c.execute(
                """
                SELECT source_key, query, query_raw,
                       fetched, scored, matched_ge4, queued, sent,
                       finished_at
                FROM query_runs
                WHERE chat_id = ? AND finished_at >= ?
                """,
                (int(chat_id), float(since_ts)),
            ))

        # Bucket by (source_key, normalised query) and accumulate both the
        # decayed (float) and raw (int) funnel sums.
        agg: dict[Tuple[str, str], dict] = {}
        for r in rows:
            key = (r["source_key"], r["query"])
            slot = agg.get(key)
            if slot is None:
                slot = {
                    "source_key": r["source_key"],
                    "query": r["query"],
                    "query_raw": r["query_raw"] or r["query"],
                    "runs": 0,
                    "fetched": 0.0, "scored": 0.0, "matched_ge4": 0.0,
                    "queued": 0.0, "sent": 0.0,
                    "raw_sent": 0, "raw_matched_ge4": 0, "raw_scored": 0,
                    "raw_fetched": 0,
                    "last_finished_at": 0.0,
                }
                agg[key] = slot
            age = max(0.0, float(now) - float(r["finished_at"] or now))
            weight = (0.5 ** (age / hl)) if hl > 0 else 1.0
            slot["runs"] += 1
            for col in ("fetched", "scored", "matched_ge4", "queued", "sent"):
                slot[col] += weight * int(r[col] or 0)
            slot["raw_fetched"] += int(r["fetched"] or 0)
            slot["raw_scored"] += int(r["scored"] or 0)
            slot["raw_matched_ge4"] += int(r["matched_ge4"] or 0)
            slot["raw_sent"] += int(r["sent"] or 0)
            slot["last_finished_at"] = max(
                slot["last_finished_at"], float(r["finished_at"] or 0.0)
            )

        out = list(agg.values())
        out.sort(
            key=lambda d: (d["sent"], d["matched_ge4"], d["scored"]),
            reverse=True,
        )
        return out

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
        result_chars: int = 0,
        cost_actual_us: Optional[int] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        cache_read_tokens: Optional[int] = None,
        cache_creation_tokens: Optional[int] = None,
        num_turns: Optional[int] = None,
    ) -> int:
        """Append a claude_calls row with both cost signals.

        `cost_estimate_us` is always computed from
        `model + prompt_chars + output_chars` via `cost.estimate_cost_us`
        and stored as the char-count SURROGATE fallback (micro-USD).

        When the CLI's `--output-format json` envelope is available, the
        wrappers pass the TRUE numbers it carries: `cost_actual_us`
        (from `total_cost_usd`), the token counts (from `usage.*`), and
        `num_turns`. Any of these left as None stays NULL in the row —
        older / text-format outputs simply don't carry them. Read paths
        prefer `cost_actual_us` and COALESCE to the surrogate.

        `output_chars` is the wire-level subprocess stdout length (the
        full JSON envelope when `--output-format json` is used).
        `result_chars` is the length of the parsed `result` field — the
        model's assistant text. Splitting them lets operators tell apart
        "subprocess produced no output" (both == 0) from "envelope present
        but model emitted nothing" (output_chars > 0, result_chars == 0).
        Old callers that omit `result_chars` get 0, which is the same
        meaning as historical rows pre-migration (no signal available).
        """
        cost_us = estimate_cost_us(model, prompt_chars, output_chars)

        def _opt_int(v: Optional[int]) -> Optional[int]:
            # Preserve NULL (no envelope signal) while coercing real
            # numbers to int. A defensive cast — the wrappers already
            # validate, but a stray float/str must not blow up the INSERT.
            if v is None:
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        with self._db._conn() as c:
            cur = c.execute(
                """
                INSERT INTO claude_calls
                  (pipeline_run_id, chat_id, caller, model, prompt_chars,
                   output_chars, result_chars, elapsed_ms, exit_code, status,
                   cost_estimate_us, cost_actual_us, input_tokens, output_tokens,
                   cache_read_tokens, cache_creation_tokens, num_turns,
                   started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pipeline_run_id, chat_id, caller, model,
                    int(prompt_chars), int(output_chars), int(result_chars),
                    int(elapsed_ms), exit_code, status, int(cost_us),
                    _opt_int(cost_actual_us),
                    _opt_int(input_tokens), _opt_int(output_tokens),
                    _opt_int(cache_read_tokens), _opt_int(cache_creation_tokens),
                    _opt_int(num_turns),
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
              'cost_us':       summed micro-USD (real cost_actual_us per row
                               when present, else the cost_estimate_us
                               surrogate — COALESCE'd per row),
              'prompt_chars':  summed,
              'output_chars':  summed (wire-level subprocess stdout),
              'result_chars':  summed (model's parsed `result` field),
              'by_model':      {model_or_'default': count, ...},
              'by_caller':     {caller: count, ...},
              'by_status':     {status: count, ...},
            }

        `result_chars` is included for the same reasons as in
        `record_claude_call` — operators querying the window summary
        can spot windows where the subprocess returned data but the
        model produced none.
        """
        with self._db._conn() as c:
            # Cost prefers the real per-call number (cost_actual_us, lifted
            # from the CLI envelope's total_cost_usd) and falls back to the
            # char-count surrogate (cost_estimate_us) per row, so a window
            # that mixes enveloped and legacy/text rows sums coherently.
            tot = c.execute(
                """
                SELECT COUNT(*) AS n,
                       COALESCE(SUM(COALESCE(cost_actual_us, cost_estimate_us)), 0) AS cost_us,
                       COALESCE(SUM(prompt_chars), 0)     AS prompt_chars,
                       COALESCE(SUM(output_chars), 0)     AS output_chars,
                       COALESCE(SUM(result_chars), 0)     AS result_chars
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
            "result_chars": int(tot["result_chars"] or 0) if tot else 0,
            "by_model": by_model,
            "by_caller": by_caller,
            "by_status": by_status,
        }

    def claude_call_cost_by_caller(self, since_ts: float) -> list[dict]:
        """Per-caller cost + volume aggregation in [since_ts, now).

        Returns rows sorted by cost_us DESC so callers can render the
        heaviest spenders first. Used by ops/summary to add a monthly
        per-agent share block to the daily digest.

            [
              {caller, n, cost_us, prompt_chars, output_chars,
               result_chars, elapsed_ms},
              ...
            ]

        `result_chars` (model assistant text length) is surfaced
        alongside `output_chars` (wire-level subprocess stdout) so
        operators can see per-caller silent-empty rates.
        """
        with self._db._conn() as c:
            # Per-row COALESCE(actual, surrogate) — same preference order as
            # claude_call_window_summary so the per-caller breakdown agrees
            # with the window total.
            rows = c.execute(
                """
                SELECT caller,
                       COUNT(*)                            AS n,
                       COALESCE(SUM(COALESCE(cost_actual_us, cost_estimate_us)), 0) AS cost_us,
                       COALESCE(SUM(prompt_chars), 0)      AS prompt_chars,
                       COALESCE(SUM(output_chars), 0)      AS output_chars,
                       COALESCE(SUM(result_chars), 0)      AS result_chars,
                       COALESCE(SUM(elapsed_ms), 0)        AS elapsed_ms
                FROM claude_calls
                WHERE finished_at >= ?
                GROUP BY caller
                ORDER BY cost_us DESC
                """,
                (float(since_ts),),
            ).fetchall()
        return [
            {
                "caller":       r["caller"],
                "n":            int(r["n"]),
                "cost_us":      int(r["cost_us"]),
                "prompt_chars": int(r["prompt_chars"]),
                "output_chars": int(r["output_chars"]),
                "result_chars": int(r["result_chars"]),
                "elapsed_ms":   int(r["elapsed_ms"]),
            }
            for r in rows
        ]

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
