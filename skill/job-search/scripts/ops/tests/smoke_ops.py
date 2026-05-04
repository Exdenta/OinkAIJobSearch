#!/usr/bin/env python3
"""Offline smoke test for slice C — ops/.

Covers everything that can run without a live Telegram or slice-A's real
sqlite-backed MonitorStore. We build:

  * `FakeTG` — records send_message calls; no network.
  * `FakeStore` — in-memory rows in the exact shapes documented in plan §3
    for the slice-A `MonitorStore` read helpers.
  * `StubAlertEnvelope` — local dataclass mirroring slice B's
    `instrumentation.contexts.AlertEnvelope` (per plan §7). The real one
    lives there; we stub it so this slice tests independently.

Anchor-substring matching (not full golden strings): the rendered output is
allowed to drift visually as long as the load-bearing facts are present.

Exits non-zero on any failure so CI / shell smoke can rely on $?.

Usage:
    cd <worktree-root>
    python3 skill/job-search/scripts/ops/tests/smoke_ops.py
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent  # ops/tests → ops → scripts
sys.path.insert(0, str(SCRIPTS))

# Slice C public surface
from ops import (  # noqa: E402
    deliver_alert,
    deliver_daily_summary,
    handle_operator_command,
    is_operator,
)
from ops.alerts import _code_block, render_alert  # noqa: E402
from ops.commands import (  # noqa: E402
    cmd_alerts,
    cmd_health,
    cmd_runlog,
    cmd_stats,
)
from ops.summary import build_daily_summary  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def contains_all(s: str, needles: list[str]) -> bool:
    missing = [n for n in needles if n not in s]
    if missing:
        print(f"      missing: {missing!r}")
        print(f"      body was:\n{s}")
        return False
    return True


# ---------------------------------------------------------------------------
# Stub AlertEnvelope (real one lives in slice B: instrumentation/contexts.py)
# ---------------------------------------------------------------------------

@dataclass
class StubAlertEnvelope:
    where: str
    error_class: str
    message_head: str
    stack_tail: str
    chat_id: int | None
    occurred_at: float
    fingerprint: str
    event_id: int


# ---------------------------------------------------------------------------
# Fake Telegram client — records send_message calls
# ---------------------------------------------------------------------------

class FakeTG:
    def __init__(self) -> None:
        self.sends: list[dict] = []

    def send_message(self, chat_id, text, parse_mode="MarkdownV2", **kwargs) -> int:
        self.sends.append({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            **kwargs,
        })
        return 1


# ---------------------------------------------------------------------------
# Fake MonitorStore — minimum surface needed by ops/. Returns canned rows in
# the shapes documented in plan §3 (slice A read helpers).
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self) -> None:
        self.toggles: dict[str, str] = {}
        self.last_source_rows: list[dict] = []
        self.recent_errs_rows: list[dict] = []
        self.recent_runs_rows: list[dict] = []
        self.window_summary: dict = {}
        self.run_with_sources: dict[int, tuple[dict, list[dict]]] = {}
        self.delivered_event_ids: list[int] = []

    # --- toggles ---
    def get_toggle(self, key: str, default: str) -> str:
        return self.toggles.get(key, default)

    def set_toggle(self, key: str, value: str) -> None:
        self.toggles[key] = value

    # --- read helpers ---
    def last_source_run_per_source(self) -> list[dict]:
        return list(self.last_source_rows)

    def recent_errors(self, since_ts: float) -> list[dict]:
        return [r for r in self.recent_errs_rows if r.get("occurred_at", 0) >= since_ts]

    def recent_pipeline_runs(self, limit: int) -> list[dict]:
        return list(self.recent_runs_rows)[:limit]

    def claude_call_window_summary(self, since_ts: float) -> dict:
        # Simulate windowed numbers: 7d window asks for since=now-7*86400; the
        # 24h window asks for since=now-86400. Threshold halfway between.
        if since_ts < time.time() - 2 * 86400:
            return {
                "calls": 974,
                "by_model": {"haiku": 900, "opus": 60, "sonnet": 14},
                "by_caller": {"job_enrich": 800, "market_research:worker": 100, "fit_analyzer": 74},
                "prompt_chars": 8_900_000,
                "output_chars": 1_200_000,
                "cost_estimate_us": 2_180_000,  # $2.18
                "active_users": 11,
                "digests_sent": 218,
            }
        return dict(self.window_summary)

    def pipeline_run_with_sources(self, run_id: int):
        return self.run_with_sources[run_id]

    def mark_alert_delivered(self, event_id: int) -> None:
        self.delivered_event_ids.append(event_id)


def _now_minus(seconds: float) -> float:
    return time.time() - seconds


def _seed_default(store: FakeStore) -> None:
    """Populate FakeStore with a realistic run that has happy + degenerate sources."""
    now = time.time()
    store.last_source_rows = [
        {"source_key": "hackernews",     "status": "ok",               "raw_count": 12, "finished_at": now - 4,    "error_class": None},
        {"source_key": "remote_boards",  "status": "ok",               "raw_count": 27, "finished_at": now - 4,    "error_class": None},
        {"source_key": "curated_boards", "status": "ok",               "raw_count": 8,  "finished_at": now - 4,    "error_class": None},
        {"source_key": "linkedin",       "status": "suspicious_zero",  "raw_count": 0,  "finished_at": now - 79200, "error_class": None},
        {"source_key": "web_search",     "status": "failed",           "raw_count": 0,  "finished_at": now - 79200, "error_class": "TimeoutExpired"},
    ]
    store.recent_errs_rows = [
        {"id": 1, "occurred_at": now - 3600, "delivered_at": now - 3600, "error_class": "TimeoutExpired"},
        {"id": 2, "occurred_at": now - 7200, "delivered_at": None,        "error_class": "RuntimeError"},
    ]
    store.recent_runs_rows = [
        {
            "id": 2031, "kind": "daily_digest", "status": "ok",      "exit_code": 0,
            "users_total": 7, "jobs_raw": 142, "jobs_sent": 31, "error_count": 0,
            "started_at": now - 261, "finished_at": now - 0, "extra_json": None,
        },
        {
            "id": 2030, "kind": "daily_digest", "status": "partial", "exit_code": 0,
            "users_total": 7, "jobs_raw": 119, "jobs_sent": 24, "error_count": 1,
            "started_at": now - 86400 - 247, "finished_at": now - 86400, "extra_json": None,
        },
        {
            "id": 2029, "kind": "market_research", "status": "ok", "exit_code": 0,
            "users_total": 1, "jobs_raw": 0, "jobs_sent": 0, "error_count": 0,
            "started_at": now - 90000, "finished_at": now - 89400, "extra_json": None,
        },
    ]
    store.window_summary = {
        "calls": 142,
        "by_model": {"haiku": 128, "opus": 12, "sonnet": 2},
        "by_caller": {
            "job_enrich": 88,
            "market_research:worker": 12,
            "fit_analyzer": 22,
            "profile_builder": 18,
            "resume_tailor": 2,
        },
        "prompt_chars": 1_200_000,
        "output_chars": 180_000,
        "cost_estimate_us": 310_000,  # $0.31
        "active_users": 5,
        "digests_sent": 31,
    }
    store.run_with_sources[2031] = (
        store.recent_runs_rows[0],
        [
            {"source_key": "hackernews",     "status": "ok",     "raw_count": 12, "user_chat_id": None},
            {"source_key": "remote_boards",  "status": "ok",     "raw_count": 27, "user_chat_id": None},
            {"source_key": "curated_boards", "status": "ok",     "raw_count": 8,  "user_chat_id": None},
            {"source_key": "linkedin",       "status": "ok",     "raw_count": 3,  "user_chat_id": 100},
            {"source_key": "web_search",     "status": "ok",     "raw_count": 4,  "user_chat_id": 100},
            {"source_key": "linkedin",       "status": "failed", "raw_count": 0,  "user_chat_id": 200},
            {"source_key": "web_search",     "status": "failed", "raw_count": 0,  "user_chat_id": 200},
        ],
    )


# ---------------------------------------------------------------------------
# Operator env management — context-manager-ish helper for safe restore
# ---------------------------------------------------------------------------

class env_var:
    """Tiny replacement for pytest's monkeypatch.setenv/delenv."""

    def __init__(self, name: str, value: str | None) -> None:
        self.name = name
        self.value = value
        self._prev: str | None = None
        self._had: bool = False

    def __enter__(self):
        self._had = self.name in os.environ
        self._prev = os.environ.get(self.name)
        if self.value is None:
            os.environ.pop(self.name, None)
        else:
            os.environ[self.name] = self.value
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._had:
            os.environ[self.name] = self._prev or ""
        else:
            os.environ.pop(self.name, None)


