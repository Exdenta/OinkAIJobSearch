#!/usr/bin/env python3
"""Smoke test for the "skip deletes message" callback behavior.

Verifies the four contract points:
  1. Default skip → tg.delete_message called, no edit_reply_markup, DB writes
     "skipped", toast says "✕ Removed".
  2. delete_message returns False (e.g. >48h) → falls back to
     edit_reply_markup, DB still writes "skipped", toast falls back.
  3. SKIP_DELETES_MESSAGE=0 → delete is NEVER attempted, edit-keyboard runs,
     preserves the original UX.
  4. Applied (kind="a") → unchanged; delete_message NOT attempted.

Runs without any Telegram / network IO via fakes.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _assert(cond: bool, msg: str) -> None:
    print(("  OK  " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeTG:
    """Records calls to delete_message / edit_reply_markup / answer_callback.

    `delete_returns` controls whether the simulated delete succeeds.
    """

    def __init__(self, delete_returns: bool = True) -> None:
        self.delete_returns = delete_returns
        self.delete_calls: list[tuple[Any, Any]] = []
        self.edit_calls: list[tuple[Any, Any, Any]] = []
        self.callback_answers: list[tuple[str, str]] = []

    def delete_message(self, chat_id, message_id) -> bool:
        self.delete_calls.append((chat_id, message_id))
        return self.delete_returns

    def edit_reply_markup(self, chat_id, message_id, reply_markup) -> None:
        self.edit_calls.append((chat_id, message_id, reply_markup))

    def answer_callback(self, cb_id, text="", show_alert=False) -> None:
        self.callback_answers.append((cb_id, text))

    # The applied-path code calls _pigs.send_sticker(tg, chat_id, ...) which
    # in turn calls tg.send_sticker. Stub it so the applied-branch test
    # doesn't crash.
    def send_sticker(self, *args, **kwargs) -> None:
        pass


class FakeRow(dict):
    """sqlite3.Row-like: supports both row['key'] and dict() over it."""

    def __getitem__(self, key):
        return super().__getitem__(key)


class FakeDB:
    """Minimal DB stub: get_job + set_application_status + get_user."""

    def __init__(self) -> None:
        self.job = FakeRow(
            source="hn",
            external_id="ext-1",
            title="Senior Engineer",
            company="Acme",
            location="Remote",
            url="https://example.com/job",
            posted_at="2026-04-29",
            snippet="A great role.",
            salary="",
        )
        self.status_writes: list[tuple[int, str, str]] = []

    def get_job(self, job_id: str):
        return self.job

    def set_application_status(self, chat_id: int, job_id: str, status: str) -> None:
        self.status_writes.append((chat_id, job_id, status))

    def get_user(self, chat_id: int):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_bot_fresh():
    """Re-import bot so SKIP_DELETES_MESSAGE env changes take effect.

    bot.py reads the env at callback time (not at import), so a fresh import
    isn't strictly required — but doing it once up front avoids surprises if
    that ever changes.
    """
    if "bot" in sys.modules:
        del sys.modules["bot"]
    import bot
    return bot


def _make_cb(chat_id: int, msg_id: int, kind: str, job_id: str) -> dict:
    return {
        "id": f"cbq-{kind}-{job_id}",
        "data": f"{kind}:{job_id}",
        "message": {
            "chat": {"id": chat_id},
            "message_id": msg_id,
        },
    }


def _patch_error_capture(bot_mod) -> None:
    """The real `error_capture` is a context manager that needs a MonitorStore.
    Replace it with a no-op so we don't have to wire one up in the fakes.
    """
    from contextlib import contextmanager

    @contextmanager
    def _noop_capture(*args, **kwargs):
        yield

    bot_mod.error_capture = _noop_capture
    # Also stub _get_store so it doesn't try to attach a MonitorStore.
    bot_mod._get_store = lambda db: None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def setup() -> None:
    # Point STATE_DIR at a tempdir so any forensic writes don't leak.
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    # Make sure FORENSIC_OFF isn't set so log_step paths are exercised.
    os.environ.pop("FORENSIC_OFF", None)


def test_skip_deletes_default() -> None:
    section("1. skip on fresh message → delete_message called, no edit, toast '✕ Removed'")
    os.environ.pop("SKIP_DELETES_MESSAGE", None)  # default = on
    bot = _import_bot_fresh()
    _patch_error_capture(bot)

    tg = FakeTG(delete_returns=True)
    db = FakeDB()
    cb = _make_cb(chat_id=123, msg_id=456, kind="n", job_id="job-1")

    bot.handle_callback(tg, db, cb)

    _assert(len(db.status_writes) == 1, "DB write happened exactly once")
    _assert(db.status_writes[0] == (123, "job-1", "skipped"),
            f"DB wrote skipped status (got {db.status_writes[0]})")
    _assert(tg.delete_calls == [(123, 456)],
            f"delete_message called with (chat_id, msg_id) (got {tg.delete_calls})")
    _assert(tg.edit_calls == [],
            f"edit_reply_markup NOT called (got {tg.edit_calls})")
    _assert(len(tg.callback_answers) == 1, "answer_callback called once")
    _assert(tg.callback_answers[0][1] == "✕ Removed",
            f"toast text (got {tg.callback_answers[0][1]!r})")


def test_skip_delete_fails_falls_back() -> None:
    section("2. delete_message returns False (>48h) → edit_reply_markup fallback, DB still writes")
    os.environ.pop("SKIP_DELETES_MESSAGE", None)
    bot = _import_bot_fresh()
    _patch_error_capture(bot)

    tg = FakeTG(delete_returns=False)
    db = FakeDB()
    cb = _make_cb(chat_id=123, msg_id=456, kind="n", job_id="job-2")

    bot.handle_callback(tg, db, cb)

    _assert(db.status_writes == [(123, "job-2", "skipped")],
            "DB still wrote skipped status despite delete failing")
    _assert(tg.delete_calls == [(123, 456)],
            "delete_message was attempted")
    _assert(len(tg.edit_calls) == 1,
            f"edit_reply_markup called as fallback (got {tg.edit_calls})")
    _assert(tg.edit_calls[0][0] == 123 and tg.edit_calls[0][1] == 456,
            "edit_reply_markup got correct chat/msg ids")
    # On failure we keep the prior toast text — user gets a non-error message.
    _assert(tg.callback_answers[0][1] != "✕ Removed",
            f"toast falls back to non-removed text (got {tg.callback_answers[0][1]!r})")


def test_skip_delete_disabled_via_env() -> None:
    section("3. SKIP_DELETES_MESSAGE=0 → delete NOT attempted, original edit-keyboard path")
    os.environ["SKIP_DELETES_MESSAGE"] = "0"
    try:
        bot = _import_bot_fresh()
        _patch_error_capture(bot)

        tg = FakeTG(delete_returns=True)
        db = FakeDB()
        cb = _make_cb(chat_id=123, msg_id=456, kind="n", job_id="job-3")

        bot.handle_callback(tg, db, cb)

        _assert(db.status_writes == [(123, "job-3", "skipped")],
                "DB still wrote skipped status")
        _assert(tg.delete_calls == [],
                f"delete_message NOT attempted when env=0 (got {tg.delete_calls})")
        _assert(len(tg.edit_calls) == 1,
                f"edit_reply_markup called (preserves original UX) (got {tg.edit_calls})")
    finally:
        os.environ.pop("SKIP_DELETES_MESSAGE", None)


def test_applied_branch_unchanged() -> None:
    section("4. Applied (a:<job_id>) — delete NOT attempted, edit_reply_markup still used")
    os.environ.pop("SKIP_DELETES_MESSAGE", None)  # default on; should still not affect applied
    bot = _import_bot_fresh()
    _patch_error_capture(bot)

    # The applied branch calls _pigs.send_sticker(tg, chat_id, ...). Make
    # _pigs a no-op so we don't depend on sticker config in the test env.
    class _NoopPigs:
        THUMBS_UP = "noop"

        @staticmethod
        def send_sticker(*args, **kwargs):
            pass

    bot._pigs = _NoopPigs()

    tg = FakeTG(delete_returns=True)
    db = FakeDB()
    cb = _make_cb(chat_id=123, msg_id=456, kind="a", job_id="job-4")

    bot.handle_callback(tg, db, cb)

    _assert(db.status_writes == [(123, "job-4", "applied")],
            "DB wrote applied status")
    _assert(tg.delete_calls == [],
            f"delete_message NOT attempted on applied branch (got {tg.delete_calls})")
    _assert(len(tg.edit_calls) == 1,
            "edit_reply_markup still used on applied branch (unchanged)")
    _assert(tg.callback_answers[0][1] == "Marked as applied ✅",
            f"applied toast unchanged (got {tg.callback_answers[0][1]!r})")


def main() -> int:
    setup()
    test_skip_deletes_default()
    test_skip_delete_fails_falls_back()
    test_skip_delete_disabled_via_env()
    test_applied_branch_unchanged()
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
