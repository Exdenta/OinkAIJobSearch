"""M2 — closed-loop query tuning: telemetry + reward + optimiser tests.

Covers all four task components against TEMP DBs only (never state/jobs.db)
and with a MOCKED LLM (never the real `claude -p`):

  1. Per-query telemetry write + read (`record_query_run`).
  2. Reward aggregation incl. time decay (`query_yield_window`).
  3. Optimiser logic with a mocked LLM: prune a dead query, keep a live one,
     add an explore variant, and the COLD-START case (0 sent but some
     scored/matched still tunes).
  4. Migration idempotency + backward-compat (re-run migrate, pre-existing
     rows untouched, ADD-COLUMN safety on an old-shaped table).
"""
from __future__ import annotations

import json
import time

import pytest

import db as dbm
from telemetry.store import MonitorStore
from telemetry.schema import migrate as migrate_schema
import query_optimizer as qo


# ---------- fixtures ----------

@pytest.fixture
def store(tmp_path):
    """A MonitorStore backed by a throwaway temp DB (NEVER state/jobs.db)."""
    db = dbm.DB(tmp_path / "jobs.db")
    return MonitorStore(db), db


def _seed_profile(db, chat_id, queries):
    db.upsert_user(chat_id, username="t")
    profile = {
        "schema_version": 4,
        "primary_role": "Frontend Engineer",
        "stack_primary": "React, TypeScript",
        "search_seeds": {
            "linkedin": {"queries": list(queries),
                         "geos": ["Spain", "European Union"],
                         "f_TPR": "r86400"},
            "web_search": {"seed_phrases": ["react remote eu"],
                           "ats_domains": [], "focus_notes": "remote EU"},
        },
        "min_match_score": 4,
    }
    db.set_user_profile(chat_id, json.dumps(profile))
    return profile


# ---------- 1. per-query telemetry write + read ----------

def test_record_query_run_write_and_read(store):
    s, db = store
    now = time.time()
    rid = s.record_query_run(
        chat_id=111, source_key="linkedin", query="React Developer",
        pipeline_run_id=7, fetched=10, scored=8, matched_ge4=3,
        queued=2, sent=1, started_at=now - 5, finished_at=now,
    )
    assert rid > 0
    rows = s.query_yield_window(111, since_ts=now - 86400, half_life_s=0, now=now)
    assert len(rows) == 1
    r = rows[0]
    # Query is normalised (lowercased, whitespace-collapsed) for roll-up.
    assert r["query"] == "react developer"
    assert r["query_raw"] == "React Developer"
    assert r["raw_fetched"] == 10
    assert r["raw_scored"] == 8
    assert r["raw_matched_ge4"] == 3
    assert r["raw_sent"] == 1
    assert r["runs"] == 1


def test_query_runs_roll_up_across_runs(store):
    s, db = store
    now = time.time()
    # Same query phrased two ways → ONE bucket.
    s.record_query_run(chat_id=1, source_key="linkedin", query="react  dev",
                       fetched=5, scored=4, matched_ge4=1, sent=0,
                       started_at=now, finished_at=now)
    s.record_query_run(chat_id=1, source_key="linkedin", query="React Dev",
                       fetched=3, scored=2, matched_ge4=2, sent=1,
                       started_at=now, finished_at=now)
    rows = s.query_yield_window(1, since_ts=now - 86400, half_life_s=0, now=now)
    assert len(rows) == 1
    r = rows[0]
    assert r["runs"] == 2
    assert r["raw_scored"] == 6
    assert r["raw_matched_ge4"] == 3
    assert r["raw_sent"] == 1


# ---------- 2. reward aggregation incl. decay ----------