# ---------------------------------------------------------------------------
# 1. is_operator — env behavior
# ---------------------------------------------------------------------------
print("1. is_operator — env handling")

with env_var("OPERATOR_CHAT_ID", None):
    check(is_operator(12345) is False, "missing env → False")
with env_var("OPERATOR_CHAT_ID", ""):
    check(is_operator(12345) is False, "empty env → False")
with env_var("OPERATOR_CHAT_ID", "not-an-int"):
    check(is_operator(12345) is False, "unparseable env → False")
with env_var("OPERATOR_CHAT_ID", "12345"):
    check(is_operator(12345) is True, "matching int → True")
    check(is_operator(99999) is False, "non-matching int → False")
    check(is_operator("12345") is True, "string chat_id matches int env")


# ---------------------------------------------------------------------------
# 2. _code_block — backtick + backslash escaping
# ---------------------------------------------------------------------------
print("\n2. _code_block — escaping")

cb = _code_block("hello `world`")
check(cb.startswith("```\n") and cb.endswith("\n```"), "wrapped in triple-backticks")
check("\\`world\\`" in cb, "inner backticks escaped")

cb2 = _code_block("path\\with\\backslash")
check("path\\\\with\\\\backslash" in cb2, "backslashes escaped first")


# ---------------------------------------------------------------------------
# 3. render_alert — preserves all envelope fields
# ---------------------------------------------------------------------------
print("\n3. render_alert — envelope preservation")

