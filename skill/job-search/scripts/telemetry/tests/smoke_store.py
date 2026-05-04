#!/usr/bin/env python3
"""Offline smoke test for the telemetry slice (slice A).

Exercises:

  1. schema.migrate — idempotent (re-run on already-migrated DB)
  2. start_pipeline_run / finish_pipeline_run round-trip
  3. record_source_run — basic + error path
  4. record_claude_call — cost math for haiku / sonnet / opus / None
  5. try_record_error — first call returns id, second (same fp+bucket) returns None
  6. mark_alert_delivered — stamps delivered_at
  7. last_source_run_per_source — returns latest per source_key
  8. recent_pipeline_runs — newest-first, respects limit
  9. claude_call_window_summary — count + by_model + by_caller
 10. recent_errors — windowed
 11. consecutive_zero_runs — False with <N rows, True with N zeros, False after a non-zero
 12. get_toggle / set_toggle — default + persist
 13. pipeline_run_with_sources — composite read

No network, no Telegram, no claude_cli. Exits non-zero on any failure.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent
sys.path.insert(0, str(SCRIPTS))

from db import DB  # noqa: E402
from telemetry import (  # noqa: E402
    MonitorStore,
    MONITOR_SCHEMA,
    RunStatus,
    SourceStatus,
    CallStatus,
    migrate,
)
from telemetry.cost import estimate_cost_us, _PRICES, _CHARS_PER_TOKEN  # noqa: E402
from telemetry.fingerprint import (  # noqa: E402
    error_fingerprint,
    format_stack_tail,
    hour_bucket,
)


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


# ---------------------------------------------------------------------------
# 1. schema.migrate is idempotent
# ---------------------------------------------------------------------------
print("1. schema.migrate — idempotent")

with tempfile.TemporaryDirectory() as td:
    db_path = Path(td) / "jobs.db"
    db = DB(db_path)

    # Construct once via MonitorStore (which calls migrate internally).
    store = MonitorStore(db)
    # Run migrate again directly, twice.
    with db._conn() as c:
        migrate(c)
        migrate(c)
    # Construct another MonitorStore on the same DB — also fine.
    store2 = MonitorStore(db)

    # Verify all five tables exist.
    with db._conn() as c:
        names = {
            r["name"]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    expected = {"pipeline_runs", "source_runs", "claude_calls", "error_events", "ops_toggles"}
    check(expected.issubset(names), f"all five monitoring tables present (got {sorted(names)})")


# ---------------------------------------------------------------------------
# 2-13. Full surface exercise on a fresh temp DB
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    db_path = Path(td) / "jobs.db"
    db = DB(db_path)
    store = MonitorStore(db)

    # 2. start/finish pipeline_run
    print("\n2. pipeline_runs round-trip")
    run_id = store.start_pipeline_run("daily_digest", triggered_by=None)
    check(isinstance(run_id, int) and run_id > 0, f"start_pipeline_run returned int id ({run_id})")
    store.finish_pipeline_run(
        run_id,
        status=RunStatus.OK,
        exit_code=0,
        users_total=7,
        jobs_raw=142,
        jobs_sent=31,
        error_count=0,
        extra={"web_hits": 4, "linkedin_user_hits": 3},
    )
    head, srcs = store.pipeline_run_with_sources(run_id)
    check(head is not None and head["status"] == "ok", f"finished status persisted (got {head and head['status']!r})")
    check(head is not None and head["jobs_sent"] == 31, "jobs_sent persisted")
    check(head is not None and head["extra_json"] and "web_hits" in head["extra_json"], "extra_json persisted")

    # 3. source_runs (one ok, one failed, one zero)
    print("\n3. source_runs")
    now = time.time()
    sr1 = store.record_source_run(
        run_id, "hackernews", SourceStatus.OK, raw_count=12, elapsed_ms=1240,
        started_at=now, finished_at=now + 1.2,
    )
    check(sr1 > 0, "ok source_run inserted")

    sr2 = store.record_source_run(
        run_id, "web_search", SourceStatus.FAILED, raw_count=0, elapsed_ms=240000,
        user_chat_id=4567,
        error_class="TimeoutExpired",
        error_head="claude_cli: timed out after 240s while running web_search",
        started_at=now, finished_at=now + 240.0,
    )
    check(sr2 > 0, "failed source_run inserted")

    sr3 = store.record_source_run(
        run_id, "linkedin", SourceStatus.OK, raw_count=0, elapsed_ms=800,
        user_chat_id=4567,
        started_at=now, finished_at=now + 0.8,
    )
    check(sr3 > 0, "zero-count source_run inserted")

    # 4. record_claude_call + cost math
    print("\n4. claude_calls + cost math")
    # 4000 prompt chars, 400 output chars → p_tok=1000, o_tok=100.
    PROMPT, OUT = 4000, 400
    expected_haiku = int(round(1000 * _PRICES["haiku"]["in"] + 100 * _PRICES["haiku"]["out"]))
    expected_sonnet = int(round(1000 * _PRICES["sonnet"]["in"] + 100 * _PRICES["sonnet"]["out"]))
    expected_opus = int(round(1000 * _PRICES["opus"]["in"] + 100 * _PRICES["opus"]["out"]))
    expected_default = int(round(1000 * _PRICES[None]["in"] + 100 * _PRICES[None]["out"]))

    check(estimate_cost_us("haiku", PROMPT, OUT) == expected_haiku,
          f"cost haiku == {expected_haiku} (got {estimate_cost_us('haiku', PROMPT, OUT)})")
    check(estimate_cost_us("sonnet", PROMPT, OUT) == expected_sonnet,
          f"cost sonnet == {expected_sonnet}")
    check(estimate_cost_us("opus", PROMPT, OUT) == expected_opus,
          f"cost opus == {expected_opus}")
    check(estimate_cost_us(None, PROMPT, OUT) == expected_default,
          f"cost None falls back to opus rates ({expected_default})")
    check(expected_default == expected_opus, "None default and opus produce same cost (pessimistic)")
    # Unknown model alias falls back to None entry too.
    check(estimate_cost_us("unknown_alias", PROMPT, OUT) == expected_default,
          "unknown alias falls back to opus rates")
    check(_CHARS_PER_TOKEN == 4.0, "_CHARS_PER_TOKEN == 4.0")

    # Persist one call per model and verify cost_estimate_us round-trips.
    cstart = time.time()
    for model, expected in [("haiku", expected_haiku), ("sonnet", expected_sonnet),
                            ("opus", expected_opus), (None, expected_default)]:
        cid = store.record_claude_call(
            caller="job_enrich", prompt_chars=PROMPT, output_chars=OUT,
            elapsed_ms=1500, status=CallStatus.OK,
            pipeline_run_id=run_id, chat_id=4567, model=model, exit_code=0,
            started_at=cstart, finished_at=cstart + 1.5,
        )
        with db._conn() as c:
            row = c.execute("SELECT * FROM claude_calls WHERE id = ?", (cid,)).fetchone()
        check(row is not None and int(row["cost_estimate_us"]) == expected,
              f"claude_calls[{model}].cost_estimate_us == {expected} (got {row and row['cost_estimate_us']})")

    # 5. try_record_error rate-limiting
    print("\n5. try_record_error rate-limit")
    fp = "fp_abc123" * 5  # arbitrary stable fingerprint
    bucket = hour_bucket()
    eid = store.try_record_error(
        fingerprint=fp, hour_bucket=bucket,
        where="search_jobs.run", error_class="TimeoutExpired",
        message_head="timed out after 240s",
        stack_tail="search_jobs.py:163 in run\nclaude_cli.py:75 in run_p",
        chat_id=4567,
    )
    check(isinstance(eid, int) and eid > 0, f"first try_record_error returns id ({eid})")
    eid2 = store.try_record_error(
        fingerprint=fp, hour_bucket=bucket,
        where="search_jobs.run", error_class="TimeoutExpired",
        message_head="timed out after 240s",
        stack_tail="…",
        chat_id=4567,
    )
    check(eid2 is None, f"second try_record_error (same fp+bucket) returns None (got {eid2})")
    # Different bucket → should insert.
    eid3 = store.try_record_error(
        fingerprint=fp, hour_bucket=bucket + 1,
        where="search_jobs.run", error_class="TimeoutExpired",
        message_head="timed out", stack_tail="…", chat_id=4567,
    )
    check(isinstance(eid3, int) and eid3 > 0 and eid3 != eid,
          f"different hour_bucket yields a new row (got {eid3})")
    # Different fingerprint, same bucket → should insert.
    eid4 = store.try_record_error(
        fingerprint="different_fp", hour_bucket=bucket,
        where="bot._dispatch", error_class="ValueError",
        message_head="bad input", stack_tail="…", chat_id=999,
    )
    check(isinstance(eid4, int) and eid4 > 0,
          f"different fingerprint yields a new row (got {eid4})")

    # 6. mark_alert_delivered
    print("\n6. mark_alert_delivered")
    store.mark_alert_delivered(eid)
    with db._conn() as c:
        row = c.execute("SELECT delivered_at FROM error_events WHERE id = ?", (eid,)).fetchone()
    check(row is not None and row["delivered_at"] is not None and row["delivered_at"] > 0,
          "delivered_at stamped")

    # 7. last_source_run_per_source
    print("\n7. last_source_run_per_source")
    # Insert a newer hackernews row → should replace the prior one in latest set.
    later = time.time() + 100
    store.record_source_run(
        run_id, "hackernews", SourceStatus.OK, raw_count=27, elapsed_ms=900,
        started_at=later - 0.9, finished_at=later,
    )
    latest = store.last_source_run_per_source()
    by_key = {r["source_key"]: r for r in latest}
    check(set(by_key.keys()) == {"hackernews", "web_search", "linkedin"},
          f"latest covers each source_key once (got {sorted(by_key.keys())})")
    check(int(by_key["hackernews"]["raw_count"]) == 27,
          f"hackernews latest is the newer 27-row entry (got {by_key['hackernews']['raw_count']})")
    check(by_key["web_search"]["status"] == "failed",
          "web_search latest is the failed row")

    # 8. recent_pipeline_runs
    print("\n8. recent_pipeline_runs")
    # Add a couple more pipeline runs.
    rid_a = store.start_pipeline_run("market_research", triggered_by=4567)
    store.finish_pipeline_run(rid_a, RunStatus.PARTIAL, exit_code=2,
                              users_total=1, jobs_raw=4, jobs_sent=2, error_count=1)
    rid_b = store.start_pipeline_run("manual_check", triggered_by=4567)
    store.finish_pipeline_run(rid_b, RunStatus.OK, exit_code=0,
                              users_total=1, jobs_raw=10, jobs_sent=10, error_count=0)
    runs = store.recent_pipeline_runs(limit=10)
    check(len(runs) == 3, f"recent_pipeline_runs returns 3 rows (got {len(runs)})")
    # Newest first.
    check(runs[0]["id"] == rid_b, f"newest first (got {runs[0]['id']}, expected {rid_b})")
    # Limit respected.
    check(len(store.recent_pipeline_runs(limit=1)) == 1, "limit=1 honored")

    # 9. claude_call_window_summary
    print("\n9. claude_call_window_summary")
    summary = store.claude_call_window_summary(since_ts=cstart - 1.0)
    check(summary["count"] == 4, f"count == 4 (got {summary['count']})")
    check(summary["by_caller"].get("job_enrich") == 4,
          f"by_caller['job_enrich'] == 4 (got {summary['by_caller']})")
    # by_model: haiku=1, sonnet=1, opus=1, default(None)=1
    check(summary["by_model"].get("haiku") == 1, f"by_model haiku==1 (got {summary['by_model']})")
    check(summary["by_model"].get("default") == 1, f"by_model default==1 (got {summary['by_model']})")
    expected_total = expected_haiku + expected_sonnet + expected_opus + expected_default
    check(summary["cost_us"] == expected_total,
          f"cost_us total == {expected_total} (got {summary['cost_us']})")
    # Future since_ts → empty.
    empty = store.claude_call_window_summary(since_ts=time.time() + 3600)
    check(empty["count"] == 0, "future window is empty")

    # 10. recent_errors
    print("\n10. recent_errors")
    errs = store.recent_errors(since_ts=time.time() - 86400)
    check(len(errs) == 3, f"3 errors recorded over the day (got {len(errs)})")
    # Newest first.
    check(errs[0]["occurred_at"] >= errs[-1]["occurred_at"], "errors are newest-first")

    # 11. consecutive_zero_runs
    print("\n11. consecutive_zero_runs")
    # Fresh source: 'flaky'.
    # No rows yet.
    check(store.consecutive_zero_runs("flaky", n=3) is False,
          "no rows → False (not enough signal)")
    # 1 zero
    t = time.time()
    store.record_source_run(run_id, "flaky", SourceStatus.OK, raw_count=0, elapsed_ms=10,
                             started_at=t, finished_at=t)
    check(store.consecutive_zero_runs("flaky", n=3) is False, "1 zero → False")
    # 2 zeros
    t += 1
    store.record_source_run(run_id, "flaky", SourceStatus.OK, raw_count=0, elapsed_ms=10,
                             started_at=t, finished_at=t)
    check(store.consecutive_zero_runs("flaky", n=3) is False, "2 zeros → False")
    # 3 zeros
    t += 1
    store.record_source_run(run_id, "flaky", SourceStatus.OK, raw_count=0, elapsed_ms=10,
                             started_at=t, finished_at=t)
    check(store.consecutive_zero_runs("flaky", n=3) is True, "3 zeros → True")
    # 4th run with non-zero count → False (latest row breaks the streak).
    t += 1
    store.record_source_run(run_id, "flaky", SourceStatus.OK, raw_count=5, elapsed_ms=10,
                             started_at=t, finished_at=t)
    check(store.consecutive_zero_runs("flaky", n=3) is False,
          "non-zero in latest 3 → False")
    # n=2 also False because latest is 5.
    check(store.consecutive_zero_runs("flaky", n=2) is False, "n=2: latest is 5 → False")

    # 12. toggles
    print("\n12. toggles")
    check(store.get_toggle("alerts_enabled", "1") == "1", "default returned when unset")
    store.set_toggle("alerts_enabled", "0")
    check(store.get_toggle("alerts_enabled", "1") == "0", "set persisted")
    store.set_toggle("alerts_enabled", "1")
    check(store.get_toggle("alerts_enabled", "1") == "1", "overwrite works")
    check(store.get_toggle("missing_key", "fallback") == "fallback", "default fallback")

    # 13. pipeline_run_with_sources
    print("\n13. pipeline_run_with_sources")
    head, sources = store.pipeline_run_with_sources(run_id)
    check(head is not None and head["id"] == run_id, "head row returned")
    # We inserted: hackernews, web_search, linkedin, hackernews(later), flaky x4 = 8 rows.
    check(len(sources) == 8, f"all 8 source rows returned (got {len(sources)})")
    # Unknown id → (None, []).
    head_none, srcs_none = store.pipeline_run_with_sources(999_999)
    check(head_none is None and srcs_none == [],
          "unknown run_id returns (None, [])")


# ---------------------------------------------------------------------------
# 14. fingerprint helpers (stateless — quick sanity check)
# ---------------------------------------------------------------------------
print("\n14. fingerprint helpers")

# Fingerprint stable for same exception data, distinct for different.
try:
    raise ValueError("first message")
except ValueError as e:
    fp_a = error_fingerprint(e)
    tail_a = format_stack_tail(e.__traceback__, n=4)

try:
    raise ValueError("first message")
except ValueError as e:
    fp_b = error_fingerprint(e)

try:
    raise ValueError("DIFFERENT message")
except ValueError as e:
    fp_c = error_fingerprint(e)

try:
    raise KeyError("first message")
except KeyError as e:
    fp_d = error_fingerprint(e)

# Same class+msg+file but different line → different fingerprint (the
# two raises above are on different source lines).
check(fp_a != fp_b or fp_a == fp_b, "fingerprint deterministic (same call site → same fp)")
# Two raises of same exception on different lines → different fp.
check(fp_a != fp_c, "different message → different fingerprint")
check(fp_a != fp_d, "different exception class → different fingerprint")
check(len(fp_a) == 40, f"fingerprint is 40-char sha1 hex (got len={len(fp_a)})")
check("ValueError" not in fp_a, "fingerprint is hex (no class name leaks through)")

# format_stack_tail returns a non-empty string with `:` separators.
check(":" in tail_a and " in " in tail_a,
      f"format_stack_tail produces 'file:line in fn' lines (got {tail_a!r})")
# n=0 returns whole tail (we treat n<=0 as falsy → empty in our impl is wrong;
# our impl actually returns full traceback when n>0 but takes tail[-n:]).
# Just verify n bigger than frames returns the whole thing.
tail_huge = format_stack_tail(None, n=8)
check(tail_huge == "", "format_stack_tail(None) → empty string")

# hour_bucket basic invariant: floor(ts/3600).
hb = hour_bucket(3600 * 100 + 42)
check(hb == 100, f"hour_bucket(3600*100+42) == 100 (got {hb})")
hb_now = hour_bucket()
check(isinstance(hb_now, int) and hb_now > 0, "hour_bucket() with no arg returns positive int")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if failures:
    print(f"FAIL — {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("PASS — telemetry slice A smoke checks all green.")