def test_query_yield_window_time_decay(store):
    s, db = store
    now = time.time()
    hl = 7 * 86400.0
    # Old run (one half-life ago) and a fresh run, identical funnel.
    s.record_query_run(chat_id=2, source_key="linkedin", query="old query",
                       matched_ge4=10, started_at=now - hl, finished_at=now - hl)
    s.record_query_run(chat_id=2, source_key="linkedin", query="fresh query",
                       matched_ge4=10, started_at=now, finished_at=now)
    rows = s.query_yield_window(2, since_ts=now - 30 * 86400,
                                half_life_s=hl, now=now)
    by_q = {r["query"]: r for r in rows}
    # Fresh weight ~1.0, old weight ~0.5 → fresh ranks first and has ~2x
    # the decayed matched_ge4.
    assert rows[0]["query"] == "fresh query"
    assert by_q["fresh query"]["matched_ge4"] == pytest.approx(10.0, abs=0.01)
    assert by_q["old query"]["matched_ge4"] == pytest.approx(5.0, abs=0.1)
    # Raw integers are NOT decayed.
    assert by_q["old query"]["raw_matched_ge4"] == 10


def test_query_yield_window_respects_since_ts(store):
    s, db = store
    now = time.time()
    s.record_query_run(chat_id=3, source_key="linkedin", query="ancient",
                       matched_ge4=1, started_at=now - 100 * 86400,
                       finished_at=now - 100 * 86400)
    s.record_query_run(chat_id=3, source_key="linkedin", query="recent",
                       matched_ge4=1, started_at=now, finished_at=now)
    rows = s.query_yield_window(3, since_ts=now - 7 * 86400, half_life_s=0, now=now)
    assert {r["query"] for r in rows} == {"recent"}


# ---------- 3. optimiser logic with a MOCKED LLM ----------

def _mock_llm(payload: dict):
    """Return a `_run_p` stub yielding `payload` as the CLI's assistant text.

    `extract_assistant_text` accepts a JSON envelope with a `result` field
    OR raw text; we wrap in the envelope shape the live CLI returns.
    """
    envelope = json.dumps({"result": json.dumps(payload), "type": "result"})

    def _run_p(prompt, **kwargs):
        _run_p.last_prompt = prompt
        return envelope
    _run_p.last_prompt = None
    return _run_p


def _filters(**over):
    f = {
        "query_optimizer_enabled": True,
        "query_optimizer_min_interval_s": 0,   # disable cadence gate in tests
        "query_optimizer_window_s": 14 * 86400,
        "query_optimizer_half_life_s": 7 * 86400,
        "query_optimizer_min_runs_before_prune": 3,
        "query_optimizer_explore_quota": 0.3,
        "query_optimizer_timeout_s": 60,
    }
    f.update(over)
    return f


def test_optimizer_prunes_dead_keeps_live_adds_explore(store):
    s, db = store
    chat_id = 42
    _seed_profile(db, chat_id, ["react developer", "php wordpress"])
    now = time.time()
    # "react developer": live (matches). "php wordpress": dead (3 runs, 0 match).
    for _ in range(3):
        s.record_query_run(chat_id=chat_id, source_key="linkedin",
                           query="react developer", fetched=10, scored=8,
                           matched_ge4=4, sent=2, started_at=now, finished_at=now)
        s.record_query_run(chat_id=chat_id, source_key="linkedin",
                           query="php wordpress", fetched=6, scored=5,
                           matched_ge4=0, sent=0, started_at=now, finished_at=now)

    # LLM keeps the live arm, drops the dead one, adds an explore variant.
    payload = {
        "linkedin_queries": ["react developer", "frontend engineer react"],
        "web_search_seed_phrases": ["react remote eu", "desarrollador frontend"],
        "kept": ["react developer"],
        "pruned": ["php wordpress"],
        "added": ["frontend engineer react"],
        "rationale": "drop dead php arm, explore adjacent frontend title",
    }
    run_p = _mock_llm(payload)
    res = qo.optimize_queries(db, s, chat_id, _filters(), now=now, _run_p=run_p)

    assert res.status == "ok", res.reason
    assert "react developer" in res.kept
    assert "php wordpress" in res.pruned
    assert "frontend engineer react" in res.added
    # The prompt must have flagged the dead arm as a prune candidate AND
    # carried the cold-start funnel signal.
    assert "PRUNE-CANDIDATE" in run_p.last_prompt
    assert "matched>=floor" in run_p.last_prompt

    # Persisted profile: queries replaced, OTHER fields untouched.
    merged = json.loads(db.get_user_profile(chat_id))
    li = merged["search_seeds"]["linkedin"]
    assert li["queries"] == ["react developer", "frontend engineer react"]
    assert li["geos"] == ["Spain", "European Union"]      # carried through
    assert li["f_TPR"] == "r86400"
    assert merged["primary_role"] == "Frontend Engineer"  # not clobbered
    assert merged["min_match_score"] == 4
    assert merged["search_seeds"]["web_search"]["seed_phrases"] == [
        "react remote eu", "desarrollador frontend",
    ]


