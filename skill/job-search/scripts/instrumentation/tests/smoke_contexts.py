#!/usr/bin/env python3
"""Offline smoke test for instrumentation/contexts.py.

Slice A's `telemetry` package is implemented in parallel by another agent
and does NOT exist in this worktree. To keep this test runnable in
isolation we inject a fake `telemetry` package into `sys.modules` BEFORE
importing `instrumentation.contexts`. The fake records every method call
in-memory and conforms to the `MonitorStore` API surface documented in
the monitoring plan §3.

Coverage:
  1. pipeline_run — happy path (status=ok), partial (errors>0), exception
  2. source_run   — happy path, zero+suspicious classification, zero+ok,
                    failed (exception re-raises)
  3. claude_call  — happy path, missing .record() bug → status='exception',
                    exception inside with → status='exception' + re-raise
  4. error_capture — re-raises, records via try_record_error, calls alert_sink
                     when a row is inserted, SHORT-CIRCUITS alert_sink when
                     try_record_error returns None (rate-limited)
  5. wrappers     — wrapped_run_p path (None → status='cli_missing'),
                    non-empty → status='ok'  (covered through monkeypatch)

claude_call status mapping documented choice:
  When caller's underlying CLI returns None we record status='cli_missing'
  (see wrappers._infer_status). The `non_zero` bucket is reserved for cases
  where future code learns to distinguish missing-CLI from non-zero-exit.

Run:
    python3 skill/job-search/scripts/instrumentation/tests/smoke_contexts.py
"""
from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent  # …/scripts
sys.path.insert(0, str(SCRIPTS))

# ---------------------------------------------------------------------------
# Fake `telemetry` package — installed BEFORE importing instrumentation.
# Mirrors the MonitorStore API surface from monitoring-plan.md §3.
# ---------------------------------------------------------------------------


@dataclass
class FakeStore:
    """In-memory MonitorStore stand-in. Records calls; fakes rate-limit."""
    pipeline_runs: list[dict] = field(default_factory=list)
    pipeline_finishes: list[dict] = field(default_factory=list)
    source_rows: list[dict] = field(default_factory=list)
    claude_rows: list[dict] = field(default_factory=list)
    error_rows: list[dict] = field(default_factory=list)
    delivered: list[int] = field(default_factory=list)
    toggles: dict[str, str] = field(default_factory=dict)

    # Behavior knobs the tests flip:
    suspicious_zero_for: set[str] = field(default_factory=set)
    seen_fingerprints: set[tuple] = field(default_factory=set)
    next_run_id: int = 0
    next_event_id: int = 0

    # ---- pipeline ----------------------------------------------------------
    def start_pipeline_run(self, kind: str, triggered_by: int | None) -> int:
        self.next_run_id += 1
        rid = self.next_run_id
        self.pipeline_runs.append(
            {"id": rid, "kind": kind, "triggered_by": triggered_by}
        )
        return rid

    def finish_pipeline_run(self, **kw) -> None:
        self.pipeline_finishes.append(kw)

    # ---- source -----------------------------------------------------------
    def record_source_run(
        self,
        pipeline_run_id: int,
        source_key: str,
        status: str,
        raw_count: int,
        elapsed_ms: int | None,
        *,
        user_chat_id: int | None = None,
        error_class: str | None = None,
        error_head: str | None = None,
        started_at: float,
        finished_at: float,
    ) -> None:
        self.source_rows.append({
            "pipeline_run_id": pipeline_run_id,
            "source_key": source_key,
            "status": status,
            "raw_count": raw_count,
            "elapsed_ms": elapsed_ms,
            "user_chat_id": user_chat_id,
            "error_class": error_class,
            "error_head": error_head,
            "started_at": started_at,
            "finished_at": finished_at,
        })

    def consecutive_zero_runs(self, source_key: str, n: int = 3) -> bool:
        return source_key in self.suspicious_zero_for

    # ---- claude -----------------------------------------------------------
    def record_claude_call(self, **kw) -> None:
        self.claude_rows.append(kw)

    # ---- errors -----------------------------------------------------------
    def try_record_error(
        self,
        *,
        fingerprint: str,
        hour_bucket: int,
        where: str,
        error_class: str,
        message_head: str,
        stack_tail: str,
        chat_id: int | None,
    ) -> int | None:
        key = (fingerprint, hour_bucket)
        if key in self.seen_fingerprints:
            return None
        self.seen_fingerprints.add(key)
        self.next_event_id += 1
        eid = self.next_event_id
        self.error_rows.append({
            "id": eid,
            "fingerprint": fingerprint,
            "hour_bucket": hour_bucket,
            "where": where,
            "error_class": error_class,
            "message_head": message_head,
            "stack_tail": stack_tail,
            "chat_id": chat_id,
        })
        return eid

    def mark_alert_delivered(self, event_id: int) -> None:
        self.delivered.append(event_id)

    # ---- toggles (unused here but part of the contract) --------------------
    def get_toggle(self, key: str, default: str) -> str:
        return self.toggles.get(key, default)

    def set_toggle(self, key: str, value: str) -> None:
        self.toggles[key] = value