env = StubAlertEnvelope(
    where="bot.handle_callback",
    error_class="TimeoutExpired",
    message_head="claude_cli: timed out after 240s\nwhile running enrich_jobs_ai for chat 45678901",
    stack_tail="bot.py:1287 in handle_callback\nbot.py:1834 in _start_fit_analysis\nfit_analyzer.py:179 in analyze_fit\nclaude_cli.py:75 in run_p",
    chat_id=45678901,
    occurred_at=time.time(),
    fingerprint="7e8a9c0d1b2e3f",
    event_id=42,
)
body = render_alert(env)
# Note: message_head is rendered as MarkdownV2 quoted text (with `> ` prefix),
# so reserved chars like `_` and `.` are escaped via mdv2_escape. The stack
# tail block is inside ``` … ``` so only backtick+backslash are escaped — the
# raw frame strings pass through verbatim.
check(contains_all(body, [
    "Bot error",
    "bot\\.handle\\_callback",        # mdv2-escaped where
    "TimeoutExpired",
    "4567",                            # redaction prefix kept
    "8901",                            # redaction suffix kept
    "claude\\_cli: timed out",         # message_head escaped underscore
    "stack tail",                      # stack-tail label inside code block
    "bot.py:1287 in handle_callback",  # first frame (verbatim in code block)
    "claude_cli.py:75 in run_p",       # last frame (verbatim in code block)
    "fp: 7e8a9c",                      # fingerprint prefix
]), "alert body contains all key fields")


