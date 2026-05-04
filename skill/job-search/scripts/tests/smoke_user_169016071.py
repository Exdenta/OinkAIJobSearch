#!/usr/bin/env python3
"""End-to-end smoke for every changed surface, scoped to user 169016071.

Tests each function this branch touched. No real Claude CLI. No Telegram
HTTP. Reads + writes against the live `state/jobs.db` so telemetry rows
land where the operator can inspect them with sqlite3 / `/runlog` later.

Run from the project root:
    python3 skill/job-search/scripts/tests/smoke_user_169016071.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
import tempfile
import logging
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
PROJECT_ROOT = SCRIPTS.parent.parent.parent

# Live DB.
DB_PATH = PROJECT_ROOT / "state" / "jobs.db"
USER_CHAT_ID = 169016071

logging.basicConfig(
    level="INFO",
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("smoke169")


def section(label: str) -> None:
    print(f"\n=== {label} ===")


def _assert(cond: bool, msg: str) -> None:
    print(("  OK  " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


# ---------------------------------------------------------------------------
# 1. user exists in DB
# ---------------------------------------------------------------------------

def test_user_present() -> None:
    section("1. user 169016071 present in live DB")
    from db import DB
    db = DB(DB_PATH)
    u = db.get_user(USER_CHAT_ID)
    _assert(u is not None, "get_user returns row")
    _assert(bool(u["resume_text"]), "user has resume_text")
    _assert(bool(u["user_profile"]), "user has user_profile")


# ---------------------------------------------------------------------------
# 2. WAL pragmas active on every connection (Task 4)
# ---------------------------------------------------------------------------

def test_wal_pragmas() -> None:
    section("2. WAL + FK + busy_timeout pragmas active")
    from db import DB
    db = DB(DB_PATH)
    with db._conn() as c:
        jm = c.execute("PRAGMA journal_mode").fetchone()[0]
        sync = c.execute("PRAGMA synchronous").fetchone()[0]
        fk = c.execute("PRAGMA foreign_keys").fetchone()[0]
        bt = c.execute("PRAGMA busy_timeout").fetchone()[0]
    _assert(jm.lower() == "wal", f"journal_mode=wal (got {jm!r})")
    _assert(sync == 1, f"synchronous=NORMAL=1 (got {sync})")
    _assert(fk == 1, f"foreign_keys=ON=1 (got {fk})")
    _assert(bt == 30000, f"busy_timeout=30000 (got {bt})")


# ---------------------------------------------------------------------------
# 3. defaults.DEFAULTS (Task 1: filters.yaml replacement)
# ---------------------------------------------------------------------------

def test_defaults_replaces_yaml() -> None:
    section("3. defaults.DEFAULTS replaces filters.yaml")
    from defaults import DEFAULTS
    _assert(isinstance(DEFAULTS, dict), "DEFAULTS is dict")
    _assert("sources" in DEFAULTS and "linkedin" in DEFAULTS["sources"],
            "DEFAULTS.sources.linkedin present")
    _assert("ai_min_match_score" in DEFAULTS, "ai_min_match_score present")
    # Legacy YAML fields must NOT be there.
    _assert("keywords" not in DEFAULTS, "no legacy 'keywords' key")
    _assert("title_must_match" not in DEFAULTS, "no legacy 'title_must_match' key")
    # filters.yaml must be deleted.
    yaml_path = PROJECT_ROOT / "config" / "filters.yaml"
    _assert(not yaml_path.exists(), f"{yaml_path} deleted")


# ---------------------------------------------------------------------------
# 4. resume_tailor opaque-data wrapping (Task 2)
# ---------------------------------------------------------------------------

def test_resume_tailor_prompt_template() -> None:
    section("4. resume_tailor uses opaque-data wrapping")
    template_path = SCRIPTS / "prompts" / "resume_tailor.txt"
    _assert(template_path.exists(), f"prompt file at {template_path}")
    body = template_path.read_text()
    _assert("<job_posting>" in body, "<job_posting> opaque block present")
    _assert("</resume_text>" in body, "</resume_text> opaque block present")
    _assert("OPAQUE DATA" in body, "instruction-ignore preamble present")
    # The Python module must reference the file (not inline _AI_PROMPT).
    rt_src = (SCRIPTS / "resume_tailor.py").read_text()
    _assert("prompts/resume_tailor.txt" in rt_src
            or "_PROMPT_PATH" in rt_src,
            "resume_tailor.py loads prompt from file")


# ---------------------------------------------------------------------------
# 5. telemetry MonitorStore lifecycle (Task 3 slice A)
# ---------------------------------------------------------------------------

def test_monitor_store_lifecycle() -> int:
    section("5. MonitorStore start/finish + source_run + counters")
    from db import DB
    from telemetry import MonitorStore
    db = DB(DB_PATH)
    store = MonitorStore(db)
    rid = store.start_pipeline_run("daily_digest", triggered_by=USER_CHAT_ID)
    _assert(isinstance(rid, int) and rid > 0, f"pipeline_run id={rid}")
    store.record_source_run(
        rid, "smoke_test_source", "ok", 7, 1234,
        user_chat_id=USER_CHAT_ID,
        started_at=time.time(),
        finished_at=time.time() + 1.234,
    )
    store.finish_pipeline_run(
        rid, status="ok", exit_code=0, users_total=1,
        jobs_raw=7, jobs_sent=3, error_count=0,
        extra={"smoke": True, "user": USER_CHAT_ID},
    )
    rows = store.recent_pipeline_runs(1)
    _assert(rows and rows[0]["id"] == rid, "recent_pipeline_runs returns the row")
    _assert(rows[0]["status"] == "ok", f"status=ok (got {rows[0]['status']!r})")
    last_per = {r["source_key"]: r for r in store.last_source_run_per_source()}
    _assert("smoke_test_source" in last_per, "smoke source recorded")
    _assert(last_per["smoke_test_source"]["raw_count"] == 7, "raw_count=7 persisted")
    return rid


# ---------------------------------------------------------------------------
# 6. instrumentation.error_capture rate-limit (Task 3 slice B)
# ---------------------------------------------------------------------------

def test_error_capture_rate_limit() -> None:
    section("6. error_capture fingerprints + rate-limits")
    from db import DB
    from telemetry import MonitorStore
    from instrumentation import error_capture, AlertEnvelope

    db = DB(DB_PATH)
    store = MonitorStore(db)
    sink_calls: list[AlertEnvelope] = []

    def sink(env: AlertEnvelope) -> None:
        sink_calls.append(env)

    # Helper so the raise comes from the SAME source file:line on both calls.
    # Fingerprint is sha1(error_class + last-frame "file:line" + msg[:80]) —
    # if the raise is in different lines, the fp differs and rate-limit
    # wouldn't fire.
    msg = f"smoke-rl-{USER_CHAT_ID}"

    def _raise_same():
        raise RuntimeError(msg)

    # First — recorded + sink fires.
    try:
        with error_capture(store, where="smoke169:test", chat_id=USER_CHAT_ID, alert_sink=sink):
            _raise_same()
    except RuntimeError:
        pass
    _assert(len(sink_calls) == 1, f"sink invoked once (got {len(sink_calls)})")
    _assert(sink_calls[0].chat_id == USER_CHAT_ID, "envelope.chat_id matches")

    # Second — same file:line + same msg = same fingerprint = rate-limited.
    sink_calls.clear()
    try:
        with error_capture(store, where="smoke169:test", chat_id=USER_CHAT_ID, alert_sink=sink):
            _raise_same()
    except RuntimeError:
        pass
    _assert(len(sink_calls) == 0, "rate-limit suppressed second alert")


# ---------------------------------------------------------------------------
# 7. wrapped_run_p (Task 6 cost capture) — stubbed claude_cli, real DB
# ---------------------------------------------------------------------------

def test_wrapped_run_p_records() -> None:
    section("7. wrapped_run_p writes claude_calls row")
    # Patch claude_cli.run_p before importing wrappers (which has its own
    # bound reference). To override, re-bind on the wrappers module too.
    import claude_cli
    import instrumentation.wrappers as _wrap
    real = _wrap.run_p

    def fake_run_p(prompt, **kwargs):
        return f"STUB-{prompt[:20]}"
    _wrap.run_p = fake_run_p
    try:
        out = _wrap.wrapped_run_p(
            None,
            "smoke169:fit_analyzer",
            "what is my fit for this role?",
            chat_id=USER_CHAT_ID,
            model="haiku",
        )
    finally:
        _wrap.run_p = real
    _assert(out is not None and out.startswith("STUB"), f"stub returned ({out!r})")

    # Confirm the row landed.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT * FROM claude_calls WHERE caller=? ORDER BY id DESC LIMIT 1",
        ("smoke169:fit_analyzer",),
    ).fetchone()
    conn.close()
    _assert(r is not None, "claude_calls row exists")
    _assert(r["chat_id"] == USER_CHAT_ID, f"chat_id={USER_CHAT_ID} stamped")
    _assert(r["model"] == "haiku", "model='haiku' stamped")
    _assert(r["status"] == "ok", "status=ok")
    _assert(r["cost_estimate_us"] > 0, f"cost_estimate_us={r['cost_estimate_us']}")


# ---------------------------------------------------------------------------
# 8. ops.is_operator + handle_operator_command ghosting for non-operator
# ---------------------------------------------------------------------------

def test_operator_commands_ghost_for_user() -> None:
    section("8. operator commands ghost for non-operator chat 169016071")
    # No OPERATOR_CHAT_ID in env → is_operator returns False for ANY chat.
    os.environ.pop("OPERATOR_CHAT_ID", None)
    from ops.operator import is_operator
    from ops.commands import handle_operator_command
    _assert(not is_operator(USER_CHAT_ID), "is_operator(user)=False with empty env")

    sends: list[tuple] = []

    class FakeTG:
        def send_message(self, chat_id, text, **kwargs):
            sends.append(("send", chat_id, text))
            return 1
        def send_plain(self, chat_id, text, **kwargs):
            sends.append(("plain", chat_id, text))
            return 1

    from db import DB
    from telemetry import MonitorStore
    db = DB(DB_PATH)
    store = MonitorStore(db)
    handled = handle_operator_command(FakeTG(), store, USER_CHAT_ID, "/health")
    _assert(handled is False, "/health returns False for non-operator (silent ghost)")
    _assert(sends == [], "no Telegram sends fired")


# ---------------------------------------------------------------------------
# 9. /marketresearch global cap + per-user cooldown (Task 7)
# ---------------------------------------------------------------------------

def test_research_throttle() -> None:
    section("9. /marketresearch cap + cooldown gates fire")
    import bot
    # Default cooldown = 3600s; mark the user finished now → cooldown should be > 0.
    bot._mark_research_finished(USER_CHAT_ID)
    remaining = bot._research_cooldown_remaining(USER_CHAT_ID)
    _assert(remaining > 0, f"cooldown remaining > 0 (got {remaining}s)")
    _assert(remaining <= bot._RESEARCH_COOLDOWN_SECONDS,
            f"cooldown ≤ MAX ({bot._RESEARCH_COOLDOWN_SECONDS})")

    # Global semaphore — acquire to capacity, next acquire fails.
    cap = bot._MAX_CONCURRENT_RESEARCH
    held = []
    for _ in range(cap):
        ok = bot._RESEARCH_GLOBAL_SEM.acquire(blocking=False)
        if ok:
            held.append(True)
    _assert(len(held) == cap, f"acquired {cap} slots")
    _assert(bot._RESEARCH_GLOBAL_SEM.acquire(blocking=False) is False,
            "next acquire fails (cap reached)")
    # Restore.
    for _ in held:
        try:
            bot._RESEARCH_GLOBAL_SEM.release()
        except ValueError:
            pass
    # Clear cooldown so we don't strand the real user.
    with bot._LAST_RESEARCH_LOCK:
        bot._LAST_RESEARCH_FINISHED.pop(USER_CHAT_ID, None)
    _assert(bot._research_cooldown_remaining(USER_CHAT_ID) == 0,
            "cooldown cleared after pop")


# ---------------------------------------------------------------------------
# 10. live search_jobs --dry-run --chat-id=169016071 path
# ---------------------------------------------------------------------------

def test_search_jobs_dry_run_for_user() -> None:
    section("10. search_jobs.run(dry_run=True, only_chat=169016071)")
    # Stub fetch_all so we don't hit the network — we only verify the wiring.
    import search_jobs
    fake_jobs: list = []
    def fake_fetch_all(filters, *, store=None, pipeline_run_id=None):
        return fake_jobs, []
    real_fetch = search_jobs.fetch_all
    search_jobs.fetch_all = fake_fetch_all
    try:
        rc = search_jobs.run(dry_run=True, only_chat=USER_CHAT_ID)
    finally:
        search_jobs.fetch_all = real_fetch
    _assert(rc == 0, f"dry-run returns 0 (got {rc})")


# ---------------------------------------------------------------------------
# 11. live search_jobs telemetered path WITHOUT Telegram traffic
# ---------------------------------------------------------------------------

def test_search_jobs_telemetered_for_user() -> None:
    section("11. live search_jobs telemetered run for chat 169016071")
    # Patch fetch_all + TelegramClient so no network or chat traffic; but
    # the pipeline_run + per-user source_run wrap MUST still write rows.
    import search_jobs
    fake_jobs: list = []
    def fake_fetch_all(filters, *, store=None, pipeline_run_id=None):
        return fake_jobs, []

    sends: list = []

    class FakeTG:
        def __init__(self, token=None): pass
        def send_message(self, *a, **k): sends.append(("send", a, k)); return 1
        def send_plain(self, *a, **k):   sends.append(("plain", a, k)); return 1

    real_fetch = search_jobs.fetch_all
    real_tg = search_jobs.TelegramClient
    os.environ["TELEGRAM_BOT_TOKEN"] = "fake"
    search_jobs.fetch_all = fake_fetch_all
    search_jobs.TelegramClient = FakeTG
    try:
        rc = search_jobs.run(dry_run=False, only_chat=USER_CHAT_ID)
    finally:
        search_jobs.fetch_all = real_fetch
        search_jobs.TelegramClient = real_tg
    _assert(rc == 0, f"live run returns 0 (got {rc})")

    # Verify pipeline_run row landed for THIS run.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT * FROM pipeline_runs WHERE kind='daily_digest' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    _assert(r is not None, "pipeline_runs has a row")
    _assert(r["status"] == "ok", f"status=ok (got {r['status']})")
    _assert(r["users_total"] >= 1, f"users_total>=1 (got {r['users_total']})")


# ---------------------------------------------------------------------------
# Final dump
# ---------------------------------------------------------------------------

def dump_recent_telemetry() -> None:
    section("LOG  recent telemetry rows (last 5 each)")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    for table in ("pipeline_runs", "source_runs", "claude_calls", "error_events"):
        try:
            rows = conn.execute(
                f"SELECT * FROM {table} ORDER BY id DESC LIMIT 5"
            ).fetchall()
        except sqlite3.OperationalError as e:
            print(f"  {table}: {e}")
            continue
        print(f"  {table}: {len(rows)} rows")
        for r in rows:
            d = dict(r)
            # trim noisy fields
            for k in ("stack_tail", "extra_json", "error_head", "message_head"):
                if k in d and isinstance(d[k], str) and len(d[k]) > 80:
                    d[k] = d[k][:77] + "..."
            print(f"    {d}")
    conn.close()


def main() -> int:
    test_user_present()
    test_wal_pragmas()
    test_defaults_replaces_yaml()
    test_resume_tailor_prompt_template()
    test_monitor_store_lifecycle()
    test_error_capture_rate_limit()
    test_wrapped_run_p_records()
    test_operator_commands_ghost_for_user()
    test_research_throttle()
    test_search_jobs_dry_run_for_user()
    test_search_jobs_telemetered_for_user()
    dump_recent_telemetry()
    print("\nPASS — all smoke checks for chat_id=169016071 green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
