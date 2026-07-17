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

Flow under test (welcome merged into question 1, CV last + skippable):
  /start → pitch+seniority → role → remote → location →
  resume (upload / keep / skip) → done.

Min-score is no longer a wizard step (a new user can't rate matches they
haven't seen). It defaults to 3 in _finalize; STEP_MINSCORE / CB_MIN_SCORE
survive only for in-flight legacy wizards, covered by a dedicated case below.

Run:  python skill/job-search/scripts/tests/smoke_onboarding.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
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


class _CaptureBuilds:
    """Swap bot._enqueue_profile_rebuild for a recorder around a finalize."""

    def __init__(self) -> None:
        self.builds: list[tuple[int, str]] = []

    def __enter__(self):
        import bot as _bot
        self._bot = _bot
        self._orig = _bot._enqueue_profile_rebuild
        _bot._enqueue_profile_rebuild = (
            lambda tg_, db_, cid_, *, trigger: self.builds.append((cid_, trigger))
        )
        return self

    def __exit__(self, *exc):
        self._bot._enqueue_profile_rebuild = self._orig
        return False


# ---------- The walk ----------

def main() -> int:
    # Isolate the onboarding tests from the live sticker registry. When
    # STICKER_FILE_IDS is populated (as it is post-paste), start() would
    # send a WAVE sticker instead of the Unicode 🐷 — which is correct
    # behavior in prod but makes these assertions flaky depending on
    # ambient state.
    import pig_stickers as _ps
    _ps.STICKER_FILE_IDS.clear()

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "jobs.db"
        db = DB(db_path)
        tg = FakeTelegramClient()
        chat_id = 777_123

        db.upsert_user(chat_id, username="alex", first_name="Alex", last_name="S")

        # ---- 1. start(): pig + ONE merged message = pitch + question 1 ----
        ob.start(tg, db, chat_id, first_name="Alex")
        _assert(len(tg.sent) == 2,
                f"welcome should send exactly two messages (animated pig + bubble), got {len(tg.sent)}")
        _assert(tg.sent[0]["text"] == "🐷",
                "first message should be a lone pig for auto-animation")
        welcome = tg.sent[1]
        _assert("Welcome, Alex" in welcome["text"], "welcome should greet by first name")
        _assert("Seniority" in welcome["text"],
                "welcome must contain question 1 — no 'Get started' interstitial")
        kb = welcome["reply_markup"]["inline_keyboard"]
        n_sen_buttons = sum(1 for row in kb for b in row
                            if b.get("callback_data", "").startswith(f"ob:{ob.CB_SENIORITY}"))
        _assert(n_sen_buttons == 5, f"expected 5 seniority buttons on welcome, got {n_sen_buttons}")
        _assert(_progress_dot_count(welcome["text"]) == 1,
                "welcome should show progress step 1 of 5")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["step"] == ob.STEP_SENIORITY,
                "state should start at 'seniority' — welcome is not a step anymore")

        # ---- 2. tap 'Senior' → advances to STEP_ROLE ----
        tg.reset()
        ob.handle_callback(
            tg, db, _make_cb("cb-2", f"ob:{ob.CB_SENIORITY}:senior", chat_id, welcome["message_id"]),
            chat_id, welcome["message_id"], f"{ob.CB_SENIORITY}:senior",
        )
        _assert(len(tg.sent) == 1 and "Target role" in tg.sent[0]["text"],
                "should send role prompt after seniority pick")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["answers"]["seniority"] == "senior", "seniority stored")
        _assert(state["step"] == ob.STEP_ROLE, "step should be 'role'")
        _assert(db.get_awaiting_state(chat_id) == ob.AWAIT_ONBOARDING_ROLE,
                "awaiting_state should be onboarding_role")

        # ---- 3. free-text role → STEP_REMOTE ----
        tg.reset()
        consumed = ob.handle_text_role(tg, db, chat_id, "React Engineer")
        _assert(consumed, "handle_text_role should consume the text")
        _assert("Work style" in tg.sent[0]["text"], "should send remote prompt")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["answers"]["role"] == "React Engineer", "role stored")
        _assert(state["step"] == ob.STEP_REMOTE, "step should be 'remote'")
        _assert(db.get_awaiting_state(chat_id) is None, "awaiting_state should clear")

        # ---- 4. tap 'Remote' → STEP_LOCATION ----
        tg.reset()
        ob.handle_callback(
            tg, db, _make_cb("cb-3", f"ob:{ob.CB_REMOTE}:remote", chat_id, 9002),
            chat_id, 9002, f"{ob.CB_REMOTE}:remote",
        )
        _assert("Location" in tg.sent[0]["text"], "should send location prompt")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["answers"]["remote"] == "remote", "remote stored")
        _assert(state["step"] == ob.STEP_LOCATION, "step should be 'location'")

        # ---- 5. free-text location → STEP_RESUME (min-score is no longer a step) ----
        tg.reset()
        consumed = ob.handle_text_location(tg, db, chat_id, "Remote EU")
        _assert(consumed, "location text consumed")
        _assert(len(tg.sent) == 1, "location should send the resume prompt directly")
        resume_prompt = tg.sent[0]
        _assert("upload your CV" in resume_prompt["text"],
                "location must advance straight to the CV upload (last) step")
        labels = [b["text"] for row in resume_prompt["reply_markup"]["inline_keyboard"] for b in row]
        _assert("Skip for now" in labels, "resume step must be skippable")
        _assert(_progress_dot_count(resume_prompt["text"]) == 5,
                "resume prompt should show step 5 of 5")
        state = json.loads(db.get_onboarding_state(chat_id))
        _assert(state["answers"]["location"] == "Remote EU", "location stored")
        _assert("min_score" not in state["answers"],
                "fresh flow must NOT collect a min_score answer")
        _assert(state["step"] == ob.STEP_RESUME, "step should be 'resume' (last)")
        # Funnel breadcrumb: every step the user landed on is logged in order,
        # with no duplicate consecutive entries, for the ops problems report.
        hist_steps = [h["step"] for h in state.get("history", [])]
        _assert(hist_steps == [ob.STEP_WELCOME, ob.STEP_SENIORITY, ob.STEP_ROLE,
                               ob.STEP_REMOTE, ob.STEP_LOCATION, ob.STEP_RESUME],
                f"history should record each step transition in order, got {hist_steps}")

        # ---- 6. CV upload finalizes the wizard (no min_score answer → default 3) ----
        db.set_resume(chat_id, f"{td}/resume.pdf", "Alex is a Senior React Engineer...")
        tg.reset()
        on_complete_called: list[int] = []
        on_run_search_called: list[int] = []
        with _CaptureBuilds() as cap:
            advanced = ob.handle_resume_uploaded(
                tg, db, chat_id,
                on_complete=lambda cid: on_complete_called.append(cid),
                on_run_search=lambda cid: on_run_search_called.append(cid),
            )
        _assert(advanced, "handle_resume_uploaded should consume the upload")
        _assert(any("Got your CV" in s["text"] for s in tg.sent),
                "should confirm the CV")
        summaries = [s for s in tg.sent if "Setup complete" in s["text"]]
        _assert(summaries, f"no summary found; sent={[s['text'][:40] for s in tg.sent]}")
        summary = summaries[0]
        _assert(summary["reply_markup"] is None,
                "summary must have NO inline buttons — the search auto-runs now")
        _assert(on_complete_called == [chat_id], "on_complete should fire with chat_id")
        # The whole point of task 2: finalize auto-fires the first search, once,
        # without the user tapping anything.
        _assert(on_run_search_called == [chat_id],
                f"finalize should auto-fire on_run_search exactly once, got {on_run_search_called}")

        # DB side-effects.
        prefs = db.get_prefs_free_text(chat_id)
        _assert(prefs and "React Engineer" in prefs and "senior" in prefs,
                f"prefs should mention role+seniority, got: {prefs!r}")
        _assert(prefs and "Remote EU" in prefs, "prefs should mention location")
        profile_raw = db.get_user_profile(chat_id)
        profile = json.loads(profile_raw) if profile_raw else {}
        _assert(int(profile.get("min_match_score") or 0) == 3,
                f"min_match_score should be 3, got {profile.get('min_match_score')}")
        # The AUTHORITATIVE ⭐ gate is the DB column, not the profile JSON.
        _assert(db.get_min_match_score(chat_id) == 3,
                f"DB min_match_score column should be 3, got {db.get_min_match_score(chat_id)}")
        # finalize must kick the deferred Opus build, exactly once.
        _assert(cap.builds == [(chat_id, "onboarding")],
                f"finalize must enqueue one 'onboarding' profile build, got {cap.builds}")
        _assert(db.get_onboarding_completed_at(chat_id) is not None,
                "onboarding_completed_at should be set")
        _assert(db.get_onboarding_state(chat_id) is None,
                "onboarding_state should be cleared post-finalize")

        # ---- 8. legacy summary card: an old 'ob:runsearch' button still fires ----
        # New summaries carry no buttons, but cards already on users' screens do.
        # The handler must keep working past finalize (state is cleared by now).
        tg.reset()
        legacy_run_called: list[int] = []
        ob.handle_callback(
            tg, db, _make_cb("cb-5", f"ob:{ob.CB_RUN_SEARCH}", chat_id, summary["message_id"]),
            chat_id, summary["message_id"], ob.CB_RUN_SEARCH,
            on_run_search=lambda cid: legacy_run_called.append(cid),
        )
        _assert(legacy_run_called == [chat_id],
                "legacy ob:runsearch callback must still fire on_run_search")

        # ---- 9. second /start does NOT restart the wizard for a completed user ----
        tg.reset()
        ob.start(tg, db, chat_id, first_name="Alex")
        _assert(len(tg.sent) == 0,
                "completed user should get no welcome from onboarding.start()")

        # ---- 10. cancel path: fresh user, start, then cancel from welcome ----
        tg.reset()
        chat2 = 777_999
        db.upsert_user(chat2, first_name="Pat")
        ob.start(tg, db, chat2, first_name="Pat")
        welcome2 = tg.sent[1]   # sent[0] is the animated-pig preface
        tg.reset()
        ob.handle_callback(
            tg, db, _make_cb("cb-cancel", f"ob:{ob.CB_CANCEL}", chat2, welcome2["message_id"]),
            chat2, welcome2["message_id"], ob.CB_CANCEL,
        )
        _assert(db.get_onboarding_state(chat2) is None, "cancel should clear state")
        _assert(db.get_onboarding_completed_at(chat2) is None,
                "cancel should NOT mark complete")

        # ---- 11. maybe_resume: stale state gets replayed ----
        tg.reset()
        chat3 = 777_555
        db.upsert_user(chat3)
        stale = {
            "step": ob.STEP_SENIORITY,
            "answers": {},
            "started_at": 0.0,
            "last_step_at": 0.0,   # 1970 → definitely stale
        }
        db.set_onboarding_state(chat3, json.dumps(stale))
        resumed = ob.maybe_resume(tg, db, chat3)
        _assert(resumed, "maybe_resume should fire on stale state")
        _assert(any("Seniority" in s["text"] for s in tg.sent),
                "maybe_resume should re-send the seniority prompt")

        # ---- 12. legacy 'Get started' button routes to question 1 ----
        tg.reset()
        chat4 = 777_111
        db.upsert_user(chat4, first_name="Jamie")
        legacy = {"step": ob.STEP_WELCOME, "answers": {},
                  "started_at": time.time(), "last_step_at": time.time()}
        db.set_onboarding_state(chat4, json.dumps(legacy))
        ob.handle_callback(
            tg, db, _make_cb("cb-legacy", f"ob:{ob.CB_START}", chat4, 9010),
            chat4, 9010, ob.CB_START,
        )
        _assert(any("Seniority" in s["text"] for s in tg.sent),
                "legacy Get-started should land on the seniority question")
        state = json.loads(db.get_onboarding_state(chat4))
        _assert(state["step"] == ob.STEP_SENIORITY, "legacy start routes to seniority")

        # ---- 13. skip-CV path finalizes without a resume ----
        tg.reset()
        skip_state = {"step": ob.STEP_RESUME,
                      "answers": {"seniority": "mid", "role": "", "remote": "any",
                                  "location": "", "min_score": 2},
                      "started_at": time.time(), "last_step_at": time.time()}
        db.set_onboarding_state(chat4, json.dumps(skip_state))
        with _CaptureBuilds() as cap:
            ob.handle_callback(
                tg, db, _make_cb("cb-skipcv", f"ob:{ob.CB_SKIP_CV}", chat4, 9011),
                chat4, 9011, ob.CB_SKIP_CV,
            )
        _assert(any("Setup complete" in s["text"] for s in tg.sent),
                "Skip-CV should finalize with the summary")
        _assert(cap.builds == [(chat4, "onboarding")],
                "Skip-CV finalize must still enqueue the profile build")
        _assert(db.get_onboarding_completed_at(chat4) is not None,
                "Skip-CV should stamp completion")

        # ---- 14. returning user with CV on file: last step shows Keep/Replace ----
        tg.reset()
        chat5 = 777_222
        db.upsert_user(chat5, first_name="Sam")
        db.set_resume(chat5, f"{td}/old_resume.pdf",
                      "Sam has 4 years of Python experience...")
        review_state = {"step": ob.STEP_RESUME,
                        "answers": {"seniority": "mid", "min_score": 3},
                        "started_at": time.time(), "last_step_at": time.time()}
        db.set_onboarding_state(chat5, json.dumps(review_state))
        ob.start(tg, db, chat5, first_name="Sam")   # resumes at the resume step
        review = tg.sent[-1]
        _assert("Your CV" in review["text"], "should render resume-review heading")
        # mdv2_escape backslash-escapes '_' and '.', so match the escaped form.
        _assert("old\\_resume\\.pdf" in review["text"],
                f"review prompt should name the existing CV, got: {review['text']!r}")
        labels = [b["text"] for row in review["reply_markup"]["inline_keyboard"] for b in row]
        _assert("Keep this CV" in labels, "should offer 'Keep this CV' button")

        # Tap "Keep this CV" → finalizes (resume is the last step now).
        tg.reset()
        with _CaptureBuilds() as cap:
            ob.handle_callback(
                tg, db, _make_cb("cb-keepcv", f"ob:{ob.CB_KEEP_CV}", chat5, review["message_id"]),
                chat5, review["message_id"], ob.CB_KEEP_CV,
            )
        _assert(any("Setup complete" in s["text"] for s in tg.sent),
                "Keep-CV should finalize with the summary")
        _assert(db.get_onboarding_completed_at(chat5) is not None,
                "Keep-CV should stamp completion")

        # ---- 14b. legacy in-flight wizard parked at STEP_MINSCORE still works ----
        # New users never reach this step, but wizards started before the
        # min-score question was removed may be parked here, with old 'ms:'
        # pickers live on screen. Both must keep working: a replay must not
        # crash, and an old picker tap must store the score and move on.
        tg.reset()
        chatL = 778_000
        db.upsert_user(chatL, first_name="Lee")
        legacy_ms = {"step": ob.STEP_MINSCORE,
                     "answers": {"seniority": "mid", "role": "Dev",
                                 "remote": "any", "location": "Berlin"},
                     "started_at": time.time(), "last_step_at": time.time()}
        db.set_onboarding_state(chatL, json.dumps(legacy_ms))
        # (a) replaying the prompt for a parked STEP_MINSCORE must not crash.
        ob._send_step_prompt(tg, db, chatL, ob.STEP_MINSCORE)
        _assert(any("Minimum match score" in s["text"] for s in tg.sent),
                "legacy STEP_MINSCORE replay should re-render the picker, not crash")
        # (b) tapping an old 'ms:4' picker stores the score and advances to resume.
        tg.reset()
        ob.handle_callback(
            tg, db, _make_cb("cb-legms", f"ob:{ob.CB_MIN_SCORE}:4", chatL, 9020),
            chatL, 9020, f"{ob.CB_MIN_SCORE}:4",
        )
        _assert(any("upload your CV" in s["text"] for s in tg.sent),
                "legacy ms pick should advance to the resume step")
        state = json.loads(db.get_onboarding_state(chatL))
        _assert(state["answers"]["min_score"] == 4, "legacy ms pick stored")
        _assert(state["step"] == ob.STEP_RESUME, "legacy ms pick advances to resume")

        # ---- 15. proactive nudge: one ping per stalled wizard, then silence ----
        tg.reset()
        chat6 = 777_333
        db.upsert_user(chat6, first_name="Kim")
        stalled = {"step": ob.STEP_WELCOME, "answers": {},   # legacy welcome-stuck user
                   "started_at": time.time() - 5 * 3600,
                   "last_step_at": time.time() - 5 * 3600}   # inside the nudge window
        db.set_onboarding_state(chat6, json.dumps(stalled))
        n = ob.nudge_stalled(tg, db)
        _assert(n == 1, f"exactly one user should be nudged, got {n}")
        _assert(any("Still with me" in s["text"] for s in tg.sent),
                "nudge should send the re-engagement ping")
        _assert(any("Seniority" in s["text"] for s in tg.sent),
                "nudge should replay the pending question (welcome→seniority)")
        state = json.loads(db.get_onboarding_state(chat6))
        _assert(state.get("nudged") is True, "nudged flag should persist")
        tg.reset()
        n = ob.nudge_stalled(tg, db)
        _assert(n == 0, f"second pass must not re-nudge, got {n}")
        # Too-fresh and too-old wizards are both left alone.
        chat7 = 777_444
        db.upsert_user(chat7)
        fresh = {"step": ob.STEP_ROLE, "answers": {},
                 "started_at": time.time(), "last_step_at": time.time()}
        db.set_onboarding_state(chat7, json.dumps(fresh))
        chat8 = 777_666
        db.upsert_user(chat8)
        ancient = {"step": ob.STEP_ROLE, "answers": {},
                   "started_at": 0.0, "last_step_at": 0.0}
        db.set_onboarding_state(chat8, json.dumps(ancient))
        tg.reset()
        n = ob.nudge_stalled(tg, db)
        _assert(n == 0, f"fresh + ancient wizards must not be nudged, got {n}")

        # ---- 18. localization: 'es' user gets translated welcome + button labels,
        # callback_data untouched; 'en'/None byte-identical; failure → English;
        # cache means a repeat send calls the translator zero extra times. ----
        _orig_llm_tr = ob._llm_translate
        tr_calls: list[tuple[str, str]] = []

        # Marker uses letters only — MarkdownV2 escapes '[]', so a bracketed
        # marker would survive in plain button labels but appear as '\[..\]' in
        # escaped prose. 'XXesXX' has no MDv2-special chars and reads identically
        # in both places.
        def _fake_llm_tr(text, lang):
            tr_calls.append((text, lang))
            return f"XX{lang}XX{text}"        # deterministic fake translation

        # (a) 'es' user: welcome copy + button labels translated, callback_data intact.
        ob._llm_translate = _fake_llm_tr
        chatES = 780_000
        db.upsert_user(chatES, first_name="Eva", language_code="es")
        tg.reset()
        ob.start(tg, db, chatES, first_name="Eva")
        welcome_es = tg.sent[1]
        _assert("XXesXX" in welcome_es["text"],
                "es welcome copy should be translated")
        # First name is user data — never sent through the translator.
        _assert("Eva" in welcome_es["text"], "es welcome must keep the untranslated name")
        es_labels = [b["text"] for row in welcome_es["reply_markup"]["inline_keyboard"] for b in row]
        _assert(all(l.startswith("XXesXX") for l in es_labels),
                f"es button labels should be translated, got {es_labels}")
        es_cbs = [b["callback_data"] for row in welcome_es["reply_markup"]["inline_keyboard"] for b in row]
        _assert(all(c.startswith("ob:") and "XXesXX" not in c for c in es_cbs),
                f"callback_data must stay untouched, got {es_cbs}")
        calls_after_first = len(tr_calls)
        _assert(calls_after_first > 0, "es welcome should have hit the translator")

        # (b) cache: re-sending the same step calls the translator ZERO extra times.
        tg.reset()
        ob._send_step_prompt(tg, db, chatES, ob.STEP_SENIORITY)
        _assert(len(tr_calls) == calls_after_first,
                f"cached strings must not re-hit the translator, "
                f"grew {calls_after_first}->{len(tr_calls)}")
        _assert(any("XXesXX" in s["text"] for s in tg.sent),
                "cached re-send must still render translated copy")

        # (c) 'en' and None users are byte-identical to no-translator output.
        for lang in ("en", "en-GB", None):
            chatEn = 780_100 + (hash(str(lang)) % 500)
            db.upsert_user(chatEn, first_name="Ed", language_code=lang)
            tg.reset()
            ob.start(tg, db, chatEn, first_name="Ed")
            got = tg.sent[1]["text"]
            expected = ob._welcome_mdv2("Ed")          # no tr → today's English
            _assert(got == expected,
                    f"lang={lang!r} welcome must be byte-identical to English")
            en_calls = len(tr_calls)
            _assert(en_calls == calls_after_first,
                    f"lang={lang!r} must make zero translator calls")

        # (d) translator failure → English exactly as today (fail-open, no cache write).
        ob._llm_translate = lambda text, lang: (_ for _ in ()).throw(RuntimeError("boom"))
        chatBoom = 781_000
        db.upsert_user(chatBoom, first_name="Bo", language_code="ru")
        tg.reset()
        ob.start(tg, db, chatBoom, first_name="Bo")
        boom_txt = tg.sent[1]["text"]
        _assert(boom_txt == ob._welcome_mdv2("Bo"),
                "translator exception → English welcome, byte-identical to today")

        ob._llm_translate = _orig_llm_tr

        print("PASS  — fresh flow (welcome=Q1, no min-score step, CV last), "
              "legacy min-score wizard, skip-CV, keep-CV, legacy start, "
              "cancel, resume, and proactive nudge")
        return 0


def _progress_dot_count(text: str) -> int:
    """Count filled dots in the rendered progress indicator. Uses the
    same '●' char the onboarding module emits."""
    return text.count("●")


if __name__ == "__main__":
    sys.exit(main())