# ---------------------------------------------------------------------------
# 4. deliver_alert — exception-safe + toggle gating + delivery confirmation
# ---------------------------------------------------------------------------
print("\n4. deliver_alert — gating and exception safety")

# 4a. No env → no send, no exception.
store = FakeStore()
tg = FakeTG()
with env_var("OPERATOR_CHAT_ID", None):
    deliver_alert(tg, store, env)
check(len(tg.sends) == 0, "no operator env → no send")

# 4b. With env, alerts_enabled='1' (default) → send + mark delivered.
store = FakeStore()
tg = FakeTG()
with env_var("OPERATOR_CHAT_ID", "9999"):
    deliver_alert(tg, store, env)
check(len(tg.sends) == 1, "env set + default → send")
check(tg.sends[0]["chat_id"] == 9999, "send goes to operator chat")
check(tg.sends[0]["parse_mode"] == "MarkdownV2", "parse_mode=MarkdownV2")
check(store.delivered_event_ids == [42], "event id marked delivered")

# 4c. alerts_enabled='0' → no send.
store = FakeStore()
store.set_toggle("alerts_enabled", "0")
tg = FakeTG()
with env_var("OPERATOR_CHAT_ID", "9999"):
    deliver_alert(tg, store, env)
check(len(tg.sends) == 0, "alerts_enabled=0 → no send")

# 4d. tg.send_message raises → swallowed, no propagation.
class ExplodingTG:
    def send_message(self, *a, **kw):
        raise RuntimeError("network down")
store = FakeStore()
with env_var("OPERATOR_CHAT_ID", "9999"):
    try:
        deliver_alert(ExplodingTG(), store, env)
        ok = True
    except Exception as e:
        ok = False
        print(f"      raised: {e!r}")
check(ok, "tg explodes → deliver_alert swallows")

# 4e. malformed envelope → swallowed.
class BadEnv:
    pass
with env_var("OPERATOR_CHAT_ID", "9999"):
    try:
        deliver_alert(FakeTG(), FakeStore(), BadEnv())
        ok = True
    except Exception:
        ok = False
check(ok, "broken envelope → deliver_alert swallows")


# ---------------------------------------------------------------------------
# 5. build_daily_summary — happy path
# ---------------------------------------------------------------------------
print("\n5. build_daily_summary — happy path")

store = FakeStore()
_seed_default(store)
summary = build_daily_summary(store, 2031)
# Daily summary body is plain MDv2 (no code block), so source keys with
# underscores get backslash-escaped: 'remote_boards' → 'remote\_boards'.
check(contains_all(summary, [
    "Daily digest",
    "Sources",
    "delivered to",
    "hackernews 12",
    "remote\\_boards 27",
    "curated\\_boards 8",
    "Run",
    "2031",
]), "daily summary contains key facts")
# Seed has 2 failed per-user sources (linkedin/web_search for chat 200) — the
# new layout surfaces partial failures inline so the operator notices them
# before they snowball. Spot-check that flag is rendered.
check("Anomalies:" in summary, "partial failures surface as anomaly")
check("source error" in summary, "anomaly mentions source errors")


# ---------------------------------------------------------------------------
# 6. build_daily_summary — zero raw → still 4 lines
# ---------------------------------------------------------------------------
print("\n6. build_daily_summary — zero raw edge case")

now = time.time()
store2 = FakeStore()
zero_run = {
    "id": 99, "kind": "daily_digest", "status": "ok", "exit_code": 0,
    "users_total": 0, "jobs_raw": 0, "jobs_sent": 0, "error_count": 0,
    "started_at": now - 5, "finished_at": now, "extra_json": None,
}
store2.run_with_sources[99] = (zero_run, [])
out = build_daily_summary(store2, 99)
non_empty_lines = [l for l in out.splitlines() if l.strip()]
check(len(non_empty_lines) >= 4, f"≥4 non-empty lines (got {len(non_empty_lines)})")
check("No jobs delivered" in out, "zero-delivered phrasing visible")
check("no source data" in out, "no-source-data placeholder visible")