def test_optimizer_cold_start_zero_sends_still_tunes(store):
    """COLD-START: a user with 0 sends but some matched_ge4 must still be
    optimised from the scored/matched funnel signal."""
    s, db = store
    chat_id = 99
    _seed_profile(db, chat_id, ["qualitative research migration", "react dev"])
    now = time.time()
    # Researcher arm: NEVER sent (quiet buffer) but DOES match → must be kept.
    for _ in range(3):
        s.record_query_run(chat_id=chat_id, source_key="linkedin",
                           query="qualitative research migration",
                           fetched=8, scored=6, matched_ge4=2, sent=0,
                           started_at=now, finished_at=now)
        # Off-domain react arm: scored but zero matches → dead.
        s.record_query_run(chat_id=chat_id, source_key="linkedin",
                           query="react dev", fetched=12, scored=10,
                           matched_ge4=0, sent=0, started_at=now, finished_at=now)

    rows = s.query_yield_window(chat_id, since_ts=now - 14 * 86400,
                                half_life_s=7 * 86400, now=now)
    # The researcher arm outranks react despite BOTH having 0 sent — proving
    # the reward uses matched_ge4, not sent.
    assert rows[0]["query"] == "qualitative research migration"
    assert rows[0]["raw_sent"] == 0

    payload = {
        "linkedin_queries": ["qualitative research migration",
                             "investigacion cualitativa migracion"],
        "web_search_seed_phrases": ["qualitative researcher migration NGO"],
        "kept": ["qualitative research migration"],
        "pruned": ["react dev"],
        "added": ["investigacion cualitativa migracion"],
        "rationale": "keep matching research arm, translate to Spanish, drop dead react",
    }
    run_p = _mock_llm(payload)
    res = qo.optimize_queries(db, s, chat_id, _filters(), now=now, _run_p=run_p)
    assert res.status == "ok", res.reason
    assert "qualitative research migration" in res.kept   # kept on match signal
    assert "react dev" in res.pruned
    # Spanish-language explore variant landed (LLM-translated, not a Python table).
    assert "investigacion cualitativa migracion" in res.added
    merged = json.loads(db.get_user_profile(chat_id))
    assert "investigacion cualitativa migracion" in \
        merged["search_seeds"]["linkedin"]["queries"]


def test_optimizer_disabled_by_flag(store):
    s, db = store
    _seed_profile(db, 5, ["react developer"])
    res = qo.optimize_queries(db, s, 5, _filters(query_optimizer_enabled=False))
    assert res.status == "disabled"


def test_optimizer_cadence_gate(store):
    s, db = store
    chat_id = 6
    _seed_profile(db, chat_id, ["react developer"])
    now = time.time()
    s.record_query_run(chat_id=chat_id, source_key="linkedin",
                       query="react developer", matched_ge4=1,
                       started_at=now, finished_at=now)
    # Stamp a recent marker → cadence gate must skip.
    s.set_toggle(qo._marker_key(chat_id), str(now - 100))
    res = qo.optimize_queries(
        db, s, chat_id,
        _filters(query_optimizer_min_interval_s=86400), now=now,
        _run_p=_mock_llm({"linkedin_queries": ["x"]}),
    )
    assert res.status == "cadence_skip"


