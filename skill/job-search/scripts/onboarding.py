"""Guided onboarding wizard.

The old /start shipped one dense welcome bubble and expected the user to
figure out the rest (upload PDF → /prefs → /minscore → wait). This module
replaces that with a multi-step wizard that advances one question at a time,
shows progress, and prefers inline-button answers over free text wherever
possible.

Steps (total: 6)
----------------
1. welcome    — "Let's get you set up" + [Get started] button.
2. resume     — wait for a PDF upload.
3. seniority  — inline buttons: Junior / Mid / Senior / Staff+ / Any.
4. role       — free text ("React Engineer", "DevRel", …) with [Skip].
5. remote     — inline buttons: Remote / Hybrid / Onsite / Any.
6. location   — free text with [Remote worldwide] shortcut.
7. minscore   — reuse the existing min-score keyboard with a recommendation.
                (This is step 6 of 6 — seniority+role share step 3; remote+
                location share step 4 conceptually but UX still one-per-screen.)
8. done       — summary + two buttons: "Try a search now" / "I'll wait for
                tomorrow". Kicks off the Opus profile rebuild in the background.

State is stored as JSON in `users.onboarding_state`. The bot reads it on every
dispatch to decide whether to intercept the message (during a wizard) or
route it normally (after completion).

Callback prefix: `ob:*`. See CALLBACKS table below for the full list.

Re-engagement
-------------
`maybe_resume(tg, db, chat_id)` inspects the stored state and, if it's stale
(> 6h) or the user has just re-sent /start, replays the last-pending question.
This catches the "uploaded CV, never set prefs" drop-off that the old flow
left hanging forever.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from db import DB
from telegram_client import (
    TelegramClient,
    ICON_JOB,
    ICON_LOCATION,
    ICON_REMOTE,
    ICON_SALARY,
    ICON_SENIORITY,
    mdv2_escape,
    min_score_keyboard,
    progress_dots,
    hr_mdv2,
)
import pig_stickers as _pigs

log = logging.getLogger(__name__)

# Pig mascot. Sprinkled into playful moments (welcome, setup complete,
# cancel acknowledgments, loading copy) but intentionally kept OFF the
# job cards — those stay clean + professional. Changing this constant
# (to "", "🐖", "🐽", etc.) re-skins every spot in one edit.
PIG = "🐷"

# ---------- step + callback codes ----------

STEP_WELCOME   = "welcome"
STEP_RESUME    = "resume"
STEP_SENIORITY = "seniority"
STEP_ROLE      = "role"
STEP_REMOTE    = "remote"
STEP_LOCATION  = "location"
STEP_MINSCORE  = "minscore"
STEP_DONE      = "done"

# Ordered for progress-dot rendering and re-entry. Welcome doesn't count
# toward the user-visible step number — progress starts ticking at resume.
_STEP_ORDER = [
    STEP_RESUME,     # 1
    STEP_SENIORITY,  # 2
    STEP_ROLE,       # 3
    STEP_REMOTE,     # 4
    STEP_LOCATION,   # 5
    STEP_MINSCORE,   # 6
]
TOTAL_STEPS = len(_STEP_ORDER)

# Awaiting-state codes used by bot.py to route free-text messages to the
# right step handler. Keyed off the existing `users.awaiting_state` column
# so we don't have to add a second plumbing path.
AWAIT_ONBOARDING_ROLE     = "onboarding_role"
AWAIT_ONBOARDING_LOCATION = "onboarding_location"

# Callback-data prefixes. Kept short (Telegram caps callback_data at 64 bytes).
#
#   ob:start            — user tapped "Get started" on the welcome screen
#   ob:sen:<level>      — junior | mid | senior | staff | any
#   ob:skiprole         — user tapped "Skip" on the role free-text step
#   ob:rmt:<policy>     — remote | hybrid | onsite | any
#   ob:locww            — shortcut for "Remote worldwide"
#   ob:ms:<n>           — min match score picked (0..5)
#   ob:runsearch        — final step: trigger a live search
#   ob:wait             — final step: "I'll wait for tomorrow"
#   ob:cancel           — abort the wizard at any point
CB_START       = "start"
CB_KEEP_CV     = "keepcv"     # resume step: user chose to keep the existing CV on file
CB_SENIORITY   = "sen"
CB_SKIP_ROLE   = "skiprole"
CB_REMOTE      = "rmt"
CB_LOC_WW      = "locww"
CB_MIN_SCORE   = "ms"
CB_RUN_SEARCH  = "runsearch"
CB_WAIT        = "wait"
CB_CANCEL      = "cancel"

# How long before a half-finished wizard is considered "cold" and we offer
# to resume it on the user's next interaction (rather than pretending
# nothing's in progress). 6h covers overnight drop-offs.
STALE_AFTER_SECONDS = 6 * 3600

_SENIORITY_LABELS = {
    "junior":    "Junior  (0–2 yrs)",
    "mid":       "Mid-level  (2–5 yrs)",
    "senior":    "Senior  (5–10 yrs)",
    "staff":     "Staff+  (10+ yrs)",
    "any":       "Any level",
}
_REMOTE_LABELS = {
    "remote": "Remote",
    "hybrid": "Hybrid",
    "onsite": "Onsite",
    "any":    "Any",
}


# ---------- state persistence helpers ----------

def _load(db: DB, chat_id: int) -> dict[str, Any] | None:
    raw = db.get_onboarding_state(chat_id)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _save(db: DB, chat_id: int, state: dict[str, Any]) -> None:
    state["last_step_at"] = time.time()
    db.set_onboarding_state(chat_id, json.dumps(state, ensure_ascii=False))


def _clear(db: DB, chat_id: int) -> None:
    db.set_onboarding_state(chat_id, None)


def _new_state() -> dict[str, Any]:
    now = time.time()
    return {
        "step": STEP_WELCOME,
        "answers": {},
        "started_at": now,
        "last_step_at": now,
    }


def is_in_progress(db: DB, chat_id: int) -> bool:
    """True if the user has an open wizard session (not yet completed / aborted)."""
    state = _load(db, chat_id)
    if not state:
        return False
    return state.get("step") not in (None, STEP_DONE)


def _step_index(step: str) -> int:
    """Position (1-based) of `step` in the numbered sequence.

    Returns 0 for the welcome screen (pre-flow).
    """
    try:
        return _STEP_ORDER.index(step) + 1
    except ValueError:
        return 0


def _progress_line(step: str) -> str:
    """Top-of-message progress indicator, or empty string for welcome."""
    idx = _step_index(step)
    if idx <= 0:
        return ""
    # MDv2: the step-count text contains no reserved chars, so no escape
    # needed. The dots are plain unicode.
    return f"`{progress_dots(idx, TOTAL_STEPS)}`"


# ---------- message builders ----------

def _welcome_mdv2(first_name: str | None = None) -> str:
    """Warm but businesslike welcome. Single pig + headline; body stays clean."""
    name = (first_name or "").strip()
    greeting = f"{PIG}  Welcome, {mdv2_escape(name)}" if name else f"{PIG}  Welcome"
    body = (
        "I'm a job-search assistant that scans LinkedIn, Indeed, Hacker News, "
        "and curated remote boards every morning, ranks postings against your "
        "resume, and delivers a shortlist here in Telegram.\n\n"
        "Setup takes about a minute. I'll ask:\n"
        "  1. Your CV (PDF)\n"
        "  2. Seniority level\n"
        "  3. Target role\n"
        "  4. Remote / hybrid / onsite\n"
        "  5. Location\n"
        "  6. Minimum match score\n\n"
        "You can skip individual questions — anything you don't answer falls "
        "back to sensible defaults."
    )
    return f"*{greeting}*\n\n" + mdv2_escape(body)


def _welcome_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "Get started →", "callback_data": f"ob:{CB_START}"}],
        [{"text": "Cancel", "callback_data": f"ob:{CB_CANCEL}"}],
    ]}


def _resume_prompt_mdv2() -> str:
    lines = [
        _progress_line(STEP_RESUME),
        "",
        "*Upload your CV*",
        "",
        mdv2_escape(
            "Send your resume as a PDF to this chat. I extract the text and "
            "use it to score each posting against your experience and to "
            "tailor per-role rewrites on demand.\n\n"
            "Your file stays on the bot server — it isn't shared with any "
            "third party. Use /cleardata at any time to wipe it."
        ),
    ]
    return "\n".join(lines)


def _resume_keyboard() -> dict:
    return {"inline_keyboard": [[
        {"text": "Cancel setup", "callback_data": f"ob:{CB_CANCEL}"},
    ]]}


def _resume_review_prompt_mdv2(resume_filename: str, word_count: int) -> str:
    """Shown when the user already has a CV on file.

    Re-asking someone to upload the same PDF is friction; silently skipping
    the step confuses them. Compromise: acknowledge the stored file, show
    its filename + parsed word count as a credibility check, and let them
    choose to keep it or upload a replacement.
    """
    lines = [
        _progress_line(STEP_RESUME),
        "",
        "*Your CV*",
        "",
        mdv2_escape(
            f"I already have a CV on file: {resume_filename} "
            f"({word_count} words parsed).\n\n"
            "Keep using this one, or upload a replacement PDF now? "
            "Uploading a new file overwrites the old one."
        ),
    ]
    return "\n".join(lines)


def _resume_review_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "Keep this CV", "callback_data": f"ob:{CB_KEEP_CV}"}],
        [{"text": "Cancel setup", "callback_data": f"ob:{CB_CANCEL}"}],
    ]}


def _seniority_prompt_mdv2() -> str:
    lines = [
        _progress_line(STEP_SENIORITY),
        "",
        f"{ICON_SENIORITY} *Seniority level*",
        "",
        mdv2_escape("Pick the bucket that best matches your experience."),
    ]
    return "\n".join(lines)


def _seniority_keyboard() -> dict:
    order = ["junior", "mid", "senior", "staff", "any"]
    rows: list[list[dict]] = []
    # Two-per-row layout for the four graded buckets, then "Any" solo.
    for i in range(0, 4, 2):
        pair = order[i:i+2]
        rows.append([
            {"text": _SENIORITY_LABELS[k], "callback_data": f"ob:{CB_SENIORITY}:{k}"}
            for k in pair
        ])
    rows.append([{"text": _SENIORITY_LABELS["any"], "callback_data": f"ob:{CB_SENIORITY}:any"}])
    rows.append([{"text": "Cancel setup", "callback_data": f"ob:{CB_CANCEL}"}])
    return {"inline_keyboard": rows}


def _role_prompt_mdv2() -> str:
    lines = [
        _progress_line(STEP_ROLE),
        "",
        f"{ICON_JOB} *Target role*",
        "",
        mdv2_escape(
            "Type the role title you want (e.g. 'React Engineer', 'Product "
            "Designer', 'DevRel'). Keep it short — I'll match against similar "
            "titles automatically.\n\n"
            "Tap Skip to let me infer the role from your CV."
        ),
    ]
    return "\n".join(lines)


def _role_keyboard() -> dict:
    return {"inline_keyboard": [[
        {"text": "Skip",         "callback_data": f"ob:{CB_SKIP_ROLE}"},
        {"text": "Cancel setup", "callback_data": f"ob:{CB_CANCEL}"},
    ]]}


def _remote_prompt_mdv2() -> str:
    lines = [
        _progress_line(STEP_REMOTE),
        "",
        f"{ICON_REMOTE} *Work style*",
        "",
        mdv2_escape("Which work arrangement are you open to?"),
    ]
    return "\n".join(lines)


def _remote_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": _REMOTE_LABELS["remote"], "callback_data": f"ob:{CB_REMOTE}:remote"},
         {"text": _REMOTE_LABELS["hybrid"], "callback_data": f"ob:{CB_REMOTE}:hybrid"}],
        [{"text": _REMOTE_LABELS["onsite"], "callback_data": f"ob:{CB_REMOTE}:onsite"},
         {"text": _REMOTE_LABELS["any"],    "callback_data": f"ob:{CB_REMOTE}:any"}],
        [{"text": "Cancel setup", "callback_data": f"ob:{CB_CANCEL}"}],
    ]}


def _location_prompt_mdv2() -> str:
    lines = [
        _progress_line(STEP_LOCATION),
        "",
        f"{ICON_LOCATION} *Location*",
        "",
        mdv2_escape(
            "Type the city, region, or country you want to work in — e.g. "
            "'Berlin', 'Spain', 'Bay Area', 'Remote EU'.\n\n"
            "Tap Remote worldwide if geography doesn't matter."
        ),
    ]
    return "\n".join(lines)


def _location_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "Remote worldwide",    "callback_data": f"ob:{CB_LOC_WW}"}],
        [{"text": "Cancel setup",         "callback_data": f"ob:{CB_CANCEL}"}],
    ]}


def _minscore_prompt_mdv2(current: int = 3) -> str:
    lines = [
        _progress_line(STEP_MINSCORE),
        "",
        "★ *Minimum match score*",
        "",
        mdv2_escape(
            "Each posting is scored 0–5 against your resume. Set the "
            "threshold below which I'll hide matches. Most users start at "
            "3+ — raise it once your digest feels noisy.\n\n"
            "You can change this any time from Settings."
        ),
    ]
    return "\n".join(lines)


def _summary_mdv2(answers: dict[str, Any]) -> str:
    """Rendered at the 'done' step — a clean recap of everything collected."""
    def _fmt(v: Any, dash: str = "—") -> str:
        s = (v or "").strip() if isinstance(v, str) else v
        return str(s) if s else dash

    seniority = _SENIORITY_LABELS.get(answers.get("seniority") or "any", "Any level")
    remote    = _REMOTE_LABELS.get(answers.get("remote") or "any", "Any")
    role      = _fmt(answers.get("role"), "(inferred from CV)")
    location  = _fmt(answers.get("location"), "(any)")
    min_score = int(answers.get("min_score") or 0)
    gate      = "Any score" if min_score == 0 else f"{min_score}+ / 5"

    body_lines = [
        f"{PIG}  *Setup complete*",
        "",
        f"{ICON_JOB} *Role*         {mdv2_escape(role)}",
        f"{ICON_SENIORITY} *Seniority*    {mdv2_escape(seniority)}",
        f"{ICON_REMOTE} *Work style*   {mdv2_escape(remote)}",
        f"{ICON_LOCATION} *Location*     {mdv2_escape(location)}",
        f"★ *Match gate*   {mdv2_escape(gate)}",
        "",
        hr_mdv2(),
        "",
        mdv2_escape(
            "I'm building your personalized profile in the background "
            "(~30–60 s). Your first daily digest lands at the scheduled "
            "time.\n\n"
            "Want a live preview right now? It scans all sources for you."
        ),
    ]
    return "\n".join(body_lines)


def _summary_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "Run a search now", "callback_data": f"ob:{CB_RUN_SEARCH}"}],
        [{"text": "I'll wait for tomorrow", "callback_data": f"ob:{CB_WAIT}"}],
    ]}


# ---------- prefs-string synthesis ----------
#
# The profile builder (profile_builder.py) consumes a free-text string plus
# the resume and produces the structured profile. The onboarding wizard
# collects a subset of that information via buttons — to keep a single
# source of truth for the builder, we concatenate the button answers into
# a natural-language paragraph and store it as prefs_free_text. The builder
# then does its normal job; nothing else needs to know onboarding exists.

def _synthesize_prefs(answers: dict[str, Any]) -> str:
    role      = (answers.get("role") or "").strip()
    seniority = answers.get("seniority") or ""
    remote    = answers.get("remote") or ""
    location  = (answers.get("location") or "").strip()

    parts: list[str] = []
    if seniority and seniority != "any":
        parts.append(f"{seniority}-level")
    if role:
        parts.append(role)
    else:
        parts.append("roles similar to my resume")

    tail: list[str] = []
    if remote == "remote":
        tail.append("remote only")
    elif remote == "hybrid":
        tail.append("hybrid work OK")
    elif remote == "onsite":
        tail.append("onsite is fine")
    # 'any' → no constraint mentioned
    if location:
        tail.append(f"location: {location}")

    sentence = " ".join(parts)
    if tail:
        sentence += ", " + ", ".join(tail)
    sentence += "."
    return sentence


# ---------- flow control ----------

def start(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    first_name: str | None = None,
    *,
    force: bool = False,
) -> None:
    """Entry point for /start.

    If the user has a completed onboarding AND we're not forcing a restart,
    fall through silently (the caller should handle that case — this module
    never re-onboards someone who's already done).

    If a half-finished wizard is still open, resume from the last-pending
    step rather than starting over.
    """
    existing = _load(db, chat_id)
    completed = db.get_onboarding_completed_at(chat_id)

    if completed and not force:
        # Caller decides what to do (usually: show the welcome-back and main menu).
        return

    if existing and existing.get("step") not in (None, STEP_DONE) and not force:
        # Resume from whatever step they left on.
        step = existing.get("step") or STEP_WELCOME
        if step == STEP_WELCOME:
            _send_welcome(tg, db, chat_id, first_name)
        else:
            _send_step_prompt(tg, db, chat_id, step)
        return

    # Fresh start (either brand new user, or forced restart after /cleardata all).
    state = _new_state()
    _save(db, chat_id, state)
    _send_welcome(tg, db, chat_id, first_name)


def maybe_resume(tg: TelegramClient, db: DB, chat_id: int) -> bool:
    """Nudge a stalled user back into the wizard. Returns True if we re-asked
    the current question (caller should stop processing). False if nothing
    was stalled.

    Called from bot.py when a user sends a message but has a stale in-progress
    wizard sitting in the DB. The common case: they uploaded a CV two days
    ago and never finished — we don't want to silently ignore random pings
    while their onboarding rots.
    """
    state = _load(db, chat_id)
    if not state or state.get("step") in (None, STEP_DONE):
        return False
    age = time.time() - float(state.get("last_step_at") or 0)
    if age < STALE_AFTER_SECONDS:
        return False
    step = state.get("step") or STEP_WELCOME
    try:
        tg.send_message(
            chat_id,
            mdv2_escape("Picking up where you left off — ")
            + "_" + mdv2_escape(f"(started {int(age/3600)}h ago)") + "_",
            reply_markup=None,
        )
    except Exception as e:
        log.debug("onboarding resume ping failed: %s", e)
    _send_step_prompt(tg, db, chat_id, step)
    return True


# ---------- per-step senders ----------

def _send_welcome(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    first_name: str | None,
) -> None:
    state = _load(db, chat_id) or _new_state()
    state["step"] = STEP_WELCOME
    _save(db, chat_id, state)
    # Greeting hierarchy: try a WAVE sticker from the pack first; fall back
    # to Telegram's built-in auto-animated 🐷 only when no sticker is
    # registered (or the send errors). This keeps the first pig on screen
    # consistent with the rest of the fat_roll_pigs once the registry is
    # populated, but degrades to the default animated emoji for a fresh
    # install.
    sent = False
    try:
        sent = _pigs.send_sticker(tg, chat_id, _pigs.WAVE)
    except Exception:
        log.debug("WAVE sticker send failed; falling back to Unicode", exc_info=True)
    if not sent:
        try:
            tg.send_plain(chat_id, PIG)
        except Exception:
            log.debug("animated-pig fallback send failed; continuing", exc_info=True)
    tg.send_message(
        chat_id,
        _welcome_mdv2(first_name),
        reply_markup=_welcome_keyboard(),
    )


def _send_step_prompt(tg: TelegramClient, db: DB, chat_id: int, step: str) -> None:
    """Send the prompt for `step`. Centralized so re-entry and first-send
    share identical copy + keyboards."""
    state = _load(db, chat_id) or _new_state()
    state["step"] = step
    _save(db, chat_id, state)

    if step == STEP_RESUME:
        db.set_awaiting_state(chat_id, None)  # resume upload has its own handler
        # If the user already has a CV on file, show the review prompt
        # (Keep / Upload new / Cancel) instead of demanding a fresh upload.
        # New users see the plain upload prompt.
        u = db.get_user(chat_id)
        has_resume = bool(u and u["resume_path"])
        if has_resume:
            # Pull a friendly filename + parsed word count for the
            # credibility check. Fallbacks keep us from crashing on a
            # malformed row.
            try:
                filename = Path(u["resume_path"]).name or "resume.pdf"
            except Exception:
                filename = "resume.pdf"
            try:
                word_count = len((u["resume_text"] or "").split())
            except Exception:
                word_count = 0
            tg.send_message(
                chat_id,
                _resume_review_prompt_mdv2(filename, word_count),
                reply_markup=_resume_review_keyboard(),
            )
        else:
            tg.send_message(chat_id, _resume_prompt_mdv2(), reply_markup=_resume_keyboard())
    elif step == STEP_SENIORITY:
        db.set_awaiting_state(chat_id, None)
        tg.send_message(chat_id, _seniority_prompt_mdv2(), reply_markup=_seniority_keyboard())
    elif step == STEP_ROLE:
        db.set_awaiting_state(chat_id, AWAIT_ONBOARDING_ROLE)
        tg.send_message(chat_id, _role_prompt_mdv2(), reply_markup=_role_keyboard())
    elif step == STEP_REMOTE:
        db.set_awaiting_state(chat_id, None)
        tg.send_message(chat_id, _remote_prompt_mdv2(), reply_markup=_remote_keyboard())
    elif step == STEP_LOCATION:
        db.set_awaiting_state(chat_id, AWAIT_ONBOARDING_LOCATION)
        tg.send_message(chat_id, _location_prompt_mdv2(), reply_markup=_location_keyboard())
    elif step == STEP_MINSCORE:
        db.set_awaiting_state(chat_id, None)
        # Reuse the existing min-score keyboard but wrap it in an
        # onboarding-flavoured prompt. Callbacks come back as `ms:<n>` —
        # bot.py routes those to our handler when the user is mid-wizard.
        tg.send_message(
            chat_id,
            _minscore_prompt_mdv2(current=3),
            reply_markup=min_score_keyboard(current=3),
        )
    elif step == STEP_DONE:
        state = _load(db, chat_id) or {}
        tg.send_message(
            chat_id,
            _summary_mdv2(state.get("answers") or {}),
            reply_markup=_summary_keyboard(),
        )
    else:
        log.warning("unknown onboarding step: %s", step)


# ---------- callback handler ----------

def handle_callback(
    tg: TelegramClient,
    db: DB,
    cb: dict,
    chat_id: int,
    msg_id: int,
    payload: str,
    *,
    on_complete: Callable[[int], None] | None = None,
    on_run_search: Callable[[int], None] | None = None,
) -> None:
    """Dispatch a `ob:*` callback.

    `payload` is everything AFTER `ob:`. For leaf buttons like `ob:start` it
    holds the single token ('start'); for nested ones like `ob:sen:senior`
    it holds 'sen:senior'.

    `on_complete` is invoked after the wizard finishes and the profile
    rebuild has been scheduled. Callers use it to kick the main-menu
    keyboard back onto the user's screen.

    `on_run_search` is invoked when the user taps 'Run a search now' on the
    summary screen. The callback runs `trigger_job_check` in the host
    process.
    """
    cb_id = cb["id"]
    # Split at most once so 'sen:senior' becomes ('sen', 'senior') but
    # 'start' stays intact.
    if ":" in payload:
        code, arg = payload.split(":", 1)
    else:
        code, arg = payload, ""

    state = _load(db, chat_id)
    # These codes must still work AFTER finalize cleared the state: the
    # summary screen's "Run a search now" / "I'll wait for tomorrow" live
    # past the wizard, and CB_START is the entry point (state may be absent
    # before the very first welcome render). Everything else bails when
    # state is missing — otherwise stale inline buttons from a prior
    # session would silently mutate a cleared user.
    _POST_FINALIZE_OK = {CB_START, CB_RUN_SEARCH, CB_WAIT, CB_CANCEL}
    if state is None and code not in _POST_FINALIZE_OK:
        tg.answer_callback(cb_id, "")
        return

    # --- CANCEL ---
    if code == CB_CANCEL:
        _clear(db, chat_id)
        db.set_awaiting_state(chat_id, None)
        try:
            tg.edit_message_text(
                chat_id, msg_id,
                f"{PIG}  " + mdv2_escape(
                    "Setup cancelled. Run /start any time to try again."
                ),
                reply_markup={"inline_keyboard": []},
            )
        except Exception as e:
            log.debug("onboarding cancel edit failed: %s", e)
        tg.answer_callback(cb_id, "Cancelled")
        return

    # --- START (from welcome screen) ---
    if code == CB_START:
        if state is None:
            state = _new_state()
        # Always route through STEP_RESUME — the step renderer itself
        # decides whether to show the upload prompt (new user) or the
        # keep/replace review prompt (returning user with a CV on file).
        # Silently skipping made the welcome bubble's "1. Your CV" promise
        # feel broken for users with a pre-existing resume.
        state["step"] = STEP_RESUME
        _save(db, chat_id, state)
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _send_step_prompt(tg, db, chat_id, STEP_RESUME)
        tg.answer_callback(cb_id, "")
        return

    # --- KEEP EXISTING CV (from resume-review step) ---
    if code == CB_KEEP_CV:
        # User already has a CV and chose to keep it. Same forward motion
        # as handle_resume_uploaded, just without the file-save side effect.
        state["step"] = STEP_SENIORITY
        _save(db, chat_id, state)
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _send_step_prompt(tg, db, chat_id, STEP_SENIORITY)
        tg.answer_callback(cb_id, "Keeping your CV")
        return

    # --- SENIORITY ---
    if code == CB_SENIORITY:
        level = arg if arg in _SENIORITY_LABELS else "any"
        state["answers"]["seniority"] = level
        state["step"] = STEP_ROLE
        _save(db, chat_id, state)
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _send_step_prompt(tg, db, chat_id, STEP_ROLE)
        tg.answer_callback(cb_id, _SENIORITY_LABELS[level])
        return

    # --- SKIP ROLE ---
    if code == CB_SKIP_ROLE:
        state["answers"]["role"] = ""
        state["step"] = STEP_REMOTE
        _save(db, chat_id, state)
        db.set_awaiting_state(chat_id, None)
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _send_step_prompt(tg, db, chat_id, STEP_REMOTE)
        tg.answer_callback(cb_id, "Skipped")
        return

    # --- REMOTE ---
    if code == CB_REMOTE:
        policy = arg if arg in _REMOTE_LABELS else "any"
        state["answers"]["remote"] = policy
        state["step"] = STEP_LOCATION
        _save(db, chat_id, state)
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _send_step_prompt(tg, db, chat_id, STEP_LOCATION)
        tg.answer_callback(cb_id, _REMOTE_LABELS[policy])
        return

    # --- LOCATION: shortcut "Remote worldwide" ---
    if code == CB_LOC_WW:
        state["answers"]["location"] = "Remote worldwide"
        state["step"] = STEP_MINSCORE
        _save(db, chat_id, state)
        db.set_awaiting_state(chat_id, None)
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _send_step_prompt(tg, db, chat_id, STEP_MINSCORE)
        tg.answer_callback(cb_id, "Remote worldwide")
        return

    # --- RUN SEARCH / WAIT on the summary screen ---
    if code == CB_RUN_SEARCH:
        # The actual search trigger lives in bot.py (trigger_job_check) to
        # keep this module free of the search_jobs dependency. We just
        # signal via the callback.
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        tg.answer_callback(cb_id, "Running a search now…")
        if on_run_search is not None:
            try:
                on_run_search(chat_id)
            except Exception:
                log.exception("on_run_search hook crashed")
        return

    if code == CB_WAIT:
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        tg.answer_callback(cb_id, "See you tomorrow")
        return

    # --- MIN SCORE ---
    # Note: min-score uses the shared 'ms:<n>' callback prefix from
    # telegram_client.min_score_keyboard. bot.py routes 'ms:' callbacks to
    # our handler only when the user is mid-onboarding; after onboarding,
    # the same prefix routes to the standalone min-score handler.
    if code == CB_MIN_SCORE:
        try:
            score = max(0, min(5, int(arg)))
        except (TypeError, ValueError):
            tg.answer_callback(cb_id, "")
            return
        state["answers"]["min_score"] = score
        state["step"] = STEP_DONE
        _save(db, chat_id, state)
        # Strip the picker keyboard in place so the user can't change it
        # post-commit without a fresh Settings visit.
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _finalize(tg, db, chat_id, on_complete=on_complete)
        tg.answer_callback(cb_id, f"Gate set to {'any' if score == 0 else f'{score}+/5'}")
        return

    tg.answer_callback(cb_id, "")


# ---------- free-text handlers ----------

def handle_text_role(tg: TelegramClient, db: DB, chat_id: int, text: str) -> bool:
    """Consume a free-text role title. Returns True if consumed."""
    state = _load(db, chat_id)
    if not state or state.get("step") != STEP_ROLE:
        return False
    role = (text or "").strip()[:120]
    state["answers"]["role"] = role
    state["step"] = STEP_REMOTE
    _save(db, chat_id, state)
    db.set_awaiting_state(chat_id, None)
    _send_step_prompt(tg, db, chat_id, STEP_REMOTE)
    return True


def handle_text_location(tg: TelegramClient, db: DB, chat_id: int, text: str) -> bool:
    """Consume a free-text location. Returns True if consumed."""
    state = _load(db, chat_id)
    if not state or state.get("step") != STEP_LOCATION:
        return False
    location = (text or "").strip()[:120]
    if not location:
        # Empty input — re-prompt rather than advance.
        _send_step_prompt(tg, db, chat_id, STEP_LOCATION)
        return True
    state["answers"]["location"] = location
    state["step"] = STEP_MINSCORE
    _save(db, chat_id, state)
    db.set_awaiting_state(chat_id, None)
    _send_step_prompt(tg, db, chat_id, STEP_MINSCORE)
    return True


def handle_resume_uploaded(tg: TelegramClient, db: DB, chat_id: int) -> bool:
    """Called by bot.py AFTER a successful resume save, while mid-wizard.

    Returns True if we advanced the wizard (the caller should suppress its
    own "resume saved" confirmations in favor of ours).
    """
    state = _load(db, chat_id)
    if not state:
        return False
    if state.get("step") != STEP_RESUME:
        # The user uploaded a CV outside the wizard's resume step — leave
        # state alone and let the caller do its normal thing.
        return False
    state["step"] = STEP_SENIORITY
    _save(db, chat_id, state)
    try:
        tg.send_message(
            chat_id,
            f"{PIG}  " + mdv2_escape("Got your CV — sniffed through it in a second. Next:"),
        )
    except Exception:
        pass
    _send_step_prompt(tg, db, chat_id, STEP_SENIORITY)
    return True


# ---------- finalization ----------

def _finalize(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    *,
    on_complete: Callable[[int], None] | None = None,
) -> None:
    """Persist the synthesized prefs string, apply the min-score, stamp the
    completion timestamp, and kick the profile rebuild."""
    state = _load(db, chat_id) or {}
    answers = state.get("answers") or {}

    # Synthesize a natural-language prefs string so the profile builder sees
    # a unified input regardless of whether the user came via /prefs or the
    # wizard.
    prefs_text = _synthesize_prefs(answers)
    try:
        db.set_prefs_free_text(chat_id, prefs_text)
    except Exception:
        log.exception("finalize: set_prefs_free_text failed")

    # Persist the min-score via the profile-level helper so the score
    # survives a subsequent profile rebuild the same way /minscore does.
    try:
        from user_profile import profile_from_json, profile_to_json, set_min_match_score
        profile = profile_from_json(db.get_user_profile(chat_id))
        new_profile = set_min_match_score(profile, int(answers.get("min_score") or 0))
        db.set_user_profile(chat_id, profile_to_json(new_profile))
    except Exception:
        log.exception("finalize: persist min_match_score failed")

    # Stamp completion BEFORE the summary send so the next /start falls
    # through to the welcome-back flow even if the summary send fails.
    try:
        db.mark_onboarding_complete(chat_id)
    except Exception:
        log.exception("finalize: mark_onboarding_complete failed")

    # Summary card + final buttons.
    tg.send_message(
        chat_id,
        _summary_mdv2(answers),
        reply_markup=_summary_keyboard(),
    )

    if on_complete is not None:
        try:
            on_complete(chat_id)
        except Exception:
            log.exception("on_complete hook crashed")


# ---------- utility: is the user expecting an onboarding text input? ----------

def current_await_state(db: DB, chat_id: int) -> str | None:
    """Return the AWAIT_* constant if the user's awaiting_state is one of
    ours, else None. Useful for bot.py's dispatch to decide whether to
    route a text message here."""
    raw = db.get_awaiting_state(chat_id)
    if raw in (AWAIT_ONBOARDING_ROLE, AWAIT_ONBOARDING_LOCATION):
        return raw
    return None