# ---------------------------------------------------------------------------
# 7. build_daily_summary — all sources failed → anomalies line present
# ---------------------------------------------------------------------------
print("\n7. build_daily_summary — all sources failed")

store3 = FakeStore()
fail_run = {
    "id": 100, "kind": "daily_digest", "status": "partial", "exit_code": 1,
    "users_total": 1, "jobs_raw": 0, "jobs_sent": 0, "error_count": 5,
    "started_at": now - 10, "finished_at": now, "extra_json": None,
}
store3.run_with_sources[100] = (fail_run, [
    {"source_key": "hackernews",    "status": "failed", "raw_count": 0, "user_chat_id": None},
    {"source_key": "remote_boards", "status": "failed", "raw_count": 0, "user_chat_id": None},
])
out = build_daily_summary(store3, 100)
check("Anomalies:" in out, "Anomalies line present when all sources failed")
check("all sources failed" in out, "synthesized anomaly text present")


# ---------------------------------------------------------------------------
# 8. build_daily_summary — extra_json anomalies pass through
# ---------------------------------------------------------------------------
print("\n8. build_daily_summary — extra_json anomalies pass through")

store4 = FakeStore()
anom_run = {
    "id": 101, "kind": "daily_digest", "status": "ok", "exit_code": 0,
    "users_total": 7, "jobs_raw": 142, "jobs_sent": 31, "error_count": 0,
    "started_at": now - 261, "finished_at": now,
    "extra_json": '{"anomalies": ["linkedin 0 results x 3 runs in a row"]}',
}
store4.run_with_sources[101] = (anom_run, [
    {"source_key": "hackernews", "status": "ok", "raw_count": 12, "user_chat_id": None},
])
out = build_daily_summary(store4, 101)
check("Anomalies:" in out, "Anomalies line present from extra_json")
check("linkedin 0 results" in out, "anomaly text passed through")


# ---------------------------------------------------------------------------
# 9. deliver_daily_summary — quiet_alerts=on → no send
# ---------------------------------------------------------------------------
print("\n9. deliver_daily_summary — quiet_alerts gating")

store = FakeStore()
_seed_default(store)
store.set_toggle("quiet_alerts", "1")
tg = FakeTG()
with env_var("OPERATOR_CHAT_ID", "9999"):
    deliver_daily_summary(tg, store, 2031)
check(len(tg.sends) == 0, "quiet_alerts=1 → no send")

# Without quiet → sends.
store.set_toggle("quiet_alerts", "0")
tg = FakeTG()
with env_var("OPERATOR_CHAT_ID", "9999"):
    deliver_daily_summary(tg, store, 2031)
check(len(tg.sends) == 1, "quiet_alerts=0 → sends")
check(tg.sends[0]["chat_id"] == 9999, "delivered to operator chat")

# No env → no send (even with quiet=0).
tg = FakeTG()
with env_var("OPERATOR_CHAT_ID", None):
    deliver_daily_summary(tg, store, 2031)
check(len(tg.sends) == 0, "no operator env → no send")

# Exception in render → swallowed.
broken_store = FakeStore()
def _broken(*a, **kw): raise RuntimeError("boom")
broken_store.pipeline_run_with_sources = _broken  # type: ignore[assignment]
with env_var("OPERATOR_CHAT_ID", "9999"):
    try:
        deliver_daily_summary(FakeTG(), broken_store, 1)
        ok = True
    except Exception:
        ok = False
check(ok, "broken store → deliver_daily_summary swallows")


# ---------------------------------------------------------------------------
# 10. cmd_health — output anchors
# ---------------------------------------------------------------------------
print("\n10. cmd_health — render anchors")

