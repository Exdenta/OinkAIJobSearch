#!/usr/bin/env python3
"""Smoke test for the 👍/👎 job-card feedback buttons.

Covers the contract points spec'd for the feature:

  1. fb+:<job_id> → db.record_job_feedback(..., 'up'); no prior application
     status → defaults to 'interested'; card re-renders with the 👍 Liked row.
  2. fb+:<job_id> on an already-applied job → feedback still recorded, but
     the 'applied' status is NOT overwritten (no extra status write).
  3. fb-:<job_id> → db.record_job_feedback(..., 'down') + status 'skipped'
     (same dedupe semantics as the legacy `n:`), then the card MORPHS into
     the structured reason picker (fbr:<code>:<job_id> buttons).
  4. A structured fbr:<code>:<job_id> tap → db.set_job_feedback_reason +
     db.append_skip_note with a synthesized "Not a fit — <reason>: <title>
     at <company>" line, then the message is cleaned up (deleted).
  5. fbr:other:<job_id> → falls through to STATE_AWAITING_SKIP_REASON with
     the same payload shape the legacy skip-reason flow uses.
  6. The following free-text reply lands in db.set_job_feedback_reason (in
     addition to the existing skip_feedback.apply_skip_feedback path).
  7. SKIP_FEEDBACK_ENABLED=0 → fb-: reverts to plain skip+delete, no reason
     picker, no follow-up question (kill switch).

All run without any Telegram / network IO via fakes.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
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
    """Records every outbound call so tests can assert on them."""

    def __init__(self, delete_returns: bool = True) -> None:
        self.delete_returns = delete_returns
        self.delete_calls: list[tuple[Any, Any]] = []
        self.edit_reply_markup_calls: list[tuple[Any, Any, Any]] = []
        self.edit_text_calls: list[tuple[Any, Any, str, Any]] = []
        self.callback_answers: list[tuple[str, str]] = []
        self.send_messages: list[tuple[Any, str, dict | None]] = []

    def delete_message(self, chat_id, message_id) -> bool:
        self.delete_calls.append((chat_id, message_id))
        return self.delete_returns

    def edit_reply_markup(self, chat_id, message_id, reply_markup) -> None:
        self.edit_reply_markup_calls.append((chat_id, message_id, reply_markup))

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None,
                          parse_mode=None, disable_preview=True) -> None:
        self.edit_text_calls.append((chat_id, message_id, text, reply_markup))

    def answer_callback(self, cb_id, text="", show_alert=False) -> None:
        self.callback_answers.append((cb_id, text))

    def send_message(self, chat_id, text, reply_markup=None, parse_mode=None,
                     disable_preview=True) -> int:
        self.send_messages.append((chat_id, text, reply_markup))
        return 1

    def send_plain(self, chat_id, text) -> int:
        self.send_messages.append((chat_id, text, None))
        return 1

    def send_sticker(self, *args, **kwargs) -> None:
        pass


class FakeRow(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


class FakeDB:
    """Minimal DB stub covering everything the fb+/fb-/fbr paths touch."""

    def __init__(self) -> None:
        self.job = FakeRow(
            source="hn",
            external_id="ext-1",
            title="Senior ML Engineer",
            company="Acme",
            location="Remote",
            url="https://example.com/job",
            posted_at="2026-04-29",
            snippet="A great role.",
            salary="",
        )
        self.status_writes: list[tuple[int, str, str]] = []
        self._status: dict[tuple[int, str], str] = {}
        self.feedback_writes: list[tuple[int, str, str]] = []
        self.feedback_clears: list[tuple[int, str]] = []
        self.status_clears: list[tuple[int, str]] = []
        self.reason_writes: list[tuple[int, str, str]] = []
        self.skip_notes: list[tuple[int, str]] = []
        self._await: dict[int, str | None] = {}

    # -- jobs --
    def get_job(self, job_id: str):
        return self.job

    # -- applications --
    def set_application_status(self, chat_id: int, job_id: str, status: str) -> None:
        self.status_writes.append((chat_id, job_id, status))
        self._status[(chat_id, job_id)] = status

    def get_application_status(self, chat_id: int, job_id: str) -> str | None:
        return self._status.get((chat_id, job_id))

    # -- job_feedback (the table under test) --
    def record_job_feedback(self, chat_id: int, job_id: str, verdict: str,
                            reason: str | None = None) -> None:
        self.feedback_writes.append((chat_id, job_id, verdict))

    def set_job_feedback_reason(self, chat_id: int, job_id: str, reason: str) -> None:
        self.reason_writes.append((chat_id, job_id, reason))

    def get_job_feedback(self, chat_id: int, job_id: str):
        return None

    def clear_job_feedback(self, chat_id: int, job_id: str) -> None:
        self.feedback_clears.append((chat_id, job_id))

    def get_job_enrichment(self, chat_id: int, job_id: str):
        return None

    def clear_application(self, chat_id: int, job_id: str) -> int:
        existed = self._status.pop((chat_id, job_id), None) is not None
        self.status_clears.append((chat_id, job_id))
        return int(existed)

    # -- skip notes --
    def append_skip_note(self, chat_id: int, text: str) -> None:
        self.skip_notes.append((chat_id, text))

    # -- misc plumbing other code paths touch --
    def get_user(self, chat_id: int):
        return None

    def get_onboarding_state(self, chat_id: int):
        return None

    def clear_blocked(self, chat_id: int) -> bool:
        return False

    def upsert_user(self, *args, **kwargs) -> None:
        pass

    # -- awaiting-state — same encoding contract as the real DB --
    def set_awaiting_state(self, chat_id: int, state, payload_json=None) -> None:
        if state is None:
            self._await[chat_id] = None
            return
        if payload_json is None:
            self._await[chat_id] = state
            return
        import json as _json
        if isinstance(payload_json, (dict, list)):
            blob = _json.dumps(payload_json, ensure_ascii=False)
        else:
            blob = str(payload_json)
        self._await[chat_id] = f"{state}|{blob}"

    def _read_raw(self, chat_id: int):
        return self._await.get(chat_id)

    def get_awaiting_state(self, chat_id: int):
        raw = self._read_raw(chat_id)
        if raw is None:
            return None
        if "|" in raw:
            return raw.split("|", 1)[0]
        return raw

    def get_awaiting_state_payload(self, chat_id: int):
        raw = self._read_raw(chat_id)
        if not raw or "|" not in raw:
            return None
        import json as _json
        _, _, blob = raw.partition("|")
        try:
            return _json.loads(blob)
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_bot_fresh():
    """Re-import bot so SKIP_FEEDBACK_ENABLED env changes take effect."""
    for mod_name in ("bot", "skip_feedback"):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
    import bot
    return bot


def _make_cb(chat_id: int, msg_id: int, kind: str, payload: str) -> dict:
    return {
        "id": f"cbq-{kind}-{payload}",
        "data": f"{kind}:{payload}",
        "message": {
            "chat": {"id": chat_id},
            "message_id": msg_id,
        },
    }


def _make_text_update(chat_id: int, text: str) -> dict:
    return {
        "update_id": 1,
        "message": {
            "chat": {"id": chat_id},
            "from": {"id": chat_id},
            "text": text,
        },
    }


def _patch_error_capture(bot_mod) -> None:
    from contextlib import contextmanager

    @contextmanager
    def _noop_capture(*args, **kwargs):
        yield

    bot_mod.error_capture = _noop_capture
    bot_mod._get_store = lambda db: None


def _install_fake_skip_feedback(returns: dict | None = None) -> dict:
    capture: dict = {"called": False, "args": None}

    def _apply(db, chat_id, payload, text):
        capture["called"] = True
        capture["args"] = (db, chat_id, payload, text)
        return returns or {"summary": "Got it, filtering similar postings."}

    fake_mod = types.ModuleType("skip_feedback")
    fake_mod.apply_skip_feedback = _apply  # type: ignore[attr-defined]
    sys.modules["skip_feedback"] = fake_mod
    return capture


def setup() -> None:
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)
    os.environ.pop("SKIP_DELETES_MESSAGE", None)
    os.environ.pop("SKIP_FEEDBACK_ENABLED", None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fbplus_records_up_and_defaults_interested() -> None:
    section("1. fb+:<job_id> → record_job_feedback('up'); no prior status → 'interested'")
    bot = _import_bot_fresh()
    _patch_error_capture(bot)

    tg = FakeTG()
    db = FakeDB()
    cb = _make_cb(chat_id=123, msg_id=456, kind="fb+", payload="job-1")

    bot.handle_callback(tg, db, cb)

    _assert(db.feedback_writes == [(123, "job-1", "up")],
            f"feedback recorded up (got {db.feedback_writes})")
    _assert(db.status_writes == [(123, "job-1", "interested")],
            f"status defaulted to interested (got {db.status_writes})")
    _assert(tg.callback_answers and "Noted" in tg.callback_answers[0][1],
            f"toast mentions Noted (got {tg.callback_answers})")
    _assert(len(tg.edit_reply_markup_calls) == 1,
            "keyboard cosmetically updated")
    kb = tg.edit_reply_markup_calls[0][2]
    labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
    _assert("👍 Liked" in labels, f"keyboard shows Liked row (got {labels})")


def test_fbplus_does_not_overwrite_applied() -> None:
    section("2. fb+:<job_id> on an already-applied job → status NOT overwritten")
    bot = _import_bot_fresh()
    _patch_error_capture(bot)

    tg = FakeTG()
    db = FakeDB()
    db.set_application_status(123, "job-2", "applied")
    db.status_writes.clear()  # only care about writes caused by THIS callback
    cb = _make_cb(chat_id=123, msg_id=456, kind="fb+", payload="job-2")

    bot.handle_callback(tg, db, cb)

    _assert(db.feedback_writes == [(123, "job-2", "up")],
            f"feedback still recorded (got {db.feedback_writes})")
    _assert(db.status_writes == [],
            f"applied status left untouched (got {db.status_writes})")
    kb = tg.edit_reply_markup_calls[0][2]
    # Locate the status row specifically (not the "View posting ↗" / AI
    # actions rows) — it should stay the single-button applied toggle,
    # unaffected by the feedback="up" argument.
    status_row = next(row for row in kb["inline_keyboard"]
                      if any(b["text"] == "✓ Applied" for b in row))
    _assert([b["text"] for b in status_row] == ["✓ Applied"],
            f"keyboard stays on the applied-only row (got {status_row})")


def test_fbminus_records_down_skipped_and_morphs() -> None:
    section("3. fb-:<job_id> → record_job_feedback('down') + status skipped, morphs to reason picker")
    os.environ["SKIP_FEEDBACK_ENABLED"] = "1"
    try:
        bot = _import_bot_fresh()
        _patch_error_capture(bot)

        tg = FakeTG()
        db = FakeDB()
        cb = _make_cb(chat_id=123, msg_id=456, kind="fb-", payload="job-3")

        bot.handle_callback(tg, db, cb)

        _assert(db.feedback_writes == [(123, "job-3", "down")],
                f"feedback recorded down (got {db.feedback_writes})")
        _assert(db.status_writes == [(123, "job-3", "skipped")],
                f"status set skipped (got {db.status_writes})")
        _assert(tg.delete_calls == [], f"no delete on morph path (got {tg.delete_calls})")
        _assert(len(tg.edit_text_calls) == 1,
                f"card morphed via edit_message_text (got {len(tg.edit_text_calls)})")
        _, _, text, kb = tg.edit_text_calls[0]
        _assert("What was off" in text, f"morph text is the reason prompt (got {text!r})")
        codes = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        _assert(any(c.startswith("fbr:loc:job-3") for c in codes),
                f"reason keyboard has structured codes (got {codes})")
        _assert("fbr:other:job-3" in codes and "fbr:back:job-3" in codes,
                f"bottom row is Other-no-reason + Return (got {codes})")
        _assert("sr:skip" not in codes,
                f"old sr:skip button gone (got {codes})")
        _assert(tg.callback_answers and "what was off" in tg.callback_answers[0][1].lower(),
                f"toast asks what was off (got {tg.callback_answers})")
    finally:
        os.environ.pop("SKIP_FEEDBACK_ENABLED", None)


def test_fbr_structured_tap_stores_reason_and_note() -> None:
    section("4. fbr:<code>:<job_id> structured tap → set_job_feedback_reason + append_skip_note, message cleaned up")
    bot = _import_bot_fresh()
    _patch_error_capture(bot)

    tg = FakeTG(delete_returns=True)
    db = FakeDB()
    cb = _make_cb(chat_id=123, msg_id=789, kind="fbr", payload="loc:job-4")

    bot.handle_callback(tg, db, cb)

    _assert(db.reason_writes == [(123, "job-4", "wrong location")],
            f"structured reason stored as human phrase (got {db.reason_writes})")
    _assert(len(db.skip_notes) == 1, f"skip note appended (got {db.skip_notes})")
    note_chat, note_text = db.skip_notes[0]
    _assert(note_chat == 123, "note recorded for the right chat")
    _assert("wrong location" in note_text, f"note names the reason (got {note_text!r})")
    _assert("Senior ML Engineer" in note_text and "Acme" in note_text,
            f"note includes title/company (got {note_text!r})")
    _assert(tg.delete_calls == [(123, 789)],
            f"prompt message deleted after structured tap (got {tg.delete_calls})")
    _assert(tg.callback_answers and tg.callback_answers[0][1] == "Got it 👍",
            f"toast acknowledges the tap (got {tg.callback_answers})")


def test_fbr_other_dismisses_with_no_reason() -> None:
    section("5. fbr:other:<job_id> → 👎 accepted with no reason, prompt cleaned up")
    bot = _import_bot_fresh()
    _patch_error_capture(bot)

    tg = FakeTG(delete_returns=True)
    db = FakeDB()
    cb = _make_cb(chat_id=123, msg_id=789, kind="fbr", payload="other:job-5")

    bot.handle_callback(tg, db, cb)

    _assert(db.get_awaiting_state(123) is None,
            "no free-text state — Other is a one-tap dismissal now")
    _assert(db.reason_writes == [], f"no reason recorded (got {db.reason_writes})")
    _assert(db.skip_notes == [], f"no skip note appended (got {db.skip_notes})")
    _assert(tg.delete_calls == [(123, 789)],
            f"prompt deleted like a structured tap (got {tg.delete_calls})")
    _assert(tg.callback_answers and tg.callback_answers[0][1] == "Got it 👍",
            f"toast acknowledges the tap (got {tg.callback_answers})")


def test_fbr_back_restores_card() -> None:
    section("5b. fbr:back:<job_id> → 👎 undone, card restored with neutral keyboard")
    bot = _import_bot_fresh()
    _patch_error_capture(bot)

    tg = FakeTG()
    db = FakeDB()
    # State right after a 👎 tap: verdict down, status skipped, card morphed.
    db.record_job_feedback(123, "job-5b", "down")
    db.set_application_status(123, "job-5b", "skipped")
    cb = _make_cb(chat_id=123, msg_id=789, kind="fbr", payload="back:job-5b")

    bot.handle_callback(tg, db, cb)

    _assert(db.feedback_clears == [(123, "job-5b")],
            f"verdict cleared (got {db.feedback_clears})")
    _assert(db.status_clears == [(123, "job-5b")],
            f"skipped status cleared (got {db.status_clears})")
    _assert(len(tg.edit_text_calls) == 1,
            f"card restored via edit_message_text (got {len(tg.edit_text_calls)})")
    _, _, text, kb = tg.edit_text_calls[0]
    _assert("Senior ML Engineer" in text, f"restored text is the job card (got {text!r})")
    labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
    _assert("👍" in labels and "👎" in labels,
            f"restored keyboard is the neutral row (got {labels})")
    _assert(tg.delete_calls == [], f"no delete on restore path (got {tg.delete_calls})")
    _assert(tg.callback_answers and "Restored" in tg.callback_answers[0][1],
            f"toast confirms restore (got {tg.callback_answers})")


def test_free_text_reply_lands_in_job_feedback() -> None:
    section("6. Free-text reply after fbr:other → also lands in db.set_job_feedback_reason")
    bot = _import_bot_fresh()
    _patch_error_capture(bot)
    _install_fake_skip_feedback()

    tg = FakeTG()
    db = FakeDB()
    payload = {
        "job_id": "job-6",
        "title": "Senior ML Engineer",
        "company": "Acme",
        "source": "hn",
        "url": "https://example.com/job",
        "snippet": "A great role.",
    }
    db.set_awaiting_state(123, bot.STATE_AWAITING_SKIP_REASON, payload)

    upd = _make_text_update(123, "Team seems disorganized based on the JD.")
    bot._dispatch(tg, db, upd)

    _assert(db.reason_writes == [(123, "job-6", "Team seems disorganized based on the JD.")],
            f"free-text reason landed in job_feedback (got {db.reason_writes})")
    _assert(db.get_awaiting_state(123) is None, "state cleared after the reply")


def test_fb0_unvotes_back_to_neutral() -> None:
    section("8. fb0:<job_id> on a Liked card → verdict + interested status cleared, neutral 👍/👎 row back")
    bot = _import_bot_fresh()
    _patch_error_capture(bot)

    tg = FakeTG()
    db = FakeDB()
    # State after a 👍 tap: verdict up, status interested.
    db.record_job_feedback(123, "job-8", "up")
    db.set_application_status(123, "job-8", "interested")
    db.status_writes.clear()
    cb = _make_cb(chat_id=123, msg_id=456, kind="fb0", payload="job-8")

    bot.handle_callback(tg, db, cb)

    _assert(db.feedback_clears == [(123, "job-8")],
            f"verdict cleared (got {db.feedback_clears})")
    _assert(db.status_clears == [(123, "job-8")],
            f"interested status cleared (got {db.status_clears})")
    _assert(db.get_application_status(123, "job-8") is None,
            "status back to absent")
    kb = tg.edit_reply_markup_calls[0][2]
    labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
    _assert("👍" in labels and "👎" in labels and "👍 Liked" not in labels,
            f"keyboard back to neutral thumbs (got {labels})")
    _assert(tg.callback_answers and "cleared" in tg.callback_answers[0][1].lower(),
            f"toast confirms the reset (got {tg.callback_answers})")


def test_liked_button_carries_fb0() -> None:
    section("9. job_keyboard(feedback='up') → Liked button is fb0:, not fb-:")
    from telegram_client import job_keyboard
    kb = job_keyboard("job-9", feedback="up")
    liked = next(b for row in kb["inline_keyboard"] for b in row
                 if b["text"] == "👍 Liked")
    _assert(liked["callback_data"] == "fb0:job-9",
            f"Liked un-votes instead of flipping to 👎 (got {liked['callback_data']})")
    kb = job_keyboard("job-9", feedback="down")
    noted = next(b for row in kb["inline_keyboard"] for b in row
                 if b["text"] == "👎 Noted")
    _assert(noted["callback_data"] == "fb0:job-9",
            f"Noted un-votes too (got {noted['callback_data']})")


def test_kill_switch_disables_reason_followup() -> None:
    section("7. SKIP_FEEDBACK_ENABLED=0 → fb-: reverts to plain skip+delete, no reason picker")
    os.environ["SKIP_FEEDBACK_ENABLED"] = "0"
    try:
        bot = _import_bot_fresh()
        _patch_error_capture(bot)

        tg = FakeTG(delete_returns=True)
        db = FakeDB()
        cb = _make_cb(chat_id=123, msg_id=456, kind="fb-", payload="job-7")

        bot.handle_callback(tg, db, cb)

        _assert(db.feedback_writes == [(123, "job-7", "down")],
                f"feedback still recorded (got {db.feedback_writes})")
        _assert(db.status_writes == [(123, "job-7", "skipped")],
                f"status still set skipped (got {db.status_writes})")
        _assert(tg.edit_text_calls == [],
                f"NO reason-picker morph (got {tg.edit_text_calls})")
        _assert(tg.send_messages == [],
                f"NO follow-up prompt sent (got {tg.send_messages})")
        _assert(tg.delete_calls == [(123, 456)],
                f"card deleted like a plain skip (got {tg.delete_calls})")
        _assert(db.get_awaiting_state(123) is None, "no awaiting state set")
        _assert(tg.callback_answers and tg.callback_answers[0][1] == "✕ Removed",
                f"toast matches the plain-skip copy (got {tg.callback_answers})")
    finally:
        os.environ.pop("SKIP_FEEDBACK_ENABLED", None)


def main() -> int:
    setup()
    test_fbplus_records_up_and_defaults_interested()
    test_fbplus_does_not_overwrite_applied()
    test_fbminus_records_down_skipped_and_morphs()
    test_fbr_structured_tap_stores_reason_and_note()
    test_fbr_other_dismisses_with_no_reason()
    test_fbr_back_restores_card()
    test_free_text_reply_lands_in_job_feedback()
    test_fb0_unvotes_back_to_neutral()
    test_liked_button_carries_fb0()
    test_kill_switch_disables_reason_followup()
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
