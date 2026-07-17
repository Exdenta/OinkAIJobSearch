"""Bot-blocked tombstone: pause the pipeline for unreachable users WITHOUT
deleting their data, and auto-resume when they return.

When a user blocks the bot (or deletes their account), Telegram answers every
send with a 403. Before this feature the continuous searcher kept fetching +
LLM-scoring for them forever — full spend, zero delivery. Now:

  1. `telegram_client._is_unreachable_chat` classifies the 403/400 replies that
     mean "chat is gone" vs transient failures.
  2. `TelegramClient._call` raises `TelegramBlocked` on those; generic
     `ok:false` still raises the plain RuntimeError.
  3. `send_per_job_digest` re-raises `TelegramBlocked` (aborts the digest)
     instead of swallowing it per-card.
  4. DB `mark_blocked` / `is_blocked` / `clear_blocked` tombstone the user; NO
     row is deleted. `onboarded_chat_ids()` drops tombstoned users so a
     restart won't respawn their searcher.
  5. `ContinuousSearcher` skips runs while blocked.
  6. `bot._maybe_resume_blocked` clears the tombstone + reconciles the moment
     any update arrives from that chat (only possible after they unblock).
"""
from __future__ import annotations

import asyncio

import pytest

import db as dbm
import telegram_client as tc
from continuous_searcher import ContinuousSearcher
from dedupe import Job


# ---------- fixtures / helpers ----------

@pytest.fixture
def db(tmp_path):
    return dbm.DB(tmp_path / "jobs.db")


def _onboard(db, chat_id):
    db.upsert_user(chat_id, username=f"u{chat_id}")
    with db._conn() as c:
        c.execute(
            "UPDATE users SET onboarding_completed_at=?, user_profile=? WHERE chat_id=?",
            (1.0, '{"schema_version": 4, "primary_role": "x"}', chat_id),
        )


def _job():
    return Job(
        source="linkedin", external_id="abc", title="Engineer",
        company="Acme", location="Remote", url="https://example.com/j/1",
        posted_at="2026-06-30", snippet="do things", salary="",
    )


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


# ---------- 1. classifier ----------

def test_is_unreachable_chat_classification():
    assert tc._is_unreachable_chat(403, "Forbidden: bot was blocked by the user")
    assert tc._is_unreachable_chat(403, "Forbidden: user is deactivated")
    assert tc._is_unreachable_chat(400, "Bad Request: chat not found")
    # Transient / unrelated failures are NOT unreachable.
    assert not tc._is_unreachable_chat(400, "Bad Request: message text is empty")
    assert not tc._is_unreachable_chat(429, "Too Many Requests")
    assert not tc._is_unreachable_chat(500, "bot was blocked by the user")  # wrong code
    assert not tc._is_unreachable_chat(None, "")


# ---------- 2. _call raises the right exception ----------

def test_call_raises_blocked_on_403(monkeypatch):
    tg = tc.TelegramClient("123:abc")
    monkeypatch.setattr(
        tc.requests, "post",
        lambda *a, **k: _FakeResp(
            {"ok": False, "error_code": 403,
             "description": "Forbidden: bot was blocked by the user"},
            status_code=403,
        ),
    )
    with pytest.raises(tc.TelegramBlocked) as ei:
        tg._call("sendMessage", {"chat_id": 111, "text": "hi"})
    assert ei.value.chat_id == 111


def test_call_raises_generic_on_other_error(monkeypatch):
    tg = tc.TelegramClient("123:abc")
    monkeypatch.setattr(
        tc.requests, "post",
        lambda *a, **k: _FakeResp(
            {"ok": False, "error_code": 400,
             "description": "Bad Request: message text is empty"},
            status_code=400,
        ),
    )
    with pytest.raises(RuntimeError) as ei:
        tg._call("sendMessage", {"chat_id": 111, "text": ""})
    assert not isinstance(ei.value, tc.TelegramBlocked)


# ---------- 3. send_per_job_digest aborts, does not swallow ----------

def test_digest_reraises_blocked_and_skips_on_sent():
    class _BlockingTg:
        def send_message(self, *a, **k):
            raise tc.TelegramBlocked(111, "Forbidden: bot was blocked by the user")

    sent_ids = []
    with pytest.raises(tc.TelegramBlocked):
        tc.send_per_job_digest(
            _BlockingTg(), 111, [_job()], {},
            on_sent=lambda mid, j: sent_ids.append(j.job_id),
            enrichments={}, pre_filtered=True,
        )
    assert sent_ids == []  # nothing confirmed sent → jobs stay queued for retry


# ---------- 4. DB tombstone round-trip, no data loss ----------