store = FakeStore()
_seed_default(store)
tg = FakeTG()
cmd_health(tg, store, 9999, [])
check(len(tg.sends) == 1, "one send")
text = tg.sends[0]["text"]
check(contains_all(text, [
    "Health",
    "Sources",
    "hackernews",
    "remote_boards",
    "curated_boards",
    "linkedin",
    "suspicious_zero",
    "web_search",
    "failed",
    "TimeoutExpired",
    "Errors (last 24h)",
    "Toggles:",
    "alerts=on",
    "quiet_alerts=off",
]), "/health contains expected anchors")


# ---------------------------------------------------------------------------
# 11. cmd_stats — both windows render with key sections
# ---------------------------------------------------------------------------
print("\n11. cmd_stats — render anchors")

store = FakeStore()
_seed_default(store)
tg = FakeTG()
cmd_stats(tg, store, 9999, [])
text = tg.sends[0]["text"]
check(contains_all(text, [
    "Stats",
    "last 24h",
    "last 7d",
    "Pipelines",
    "daily_digest",
    "Claude CLI",
    "calls",
    "by model:",
    "haiku 128",
    "by caller:",
    "job_enrich",
    "prompt chars",
    "est. cost",
    "surrogate",
    "Errors",
]), "/stats contains expected anchors")

# 24h variant — single window
tg = FakeTG()
cmd_stats(tg, store, 9999, ["24h"])
text = tg.sends[0]["text"]
check("last 24h" in text, "/stats 24h labels last 24h")
check("last 7d" not in text, "/stats 24h omits last 7d column")


# ---------------------------------------------------------------------------
# 12. cmd_alerts — toggle behavior
# ---------------------------------------------------------------------------
print("\n12. cmd_alerts — toggle behavior")

store = FakeStore()
tg = FakeTG()

# Bare /alerts shows current state.
cmd_alerts(tg, store, 9999, [])
check("alerts=on" in tg.sends[-1]["text"], "bare /alerts shows alerts=on default")
check("quiet_alerts=off" in tg.sends[-1]["text"], "bare /alerts shows quiet_alerts=off default")

# /alerts off → toggles, returns 'DISABLED'.
cmd_alerts(tg, store, 9999, ["off"])
check(store.toggles.get("alerts_enabled") == "0", "alerts_enabled set to '0'")
check("DISABLED" in tg.sends[-1]["text"], "render says DISABLED")

# /alerts on → toggles back.
cmd_alerts(tg, store, 9999, ["on"])
check(store.toggles.get("alerts_enabled") == "1", "alerts_enabled set to '1'")
check("ENABLED" in tg.sends[-1]["text"], "render says ENABLED")

# /alerts quiet on → toggles quiet_alerts.
cmd_alerts(tg, store, 9999, ["quiet", "on"])
check(store.toggles.get("quiet_alerts") == "1", "quiet_alerts set to '1'")
check("suppressed" in tg.sends[-1]["text"], "render says suppressed")

# /alerts quiet off → unsets.
cmd_alerts(tg, store, 9999, ["quiet", "off"])
check(store.toggles.get("quiet_alerts") == "0", "quiet_alerts set to '0'")

# Bogus arg → usage hint, no toggle change.
prev = dict(store.toggles)
cmd_alerts(tg, store, 9999, ["nonsense"])
check(store.toggles == prev, "unknown arg leaves toggles untouched")
check("usage:" in tg.sends[-1]["text"], "unknown arg shows usage")


# ---------------------------------------------------------------------------
# 13. cmd_runlog — N clamping + render anchors
# ---------------------------------------------------------------------------
print("\n13. cmd_runlog — clamping and anchors")

store = FakeStore()
_seed_default(store)

# Default → 10
tg = FakeTG()
cmd_runlog(tg, store, 9999, [])
text = tg.sends[0]["text"]
check(contains_all(text, ["Runs", "#2031", "daily_digest", "u=7", "raw=142", "sent=31"]),
      "/runlog default contains expected anchors")