def test_optimizer_refuses_to_wipe_on_empty_emit(store):
    s, db = store
    chat_id = 7
    _seed_profile(db, chat_id, ["react developer"])
    now = time.time()
    s.record_query_run(chat_id=chat_id, source_key="linkedin",
                       query="react developer", matched_ge4=1,
                       started_at=now, finished_at=now)
    run_p = _mock_llm({"linkedin_queries": [], "pruned": ["react developer"]})
    res = qo.optimize_queries(db, s, chat_id, _filters(), now=now, _run_p=run_p)
    assert res.status == "no_change"
    # Original query survives.
    merged = json.loads(db.get_user_profile(chat_id))
    assert merged["search_seeds"]["linkedin"]["queries"] == ["react developer"]


def test_optimizer_no_reward_data(store):
    s, db = store
    _seed_profile(db, 8, ["react developer"])
    res = qo.optimize_queries(db, s, 8, _filters(),
                              _run_p=_mock_llm({"linkedin_queries": ["x"]}))
    assert res.status == "no_reward"


def test_optimizer_handles_paired_legacy_shape(store):
    """PAIRED legacy linkedin shape ({q,geo,f_TPR}) is normalised, not lost."""
    s, db = store
    chat_id = 21
    db.upsert_user(chat_id, username="t")
    profile = {
        "schema_version": 4,
        "primary_role": "Frontend Engineer",
        "search_seeds": {
            "linkedin": {"queries": [
                {"q": "react developer", "geo": "Spain", "f_TPR": "r86400"},
            ]},
        },
    }
    db.set_user_profile(chat_id, json.dumps(profile))
    now = time.time()
    s.record_query_run(chat_id=chat_id, source_key="linkedin",
                       query="react developer", matched_ge4=3,
                       started_at=now, finished_at=now)
    payload = {
        "linkedin_queries": ["react developer", "vue developer"],
        "kept": ["react developer"], "added": ["vue developer"], "pruned": [],
    }
    res = qo.optimize_queries(db, s, chat_id, _filters(), now=now,
                              _run_p=_mock_llm(payload))
    assert res.status == "ok", res.reason
    merged = json.loads(db.get_user_profile(chat_id))
    li = merged["search_seeds"]["linkedin"]
    # Normalised to SEPARATED shape; geo from the paired entry carried over.
    assert li["queries"] == ["react developer", "vue developer"]
    assert li["geos"] == ["Spain"]
    assert li["f_TPR"] == "r86400"


# ---------- 4. migration idempotency + backward-compat ----------

def test_migration_idempotent(tmp_path):
    db = dbm.DB(tmp_path / "jobs.db")
    # Re-run the telemetry migration repeatedly — must not raise.
    with db._conn() as c:
        migrate_schema(c)
        migrate_schema(c)
        cols = {r[1] for r in c.execute("PRAGMA table_info(query_runs)")}
    assert {"pipeline_run_id", "chat_id", "source_key", "query", "query_raw",
            "fetched", "scored", "matched_ge4", "queued", "sent",
            "started_at", "finished_at"} <= cols


def test_migration_backward_compat_preserves_rows(tmp_path):
    """A pre-existing DB with claude_calls rows is untouched by re-migrating;
    the new query_runs table is additive."""
    db = dbm.DB(tmp_path / "jobs.db")
    s = MonitorStore(db)
    # Write a claude_calls row + a query_runs row, then re-run migrate and
    # confirm both survive.
    now = time.time()
    s.record_claude_call("caller", 10, 20, 5, "ok",
                         started_at=now, finished_at=now)
    s.record_query_run(chat_id=1, source_key="linkedin", query="q",
                       fetched=1, started_at=now, finished_at=now)
    with db._conn() as c:
        migrate_schema(c)
        cc = c.execute("SELECT COUNT(*) FROM claude_calls").fetchone()[0]
        qr = c.execute("SELECT COUNT(*) FROM query_runs").fetchone()[0]
    assert cc == 1
    assert qr == 1


def test_migration_adds_query_runs_to_old_db(tmp_path):
    """A DB that predates query_runs gets the table on next migrate()."""
    import sqlite3
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    # Simulate an OLD telemetry DB: only the original tables, no query_runs.
    conn.executescript(
        "CREATE TABLE ops_toggles (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at REAL NOT NULL);"
    )
    conn.commit()
    # Sanity: query_runs absent.
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "query_runs" not in have
    migrate_schema(conn)
    conn.commit()
    have2 = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "query_runs" in have2
    conn.close()


