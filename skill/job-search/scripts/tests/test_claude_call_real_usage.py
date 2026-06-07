"""Tier 3 tests: capture REAL CLI usage/cost from the JSON envelope.

`claude -p --output-format json` already returns the truth — total_cost_usd,
usage.{input,output,cache_*}_tokens, num_turns — which the wrappers used to
throw away in favor of the char-count surrogate. These tests cover:

  1. Schema migration is idempotent + additively backfills the new nullable
     columns onto a pre-existing claude_calls table that lacks them (the
     live-DB-safety guarantee — exercised against a legacy temp DB, never
     the real one).
  2. `record_claude_call` stores the new optional fields when given, and
     leaves them NULL (surrogate retained) when omitted.
  3. `wrapped_run_p` / `wrapped_run_p_with_tools` parse a sample envelope
     carrying usage + total_cost_usd and persist the real numbers.
  4. A text / empty / envelope-without-usage degrades gracefully: columns
     stay NULL, the surrogate `cost_estimate_us` is still populated, and
     the call path returns the underlying stdout unchanged.
  5. Read paths (`claude_call_window_summary`, `claude_call_cost_by_caller`)
     prefer `cost_actual_us` per row and fall back to the surrogate.

No network, no real `claude` CLI — `run_p` is monkey-patched on the
wrappers module exactly like smoke_result_chars.py does.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from db import DB  # noqa: E402
from telemetry import MonitorStore, CallStatus, migrate  # noqa: E402
from telemetry.cost import estimate_cost_us  # noqa: E402
from instrumentation import wrappers as _wrappers  # noqa: E402


# The new nullable columns this tier adds.
_NEW_COLS = (
    "cost_actual_us",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "num_turns",
)


def _sample_envelope(
    *,
    result: str = '{"ok": true}',
    cost_usd: float = 0.0123,
    input_tokens: int = 1200,
    output_tokens: int = 340,
    cache_read: int = 980,
    cache_creation: int = 0,
    num_turns: int = 1,
) -> str:
    """A realistic `--output-format json` envelope with full usage block."""
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result,
        "stop_reason": "end_turn",
        "num_turns": num_turns,
        "duration_ms": 4210,
        "duration_api_ms": 3990,
        "total_cost_usd": cost_usd,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
    })


@pytest.fixture
def store(tmp_path):
    return MonitorStore(DB(tmp_path / "jobs.db"))


# ---------------------------------------------------------------------------
# 1. Migration: idempotent + additive backfill on a legacy table
# ---------------------------------------------------------------------------

def test_new_columns_present_after_init(store):
    with store._db._conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(claude_calls)")}
    for col in _NEW_COLS:
        assert col in cols, f"{col} missing after init (cols={sorted(cols)})"
    # Pre-existing columns survive untouched.
    assert "cost_estimate_us" in cols
    assert "result_chars" in cols


def test_migrate_is_idempotent(store):
    # Re-running migrate twice on an already-migrated DB must not raise.
    with store._db._conn() as c:
        migrate(c)
        migrate(c)
    # Still constructible — MonitorStore runs migrate again on construction.
    MonitorStore(store._db)


def test_migrate_backfills_legacy_table(tmp_path):
    """A claude_calls table created BEFORE this tier (no new columns, with a
    seeded row) must gain the columns as NULLable additions, preserving the
    existing row. This is the exact shape of the live 63MB DB."""
    db_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(db_path))
    raw.row_factory = sqlite3.Row
    # Schema as it existed at the result_chars tier — no Tier 3 columns.
    raw.execute("""
        CREATE TABLE claude_calls (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            pipeline_run_id  INTEGER,
            chat_id          INTEGER,
            caller           TEXT    NOT NULL,
            model            TEXT,
            prompt_chars     INTEGER NOT NULL DEFAULT 0,
            output_chars     INTEGER NOT NULL DEFAULT 0,
            result_chars     INTEGER NOT NULL DEFAULT 0,
            elapsed_ms       INTEGER NOT NULL DEFAULT 0,
            exit_code        INTEGER,
            status           TEXT    NOT NULL,
            cost_estimate_us INTEGER NOT NULL DEFAULT 0,
            started_at       REAL    NOT NULL,
            finished_at      REAL    NOT NULL
        )
    """)
    raw.execute(
        "INSERT INTO claude_calls "
        "(caller, prompt_chars, output_chars, result_chars, elapsed_ms, "
        " status, cost_estimate_us, started_at, finished_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy_caller", 100, 500, 50, 1000, "ok", 250, time.time(), time.time()),
    )
    raw.commit()

    pre_cols = {r["name"] for r in raw.execute("PRAGMA table_info(claude_calls)")}
    for col in _NEW_COLS:
        assert col not in pre_cols

    migrate(raw)
    raw.commit()

    post_cols = {r["name"] for r in raw.execute("PRAGMA table_info(claude_calls)")}
    for col in _NEW_COLS:
        assert col in post_cols, f"{col} not added by migrate()"

    # Legacy row preserved; new columns default to NULL; surrogate intact.
    row = raw.execute(
        "SELECT * FROM claude_calls WHERE caller = 'legacy_caller'"
    ).fetchone()
    assert row is not None
    assert int(row["cost_estimate_us"]) == 250
    assert int(row["output_chars"]) == 500
    for col in _NEW_COLS:
        assert row[col] is None, f"{col} should default NULL on legacy row"

    # Idempotent on the post-migrated DB too.
    migrate(raw)
    raw.commit()
    raw.close()


# ---------------------------------------------------------------------------
# 2. record_claude_call stores / omits the new fields
# ---------------------------------------------------------------------------

def test_record_stores_real_usage(store):
    t = time.time()
    cid = store.record_claude_call(
        caller="job_enrich", prompt_chars=4000, output_chars=800,
        elapsed_ms=1500, status=CallStatus.OK, model="haiku",
        started_at=t, finished_at=t + 1.5, result_chars=200,
        cost_actual_us=12_300, input_tokens=1200, output_tokens=340,
        cache_read_tokens=980, cache_creation_tokens=0, num_turns=1,
    )
    with store._db._conn() as c:
        row = c.execute("SELECT * FROM claude_calls WHERE id = ?", (cid,)).fetchone()
    assert int(row["cost_actual_us"]) == 12_300
    assert int(row["input_tokens"]) == 1200
    assert int(row["output_tokens"]) == 340
    assert int(row["cache_read_tokens"]) == 980
    assert int(row["cache_creation_tokens"]) == 0
    assert int(row["num_turns"]) == 1
    # Surrogate is still computed + stored alongside the real number.
    assert int(row["cost_estimate_us"]) == estimate_cost_us("haiku", 4000, 800)


def test_record_omits_real_usage_leaves_null(store):
    t = time.time()
    cid = store.record_claude_call(
        caller="profile_builder", prompt_chars=2000, output_chars=400,
        elapsed_ms=900, status=CallStatus.OK, model="opus",
        started_at=t, finished_at=t + 0.9,
    )
    with store._db._conn() as c:
        row = c.execute("SELECT * FROM claude_calls WHERE id = ?", (cid,)).fetchone()
    for col in _NEW_COLS:
        assert row[col] is None, f"{col} should be NULL when omitted"
    # Surrogate still the fallback signal.
    assert int(row["cost_estimate_us"]) == estimate_cost_us("opus", 2000, 400)


# ---------------------------------------------------------------------------
# 3. wrappers parse the envelope and persist real numbers
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_run_p():
    """Yield a setter that swaps both run_p / run_p_with_tools on the
    wrappers module for a fixed stdout, restoring on teardown."""
    orig = (_wrappers.run_p, _wrappers.run_p_with_tools)

    def _set(stdout):
        _wrappers.run_p = lambda prompt, **kw: stdout
        _wrappers.run_p_with_tools = lambda prompt, **kw: stdout

    yield _set
    _wrappers.run_p, _wrappers.run_p_with_tools = orig


def test_wrapped_run_p_captures_envelope(store, fake_run_p):
    envelope = _sample_envelope(cost_usd=0.0123)
    fake_run_p(envelope)
    out = _wrappers.wrapped_run_p(store, "cap_normal", "prompt", model="haiku")
    assert out == envelope  # return contract unchanged

    with store._db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='cap_normal'"
        ).fetchone()
    assert row is not None
    # 0.0123 USD -> 12300 micro-USD.
    assert int(row["cost_actual_us"]) == 12_300
    assert int(row["input_tokens"]) == 1200
    assert int(row["output_tokens"]) == 340
    assert int(row["cache_read_tokens"]) == 980
    assert int(row["cache_creation_tokens"]) == 0
    assert int(row["num_turns"]) == 1
    assert row["status"] == "ok"
    # Surrogate retained.
    assert int(row["cost_estimate_us"]) > 0


def test_wrapped_run_p_with_tools_captures_envelope(store, fake_run_p):
    envelope = _sample_envelope(cost_usd=0.5, input_tokens=50_000, num_turns=3)
    fake_run_p(envelope)
    _wrappers.wrapped_run_p_with_tools(store, "cap_tools", "prompt", model="sonnet")
    with store._db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='cap_tools'"
        ).fetchone()
    assert int(row["cost_actual_us"]) == 500_000
    assert int(row["input_tokens"]) == 50_000
    assert int(row["num_turns"]) == 3


# ---------------------------------------------------------------------------
# 4. Graceful fallback on text / empty / no-usage envelopes
# ---------------------------------------------------------------------------

def test_text_output_falls_back_to_surrogate(store, fake_run_p):
    # --output-format text: a plain string, not JSON.
    fake_run_p("just some plain assistant text, no envelope")
    out = _wrappers.wrapped_run_p(store, "fb_text", "prompt", model="haiku")
    assert out == "just some plain assistant text, no envelope"
    with store._db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='fb_text'"
        ).fetchone()
    for col in _NEW_COLS:
        assert row[col] is None, f"{col} should be NULL for text output"
    assert int(row["cost_estimate_us"]) > 0  # surrogate is the only signal
    assert row["status"] == "ok"


def test_none_stdout_falls_back(store, fake_run_p):
    fake_run_p(None)  # CLI missing / crashed
    out = _wrappers.wrapped_run_p(store, "fb_none", "prompt", model="haiku")
    assert out is None
    with store._db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='fb_none'"
        ).fetchone()
    for col in _NEW_COLS:
        assert row[col] is None
    assert row["status"] == "cli_missing"


def test_envelope_without_usage_falls_back(store, fake_run_p):
    # Well-formed envelope with a real result but no usage/cost block
    # (e.g. an older CLI). result_chars should populate; usage stays NULL.
    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "hello", "stop_reason": "end_turn",
    })
    fake_run_p(envelope)
    _wrappers.wrapped_run_p(store, "fb_nousage", "prompt", model="haiku")
    with store._db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='fb_nousage'"
        ).fetchone()
    for col in _NEW_COLS:
        assert row[col] is None, f"{col} should be NULL without a usage block"
    assert int(row["result_chars"]) == len("hello")
    assert int(row["cost_estimate_us"]) > 0


def test_empty_result_envelope_with_usage(store, fake_run_p):
    # The silent-failure mode: result == "" but the envelope still carries
    # usage (input tokens were spent producing nothing). We should record
    # the real input cost AND flag status=empty_result.
    envelope = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "", "stop_reason": "end_turn",
        "total_cost_usd": 0.002,
        "usage": {"input_tokens": 1500, "output_tokens": 0,
                  "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0},
    })
    fake_run_p(envelope)
    _wrappers.wrapped_run_p(store, "fb_empty", "prompt", model="haiku")
    with store._db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='fb_empty'"
        ).fetchone()
    assert row["status"] == "empty_result"
    assert int(row["result_chars"]) == 0
    assert int(row["cost_actual_us"]) == 2_000
    assert int(row["input_tokens"]) == 1500
    assert int(row["output_tokens"]) == 0


# ---------------------------------------------------------------------------
# 5. Read paths prefer cost_actual_us, fall back per row to surrogate
# ---------------------------------------------------------------------------

def test_read_paths_prefer_actual_cost(store):
    t = time.time()
    # Row A: real cost present (actual=10000, surrogate would differ).
    store.record_claude_call(
        caller="job_enrich", prompt_chars=4000, output_chars=800,
        elapsed_ms=100, status=CallStatus.OK, model="haiku",
        started_at=t, finished_at=t + 0.1, cost_actual_us=10_000,
        input_tokens=100, output_tokens=10,
    )
    # Row B: NO real cost -> falls back to surrogate for THAT row.
    surrogate_b = estimate_cost_us("opus", 2000, 400)
    store.record_claude_call(
        caller="job_enrich", prompt_chars=2000, output_chars=400,
        elapsed_ms=100, status=CallStatus.OK, model="opus",
        started_at=t + 1, finished_at=t + 1.1,
    )

    summary = store.claude_call_window_summary(since_ts=t - 1)
    assert summary["count"] == 2
    # Window cost = actual(A) + surrogate(B).
    assert summary["cost_us"] == 10_000 + surrogate_b

    by_caller = store.claude_call_cost_by_caller(since_ts=t - 1)
    je = {r["caller"]: r for r in by_caller}["job_enrich"]
    assert je["cost_us"] == 10_000 + surrogate_b