# N=2 → only 2 rows
tg = FakeTG()
cmd_runlog(tg, store, 9999, ["2"])
text = tg.sends[0]["text"]
check("#2029" not in text, "/runlog 2 trims to 2 rows")

# N=999 → clamped to 50, no crash
tg = FakeTG()
cmd_runlog(tg, store, 9999, ["999"])
check(len(tg.sends) == 1, "/runlog 999 clamped without raising")

# N=0 → clamped to 1 → no crash
tg = FakeTG()
cmd_runlog(tg, store, 9999, ["0"])
check(len(tg.sends) == 1, "/runlog 0 clamped to 1 without raising")

# N='banana' → falls back to default
tg = FakeTG()
cmd_runlog(tg, store, 9999, ["banana"])
check(len(tg.sends) == 1, "/runlog banana falls back to default")

# Empty store → no crash, helpful message.
empty = FakeStore()
tg = FakeTG()
cmd_runlog(tg, empty, 9999, [])
check("no pipeline runs" in tg.sends[0]["text"], "empty store shows friendly message")


# ---------------------------------------------------------------------------
# 14. handle_operator_command — dispatch + ghosting
# ---------------------------------------------------------------------------
print("\n14. handle_operator_command — dispatch and ghosting")

store = FakeStore()
_seed_default(store)
tg = FakeTG()

# Non-operator + recognized command → False (silent ghost).
with env_var("OPERATOR_CHAT_ID", "9999"):
    handled = handle_operator_command(tg, store, 1111, "/health")
check(handled is False, "non-operator + recognized → False (ghost)")
check(len(tg.sends) == 0, "non-operator → no send")

# Operator + recognized → True + send.
with env_var("OPERATOR_CHAT_ID", "9999"):
    handled = handle_operator_command(tg, store, 9999, "/health")
check(handled is True, "operator + /health → True")
check(len(tg.sends) == 1, "operator + /health → one send")

# Unrecognized command → False.
with env_var("OPERATOR_CHAT_ID", "9999"):
    handled = handle_operator_command(tg, store, 9999, "/jobs")
check(handled is False, "unrecognized command → False")

# Plain text → False.
handled = handle_operator_command(tg, store, 9999, "hello there")
check(handled is False, "plain text → False")

# Empty text → False.
handled = handle_operator_command(tg, store, 9999, "")
check(handled is False, "empty text → False")

# /alerts off via dispatcher.
tg = FakeTG()
store2 = FakeStore()
with env_var("OPERATOR_CHAT_ID", "9999"):
    handled = handle_operator_command(tg, store2, 9999, "/alerts off")
check(handled is True, "/alerts off → handled")
check(store2.toggles.get("alerts_enabled") == "0", "/alerts off persisted")

# /stats 7d via dispatcher.
tg = FakeTG()
with env_var("OPERATOR_CHAT_ID", "9999"):
    handled = handle_operator_command(tg, store, 9999, "/stats 7d")
check(handled is True, "/stats 7d → handled")
check("last 7d" in tg.sends[-1]["text"], "/stats 7d argument forwarded")

# Trailing whitespace + @botname suffix on command name should still match.
tg = FakeTG()
with env_var("OPERATOR_CHAT_ID", "9999"):
    handled = handle_operator_command(tg, store, 9999, "/health@MyBot")
check(handled is True, "/health@MyBot → handled (suffix stripped)")

# No env set + recognized command + chat_id matches nothing → False (ghost).
tg = FakeTG()
with env_var("OPERATOR_CHAT_ID", None):
    handled = handle_operator_command(tg, store, 9999, "/health")
check(handled is False, "no env + recognized → False (ghost)")
check(len(tg.sends) == 0, "no env → no send")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if failures:
    print(f"FAIL: {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("PASS: All ops smoke checks passed.")