def test_db_mark_clear_is_blocked(db):
    _onboard(db, 111)
    assert db.is_blocked(111) is False
    db.mark_blocked(111)
    assert db.is_blocked(111) is True
    # Data still present — we paused, we did not delete.
    with db._conn() as c:
        row = c.execute("SELECT user_profile FROM users WHERE chat_id=?", (111,)).fetchone()
    assert row is not None and row["user_profile"]  # profile retained
    # clear returns True only on a real recovery.
    assert db.clear_blocked(111) is True
    assert db.is_blocked(111) is False
    assert db.clear_blocked(111) is False  # already active → no-op


def test_onboarded_excludes_blocked(db):
    for cid in (111, 222, 333):
        _onboard(db, cid)
    assert set(db.onboarded_chat_ids()) == {111, 222, 333}
    db.mark_blocked(222)
    assert set(db.onboarded_chat_ids()) == {111, 333}
    db.clear_blocked(222)
    assert 222 in db.onboarded_chat_ids()


def test_users_for_search_excludes_disabled_and_blocked(db):
    for cid in (111, 222, 333):
        _onboard(db, cid)
        db.set_resume(cid, f"/tmp/{cid}.pdf", "resume text")
    db.set_auto_search_enabled(222, False)
    db.mark_blocked(333)

    assert [row["chat_id"] for row in db.users_for_search()] == [111]


def test_users_for_search_includes_cvless_onboarded_users(db):
    """A profile is enough — the wizard's "Skip for now" CV path must not
    exile a user from the digest run (it used to, while the continuous
    searcher still ran them: full spend, zero delivery)."""
    _onboard(db, 111)                                   # profile, no CV
    _onboard(db, 222)
    db.set_resume(222, "/tmp/222.pdf", "resume text")   # profile + CV

    assert {row["chat_id"] for row in db.users_for_search()} == {111, 222}

    # Still excluded: never onboarded (no profile, no CV).
    db.upsert_user(444, username="u444")
    assert 444 not in {row["chat_id"] for row in db.users_for_search()}


# ---------- 5. searcher skips while blocked ----------

def _run_one_iteration(searcher, monkeypatch):
    """Drive `run_forever` for exactly one iteration by cancelling at the
    first sleep, then swallow the CancelledError it re-raises."""
    async def _fake_sleep(_delay, *a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    async def _driver():
        try:
            await searcher.run_forever()
        except asyncio.CancelledError:
            pass

    asyncio.run(_driver())


class _FakeDB:
    def __init__(self, blocked):
        self._blocked = blocked

    def get_auto_search_enabled(self, chat_id):
        return True

    def is_blocked(self, chat_id):
        return self._blocked


def test_searcher_skips_when_blocked(monkeypatch):
    ran = []
    searcher = ContinuousSearcher(
        db=_FakeDB(blocked=True), chat_id=111, interval_seconds=1,
        search_run_callable=lambda **k: ran.append(k), min_sleep_seconds=0,
    )
    _run_one_iteration(searcher, monkeypatch)
    assert ran == []  # blocked → search never invoked


def test_searcher_runs_when_not_blocked(monkeypatch):
    ran = []
    searcher = ContinuousSearcher(
        db=_FakeDB(blocked=False), chat_id=111, interval_seconds=1,
        search_run_callable=lambda **k: ran.append(k), min_sleep_seconds=0,
    )
    _run_one_iteration(searcher, monkeypatch)
    assert len(ran) == 1  # gate lets an unblocked user through


# ---------- 6. bot auto-resume on return ----------

def test_maybe_resume_blocked_clears_and_reconciles(db, monkeypatch):
    import bot
    _onboard(db, 111)
    db.mark_blocked(111)
    calls = {"reconcile": 0}
    monkeypatch.setattr(bot, "_continuous_mode_enabled", lambda: True)
    monkeypatch.setattr(bot, "_reconcile_continuous_once",
                        lambda _db: calls.__setitem__("reconcile", calls["reconcile"] + 1) or [])

    bot._maybe_resume_blocked(db, 111)
    assert db.is_blocked(111) is False
    assert calls["reconcile"] == 1

    # Second call: user already active → no clear, no reconcile.
    bot._maybe_resume_blocked(db, 111)
    assert calls["reconcile"] == 1


def test_update_chat_id_extracts_from_shapes():
    import bot
    assert bot._update_chat_id({"message": {"chat": {"id": 5}}}) == 5
    assert bot._update_chat_id({"edited_message": {"chat": {"id": 6}}}) == 6
    assert bot._update_chat_id(
        {"callback_query": {"message": {"chat": {"id": 7}}}}) == 7
    assert bot._update_chat_id({"poll": {}}) is None