# ---------- 5. integration: optimiser fires on BOTH search-loop paths ----------
#
# Round-1 critic concern (structural placement bug): the optimiser was wired
# ONLY on the flush/send path. A ~0-send user (the exact user this feature
# exists for) spends nearly every iteration on the HOLD path (buffer not
# flushed) and so was never tuned, defeating the cold-start purpose. The fix
# adds the call on the hold path too, mirroring the auto-rebuild precedent.

def test_maybe_optimize_queries_invokes_optimizer_when_enabled():
    """`_maybe_optimize_queries` calls the optimiser and reports ok=True."""
    import search_jobs as sj

    calls = []

    class _Res:
        status = "ok"
        kept = ["a"]
        pruned = ["b"]
        added = ["c"]

    def _fake_optimize(db, store, chat_id, filters):
        calls.append(chat_id)
        return _Res()

    out = sj._maybe_optimize_queries(
        db=object(), store=object(), chat_id=99,
        filters={"query_optimizer_enabled": True},
        _optimize=_fake_optimize,
    )
    assert out is True
    assert calls == [99]


def test_maybe_optimize_queries_short_circuits_when_disabled():
    """Flag OFF → optimiser is never imported/called; returns False."""
    import search_jobs as sj

    def _boom(*a, **k):
        raise AssertionError("optimiser must not run when disabled")

    out = sj._maybe_optimize_queries(
        db=object(), store=object(), chat_id=99,
        filters={"query_optimizer_enabled": False},
        _optimize=_boom,
    )
    assert out is False


def test_maybe_optimize_queries_never_raises_on_optimizer_failure():
    """A raising optimiser is swallowed (best-effort) and returns False."""
    import search_jobs as sj

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    out = sj._maybe_optimize_queries(
        db=object(), store=object(), chat_id=99,
        filters={"query_optimizer_enabled": True},
        _optimize=_boom,
    )
    assert out is False


def test_optimizer_is_wired_on_BOTH_hold_and_flush_paths():
    """Regression for the Round-1 critic concern.

    The run loop in `search_jobs.py` has two terminal branches per user:
    a HOLD branch (buffer not flushed) that ends in `continue`, and a
    FLUSH/send branch. Both record the per-query funnel (`_record_query_funnel`)
    and BOTH must then call `_maybe_optimize_queries` — otherwise the
    cold-start (~0-send) user, who lives on the hold branch, is never tuned.

    We assert structurally that `_maybe_optimize_queries` is invoked at least
    TWICE in the source (once per path) and that the hold-branch call sits
    between its funnel-record and its `continue`.
    """
    import inspect
    import re
    import search_jobs as sj

    src = inspect.getsource(sj)

    # Count CALL sites (exclude the def line itself).
    call_sites = len(re.findall(r"_maybe_optimize_queries\(", src))
    def_sites = len(re.findall(r"def _maybe_optimize_queries\(", src))
    assert def_sites == 1, "expected exactly one definition"
    assert call_sites - def_sites >= 2, (
        "expected >=2 call sites (hold path + flush path); "
        f"found {call_sites - def_sites}"
    )

    # The hold branch ends in `continue`. The optimiser call on that branch
    # must appear AFTER a `_record_query_funnel(` and BEFORE the `continue`
    # that terminates the hold branch. We locate the hold-branch funnel
    # record that passes `sent_job_ids=set()` (the hold-path signature) and
    # check an optimiser call precedes the next `continue`.
    hold_anchor = src.find("sent_job_ids=set(),")
    assert hold_anchor != -1, "hold-path funnel record not found"
    tail = src[hold_anchor:]
    opt_pos = tail.find("_maybe_optimize_queries(")
    cont_pos = tail.find("continue")
    assert opt_pos != -1, "optimiser not called on the hold path"
    assert cont_pos != -1, "hold-path `continue` not found"
    assert opt_pos < cont_pos, (
        "optimiser call must precede the hold-path `continue`"
    )
