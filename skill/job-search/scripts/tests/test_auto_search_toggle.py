"""Per-user auto-search toggle + auto-enroll scheduler behavior.

Covers the bottom-bar "Auto-search: ON/OFF" feature against TEMP DBs only:

  1. Migration adds `auto_search_enabled` (default 1) idempotently; existing
     onboarded users default to enabled.
  2. get/set_auto_search_enabled round-trips; unknown / NULL -> True.
  3. onboarded_chat_ids() drops users with the flag off.
  4. Resolver auto-enrolls onboarded users, EXCLUDES the operator, and a
     toggled-off user is not enrolled.
  5. reply_keyboard() renders the correct ON/OFF label per user.
"""
from __future__ import annotations

import os

import pytest

import db as dbm


def _onboard(db, chat_id):
    db.upsert_user(chat_id, username=f"u{chat_id}")
    db.set_user_profile(chat_id, '{"schema_version": 4, "primary_role": "x"}') \
        if hasattr(db, "set_user_profile") else None
    # Mark onboarding complete + non-empty profile the way the schema expects.
    with db._conn() as c:
        c.execute(
            "UPDATE users SET onboarding_completed_at=?, user_profile=? WHERE chat_id=?",
            (1.0, '{"schema_version": 4, "primary_role": "x"}', chat_id),
        )


@pytest.fixture
def db(tmp_path):
    return dbm.DB(tmp_path / "jobs.db")


def test_migration_default_and_roundtrip(db):
    _onboard(db, 111)
    # Default after migration = enabled.
    assert db.get_auto_search_enabled(111) is True
    # Unknown user -> True (never silence someone who never opted out).
    assert db.get_auto_search_enabled(999999) is True
    db.set_auto_search_enabled(111, False)
    assert db.get_auto_search_enabled(111) is False
    db.set_auto_search_enabled(111, True)
    assert db.get_auto_search_enabled(111) is True


def test_migration_idempotent(tmp_path):
    p = tmp_path / "jobs.db"
    dbm.DB(p)            # first migrate
    db2 = dbm.DB(p)      # re-open -> _migrate re-runs, must not error
    _onboard(db2, 222)
    assert db2.get_auto_search_enabled(222) is True


def test_onboarded_excludes_disabled(db):
    for cid in (111, 222, 333):
        _onboard(db, cid)
    assert set(db.onboarded_chat_ids()) == {111, 222, 333}
    db.set_auto_search_enabled(222, False)
    assert set(db.onboarded_chat_ids()) == {111, 333}
    db.set_auto_search_enabled(222, True)
    assert 222 in db.onboarded_chat_ids()


def test_resolver_autoenrolls_and_excludes_operator(db, monkeypatch):
    import bot
    for cid in (111, 222, 169016071):
        _onboard(db, cid)
    monkeypatch.setenv("OPERATOR_CHAT_ID", "169016071")
    monkeypatch.delenv("OINK_CONTINUOUS_CHAT_ID", raising=False)
    ids = set(bot._resolve_continuous_chat_ids(db))
    assert 169016071 not in ids          # operator never auto-searched
    assert {111, 222} <= ids             # everyone else auto-enrolled
    # Opt-out removes a user from enrollment.
    db.set_auto_search_enabled(222, False)
    assert 222 not in set(bot._resolve_continuous_chat_ids(db))


def test_resolver_operator_excluded_even_in_pinned_list(db, monkeypatch):
    import bot
    _onboard(db, 111)
    monkeypatch.setenv("OPERATOR_CHAT_ID", "169016071")
    monkeypatch.setenv("OINK_CONTINUOUS_CHAT_ID", "111,169016071")
    ids = set(bot._resolve_continuous_chat_ids(db))
    assert ids == {111}                  # operator filtered out of the pin too


def test_reply_keyboard_label_offers_the_inverse_action(db):
    """The label names the tap's effect, so it is the inverse of the flag.

    Regression guard: the labels used to state the flag ("Auto-search: ON"),
    which new users read as "tap to turn on" and thereby turned it off.
    """
    import bot
    _onboard(db, 111)
    on = [b["text"] for b in bot.reply_keyboard(db, 111)["keyboard"][2]]
    assert bot.BTN_SETTINGS in on and bot.BTN_AUTO_PAUSE in on
    db.set_auto_search_enabled(111, False)
    off = [b["text"] for b in bot.reply_keyboard(db, 111)["keyboard"][2]]
    assert bot.BTN_AUTO_RESUME in off
    # No-context fallback assumes ON and offers Pause; never crashes.
    assert bot.BTN_AUTO_PAUSE in [b["text"] for b in bot.reply_keyboard()["keyboard"][2]]


def test_legacy_auto_labels_still_dispatch():
    """Users with a cached keyboard tap the old label — it must still toggle."""
    import bot
    for stale in ("🔍  Auto-search: ON", "⏸  Auto-search: OFF"):
        assert stale in bot._AUTO_BUTTONS