def _install_fake_telemetry() -> None:
    """Inject a fake `telemetry` package into sys.modules.

    instrumentation.contexts does `from telemetry import MonitorStore` and
    `from telemetry.fingerprint import ...` at import time, so we must
    register the package and its `fingerprint` submodule before that import.
    """
    pkg = types.ModuleType("telemetry")
    pkg.__path__ = []  # mark as a package for submodule imports
    pkg.MonitorStore = FakeStore  # type: ignore[attr-defined]
    sys.modules["telemetry"] = pkg

    fp = types.ModuleType("telemetry.fingerprint")

    def error_fingerprint(exc: BaseException) -> str:
        # Fingerprint by class name + first 80 chars of message — same
        # exceptions in the same hour collide (per spec).
        try:
            msg = str(exc)
        except Exception:
            msg = ""
        return f"{type(exc).__name__}:{msg[:80]}"

    def format_stack_tail(tb, n: int = 8) -> str:
        import traceback as _tb
        frames = _tb.extract_tb(tb)
        tail = frames[-n:]
        return "\n".join(f"{f.filename}:{f.lineno} in {f.name}" for f in tail)

    def hour_bucket(ts: float | None = None) -> int:
        import time as _t
        return int((_t.time() if ts is None else ts) // 3600)

    fp.error_fingerprint = error_fingerprint
    fp.format_stack_tail = format_stack_tail
    fp.hour_bucket = hour_bucket
    sys.modules["telemetry.fingerprint"] = fp
    pkg.fingerprint = fp  # type: ignore[attr-defined]


_install_fake_telemetry()

# Now we can import the slice-B code.
from instrumentation.contexts import (  # noqa: E402
    AlertEnvelope,
    claude_call,
    error_capture,
    pipeline_run,
    source_run,
)


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


# ---------------------------------------------------------------------------
# 1. pipeline_run
# ---------------------------------------------------------------------------
print("1. pipeline_run")

store = FakeStore()
with pipeline_run(store, "daily_digest", triggered_by=None) as p:
    p.set_users_total(7)
    p.set_jobs_raw(142)
    p.set_jobs_sent(31)
    p.record_extra("web_hits", 4)

check(len(store.pipeline_runs) == 1, "pipeline started")
check(len(store.pipeline_finishes) == 1, "pipeline finished")
fin = store.pipeline_finishes[0]
check(fin["status"] == "ok", f"happy path status=ok (got {fin['status']!r})")
check(fin["users_total"] == 7, "users_total propagated")
check(fin["jobs_raw"] == 142, "jobs_raw propagated")
check(fin["jobs_sent"] == 31, "jobs_sent propagated")
check(fin["extra"].get("web_hits") == 4, "extra propagated")

# Partial: errors>0
store = FakeStore()
with pipeline_run(store, "daily_digest") as p:
    p.incr_errors(2)
fin = store.pipeline_finishes[0]
check(fin["status"] == "partial", f"errors>0 → partial (got {fin['status']!r})")
check(fin["error_count"] == 2, "error_count tracked")

# Exception path
store = FakeStore()
raised = False
try:
    with pipeline_run(store, "manual_check", triggered_by=42):
        raise RuntimeError("boom")
except RuntimeError:
    raised = True
check(raised, "pipeline_run re-raises exception")
check(len(store.pipeline_finishes) == 1, "pipeline still finalized on exception")
check(
    store.pipeline_finishes[0]["status"] == "exception",
    f"exception → status=exception (got {store.pipeline_finishes[0]['status']!r})",
)


# ---------------------------------------------------------------------------
# 2. source_run
# ---------------------------------------------------------------------------
print("\n2. source_run")

# Happy path: count > 0 → ok
store = FakeStore()
with source_run(store, pipeline_run_id=1, source_key="hackernews") as s:
    s.set_count(12)
check(len(store.source_rows) == 1, "source row written")
check(store.source_rows[0]["status"] == "ok", "count>0 → ok")
check(store.source_rows[0]["raw_count"] == 12, "raw_count propagated")

# Zero results, NOT suspicious → ok
store = FakeStore()
with source_run(store, pipeline_run_id=1, source_key="linkedin") as s:
    s.set_count(0)
check(store.source_rows[0]["status"] == "ok",
      f"zero+not-suspicious → ok (got {store.source_rows[0]['status']!r})")

# Zero results, IS suspicious → suspicious_zero
store = FakeStore()
store.suspicious_zero_for = {"linkedin"}
with source_run(store, pipeline_run_id=1, source_key="linkedin") as s:
    s.set_count(0)
check(store.source_rows[0]["status"] == "suspicious_zero",
      f"zero+suspicious → suspicious_zero (got {store.source_rows[0]['status']!r})")

# Exception → failed + re-raise
store = FakeStore()
raised = False
try:
    with source_run(store, pipeline_run_id=1, source_key="web_search",
                    user_chat_id=42) as s:
        raise ValueError("network down")
except ValueError:
    raised = True
check(raised, "source_run re-raises")
check(store.source_rows[0]["status"] == "failed", "exception → failed")
check(store.source_rows[0]["error_class"] == "ValueError", "error_class set")
check(store.source_rows[0]["error_head"] == "network down", "error_head captured")
check(store.source_rows[0]["user_chat_id"] == 42, "user_chat_id propagated")


# ---------------------------------------------------------------------------
# 3. claude_call
# ---------------------------------------------------------------------------
print("\n3. claude_call")

# Happy path: caller calls .record()
store = FakeStore()
with claude_call(store, caller="job_enrich", model="haiku", chat_id=42) as c:
    c.record(prompt_chars=1234, output_chars=567, exit_code=0, status="ok")
check(len(store.claude_rows) == 1, "row recorded")
row = store.claude_rows[0]
check(row["status"] == "ok", "happy status=ok")
check(row["prompt_chars"] == 1234, "prompt_chars propagated")
check(row["output_chars"] == 567, "output_chars propagated")
check(row["caller"] == "job_enrich", "caller propagated")
check(row["model"] == "haiku", "model propagated")
check(row["chat_id"] == 42, "chat_id propagated")

# Caller forgot to call .record() → best-effort exception row
store = FakeStore()
with claude_call(store, caller="profile_builder") as c:
    pass  # caller bug — never called .record()
check(len(store.claude_rows) == 1, "row recorded even when .record() missed")
check(store.claude_rows[0]["status"] == "exception",
      f"missing-record → status=exception (got {store.claude_rows[0]['status']!r})")

# Exception inside with → status=exception + re-raise
store = FakeStore()
raised = False
try:
    with claude_call(store, caller="market_research:worker") as c:
        c.record(prompt_chars=10, output_chars=0, exit_code=None, status="ok")
        raise RuntimeError("post-record blowup")
except RuntimeError:
    raised = True
check(raised, "claude_call re-raises")
check(store.claude_rows[0]["status"] == "exception",
      "exception inside with → status=exception (overrides recorded status)")


# ---------------------------------------------------------------------------
# 4. error_capture
# ---------------------------------------------------------------------------
print("\n4. error_capture")

# Re-raises + records + invokes alert_sink on first occurrence
store = FakeStore()
sink_calls: list[AlertEnvelope] = []

def sink(env: AlertEnvelope) -> None:
    sink_calls.append(env)

raised = False
try:
    with error_capture(store, where="bot._dispatch", chat_id=42, alert_sink=sink):
        raise RuntimeError("first")
except RuntimeError:
    raised = True
check(raised, "error_capture re-raises")
check(len(store.error_rows) == 1, "first occurrence recorded")
check(len(sink_calls) == 1, "alert_sink called for first occurrence")
env = sink_calls[0]
check(env.where == "bot._dispatch", "envelope where")
check(env.error_class == "RuntimeError", "envelope error_class")
check(env.message_head == "first", "envelope message_head")
check(env.chat_id == 42, "envelope chat_id")
check(env.event_id == 1, "envelope event_id")
check(env.stack_tail != "", "envelope stack_tail non-empty")

# Rate-limit: SAME exception again → try_record_error returns None →
# alert_sink NOT called
raised = False
try:
    with error_capture(store, where="bot._dispatch", chat_id=42, alert_sink=sink):
        raise RuntimeError("first")  # same fp → same hour-bucket
except RuntimeError:
    raised = True
check(raised, "rate-limited path still re-raises")
check(len(store.error_rows) == 1, "rate-limited: no new error row")
check(len(sink_calls) == 1, "rate-limited: alert_sink NOT called again")

# Different exception class — fresh fingerprint → sink called again
raised = False
try:
    with error_capture(store, where="bot._dispatch", chat_id=42, alert_sink=sink):
        raise ValueError("different")
except ValueError:
    raised = True
check(raised, "second class re-raises")
check(len(store.error_rows) == 2, "different fp → new error row")
check(len(sink_calls) == 2, "different fp → sink called")

# alert_sink can be None — should still record + re-raise
store2 = FakeStore()
raised = False
try:
    with error_capture(store2, where="x", chat_id=None, alert_sink=None):
        raise KeyError("k")
except KeyError:
    raised = True
check(raised, "no-sink path re-raises")
check(len(store2.error_rows) == 1, "no-sink path still records")

# alert_sink that raises must NOT replace the original exception
def bad_sink(env: AlertEnvelope) -> None:
    raise RuntimeError("sink kaboom")

store3 = FakeStore()
got_exc: BaseException | None = None
try:
    with error_capture(store3, where="x", alert_sink=bad_sink):
        raise ValueError("real")
except BaseException as e:
    got_exc = e
check(isinstance(got_exc, ValueError),
      f"bad_sink does not replace original exc (got {type(got_exc).__name__})")

# BaseException (KeyboardInterrupt) propagates without being captured
store4 = FakeStore()
got_exc = None
try:
    with error_capture(store4, where="x", alert_sink=sink):
        raise KeyboardInterrupt
except BaseException as e:
    got_exc = e
check(isinstance(got_exc, KeyboardInterrupt),
      "BaseException propagates untouched")
check(len(store4.error_rows) == 0, "BaseException is NOT recorded")


# ---------------------------------------------------------------------------
# 5. wrappers — claude_cli passthrough with monkeypatched run_p
# ---------------------------------------------------------------------------
print("\n5. wrappers")

import claude_cli  # noqa: E402
import instrumentation.wrappers as W  # noqa: E402

# Happy path — non-empty stdout → status='ok'
def _fake_run_p_ok(prompt, *args, **kwargs):
    return "hello"

orig_run_p = W.run_p
W.run_p = _fake_run_p_ok
try:
    store = FakeStore()
    out = W.wrapped_run_p(
        store, caller="job_enrich", prompt="x" * 100,
        chat_id=42, pipeline_run_id=7, model="haiku",
    )
    check(out == "hello", "wrapped_run_p returns underlying stdout")
    check(len(store.claude_rows) == 1, "wrapped_run_p records a row")
    row = store.claude_rows[0]
    check(row["status"] == "ok", "non-empty stdout → status=ok")
    check(row["prompt_chars"] == 100, "prompt_chars=len(prompt)")
    check(row["output_chars"] == 5, "output_chars=len(stdout)")
    check(row["caller"] == "job_enrich", "caller propagated")
    check(row["pipeline_run_id"] == 7, "pipeline_run_id propagated")
    check(row["model"] == "haiku", "model propagated")
finally:
    W.run_p = orig_run_p

# None return → status='cli_missing'
def _fake_run_p_none(prompt, *args, **kwargs):
    return None

orig_run_p = W.run_p
W.run_p = _fake_run_p_none
try:
    store = FakeStore()
    out = W.wrapped_run_p(store, caller="job_enrich", prompt="x")
    check(out is None, "None passthrough")
    check(store.claude_rows[0]["status"] == "cli_missing",
          f"None → status=cli_missing (got {store.claude_rows[0]['status']!r})")
    check(store.claude_rows[0]["output_chars"] == 0, "output_chars=0 for None")
finally:
    W.run_p = orig_run_p

# wrapped_run_p_with_tools — non-empty
def _fake_tools_ok(prompt, *args, **kwargs):
    return "tooled"

orig_tools = W.run_p_with_tools
W.run_p_with_tools = _fake_tools_ok
try:
    store = FakeStore()
    out = W.wrapped_run_p_with_tools(
        store, caller="market_research:worker",
        prompt="hi", model="opus",
    )
    check(out == "tooled", "wrapped_run_p_with_tools passthrough")
    check(store.claude_rows[0]["status"] == "ok", "tools status=ok")
    check(store.claude_rows[0]["caller"] == "market_research:worker",
          "tools caller propagated")
finally:
    W.run_p_with_tools = orig_tools


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if failures:
    print(f"FAIL: {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("All instrumentation smoke checks passed.")
