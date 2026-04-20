#!/usr/bin/env python3
"""End-to-end smoke test for the onboarding wizard.

Walks a fresh user through every step of `onboarding.py` using a fake
TelegramClient that captures send_message / edit calls. Asserts at each
step:
  • the correct step is persisted in the DB
  • the correct message body + keyboard are sent
  • free-text captures advance the state
  • the finalize step stamps onboarding_completed_at and writes
    prefs_free_text + min_match_score into the profile

Run:  python skill/job-search/scripts/tests/smoke_onboarding.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import onboarding as ob                # noqa: E402
from db import DB                      # noqa: E402


# ---------- Fake Telegram client ----------

class FakeTelegramClient:
    """Captures every outbound call. Zero network I/O."""

    def __init__(self) -> None:
        self.sent: list[dict] = []      # list of {chat_id, text, reply_markup}
        self.edits: list[dict] = []
        self.callbacks_answered: list[dict] = []
        self.raw_calls: list[dict] = []  # captures _call(...) for sendSticker etc.
        self._next_msg_id = 1000

    def _call(self, method: str, payload: dict | None = None,
              files: dict | None = None, http_timeout: int | None = None) -> dict:
        """Stub for raw Bot API calls (e.g. sendSticker). Real TelegramClient
        hits the API here; the fake just captures the call. Without this
        stub, pig_stickers.send_sticker() logs a noisy warning during tests
        even though the logic is correct."""
        self.raw_calls.append({"method": method, "payload": payload})
        return {}

    def send_message(self, chat_id, text, parse_mode="MarkdownV2",
                     reply_markup=None, disable_preview=True):
        self._next_msg_id += 1
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
            "message_id": self._next_msg_id,
        })
        return self._next_msg_id

    def send_plain(self, chat_id, text):
        return self.send_message(chat_id, text, parse_mode="")

    def edit_message_text(self, chat_id, message_id, text,
                          parse_mode="MarkdownV2", reply_markup=None,
                          disable_preview=True):
        self.edits.append({
            "chat_id": chat_id, "message_id": message_id,
            "text": text, "reply_markup": reply_markup,
        })

    def edit_reply_markup(self, chat_id, message_id, reply_markup):
        self.edits.append({
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": reply_markup,
        })

    def answer_callback(self, cb_id, text="", show_alert=False):
        self.callbacks_answered.append({"id": cb_id, "text": text})

    def last_sent(self) -> dict:
        assert self.sent, "No messages sent yet"
        return self.sent[-1]

    def reset(self) -> None:
        self.sent.clear()
        self.edits.clear()
        self.callbacks_answered.clear()


# ---------- Helpers ----------

def _make_cb(cb_id: str, data: str, chat_id: int, msg_id: int) -> dict:
    return {
        "id": cb_id,
        "data": data,
        "message": {"chat": {"id": chat_id}, "message_id": msg_id},
    }


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


# ---------- The walk ----------

def main() -> int:
    # Isolate the onboarding tests from the live sticker registry. When
    # STICKER_FILE_IDS is populated (as it is post-paste), start() would
    # send a WAVE sticker instead of the Unicode 🐷 — which is correct
    # behavior in prod but makes these assertions flaky depending on
    # ambient state. The Unicode-fallback path we exercise here still
    # asserts the core state-machine guarantees; the sticker path has
    # its own coverage in smoke_pig_stickers.py.
    import pig_stickers as _ps
    _ps.STICKER_FILE_IDS.clear()

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "jobs.db"
        db = DB(db_path)
        tg = FakeTelegramClient()
        chat_id = 777_123

        # Register the user so set_prefs_free_text et al. find a row.
        db.upsert_user(chat_id, username="alex", first_name="Alex", last_name="S")

        # ---- 1. start() with a fresh user sends the animated pig + welcome ----
        ob.start(tg, db, chat_id, first_name="Alex")
        # Expected: a lone "🐷" first (Telegram auto-animates single-emoji
        # messages), then the welcome bubble with inline keyboard.
        _assert(len(tg.sent) == 2,
                f"welcome should send exactly two messages (animated pig + bubble), got {len(tg.sent)}")
        _assert(tg.sent[0]["text"] == "🐷",
                "first message should be a lone pig for auto-animation")
        welcome = tg.sent[1]
        _assert("Welcome, Alex" in welcome["text"], "welcome should greet by first name")
        _assert(welcome["reply_markup"] is not None, "welcome should carry inline keyboard")
        kb = welcome["reply_markup"]["inline_keyboard"]
        _assert(any("Get started" in b["text"] for row in kb for b in row),
                "welcome keyboard should have 'Get started'")
        state_raw = db.get_onboarding_state(chat_id)
        state = json.loads(state_raw)
        _assert(state["step"] == ob.STEP_WELCOME, "step should be 'welcome' after start()")

        # ---- 2. tap 'Get started' → advances to STEP_RESUME ----
        tg.reset()
        ob.handle_callback(
            tg, db, _make_cb("cb-1", f"ob:{ob.CB_START}", chat_id, welcome["message_id"]),
            chat_id, welcome["message_id"], ob.CB_START,
        )
        # Expected: 1 edit_reply_markup (clear welcome buttons) + 1 send_message (resume prompt)
        _assert(len(tg.sent) == 1, f"expected 1 message after 'start' tap, got {len(tg.sent)}")
        resume_prompt = tg.sent[0]
        _assert("Upload your CV" in resume_prompt["text"],
                "should prompt for CV upload")
        _assert(_progress_dot_count(resume_prompt["text"]) == 1,
                "progress should show step 1 of 6")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["step"] == ob.STEP_RESUME, "step should advance to 'resume'")

        # ---- 3. simulate CV upload side-effect ----
        db.set_resume(chat_id, f"{td}/resume.pdf", "Alex is a Senior React Engineer...")
        tg.reset()
        advanced = ob.handle_resume_uploaded(tg, db, chat_id)
        _assert(advanced, "handle_resume_uploaded should advance the wizard")
        _assert(len(tg.sent) == 2, "should send 'got your CV' + seniority prompt")
        _assert("Got your CV" in tg.sent[0]["text"], "first message should confirm CV")
        _assert("Seniority" in tg.sent[1]["text"], "second message should ask seniority")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["step"] == ob.STEP_SENIORITY, "step should be 'seniority'")
        # Seniority keyboard should have 5 choices (4 buckets + any) + cancel
        sk = tg.sent[1]["reply_markup"]["inline_keyboard"]
        n_sen_buttons = sum(1 for row in sk for b in row if b.get("callback_data", "").startswith(f"ob:{ob.CB_SENIORITY}"))
        _assert(n_sen_buttons == 5, f"expected 5 seniority buttons, got {n_sen_buttons}")

        # ---- 4. tap 'Senior' → advances to STEP_ROLE ----
        tg.reset()
        sen_msg_id = 9001  # synthetic
        ob.handle_callback(
            tg, db, _make_cb("cb-2", f"ob:{ob.CB_SENIORITY}:senior", chat_id, sen_msg_id),
            chat_id, sen_msg_id, f"{ob.CB_SENIORITY}:senior",
        )
        _assert(len(tg.sent) == 1 and "Target role" in tg.sent[0]["text"],
                "should send role prompt after seniority pick")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["answers"]["seniority"] == "senior", "seniority stored")
        _assert(state["step"] == ob.STEP_ROLE, "step should be 'role'")
        # awaiting_state should be set so free-text lands here
        _assert(db.get_awaiting_state(chat_id) == ob.AWAIT_ONBOARDING_ROLE,
                "awaiting_state should be onboarding_role")

        # ---- 5. send free-text role 'React Engineer' → advances to STEP_REMOTE ----
        tg.reset()
        consumed = ob.handle_text_role(tg, db, chat_id, "React Engineer")
        _assert(consumed, "handle_text_role should consume the text")
        _assert("Work style" in tg.sent[0]["text"], "should send remote prompt")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["answers"]["role"] == "React Engineer", "role stored")
        _assert(state["step"] == ob.STEP_REMOTE, "step should be 'remote'")
        _assert(db.get_awaiting_state(chat_id) is None, "awaiting_state should clear")

        # ---- 6. tap 'Remote' → advances to STEP_LOCATION ----
        tg.reset()
        rmt_msg_id = 9002
        ob.handle_callback(
            tg, db, _make_cb("cb-3", f"ob:{ob.CB_REMOTE}:remote", chat_id, rmt_msg_id),
            chat_id, rmt_msg_id, f"{ob.CB_REMOTE}:remote",
        )
        _assert("Location" in tg.sent[0]["text"], "should send location prompt")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["answers"]["remote"] == "remote", "remote stored")
        _assert(state["step"] == ob.STEP_LOCATION, "step should be 'location'")
        _assert(db.get_awaiting_state(chat_id) == ob.AWAIT_ONBOARDING_LOCATION,
                "awaiting_state should be onboarding_location")

        # ---- 7. free-text location → advances to STEP_MINSCORE ----
        tg.reset()
        consumed = ob.handle_text_location(tg, db, chat_id, "Remote EU")
        _assert(consumed, "location text consumed")
        _assert("Minimum match score" in tg.sent[0]["text"], "should send minscore prompt")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["answers"]["location"] == "Remote EU", "location stored")
        _assert(state["step"] == ob.STEP_MINSCORE, "step should be 'minscore'")

        # ---- 8. tap 'ms:3' → finalizes the wizard ----
        tg.reset()
        ms_msg_id = 9003
        on_complete_called = []
        on_run_search_called = []
        ob.handle_callback(
            tg, db, _make_cb("cb-4", f"ob:{ob.CB_MIN_SCORE}:3", chat_id, ms_msg_id),
            chat_id, ms_msg_id, f"{ob.CB_MIN_SCORE}:3",
            on_complete=lambda cid: on_complete_called.append(cid),
            on_run_search=lambda cid: on_run_search_called.append(cid),
        )
        # Expected: 1 summary message with the two-button final keyboard
        summaries = [s for s in tg.sent if "Setup complete" in s["text"]]
        _assert(summaries, f"no summary found; sent={[s['text'][:40] for s in tg.sent]}")
        summary = summaries[0]
        kb = summary["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in kb for b in row]
        _assert("Run a search now" in labels, "summary should have 'Run a search now'")
        _assert("I'll wait for tomorrow" in labels, "summary should have wait option")
        _assert(on_complete_called == [chat_id], "on_complete should fire with chat_id")

        # DB side-effects: prefs_free_text populated, profile has min_match_score,
        # onboarding_completed_at stamped, onboarding_state cleared.
        prefs = db.get_prefs_free_text(chat_id)
        _assert(prefs and "React Engineer" in prefs and "senior" in prefs,
                f"prefs should mention role+seniority, got: {prefs!r}")
        _assert(prefs and "Remote EU" in prefs, "prefs should mention location")
        profile_raw = db.get_user_profile(chat_id)
        profile = json.loads(profile_raw) if profile_raw else {}
        _assert(int(profile.get("min_match_score") or 0) == 3,
                f"min_match_score should be 3, got {profile.get('min_match_score')}")
        _assert(db.get_onboarding_completed_at(chat_id) is not None,
                "onboarding_completed_at should be set")
        _assert(db.get_onboarding_state(chat_id) is None,
                "onboarding_state should be cleared post-finalize")

        # ---- 9. tap 'Run a search now' → on_run_search fires ----
        tg.reset()
        on_run_search_called.clear()
        summary_msg_id = summary["message_id"]
        ob.handle_callback(
            tg, db, _make_cb("cb-5", f"ob:{ob.CB_RUN_SEARCH}", chat_id, summary_msg_id),
            chat_id, summary_msg_id, ob.CB_RUN_SEARCH,
            on_complete=lambda cid: on_complete_called.append(cid),
            on_run_search=lambda cid: on_run_search_called.append(cid),
        )
        _assert(on_run_search_called == [chat_id], "on_run_search should fire")

        # ---- 10. second /start does NOT restart the wizard for a completed user ----
        tg.reset()
        ob.start(tg, db, chat_id, first_name="Alex")
        _assert(len(tg.sent) == 0,
                "completed user should get no welcome from onboarding.start() "
                "(the bot handler shows welcome-back itself)")

        # ---- 11. cancel path: fresh user, start, then cancel ----
        tg.reset()
        chat2 = 777_999
        db.upsert_user(chat2, first_name="Pat")
        ob.start(tg, db, chat2, first_name="Pat")
        # start() now emits the animated pig first (tg.sent[0]); the welcome
        # bubble is at index 1.
        welcome2 = tg.sent[1]
        tg.reset()
        ob.handle_callback(
            tg, db, _make_cb("cb-cancel", f"ob:{ob.CB_CANCEL}", chat2, welcome2["message_id"]),
            chat2, welcome2["message_id"], ob.CB_CANCEL,
        )
        _assert(db.get_onboarding_state(chat2) is None, "cancel should clear state")
        _assert(db.get_onboarding_completed_at(chat2) is None,
                "cancel should NOT mark complete")

        # ---- 12. maybe_resume: stale state gets replayed ----
        tg.reset()
        chat3 = 777_555
        db.upsert_user(chat3)
        # Fake a stale mid-wizard state
        stale = {
            "step": ob.STEP_SENIORITY,
            "answers": {},
            "started_at": 0.0,
            "last_step_at": 0.0,   # 1970 → definitely stale
        }
        db.set_onboarding_state(chat3, json.dumps(stale))
        resumed = ob.maybe_resume(tg, db, chat3)
        _assert(resumed, "maybe_resume should fire on stale state")
        # Should send the ping + re-send the seniority prompt.
        _assert(any("Seniority" in s["text"] for s in tg.sent),
                "maybe_resume should re-send the seniority prompt")

        # ---- 13. returning user with CV on file: resume step shows Keep/Upload-new ----
        tg.reset()
        chat4 = 777_111
        db.upsert_user(chat4, first_name="Jamie")
        # Simulate a user who uploaded a CV under the old bot but never
        # completed onboarding — exactly the case the original bug hit.
        db.set_resume(chat4, f"{td}/old_resume.pdf",
                      "Jamie has 4 years of Python experience...")
        ob.start(tg, db, chat4, first_name="Jamie")
        welcome4 = tg.sent[1]   # sent[0] is the animated-pig preface
        tg.reset()
        ob.handle_callback(
            tg, db, _make_cb("cb-13a", f"ob:{ob.CB_START}", chat4, welcome4["message_id"]),
            chat4, welcome4["message_id"], ob.CB_START,
        )
        _assert(len(tg.sent) == 1, "should send exactly one message after 'start' tap")
        resume_review = tg.sent[0]
        _assert("Your CV" in resume_review["text"], "should render resume-review heading")
        # The filename renders through mdv2_escape, which backslash-escapes
        # both '_' and '.', so the raw "old_resume.pdf" won't appear
        # literally — match on the MDv2-escaped form.
        _assert("old\\_resume\\.pdf" in resume_review["text"],
                f"review prompt should name the existing CV, got: {resume_review['text']!r}")
        kb = resume_review["reply_markup"]["inline_keyboard"]
        labels = [b["text"] for row in kb for b in row]
        _assert("Keep this CV" in labels, "should offer 'Keep this CV' button")
        state = json.loads(db.get_onboarding_state(chat4))
        _assert(state["step"] == ob.STEP_RESUME,
                "step should be RESUME (no silent skip)")

        # Tap "Keep this CV" → advances to seniority without a new upload.
        tg.reset()
        ob.handle_callback(
            tg, db, _make_cb("cb-13b", f"ob:{ob.CB_KEEP_CV}", chat4, resume_review["message_id"]),
            chat4, resume_review["message_id"], ob.CB_KEEP_CV,
        )
        _assert(any("Seniority" in s["text"] for s in tg.sent),
                "Keep-CV should advance to seniority")
        state = json.loads(db.get_onboarding_state(chat4))
        _assert(state["step"] == ob.STEP_SENIORITY, "step should be SENIORITY after keep")

        # ---- 14. returning user who uploads a replacement advances normally ----
        tg.reset()
        chat5 = 777_222
        db.upsert_user(chat5, first_name="Sam")
        db.set_resume(chat5, f"{td}/sam_old.pdf", "Sam old...")
        ob.start(tg, db, chat5, first_name="Sam")
        welcome5 = tg.sent[1]   # sent[0] is the animated-pig preface
        tg.reset()
        ob.handle_callback(
            tg, db, _make_cb("cb-14a", f"ob:{ob.CB_START}", chat5, welcome5["message_id"]),
            chat5, welcome5["message_id"], ob.CB_START,
        )
        # Simulate a fresh upload overwriting the file
        db.set_resume(chat5, f"{td}/sam_new.pdf", "Sam newly rewrote everything...")
        tg.reset()
        advanced = ob.handle_resume_uploaded(tg, db, chat5)
        _assert(advanced, "replacement upload should advance the wizard")
        state = json.loads(db.get_onboarding_state(chat5))
        _assert(state["step"] == ob.STEP_SENIORITY,
                "step should advance to SENIORITY after replacement upload")

        print("PASS  — 17 assertions across fresh flow, re-entry, cancel, resume, "
              "and existing-CV review")
        return 0


def _progress_dot_count(text: str) -> int:
    """Count filled dots in the rendered progress indicator. Uses the
    same '●' char the onboarding module emits."""
    # The indicator renders inside a monospace code block; we just count
    # occurrences in the whole text which is unique enough.
    return text.count("●")


if __name__ == "__main__":
    sys.exit(main())
