#!/usr/bin/env python3
"""Regression smoke test for the `result_chars` column split.

Covers the three call shapes called out in the design doc:

  1. Normal CLI call — non-empty envelope with non-empty `result` field.
     Expectation: output_chars > 0 AND result_chars > 0 AND status == 'ok'.

  2. CLI subprocess missing / crashed (stdout is None — the underlying
     `claude_cli.run_p` returns None on timeout / non-zero / missing CLI).
     Expectation: output_chars == 0 AND result_chars == 0 AND
                  status == 'cli_missing'.

  3. CLI subprocess succeeded with a well-formed JSON envelope, but the
     model emitted nothing (`"result": ""`). This is the silent-failure
     mode that the column split was introduced to make visible.
     Expectation: output_chars > 0 AND result_chars == 0 AND
                  status == 'empty_result'.

Also exercises:
  * Schema migration idempotency — running migrate() against an already
    migrated DB does not error.
  * Defensive ALTER TABLE backfills `result_chars` on a pre-existing DB
    that was created BEFORE the column existed.
  * `claude_call_window_summary` returns the new `result_chars` total.
  * `claude_call_cost_by_caller` returns per-caller `result_chars`.
  * `extract_assistant_text` returns "" for an envelope with
    `"result": ""` (the function-level root-cause fix).

No network. No real `claude` CLI. Exits non-zero on any failure.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent
sys.path.insert(0, str(SCRIPTS))

from db import DB  # noqa: E402
from telemetry import MonitorStore, CallStatus, migrate  # noqa: E402
from claude_cli import extract_assistant_text  # noqa: E402
from instrumentation import wrappers as _wrappers  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


# ---------------------------------------------------------------------------
# 1. extract_assistant_text now returns "" for empty-result envelopes
# ---------------------------------------------------------------------------
print("1. extract_assistant_text — empty result envelope")

empty_envelope = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "", "stop_reason": "end_turn",
})
text = extract_assistant_text(empty_envelope)
check(text == "", f"empty-result envelope → '' (got {text!r})")

non_empty_envelope = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "hello world", "stop_reason": "end_turn",
})
check(extract_assistant_text(non_empty_envelope) == "hello world",
      "non-empty result envelope → 'hello world'")

# Non-JSON stdout still falls back to the raw string (unchanged behavior).
check(extract_assistant_text("just plain text") == "just plain text",
      "non-JSON stdout → raw text (legacy fallback preserved)")

# Empty stdout → "" (unchanged).
check(extract_assistant_text("") == "",
      "empty stdout → ''")
check(extract_assistant_text(None) == "",  # type: ignore[arg-type]
      "None stdout → ''")

# Legacy fallback: envelope without `result` but with `content` is honored.
legacy_envelope = json.dumps({"content": "legacy text"})
check(extract_assistant_text(legacy_envelope) == "legacy text",
      "envelope without result but with content → 'legacy text' (legacy fallback)")


# ---------------------------------------------------------------------------
# 2. Schema migration: idempotent + ALTER TABLE backfill on pre-existing DB
# ---------------------------------------------------------------------------
print("\n2. schema migration — idempotent + backfill")

with tempfile.TemporaryDirectory() as td:
    db_path = Path(td) / "jobs.db"
    db = DB(db_path)
    store = MonitorStore(db)

    # Verify result_chars column exists after the standard init path.
    with db._conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(claude_calls)")}
    check("result_chars" in cols,
          f"claude_calls.result_chars present after init (cols={sorted(cols)})")
    check("output_chars" in cols,
          "claude_calls.output_chars still present (unchanged semantics)")

    # Re-run migrate() to confirm idempotency.
    with db._conn() as c:
        migrate(c)
        migrate(c)
    check(True, "migrate() re-run is a no-op (no exception)")

# Simulate a pre-existing DB that was created before result_chars existed:
# manually create the table without the column, then call migrate() and
# verify the ALTER TABLE adds it.
print("\n2b. backfill on a legacy schema lacking result_chars")
with tempfile.TemporaryDirectory() as td:
    db_path = Path(td) / "legacy.db"
    raw = sqlite3.connect(str(db_path))
    raw.row_factory = sqlite3.Row
    raw.execute("""
        CREATE TABLE claude_calls (
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
        )
    """)
    # Seed a row so we can verify it still reads correctly after migration.
    raw.execute(
        "INSERT INTO claude_calls "
        "(caller, prompt_chars, output_chars, elapsed_ms, status, "
        " cost_estimate_us, started_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy_caller", 100, 500, 1000, "ok", 250, time.time(), time.time()),
    )
    raw.commit()

    # Verify the legacy table really lacks result_chars.
    pre_cols = {r["name"] for r in raw.execute("PRAGMA table_info(claude_calls)")}
    check("result_chars" not in pre_cols,
          f"legacy table starts without result_chars (cols={sorted(pre_cols)})")

    # Apply migrate() over the legacy connection.
    migrate(raw)
    raw.commit()

    post_cols = {r["name"] for r in raw.execute("PRAGMA table_info(claude_calls)")}
    check("result_chars" in post_cols,
          f"result_chars added by migrate() (cols={sorted(post_cols)})")

    # Pre-existing row is preserved with result_chars defaulted to 0.
    row = raw.execute(
        "SELECT * FROM claude_calls WHERE caller = 'legacy_caller'"
    ).fetchone()
    check(row is not None and int(row["result_chars"]) == 0,
          "legacy row has result_chars defaulted to 0")
    check(row is not None and int(row["output_chars"]) == 500,
          "legacy row's output_chars unchanged by migration")

    # Re-run migrate to confirm idempotency on the post-migrated DB.
    migrate(raw)
    raw.commit()
    check(True, "migrate() re-run on post-migrated legacy DB is a no-op")

    raw.close()


# ---------------------------------------------------------------------------
# 3. record_claude_call accepts result_chars; existing callers stay 0
# ---------------------------------------------------------------------------
print("\n3. record_claude_call — kwarg + back-compat")

with tempfile.TemporaryDirectory() as td:
    db_path = Path(td) / "jobs.db"
    db = DB(db_path)
    store = MonitorStore(db)

    t = time.time()

    # 3a. New caller passes result_chars explicitly.
    cid_new = store.record_claude_call(
        caller="job_enrich", prompt_chars=4000, output_chars=800,
        elapsed_ms=1500, status=CallStatus.OK,
        model="haiku", started_at=t, finished_at=t + 1.5,
        result_chars=200,
    )
    with db._conn() as c:
        row = c.execute("SELECT * FROM claude_calls WHERE id = ?", (cid_new,)).fetchone()
    check(row is not None and int(row["result_chars"]) == 200,
          f"explicit result_chars persists (got {row and row['result_chars']})")
    check(row is not None and int(row["output_chars"]) == 800,
          "output_chars persists unchanged alongside result_chars")

    # 3b. Old caller does NOT pass result_chars — defaults to 0.
    cid_old = store.record_claude_call(
        caller="profile_builder", prompt_chars=2000, output_chars=400,
        elapsed_ms=900, status=CallStatus.OK,
        model="opus", started_at=t, finished_at=t + 0.9,
    )
    with db._conn() as c:
        row = c.execute("SELECT * FROM claude_calls WHERE id = ?", (cid_old,)).fetchone()
    check(row is not None and int(row["result_chars"]) == 0,
          f"omitted result_chars defaults to 0 (got {row and row['result_chars']})")


# ---------------------------------------------------------------------------
# 4. Read helpers surface result_chars
# ---------------------------------------------------------------------------
print("\n4. read helpers — window summary + cost_by_caller")

with tempfile.TemporaryDirectory() as td:
    db_path = Path(td) / "jobs.db"
    db = DB(db_path)
    store = MonitorStore(db)

    t = time.time()
    # Three calls under two callers, two of which carry explicit result_chars.
    store.record_claude_call(
        caller="job_enrich", prompt_chars=1000, output_chars=300,
        elapsed_ms=400, status=CallStatus.OK, model="haiku",
        started_at=t, finished_at=t + 0.4, result_chars=120,
    )
    store.record_claude_call(
        caller="job_enrich", prompt_chars=1500, output_chars=350,
        elapsed_ms=500, status=CallStatus.OK, model="haiku",
        started_at=t + 1, finished_at=t + 1.5, result_chars=180,
    )
    store.record_claude_call(
        caller="fit_analyzer", prompt_chars=800, output_chars=200,
        elapsed_ms=300, status=CallStatus.OK, model="sonnet",
        started_at=t + 2, finished_at=t + 2.3, result_chars=90,
    )

    summary = store.claude_call_window_summary(since_ts=t - 1)
    check("result_chars" in summary,
          f"window summary exposes result_chars key (got {sorted(summary.keys())})")
    check(int(summary["result_chars"]) == 390,
          f"summed result_chars == 390 (got {summary.get('result_chars')})")
    check(int(summary["output_chars"]) == 850,
          f"summed output_chars unchanged (got {summary.get('output_chars')})")
    check(int(summary["count"]) == 3, "count unchanged by column split")

    by_caller = store.claude_call_cost_by_caller(since_ts=t - 1)
    by_name = {r["caller"]: r for r in by_caller}
    check("job_enrich" in by_name and int(by_name["job_enrich"]["result_chars"]) == 300,
          f"by_caller job_enrich.result_chars == 300 "
          f"(got {by_name.get('job_enrich', {}).get('result_chars')})")
    check("fit_analyzer" in by_name and int(by_name["fit_analyzer"]["result_chars"]) == 90,
          f"by_caller fit_analyzer.result_chars == 90 "
          f"(got {by_name.get('fit_analyzer', {}).get('result_chars')})")
    # output_chars totals still correct after join with the new column.
    check(int(by_name["job_enrich"]["output_chars"]) == 650,
          f"by_caller job_enrich.output_chars == 650 "
          f"(got {by_name.get('job_enrich', {}).get('output_chars')})")


# ---------------------------------------------------------------------------
# 5. End-to-end wrapper test — three call shapes
# ---------------------------------------------------------------------------
print("\n5. wrapped_run_p — three call shapes")


def _install_fake_run_p(stdout: str | None) -> tuple[Any, Any]:
    """Replace `claude_cli.run_p` *as used by wrappers* with a fixed stub.

    Returns (original_run_p, original_run_p_with_tools) so the caller can
    restore afterwards. The wrappers module imported `run_p` and
    `run_p_with_tools` by name from `claude_cli`, so we monkey-patch the
    names on the wrappers module directly.
    """
    orig_run_p = _wrappers.run_p
    orig_run_p_tools = _wrappers.run_p_with_tools
    _wrappers.run_p = lambda prompt, **kw: stdout
    _wrappers.run_p_with_tools = lambda prompt, **kw: stdout
    return orig_run_p, orig_run_p_tools


def _restore(orig: tuple[Any, Any]) -> None:
    _wrappers.run_p, _wrappers.run_p_with_tools = orig


with tempfile.TemporaryDirectory() as td:
    db_path = Path(td) / "jobs.db"
    db = DB(db_path)
    store = MonitorStore(db)

    # 5a. Normal call — non-empty envelope with non-empty `result`.
    normal_envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "{\"hello\": \"world\"}", "stop_reason": "end_turn",
    })
    orig = _install_fake_run_p(normal_envelope)
    try:
        out = _wrappers.wrapped_run_p(store, "test_caller_normal", "prompt")
    finally:
        _restore(orig)
    check(out == normal_envelope, "wrapped_run_p returns underlying stdout unchanged")

    with db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='test_caller_normal'"
        ).fetchone()
    check(row is not None, "normal call recorded")
    check(int(row["output_chars"]) > 0, f"output_chars > 0 (got {row['output_chars']})")
    check(int(row["result_chars"]) > 0, f"result_chars > 0 (got {row['result_chars']})")
    # The recorded result_chars equals the length of "result" field.
    check(int(row["result_chars"]) == len("{\"hello\": \"world\"}"),
          f"result_chars matches len(result_field) (got {row['result_chars']})")
    check(row["status"] == "ok", f"status == 'ok' (got {row['status']!r})")

    # 5b. CLI missing — stdout is None.
    orig = _install_fake_run_p(None)
    try:
        out = _wrappers.wrapped_run_p(store, "test_caller_missing", "prompt")
    finally:
        _restore(orig)
    check(out is None, "wrapped_run_p propagates None")

    with db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='test_caller_missing'"
        ).fetchone()
    check(row is not None, "cli_missing call recorded")
    check(int(row["output_chars"]) == 0,
          f"output_chars == 0 (got {row['output_chars']})")
    check(int(row["result_chars"]) == 0,
          f"result_chars == 0 (got {row['result_chars']})")
    check(row["status"] == "cli_missing",
          f"status == 'cli_missing' (got {row['status']!r})")

    # 5c. Empty result — well-formed envelope with `result == ""`.
    empty_result_envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "", "stop_reason": "end_turn",
        # Padding so output_chars is comfortably > 0.
        "usage": {"input_tokens": 1200, "output_tokens": 0},
    })
    orig = _install_fake_run_p(empty_result_envelope)
    try:
        out = _wrappers.wrapped_run_p(store, "test_caller_empty", "prompt")
    finally:
        _restore(orig)
    check(out == empty_result_envelope,
          "wrapped_run_p propagates empty-result envelope verbatim")

    with db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='test_caller_empty'"
        ).fetchone()
    check(row is not None, "empty_result call recorded")
    check(int(row["output_chars"]) > 0,
          f"output_chars > 0 (got {row['output_chars']})")
    check(int(row["result_chars"]) == 0,
          f"result_chars == 0 (got {row['result_chars']})")
    check(row["status"] == "empty_result",
          f"status == 'empty_result' (got {row['status']!r})")

    # 5d. Same three shapes via wrapped_run_p_with_tools — sanity check the
    # second wrapper has matching behavior (the wiring is identical).
    print("\n5d. wrapped_run_p_with_tools — sanity")
    orig = _install_fake_run_p(empty_result_envelope)
    try:
        out = _wrappers.wrapped_run_p_with_tools(
            store, "test_tools_empty", "prompt",
        )
    finally:
        _restore(orig)
    with db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='test_tools_empty'"
        ).fetchone()
    check(row is not None and row["status"] == "empty_result",
          "wrapped_run_p_with_tools also detects empty_result")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if failures:
    print(f"FAIL — {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("PASS — result_chars column split smoke checks all green.")
