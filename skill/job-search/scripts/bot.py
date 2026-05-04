#!/usr/bin/env python3
"""Long-running Telegram bot: /start onboarding, resume upload, and button callbacks.

Architecture
------------
Runs a blocking long-poll loop against Telegram's getUpdates. Handles:

  • /start, /help                — greet + instructions + persistent reply keyboard
  • /jobs, "🔍 Check for jobs now"  — run the search pipeline on demand for this user
  • /applied, "📋 My applications" — list jobs the user has marked applied
  • Document uploads (PDF)       — save resume to state/users/<chat_id>/resume.pdf,
                                   extract text, stash in DB
  • callback_query a:<job_id>    — mark job applied, update the message's keyboard
  • callback_query n:<job_id>    — mark job skipped, update keyboard
  • callback_query r:<job_id>    — kick off the AI tailor; show suggestions + Apply/Dismiss
  • callback_query ra:<job_id>   — Apply the stored plan; send rewritten resume as a file
  • callback_query rd:<job_id>   — Dismiss the suggestions; clear plan

Run it:
    python skill/job-search/scripts/bot.py

Stop with Ctrl-C. Only one instance should run at a time (getUpdates is
single-consumer).
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

# Make sibling modules importable regardless of CWD.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from db import DB                         # noqa: E402
from dedupe import Job                    # noqa: E402
from resume_tailor import (               # noqa: E402
    extract_pdf_text,
    build_tailor_note,
    build_tailor_plan_ai,
)
from telegram_client import (             # noqa: E402
    TelegramClient,
    format_job_mdv2,
    job_keyboard,
    mdv2_escape,
    min_score_keyboard,
    suggestions_keyboard,
    render_suggestions_mdv2,
    clean_data_menu_keyboard,
    clean_data_confirm_keyboard,
    digest_header_keyboard,
    send_per_job_digest,
    CLEAN_DATA_KINDS,
)
from user_profile import (                # noqa: E402
    profile_from_json,
    profile_to_json,
    format_profile_summary_mdv2,
    set_min_match_score,
)
from safety_check import check_user_input  # noqa: E402
import profile_builder as _profile_builder  # noqa: E402
import onboarding as _onboarding            # noqa: E402
import pig_stickers as _pigs                # noqa: E402
import fit_analyzer as _fit                 # noqa: E402
from telemetry import MonitorStore          # noqa: E402
from instrumentation import error_capture  # noqa: E402
from ops.commands import handle_operator_command as _ops_handle_command  # noqa: E402
from ops.alerts import deliver_alert as _ops_deliver_alert  # noqa: E402
import json as _json
import shutil as _shutil

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

log = logging.getLogger("bot")

PROJECT_ROOT = HERE.parent.parent.parent
STATE_DIR = PROJECT_ROOT / os.environ.get("STATE_DIR", "state")
USERS_DIR = STATE_DIR / "users"
DB_PATH = STATE_DIR / "jobs.db"

# Lazy MonitorStore singleton — the operator-command shim and any future
# instrumentation hookups read this. First call constructs it; the DB
# instance is reused thereafter.
_STORE: MonitorStore | None = None


def _get_store(db: DB) -> MonitorStore:
    global _STORE
    if _STORE is None:
        _STORE = MonitorStore(db)
    return _STORE


# Pig mascot — sprinkled into celebratory / conversational moments (welcome
# back, cancelled, setup complete, loading copy). Kept OFF the job cards
# so the daily digest reads as a professional shortlist. Change this
# constant to "", "🐖", "🐽" to re-skin every spot in one place.
PIG = "🐷"

BTN_CHECK_NOW  = "🔍  Search"
BTN_MY_APPS    = "📋  Applied"
BTN_PROFILE    = "👤  Profile"
BTN_RESEARCH   = "🔬  Research"
BTN_SETTINGS   = "⚙️  Settings"
BTN_CANCEL     = "✕  Cancel"

# Legacy button labels — some existing users may still have old persistent
# keyboards with these labels cached. We text-match them too so they don't
# dead-end. After a few weeks this list can be pruned.
_LEGACY_BUTTONS = {
    "🔍 Check for jobs now": "search",
    "📋 My applications":     "applied",
    "🎯 Set preferences":     "prefs",
    "⭐ Min match score":     "minscore",
    "🧹 Clean my data":       "cleandata",
    "✖️ Cancel":              "cancel",
}

# Persistent reply keyboard under the input field. Five primary verbs on
# three rows — every row is balanced, no row has a lone destructive action.
# Settings aggregates the lower-frequency surfaces (preferences, min score,
# rebuild, clean data) into a single inline menu so the persistent keyboard
# stays uncluttered.
REPLY_KEYBOARD: dict = {
    "keyboard": [
        [{"text": BTN_CHECK_NOW}, {"text": BTN_MY_APPS}],
        [{"text": BTN_PROFILE},   {"text": BTN_RESEARCH}],
        [{"text": BTN_SETTINGS}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

# Shown while we're waiting for the user's free-form preferences message.
# A single-button keyboard that lets them back out without sending random text.
PREFS_INPUT_KEYBOARD: dict = {
    "keyboard": [[{"text": BTN_CANCEL}]],
    "resize_keyboard": True,
    "one_time_keyboard": False,
    "is_persistent": True,
}

PREFS_PROMPT = (
    "*Preferences*\n\n"
    + mdv2_escape(
        "Describe what you're looking for in plain English. The more "
        "specific, the better the matches.\n\n"
        "Examples:\n"
        "  •  Remote only jobs in Canada\n"
        "  •  Onsite in Madrid, Spanish or English, no WordPress\n"
        "  •  Remote EU, React + TypeScript, min 80k USD\n\n"
        "Your wording is combined with your resume to rebuild your profile "
        "(roles, locations, remote policy, salary, seniority, language) and "
        "tailor search seeds for each source. Anything you mention overrides "
        "the global defaults for your digest only.\n\n"
        "Send ✕ Cancel to abort, or /clearprefs to reset."
    )
)

# Settings sub-menu. Opened from the persistent "⚙️ Settings" button. Each
# row is a single inline option so the menu reads as a scannable list on
# narrow screens. Callback-data prefix is `st:`; see _handle_settings_cb.
SETTINGS_MENU_BODY = (
    "*Settings*\n\n"
    + mdv2_escape(
        "Pick what you want to tweak. None of these start background jobs "
        "until you confirm on the next screen."
    )
)


def settings_keyboard() -> dict:
    return {"inline_keyboard": [
        [{"text": "Preferences",          "callback_data": "st:prefs"}],
        [{"text": "Min match score",      "callback_data": "st:minscore"}],
        [{"text": "Rebuild profile",      "callback_data": "st:rebuild"}],
        [{"text": "Clean my data",        "callback_data": "st:cleandata"}],
        [{"text": "Close",                "callback_data": "st:close"}],
    ]}


def _show_settings_menu(tg: TelegramClient, db: DB, chat_id: int) -> None:
    db.upsert_user(chat_id)
    tg.send_message(chat_id, SETTINGS_MENU_BODY, reply_markup=settings_keyboard())

STATE_AWAITING_PREFS = "awaiting_prefs"
STATE_AWAITING_RESEARCH_LOCATION = "awaiting_research_location"
# Set after the user taps "🚫 Not applied" on a job card. The bot then asks
# them why this posting didn't fit; the next text message becomes free-text
# feedback that `skip_feedback.apply_skip_feedback` parses into profile
# exclusions. Gated behind SKIP_FEEDBACK_ENABLED so the prompt is opt-in
# during the rollout.
STATE_AWAITING_SKIP_REASON = "awaiting_skip_reason"

# Opt-in flag for the skip-reason follow-up prompt. Default OFF so existing
# users don't suddenly get an extra question after every skip; ramp by setting
# SKIP_FEEDBACK_ENABLED=1 in the bot's environment.
def _skip_feedback_enabled() -> bool:
    return os.environ.get("SKIP_FEEDBACK_ENABLED", "0") not in ("0", "false", "False", "")


# Prompt for the skip-reason capture flow. We use Telegram's `ForceReply`
# markup so the user's input field auto-focuses with a placeholder hint —
# a regular ReplyKeyboardMarkup would REPLACE the user's keyboard with
# just "✕ Cancel", hiding the text input and confusing the user (the
# screenshot bug from 2026-04-30). With ForceReply they see their own
# typing keyboard + a clear "type your reason here" placeholder.
SKIP_REASON_PROMPT_MDV2 = (
    "❓ *Why didn't this fit?*\n\n"
    + mdv2_escape(
        "Tell me in plain English (role / stack / location / seniority / "
        "company / anything else). An AI will parse your reply, update your "
        "profile, and your NEXT digest will filter similar postings out "
        "automatically. Send "
    )
    + "`skip`"
    + mdv2_escape(" to dismiss this question.")
)

# Inline keyboard with a Skip button beneath the "Why?" prompt. Telegram
# does NOT allow combining `force_reply` and `inline_keyboard` on the same
# message, so we pick the inline button — it's tappable and visible right
# below the prompt, which the user explicitly asked for. Trade-off: input
# field doesn't auto-focus; user taps the message box to type. Acceptable
# UX since the prompt body says "Type a short reason in the message box
# below" so the input action is signposted.
SKIP_REASON_INLINE_KB: dict = {
    "inline_keyboard": [[{"text": "✕ Skip", "callback_data": "sr:skip"}]],
}


# URL where the full privacy policy is hosted. If you haven't published
# it online yet, leave this as None and the /privacy command will show
# only the in-chat summary — users can still email the operator for the
# full text. When you host the Markdown file (e.g. on GitHub), set the env
# var PRIVACY_POLICY_URL and the link appears in the chat message.
PRIVACY_POLICY_URL = (os.environ.get("PRIVACY_POLICY_URL") or "").strip()

# Operator contact shown in /privacy. Set OPERATOR_CONTACT in .env.
OPERATOR_CONTACT = (os.environ.get("OPERATOR_CONTACT") or "").strip()

PRIVACY_BODY_MDV2 = (
    "*Privacy — short version*\n\n"
    + mdv2_escape(
        "• Your résumé, preferences, profile, and application history live "
        "on the bot's server. Nothing is sold or shared for advertising.\n"
        "• When you tap 'Tailor resume', 'Analyze fit', or run "
        "/marketresearch, the relevant excerpts of your résumé and the "
        "posting are sent to Anthropic (Claude) to generate the result.\n"
        "• Job postings come from LinkedIn, Indeed, Hacker News, and "
        "curated remote boards — the bot only reads from them, it does "
        "not send them anything about you.\n"
        "• You can delete any part of your data any time with /cleardata."
        + (f"\n• Operator contact: {OPERATOR_CONTACT}" if OPERATOR_CONTACT else "")
    )
)


def _privacy_mdv2() -> str:
    """Assemble the /privacy response. Appends a link to the full policy
    when PRIVACY_POLICY_URL is configured, otherwise just sends the summary."""
    body = PRIVACY_BODY_MDV2
    if PRIVACY_POLICY_URL:
        # URL chars that are MDv2-reserved inside an inline link: `(`, `)`.
        url = PRIVACY_POLICY_URL.replace("(", "\\(").replace(")", "\\)")
        body += "\n\n" + mdv2_escape("Full policy: ") + f"[read here]({url})"
    else:
        body += "\n\n" + mdv2_escape(
            "Full policy: ask the operator by email (link not configured)."
        )
    return body


# Shown when a user who has already completed onboarding sends /start or
# /help — a brief command reference + the main-menu keyboard. Pre-onboarding
# users go through the guided wizard instead (see onboarding.start).
HELP_MDV2 = (
    "*Job Alert — command reference*\n\n"
    + mdv2_escape(
        "Day-to-day use is via the buttons below the text field. If you "
        "prefer typing, these commands do the same work:\n"
        "\n"
        "  /jobs            run a search now\n"
        "  /applied         list roles you've marked applied\n"
        "  /myprofile       show your current profile\n"
        "  /prefs           rewrite your preferences in plain English\n"
        "  /minscore        set the match-score filter\n"
        "  /rebuildprofile  force a fresh profile rebuild\n"
        "  /marketresearch  run a deep market scan (~25-40 min, .docx)\n"
        "  /cleardata       delete resume, history, profile, or everything\n"
        "  /privacy         what data is stored and who sees it\n"
        "  /start           re-run the setup wizard"
    )
)

# Short welcome-back bubble for users who already completed onboarding.
def _welcome_back_mdv2(first_name: str | None = None) -> str:
    greet = f"Welcome back, {mdv2_escape((first_name or '').strip())}" \
        if (first_name or "").strip() else "Welcome back"
    return (
        f"{PIG}  *{greet}*\n\n"
        + mdv2_escape(
            "Your profile is already set up. Use the buttons below to run a "
            "search, review applications, or tweak settings. Send /help to "
            "see every command."
        )
    )


def load_env():
    if load_dotenv:
        load_dotenv(PROJECT_ROOT / ".env")


def user_dir(chat_id: int) -> Path:
    d = USERS_DIR / str(chat_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------- handlers ----------

def handle_command(tg: TelegramClient, db: DB, chat_id: int, text: str, user: dict) -> None:
    # Operator-command shim runs first. For the OPERATOR_CHAT_ID it handles
    # /health, /stats, /alerts, /runlog and short-circuits the rest of the
    # dispatcher. For every other chat it returns False (silent ghost) and
    # the regular dispatcher below runs as usual. Note: /stats overlaps with
    # the legacy `_show_admin_stats` path — operator-mode wins.
    try:
        if _ops_handle_command(tg, _get_store(db), chat_id, text):
            return
    except Exception:
        log.exception("operator-command shim raised; falling through")
    cmd = text.split()[0].lower()
    if cmd == "/start":
        db.upsert_user(
            chat_id,
            username=user.get("username"),
            first_name=user.get("first_name"),
            last_name=user.get("last_name"),
        )
        first_name = (user or {}).get("first_name")
        completed_at = db.get_onboarding_completed_at(chat_id)
        if completed_at:
            # Returning user — skip the wizard, show a welcome-back bubble +
            # the main-menu keyboard. They can re-run the wizard explicitly
            # via the Settings > "Re-run setup" path (not wired yet) or via
            # /cleardata → everything, which triggers a fresh /start flow.
            tg.send_message(chat_id, _welcome_back_mdv2(first_name), reply_markup=REPLY_KEYBOARD)
        else:
            # Fresh user (or mid-flow user) — hand off to the onboarding
            # wizard. It takes over the conversation until the user either
            # cancels or finishes. The persistent reply keyboard is
            # intentionally NOT sent here — the wizard uses inline keyboards
            # per-step, and attaching the reply keyboard would make it hard
            # to distinguish "active question" from "idle menu". It gets
            # attached on completion.
            _onboarding.start(tg, db, chat_id, first_name=first_name)
    elif cmd == "/help":
        db.upsert_user(
            chat_id,
            username=user.get("username"),
            first_name=user.get("first_name"),
            last_name=user.get("last_name"),
        )
        tg.send_message(chat_id, HELP_MDV2, reply_markup=REPLY_KEYBOARD)
    elif cmd in ("/jobs", "/checknow", "/check"):
        trigger_job_check(tg, db, chat_id)
    elif cmd in ("/applied", "/status"):
        _send_applied_list(tg, db, chat_id)
    elif cmd in ("/prefs", "/preferences", "/setprefs"):
        _ask_for_prefs(tg, db, chat_id)
    elif cmd in ("/clearprefs", "/resetprefs"):
        _clear_prefs(tg, db, chat_id)
    elif cmd in ("/minscore", "/score", "/rating"):
        _ask_min_score(tg, db, chat_id)
    elif cmd in ("/myprofile", "/profile", "/myprefs", "/showprefs"):
        _show_profile(tg, db, chat_id)
    elif cmd in ("/rebuildprofile", "/rebuild"):
        _rebuild_profile(tg, db, chat_id)
    elif cmd in ("/marketresearch", "/research", "/mr"):
        _start_market_research(tg, db, chat_id)
    elif cmd in ("/cleardata", "/reset", "/mydata", "/wipe"):
        _ask_clean_data(tg, db, chat_id)
    elif cmd in ("/privacy", "/privacypolicy", "/datapolicy"):
        db.upsert_user(
            chat_id,
            username=user.get("username"),
            first_name=user.get("first_name"),
            last_name=user.get("last_name"),
        )
        tg.send_message(chat_id, _privacy_mdv2(), reply_markup=REPLY_KEYBOARD)
    elif cmd in ("/stats", "/adminstats"):
        _show_admin_stats(tg, db, chat_id)
    else:
        tg.send_plain(chat_id, "Unknown command. Try /help.")


# ---------- min-match-score picker ----------

def _current_min_score(db: DB, chat_id: int) -> int:
    profile = profile_from_json(db.get_user_profile(chat_id))
    return int((profile or {}).get("min_match_score") or 0)


def _min_score_prompt_mdv2(current: int) -> str:
    if current <= 0:
        status = mdv2_escape("Currently showing all matches (no minimum).")
    else:
        stars = "⭐" * current + "☆" * (5 - current)
        status = f"Currently showing *{mdv2_escape(f'{current}+ / 5')}* matches only  ·  {stars}"
    return (
        "⭐ *Filter by match score*\n\n"
        + status + "\n\n"
        + mdv2_escape(
            "Pick the minimum score for new postings. Scores come from the "
            "AI enrichment pass that compares each posting to your resume. "
            "Jobs without a score are treated as 0, so a high minimum will "
            "hide any job the AI couldn't score."
        )
    )


def _ask_min_score(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Send the inline-keyboard picker. Tapping a tier fires callback `ms:<n>`."""
    db.upsert_user(chat_id)
    current = _current_min_score(db, chat_id)
    tg.send_message(
        chat_id,
        _min_score_prompt_mdv2(current),
        reply_markup=min_score_keyboard(current=current),
    )


def _handle_filter_button(
    tg: TelegramClient,
    db: DB,
    cb: dict,
    chat_id: int,
    msg_id: int,
    payload: str,
) -> None:
    """Dispatch the ⬇ / ⬆ digest-header buttons.

    Payload shapes:
      ``lwr:<run_id>:<new_floor>`` — replay cached unsent jobs with score
        ≥ new_floor for this run (inclusive-upward, matches the (+M) on the
        button), append to chat (no new header), and persist new_floor onto
        the user profile so the next digest also uses it.
      ``rse:<new_floor>`` — only update profile.min_match_score; the in-chat
        digest is unchanged.
    """
    cb_id = cb["id"]
    parts = (payload or "").split(":")
    if not parts or not parts[0]:
        tg.answer_callback(cb_id, "Invalid filter button.")
        return
    sub = parts[0]

    if sub == "rse":
        try:
            new_floor = int(parts[1])
        except (IndexError, ValueError):
            tg.answer_callback(cb_id, "Invalid floor.")
            return
        new_floor = max(0, min(5, new_floor))
        profile = profile_from_json(db.get_user_profile(chat_id))
        db.set_user_profile(chat_id, profile_to_json(set_min_match_score(profile, new_floor)))
        try:
            kb = digest_header_keyboard(run_id=None, current_floor=new_floor, lower_count=0)
            tg.edit_reply_markup(chat_id, msg_id, kb or {"inline_keyboard": []})
        except Exception:
            log.debug("flt:rse edit_reply_markup failed; continuing", exc_info=True)
        tg.answer_callback(cb_id, f"Next digest will use ≥{new_floor}/5")
        return

    if sub == "lwr":
        try:
            run_id = int(parts[1])
            new_floor = int(parts[2])
        except (IndexError, ValueError):
            tg.answer_callback(cb_id, "Invalid filter button.")
            return
        new_floor = max(0, min(5, new_floor))
        cached = db.fetch_unsent_at_score(chat_id, run_id, new_floor)
        if not cached:
            tg.answer_callback(cb_id, "No more postings at that score.")
            return
        jobs: list[Job] = []
        enrichments: dict[str, dict] = {}
        for jid, score, enr_json in cached:
            job_row = db.get_job(jid)
            j = _row_to_job(job_row)
            if j is None:
                continue
            try:
                enr = _json.loads(enr_json) if enr_json else {"match_score": score}
            except (TypeError, ValueError):
                enr = {"match_score": score}
            if not isinstance(enr, dict):
                enr = {"match_score": score}
            jobs.append(j)
            enrichments[jid] = enr
        if not jobs:
            tg.answer_callback(cb_id, "Postings expired from cache.")
            return

        sent_ids: list[str] = []
        def _on_sent_replay(mid, j, _cid=chat_id, _sink=sent_ids):
            db.log_sent(_cid, mid, j.job_id)
            _sink.append(j.job_id)

        # Replay cards inherit the same snippet config as the live digest;
        # callers can flip these via env if they ever need to.
        cfg = {"message": {"include_snippet": True, "snippet_chars": 240}}
        try:
            send_per_job_digest(
                tg, chat_id, jobs, cfg,
                on_sent=_on_sent_replay,
                enrichments=enrichments,
                min_score=new_floor,
                run_id=run_id,
                skip_header=True,
            )
        except Exception:
            log.exception("flt:lwr replay failed")
            tg.answer_callback(cb_id, "Replay failed — see logs.")
            return

        if sent_ids:
            try:
                db.mark_digest_jobs_sent(chat_id, run_id, sent_ids, floor=new_floor)
            except Exception:
                log.debug("mark_digest_jobs_sent failed; continuing", exc_info=True)

        profile = profile_from_json(db.get_user_profile(chat_id))
        db.set_user_profile(chat_id, profile_to_json(set_min_match_score(profile, new_floor)))

        try:
            next_lower = (
                db.unsent_count_at_score(chat_id, run_id, new_floor - 1)
                if new_floor > 0 else 0
            )
            kb = digest_header_keyboard(
                run_id=run_id, current_floor=new_floor, lower_count=next_lower,
            )
            tg.edit_reply_markup(chat_id, msg_id, kb or {"inline_keyboard": []})
        except Exception:
            log.debug("flt:lwr edit_reply_markup failed; continuing", exc_info=True)

        tg.answer_callback(cb_id, f"Lowered to ≥{new_floor}/5 — added {len(sent_ids)}")
        return

    tg.answer_callback(cb_id, "Unknown filter button.")


def _apply_min_score(tg: TelegramClient, db: DB, cb: dict, chat_id: int, msg_id: int,
                     score: int) -> None:
    """Callback handler for `ms:<n>`: persist score into the user's profile JSON
    and echo back. If the user has no profile yet, a minimal stub containing
    only the score is stored; the next successful Opus rebuild carries the
    score forward (see profile_builder._run_one)."""
    score = max(0, min(5, int(score)))
    profile = profile_from_json(db.get_user_profile(chat_id))
    new_profile = set_min_match_score(profile, score)
    db.set_user_profile(chat_id, profile_to_json(new_profile))

    # Edit the picker message in place so the row's marker updates and the
    # status line reflects the new threshold. Tapping the already-selected
    # tier makes the payload byte-identical to the current message, which
    # Telegram rejects with "message is not modified" — harmless; ignore it.
    try:
        tg.edit_message_text(
            chat_id, msg_id,
            _min_score_prompt_mdv2(score),
            reply_markup=min_score_keyboard(current=score),
        )
    except Exception as e:
        if "not modified" not in str(e):
            log.warning("edit min_score message failed: %s", e)

    if score == 0:
        toast = "Showing all matches"
    else:
        toast = f"Minimum set to {score}+ / 5"
    tg.answer_callback(cb["id"], toast)


# ---------- preferences flow ----------

def _ask_for_prefs(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Put the user in the 'awaiting_prefs' state and show the prompt + Cancel keyboard."""
    db.upsert_user(chat_id)  # make sure the row exists before writing state
    db.set_awaiting_state(chat_id, STATE_AWAITING_PREFS)
    tg.send_message(chat_id, PREFS_PROMPT, reply_markup=PREFS_INPUT_KEYBOARD)


def _clear_prefs(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Wipe the user's free-text prefs and stored profile, but preserve the ⭐
    min-score gate — users think of the slider and the prefs as independent
    features, so "clear preferences" should only clear the typed wording and
    the structured profile derived from it, not the score they tapped."""
    existing = profile_from_json(db.get_user_profile(chat_id)) or {}
    kept_score = int(existing.get("min_match_score") or 0)
    db.set_awaiting_state(chat_id, None)
    db.set_prefs_free_text(chat_id, None)

    if kept_score > 0:
        # Rebuild a stub profile containing only the preserved score gate.
        stub = set_min_match_score(None, kept_score)
        db.set_user_profile(chat_id, profile_to_json(stub))
        note = (
            f"🧹 Preferences cleared — using global defaults. "
            f"Your ⭐ min match score ({kept_score}+ / 5) is still active."
        )
    else:
        db.clear_user_profile(chat_id)
        note = "🧹 Preferences cleared — your digest will use the global defaults."

    tg.send_message(chat_id, mdv2_escape(note), reply_markup=REPLY_KEYBOARD)


# ---------- profile commands ----------

def _show_profile(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Render the user's stored profile (Opus-built, schema_version=2).

    Falls back to a helpful message when no profile has been built yet. This
    is a READ — it doesn't trigger a rebuild. Use /rebuildprofile for that.
    """
    db.upsert_user(chat_id)
    profile = profile_from_json(db.get_user_profile(chat_id))

    # A stub profile containing only `min_match_score` is not a "real" built
    # profile from the user's perspective — treat it as absent for display.
    if not profile or set(profile.keys()) == {"min_match_score"}:
        u = db.get_user(chat_id)
        has_resume = bool(u and u["resume_path"])
        has_prefs = bool((db.get_prefs_free_text(chat_id) or "").strip())
        missing: list[str] = []
        if not has_resume:
            missing.append("upload your resume")
        if not has_prefs:
            missing.append("run /prefs to describe what you're looking for")
        if missing:
            hint = " and ".join(missing)
            body = (
                "🤖 Your profile hasn't been built yet.\n\n"
                f"To build one: {hint}. The bot will rebuild the profile "
                "automatically after each change (it takes ~30-60 seconds)."
            )
        else:
            body = (
                "🤖 Your profile hasn't been built yet.\n\n"
                "It should land within a minute of your last /prefs or "
                "resume upload. Try /rebuildprofile if it's been longer."
            )
        tg.send_message(chat_id, mdv2_escape(body), reply_markup=REPLY_KEYBOARD)
        return

    summary = format_profile_summary_mdv2(profile, mdv2_escape)
    tg.send_message(chat_id, summary, reply_markup=REPLY_KEYBOARD)


def _rebuild_profile(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Force-enqueue an Opus profile rebuild.

    Skips the queue's debounce window because the user explicitly asked for
    it. The queue still coalesces against an already-running build for the
    same user, so spamming /rebuildprofile won't stack calls.
    """
    db.upsert_user(chat_id)
    u = db.get_user(chat_id)
    has_resume = bool(u and u["resume_path"])
    has_prefs = bool((db.get_prefs_free_text(chat_id) or "").strip())
    if not has_resume and not has_prefs:
        tg.send_message(
            chat_id,
            mdv2_escape(
                "🤷 Nothing to build from yet — upload a resume and / or run "
                "/prefs first. The builder needs at least one of those to "
                "produce a profile."
            ),
            reply_markup=REPLY_KEYBOARD,
        )
        return

    try:
        # "manual" is in _IMMEDIATE_TRIGGERS → bypasses the 60s debounce.
        _enqueue_profile_rebuild(tg, db, chat_id, trigger="manual")
    except Exception as e:
        log.exception("manual rebuild enqueue failed")
        tg.send_message(
            chat_id,
            mdv2_escape(f"⚠️ Could not enqueue rebuild: {e}"),
            reply_markup=REPLY_KEYBOARD,
        )
        return

    tg.send_message(
        chat_id,
        mdv2_escape(
            "🤖 Profile rebuild queued. This usually takes ~30-60 seconds. "
            "I'll message you when it lands. Check the result with /myprofile."
        ),
        reply_markup=REPLY_KEYBOARD,
    )


# ---------- clean-my-data flow ----------
#
# Two-step UX (picked in the product review):
#   1. User taps 🧹 Clean my data → we send a menu of 6 categories (Prefs,
#      Resume, Job history, Tailored resumes, v2 profile, Everything).
#   2. Tapping a category → a confirm message with exactly what gets
#      removed and Yes / Cancel buttons. Nothing is deleted until the
#      user taps ✅ Yes, delete.
#
# Callback codes:
#   cd:<kind>   → show the confirm screen for <kind>
#   cdc:<kind>  → execute the deletion
#   cdx:        → cancel; close the dialog
#
# Kinds are declared in telegram_client.CLEAN_DATA_KINDS so the menu and the
# handler stay in sync.

_CLEAN_KIND_CODES = {k[0] for k in CLEAN_DATA_KINDS}


def _clean_data_overview_mdv2(db: DB, chat_id: int) -> str:
    """Short summary block shown above the menu so the user sees what's
    actually stored before they wipe anything. Avoids the "I don't know
    what this will delete" UX trap."""
    counts = db.count_user_data(chat_id)
    bits: list[str] = []
    bits.append(("📝 Free-text prefs: " + ("set" if counts["has_free_text"] else "—")))
    bits.append(("📄 Resume: " + ("uploaded" if counts["has_resume"] else "—")))
    bits.append(("🤖 Profile: " + ("built" if counts["has_profile"] else "—")))
    bits.append(f"📋 Applications: {counts['applications']}")
    bits.append(f"📨 Digest sent-log: {counts['sent_messages']}")
    bits.append(f"✍️ Tailored plans: {counts['suggestions']}")
    bits.append(f"🔬 Research runs: {counts.get('research_runs', 0)}")
    body = "\n".join("  • " + mdv2_escape(b) for b in bits)
    return "🧹 *Clean my data*\n\n" + mdv2_escape("What would you like to remove?") + "\n\n" + body


def _ask_clean_data(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Step 1: show the category menu with a current-data overview."""
    db.upsert_user(chat_id)
    tg.send_message(
        chat_id,
        _clean_data_overview_mdv2(db, chat_id),
        reply_markup=clean_data_menu_keyboard(),
    )


def _confirm_clean_data(
    tg: TelegramClient,
    db: DB,
    cb: dict,
    chat_id: int,
    msg_id: int,
    kind: str,
) -> None:
    """Step 2: edit the menu message in place to show the confirm prompt."""
    if kind not in _CLEAN_KIND_CODES:
        tg.answer_callback(cb["id"], "Unknown option.")
        return
    # Pull the category's label + description from the shared registry so
    # the menu and the confirm screen never drift.
    label, desc = next((lbl, d) for (c, lbl, d) in CLEAN_DATA_KINDS if c == kind)
    if kind == "all":
        body = (
            "⚠️ *" + mdv2_escape("Delete EVERYTHING?") + "*\n\n"
            + mdv2_escape(
                "This removes your preferences, resume, profile, "
                "applications, sent-digest log, tailored resume plans, and "
                "your user record. You'll need to /start from scratch to use "
                "the bot again. This cannot be undone."
            )
        )
    else:
        body = (
            "🧹 *" + mdv2_escape(f"Delete: {label}?") + "*\n\n"
            + mdv2_escape(desc)
            + "\n\n"
            + mdv2_escape("This cannot be undone.")
        )
    try:
        tg.edit_message_text(
            chat_id, msg_id, body,
            reply_markup=clean_data_confirm_keyboard(kind),
        )
    except Exception as e:
        log.warning("clean-data confirm edit failed: %s", e)
    tg.answer_callback(cb["id"], "")


def _cancel_clean_data(
    tg: TelegramClient,
    cb: dict,
    chat_id: int,
    msg_id: int,
) -> None:
    """Step 2 cancel: strip the keyboard and acknowledge. Leave the message
    body in place so the user sees what they were about to act on."""
    try:
        tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
    except Exception as e:
        log.warning("clean-data cancel edit failed: %s", e)
    try:
        # Append a "cancelled" line so there's no ambiguity about the state.
        pass  # edit_message_text with identical body would 400; the removed keyboard is signal enough
    except Exception:
        pass
    tg.answer_callback(cb["id"], "Cancelled — nothing deleted.")


def _execute_clean_data(
    tg: TelegramClient,
    db: DB,
    cb: dict,
    chat_id: int,
    msg_id: int,
    kind: str,
) -> None:
    """Step 3: the deletion actually happens. Each branch is self-contained
    so adding a new cleanup category later is a one-place edit.

    Filesystem side-effects intentionally live here (not in db.py) so the
    DB layer stays storage-agnostic. We always check existence before
    removing — the bot must not crash because a user never uploaded a
    resume or never received a tailor plan."""
    if kind not in _CLEAN_KIND_CODES:
        tg.answer_callback(cb["id"], "Unknown option.")
        return

    udir = user_dir(chat_id)  # ensures the dir exists; side-effect is fine
    summary_lines: list[str] = []

    try:
        if kind == "resume":
            db.clear_resume(chat_id)
            # Fit analyses reference the resume version they were run against
            # (via resume_sha1). Clearing the resume invalidates them — blow
            # away the cache so we don't surface stale results if the user
            # later uploads a different resume.
            n_fit = 0
            try:
                n_fit = db.delete_fit_analyses(chat_id)
            except Exception:
                log.exception("resume wipe: delete_fit_analyses failed")
            for name in ("resume.pdf", "resume.txt"):
                p = udir / name
                try:
                    if p.exists():
                        p.unlink()
                except Exception as e:
                    log.warning("resume file unlink %s failed: %s", p, e)
            if n_fit:
                summary_lines.append(
                    f"📄 Resume removed (DB + files). Cleared {n_fit} cached fit analyses."
                )
            else:
                summary_lines.append("📄 Resume removed (DB + files).")

        elif kind == "history":
            n_apps = db.delete_applications(chat_id)
            n_sent = db.delete_sent_messages(chat_id)
            summary_lines.append(
                f"📋 Job history cleared — {n_apps} applications, {n_sent} sent-log entries."
            )

        elif kind == "tailored":
            n_rows = db.delete_suggestions(chat_id)
            tdir = udir / "tailored"
            files_removed = 0
            if tdir.exists():
                for p in tdir.glob("*.md"):
                    try:
                        p.unlink()
                        files_removed += 1
                    except Exception as e:
                        log.warning("tailored unlink %s failed: %s", p, e)
                try:
                    tdir.rmdir()
                except OSError:
                    pass  # non-empty is fine; we only clear .md
            summary_lines.append(
                f"✍️ Tailored resumes cleared — {n_rows} plans, {files_removed} files."
            )

        elif kind == "profile":
            db.clear_user_profile(chat_id)
            summary_lines.append(
                "🤖 Profile + free-text prefs wiped. Resume survives; upload /prefs "
                "again (or /rebuildprofile) to produce a fresh profile."
            )

        elif kind == "research":
            n_rows = db.delete_research_runs(chat_id)
            rdir = udir / "research"
            files_removed = 0
            if rdir.exists():
                for p in rdir.glob("*.docx"):
                    try:
                        p.unlink()
                        files_removed += 1
                    except Exception as e:
                        log.warning("research unlink %s failed: %s", p, e)
                try:
                    rdir.rmdir()
                except OSError:
                    pass  # non-empty is fine; we only clear .docx
            summary_lines.append(
                f"🔬 Research history cleared — {n_rows} runs, {files_removed} DOCX files."
            )

        elif kind == "all":
            # Full wipe. DB helpers handle the cross-table DELETEs; here we
            # deal with the filesystem side. We wipe the whole per-user
            # directory rather than picking at individual files — there may
            # be leftover state (draft files, legacy names) that the typed
            # cleanup above didn't cover.
            db.delete_user(chat_id)
            try:
                if udir.exists():
                    _shutil.rmtree(udir, ignore_errors=True)
            except Exception as e:
                log.warning("user_dir rmtree failed for %s: %s", udir, e)
            # Emotional texture on the full-wipe path. Sends only if a SAD
            # sticker is registered; otherwise just proceeds to the text
            # confirmation below.
            _pigs.send_sticker(tg, chat_id, _pigs.SAD)
            summary_lines.append(
                "⚠️ Everything deleted. Send /start if you want to use the bot again."
            )
        else:
            tg.answer_callback(cb["id"], "Unknown option.")
            return
    except Exception as e:
        log.exception("clean-data execution failed for kind=%s chat=%s", kind, chat_id)
        tg.answer_callback(cb["id"], f"⚠️ Cleanup failed: {e}", show_alert=True)
        try:
            tg.send_message(
                chat_id, mdv2_escape(f"⚠️ Cleanup failed: {e}"),
                reply_markup=REPLY_KEYBOARD,
            )
        except Exception:
            pass
        return

    # Edit the confirm message in place so the user sees the final state
    # without an extra bubble, then drop a short follow-up with the reply
    # keyboard in case the previous message stripped it.
    body = "✅ *" + mdv2_escape("Done.") + "*\n\n" + "\n".join(
        mdv2_escape(s) for s in summary_lines
    )
    try:
        tg.edit_message_text(chat_id, msg_id, body, reply_markup={"inline_keyboard": []})
    except Exception as e:
        log.warning("clean-data done edit failed: %s", e)
    tg.answer_callback(cb["id"], "Deleted.")

    # Full-wipe: the user row is gone, so don't re-send the reply keyboard
    # — /start will rebuild it. For scoped wipes, re-assert the keyboard
    # so the user lands back in the normal-ops UI.
    if kind != "all":
        try:
            tg.send_message(
                chat_id,
                mdv2_escape("Use /help to see what's still set up."),
                reply_markup=REPLY_KEYBOARD,
            )
        except Exception:
            pass


def _attach_main_menu(tg: TelegramClient, chat_id: int) -> None:
    """Send a tiny one-liner whose sole purpose is to re-surface the
    persistent reply keyboard after the onboarding wizard finishes.

    The wizard uses inline keyboards throughout, which suppresses the
    persistent one. Calling this at the end of the flow gives the user the
    standard Search / Applied / Profile / Research / Settings row.

    If a CELEBRATE sticker is configured, fire it first — setup completion
    is exactly the kind of one-off moment a sticker is built for. Silent
    fail-through when no sticker id is registered.
    """
    _pigs.send_sticker(tg, chat_id, _pigs.CELEBRATE)
    try:
        tg.send_message(
            chat_id,
            f"{PIG}  " + mdv2_escape("You're all set. The main menu is below."),
            reply_markup=REPLY_KEYBOARD,
        )
    except Exception as e:
        log.debug("attach_main_menu failed: %s", e)


def _handle_settings_cb(
    tg: TelegramClient,
    db: DB,
    cb: dict,
    chat_id: int,
    msg_id: int,
    payload: str,
) -> None:
    """Dispatch a `st:<action>` callback from the Settings inline menu.

    Each branch acknowledges the callback, strips the Settings keyboard
    (so the user can't double-tap), and calls the existing standalone
    handler. We don't try to keep the Settings message around — once the
    user picks a path, they get the deeper prompt for that action and
    Settings itself becomes stale.
    """
    cb_id = cb["id"]
    action = (payload or "").strip()

    # Strip the inline keyboard on every branch so the Settings bubble
    # reads as "resolved". Safe to swallow errors — the worst case is a
    # lingering button row.
    try:
        tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
    except Exception:
        pass

    if action == "prefs":
        tg.answer_callback(cb_id, "")
        _ask_for_prefs(tg, db, chat_id)
    elif action == "minscore":
        tg.answer_callback(cb_id, "")
        _ask_min_score(tg, db, chat_id)
    elif action == "rebuild":
        tg.answer_callback(cb_id, "")
        _rebuild_profile(tg, db, chat_id)
    elif action == "cleandata":
        tg.answer_callback(cb_id, "")
        _ask_clean_data(tg, db, chat_id)
    elif action == "close":
        tg.answer_callback(cb_id, "")
        # Nothing to do — the keyboard is already stripped.
    else:
        tg.answer_callback(cb_id, "")


def _is_admin(chat_id: int) -> bool:
    """Admin gate for privileged commands.

    ADMIN_CHAT_ID can be a single int or a comma-separated list. Empty or
    unset → admin commands are disabled entirely (safer default than "any
    user").
    """
    raw = (os.environ.get("ADMIN_CHAT_ID") or "").strip()
    if not raw:
        return False
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            if int(tok) == int(chat_id):
                return True
        except ValueError:
            continue
    return False


def _show_admin_stats(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Admin-only rollout dashboard. Gated by ADMIN_CHAT_ID env var.

    Prints:
      * User counts (total, with resume, with free-text prefs, with profile)
      * Profile-build health (last 20 attempts: ok / error breakdown)

    Everything comes from the SAME DB the pipeline reads, so the numbers
    here match what the digest will actually do on the next /jobs run.
    """
    if not _is_admin(chat_id):
        # Silent-ish: pretend the command doesn't exist rather than leaking
        # "you aren't an admin" to random users who guess commands.
        tg.send_plain(chat_id, "Unknown command. Try /help.")
        return

    try:
        users = db.all_users()
    except Exception as e:
        tg.send_plain(chat_id, f"⚠️ DB error: {e!r}")
        return

    n_total = len(users)
    n_resume = 0
    n_free_text = 0
    n_profile = 0
    for u in users:
        try:
            if u["resume_path"]:
                n_resume += 1
        except (IndexError, KeyError):
            pass
        try:
            if "prefs_free_text" in u.keys() and (u["prefs_free_text"] or "").strip():
                n_free_text += 1
        except (IndexError, KeyError, TypeError):
            pass
        try:
            if u["user_profile"]:
                n_profile += 1
        except (IndexError, KeyError):
            pass

    # Last 20 builds — status breakdown.
    status_counts: dict[str, int] = {}
    recent_ok_latency: list[int] = []
    try:
        recent = db.recent_profile_builds(20)
        for row in recent:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
            if row["status"] == "ok" and row["elapsed_ms"] is not None:
                recent_ok_latency.append(int(row["elapsed_ms"]))
    except Exception as e:
        log.exception("admin /stats: recent_profile_builds failed: %s", e)

    avg_ms = (sum(recent_ok_latency) // len(recent_ok_latency)) if recent_ok_latency else 0

    lines = [
        "📊 Admin stats",
        "",
        "*Users*",
        f"  • total:             {n_total}",
        f"  • with resume:       {n_resume}",
        f"  • with free-text:    {n_free_text}",
        f"  • with profile:      {n_profile}",
        "",
        "*Profile builds (last 20)*",
    ]
    if status_counts:
        for k in sorted(status_counts):
            lines.append(f"  • {k}: {status_counts[k]}")
        if avg_ms:
            lines.append(f"  • avg OK latency: {avg_ms // 1000}s")
    else:
        lines.append("  (no builds yet)")

    tg.send_plain(chat_id, "\n".join(lines))


def _save_prefs_from_text(tg: TelegramClient, db: DB, chat_id: int, text: str) -> None:
    """Persist the user's free-form preferences text and kick off an Opus
    rebuild.

    Flow:
      1. Bot-boundary safety screen (`safety_check.check_user_input`). The
         text is about to be foregrounded in a Claude sub-agent that has
         WebSearch + WebFetch, so a successful prompt injection here could
         pivot the agent off-task and waste the user's tokens. We reject on
         any hit and tell the user WHY so they can rephrase.
      2. Store the raw text verbatim in `users.prefs_free_text` — decoupled
         from the structured profile JSON so it survives rebuilds and can
         feed the builder with the user's exact wording on every run.
      3. Enqueue a debounced Opus rebuild; the queue handles in-flight,
         coalescing, and user-facing "rebuilt" pings.
    """
    # Boundary screen — runs synchronously on the polling thread because it's
    # cheap (regex only by default). If it blocks we never enqueue the
    # builder and the text never reaches any downstream Claude call.
    verdict = check_user_input(text)
    if verdict.get("verdict") == "block":
        db.set_awaiting_state(chat_id, None)
        reason = str(verdict.get("reason") or "prompt-injection fingerprint")
        tg.send_message(
            chat_id,
            mdv2_escape(
                f"🛡️ I couldn't accept that — it looked like a {reason}. "
                "Preferences should be a normal job-search description "
                "(what role, where, tech stack, salary, etc.). Try rephrasing."
            ),
            reply_markup=REPLY_KEYBOARD,
        )
        log.info("prefs rejected by safety_check for chat %s: %s (method=%s)",
                 chat_id, reason, verdict.get("method"))
        return

    try:
        db.set_prefs_free_text(chat_id, text)
        db.set_awaiting_state(chat_id, None)
    except Exception as e:
        log.exception("save_prefs: set_prefs_free_text failed")
        tg.send_message(
            chat_id,
            mdv2_escape(f"⚠️ Could not save preferences: {e}"),
            reply_markup=REPLY_KEYBOARD,
        )
        return

    tg.send_message(
        chat_id,
        mdv2_escape(
            "✅ Got it — your preferences are saved. I'm rebuilding your "
            "profile in the background (this usually takes ~30-60 seconds). "
            "I'll message you when it lands; inspect the result with /myprofile."
        ),
        reply_markup=REPLY_KEYBOARD,
    )

    try:
        _enqueue_profile_rebuild(tg, db, chat_id, trigger="prefs_change")
    except Exception:
        log.exception("save_prefs: profile rebuild enqueue failed for chat=%s", chat_id)


# ---------- skip-reason capture flow ----------

# Tokens accepted in lieu of an actual reason. Includes the literal Cancel
# button label so a user tapping ✕ Cancel mid-prompt is treated as "no
# feedback" rather than a real reason. (BTN_CANCEL is also short-circuited
# by the global cancel handler in _dispatch — this list is the in-state
# safety net for legacy keyboard variants.)
_SKIP_REASON_CANCEL_TOKENS = {"skip", "/skip", "cancel", "/cancel"}


def _is_skip_reason_cancel(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if t.lower() in _SKIP_REASON_CANCEL_TOKENS:
        return True
    # Match the "✕ Cancel" / "✖️ Cancel" reply-keyboard buttons.
    if t == BTN_CANCEL:
        return True
    if _LEGACY_BUTTONS.get(t) == "cancel":
        return True
    return False


def _handle_skip_reason_text(
    tg: TelegramClient, db: DB, chat_id: int, text: str,
) -> None:
    """Process the user's free-text reply to "Why didn't this fit?".

    Contract:
      • `skip` / Cancel → clear state, no feedback recorded.
      • `safety_check` block → reject + clear state, never reach Claude.
      • Else → import skip_feedback (sibling-agent module) and call
        apply_skip_feedback with the captured job_context payload. Surface
        the returned summary to the user. Always clear state on the way out.

    The job context comes from the JSON payload bundled with the awaiting
    state row (`db.get_awaiting_state_payload`). If the payload is missing
    we treat it as a stale prompt — clear state silently and bail.
    """
    payload = db.get_awaiting_state_payload(chat_id) or {}

    # Fast-path: user opted out.
    if _is_skip_reason_cancel(text):
        db.set_awaiting_state(chat_id, None)
        tg.send_message(
            chat_id,
            mdv2_escape("Got it, no feedback recorded."),
            reply_markup=REPLY_KEYBOARD,
        )
        return

    # Boundary safety screen — same gate as /prefs / /marketresearch. The
    # reason text will be foregrounded in a Claude call inside
    # skip_feedback, so injection attempts must not slip past this layer.
    verdict = check_user_input(text)
    if verdict.get("verdict") == "block":
        db.set_awaiting_state(chat_id, None)
        reason = str(verdict.get("reason") or "prompt-injection fingerprint")
        tg.send_message(
            chat_id,
            mdv2_escape(
                f"🛡️ I couldn't use that as feedback — it looked like a {reason}. "
                "No worries, the job is still skipped. Try a plain reason next time "
                "(role/stack/location/seniority)."
            ),
            reply_markup=REPLY_KEYBOARD,
        )
        log.info("skip-reason rejected by safety_check for chat %s: %s (method=%s)",
                 chat_id, reason, verdict.get("method"))
        return

    # Hand off to the sibling-agent module. Imported lazily so this file
    # stays importable even when skip_feedback isn't on disk yet.
    summary_text = ""
    added_lists: dict = {}
    err: Exception | None = None
    try:
        import skip_feedback  # noqa: WPS433 — intentional lazy import
        result = skip_feedback.apply_skip_feedback(db, chat_id, payload, text) or {}
        if not isinstance(result, dict):
            result = {}
        summary_text = str(result.get("summary") or "").strip()
        added_lists = {
            k: v for k, v in result.items() if k.startswith("added_")
        }
    except Exception as e:  # pragma: no cover — defensive
        err = e
        log.exception("apply_skip_feedback failed for chat=%s", chat_id)

    db.set_awaiting_state(chat_id, None)

    if err is not None:
        tg.send_message(
            chat_id,
            mdv2_escape(
                "Thanks — I noted that. (Couldn't update your profile from it "
                "automatically, but the job is still skipped.)"
            ),
            reply_markup=REPLY_KEYBOARD,
        )
    else:
        body = "✅ " + (summary_text or "Got it — feedback recorded.")
        tg.send_message(chat_id, mdv2_escape(body), reply_markup=REPLY_KEYBOARD)

    try:
        import forensic as _forensic
        _forensic.log_step(
            "bot.skip_reason_received",
            input={
                "chat_id": chat_id,
                "job_id": payload.get("job_id"),
                "reason_chars": len(text or ""),
            },
            output={
                "summary": summary_text,
                "added_lists": added_lists,
                "error": str(err) if err else None,
            },
            chat_id=chat_id if isinstance(chat_id, int) else None,
        )
    except Exception:
        pass


def _send_applied_list(tg: TelegramClient, db: DB, chat_id: int) -> None:
    rows = db.applied_jobs(chat_id)
    if not rows:
        tg.send_plain(chat_id, "No jobs marked as applied yet.")
        return
    # MDv2: `(`, `)`, `.`, `'` in plain body text all need escaping. Use
    # mdv2_escape for the header's variable label and the bullet separator.
    header = "📋 *" + mdv2_escape(f"You've applied to {len(rows)} role(s):") + "*"
    lines = [header, ""]
    for r in rows[:40]:
        title = mdv2_escape(r["title"] or "")
        company = mdv2_escape(r["company"] or "")
        # Inline-link URLs need `(` and `)` escaped per MDv2 spec.
        url = (r["url"] or "").replace("(", "\\(").replace(")", "\\)")
        sep = mdv2_escape(" — ")  # em-dash is safe, but surrounding spaces aren't special either; cheap insurance
        lines.append(f"• [{title}]({url}){sep}{company}")
    tg.send_message(chat_id, "\n".join(lines))


# -- Per-process profile-builder queue. Lazily initialized on first trigger
# -- so importing bot.py for tests/tools doesn't spin up background threads.
_PROFILE_QUEUE: _profile_builder.ProfileBuilderQueue | None = None
_PROFILE_QUEUE_GUARD = threading.Lock()


def _get_profile_queue(db: DB, tg: TelegramClient) -> _profile_builder.ProfileBuilderQueue:
    global _PROFILE_QUEUE
    with _PROFILE_QUEUE_GUARD:
        if _PROFILE_QUEUE is None:
            _PROFILE_QUEUE = _profile_builder.ProfileBuilderQueue(db=db, tg=tg)
        return _PROFILE_QUEUE


def _enqueue_profile_rebuild(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    *,
    trigger: str,
) -> None:
    """Read the user's resume_text + /prefs free-text and schedule an Opus
    rebuild. Fire-and-forget; the queue handles debounce, in-flight, and
    progress messages."""
    row = db.get_user(chat_id)
    if row is None:
        return
    resume_text = ""
    try:
        resume_text = row["resume_text"] or ""
    except (IndexError, KeyError):
        resume_text = ""
    free_text = (db.get_prefs_free_text(chat_id) or "").strip()
    try:
        _get_profile_queue(db, tg).enqueue(
            chat_id=chat_id,
            resume_text=resume_text,
            free_text=free_text,
            trigger=trigger,
        )
    except Exception:
        log.exception("profile rebuild enqueue failed for chat=%s", chat_id)


# -- lightweight lock so a user can't hammer "Check for jobs now" and spawn
# -- overlapping scrapes against the same sources.
_CHECK_LOCKS: dict[int, threading.Lock] = {}
_CHECK_LOCKS_GUARD = threading.Lock()


def _lock_for(chat_id: int) -> threading.Lock:
    with _CHECK_LOCKS_GUARD:
        lock = _CHECK_LOCKS.get(chat_id)
        if lock is None:
            lock = _CHECK_LOCKS[chat_id] = threading.Lock()
        return lock


def trigger_job_check(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Kick off an on-demand scrape for this user in a background thread so the
    polling loop stays responsive."""
    lock = _lock_for(chat_id)
    if not lock.acquire(blocking=False):
        tg.send_plain(chat_id, "⏳ A check is already running — hold on a moment.")
        return

    # Playful loading copy — the pig is "sniffing" job boards. Keeps the
    # wait feel less dead-air-y while staying honest about the 15-45s range.
    _pigs.send_sticker(tg, chat_id, _pigs.SNIFF)
    tg.send_plain(chat_id, f"{PIG} Sniffing through job boards… usually 15–45 seconds.")

    def _work():
        try:
            # Import lazily to avoid a top-level cycle (search_jobs imports dedupe/db too).
            from search_jobs import run as run_search
            rc = run_search(dry_run=False, only_chat=chat_id)
            if rc == 1:
                tg.send_plain(chat_id, "⚠️ Config/credentials issue — ask the admin to check .env.")
            elif rc == 3:
                tg.send_plain(chat_id, "⚠️ Could not post to Telegram. See bot logs.")
            # rc == 0 or 2 → run completed; messages (or a 'no new' header) were sent by run_search.
        except Exception as e:
            log.exception("on-demand check failed")
            try:
                tg.send_plain(chat_id, f"⚠️ Check failed: {e}")
            except Exception:
                pass
        finally:
            lock.release()

    threading.Thread(target=_work, daemon=True, name=f"check-{chat_id}").start()


def handle_document(tg: TelegramClient, db: DB, chat_id: int, doc: dict, user: dict) -> None:
    mime = doc.get("mime_type") or ""
    name = doc.get("file_name") or "resume.pdf"
    if not (mime == "application/pdf" or name.lower().endswith(".pdf")):
        tg.send_plain(chat_id, "Please upload your CV as a PDF (.pdf).")
        return

    db.upsert_user(
        chat_id,
        username=user.get("username"),
        first_name=user.get("first_name"),
        last_name=user.get("last_name"),
    )
    try:
        file_path = tg.get_file_path(doc["file_id"])
        dest = user_dir(chat_id) / "resume.pdf"
        tg.download_file(file_path, dest)
    except Exception as e:
        log.exception("resume download failed")
        tg.send_plain(chat_id, f"⚠️ Failed to save your resume: {e}")
        return

    # Extract text for later tailoring.
    try:
        text = extract_pdf_text(dest)
    except Exception as e:
        log.warning("resume text extraction failed: %s", e)
        text = ""

    (user_dir(chat_id) / "resume.txt").write_text(text)
    db.set_resume(chat_id, str(dest), text)

    word_count = len(text.split())

    # If the user is mid-wizard, let the onboarding state machine handle the
    # post-save message (it sends its own "got your CV" + next question).
    # Otherwise fall back to the classic standalone confirmation.
    advanced_by_wizard = _onboarding.handle_resume_uploaded(tg, db, chat_id)
    if not advanced_by_wizard:
        tg.send_plain(
            chat_id,
            f"Resume saved ({word_count} words parsed). "
            f"You'll start getting the daily digest at the next scheduled run. "
            f"Use /help any time."
        )
        # Kick off the Opus-backed profile rebuild in the background. The
        # wizard defers this until its final step so we only run it once
        # the user has given all their signals; outside the wizard, the
        # moment a new resume arrives is the right time to rebuild.
        tg.send_plain(chat_id, "Rebuilding your profile in the background…")
        _enqueue_profile_rebuild(tg, db, chat_id, trigger="resume_upload")


def _row_to_job(row) -> Job | None:
    if row is None:
        return None
    return Job(
        source=row["source"] or "",
        external_id=row["external_id"] or "",
        title=row["title"] or "",
        company=row["company"] or "",
        location=row["location"] or "",
        url=row["url"] or "",
        posted_at=row["posted_at"] or "",
        snippet=row["snippet"] or "",
        salary=row["salary"] or "",
    )


def handle_callback(tg: TelegramClient, db: DB, cb: dict) -> None:
    cb_id = cb["id"]
    data = cb.get("data") or ""
    message = cb.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    msg_id = message.get("message_id")
    # Inner error_capture so callback-path bugs are fingerprinted under
    # `where='handle_callback'` rather than the broader `bot._dispatch`.
    # Re-raises after recording — outer _dispatch wrap keeps the bot alive.
    _store = _get_store(db)
    with error_capture(
        _store,
        where="handle_callback",
        chat_id=chat_id,
        alert_sink=lambda env, _tg=tg, _s=_store: _ops_deliver_alert(_tg, _s, env),
    ):
        return _handle_callback_inner(tg, db, cb, cb_id, data, message, chat_id, msg_id)


def _handle_callback_inner(
    tg: TelegramClient, db: DB, cb: dict, cb_id, data, message, chat_id, msg_id,
) -> None:
    if not chat_id or not msg_id or ":" not in data:
        tg.answer_callback(cb_id, "Invalid button.")
        return

    kind, payload = data.split(":", 1)

    # Explicit Skip button under the "Why didn't this fit?" prompt. Handle
    # BEFORE the stale-prompt guard so we can send the user a confirmation
    # toast + delete the prompt rather than just silently clearing state.
    if kind == "sr" and payload == "skip":
        try:
            db.set_awaiting_state(chat_id, None)
        except Exception:
            pass
        try:
            tg.delete_message(chat_id, msg_id)
        except Exception:
            log.debug("sr:skip — delete_message failed; continuing", exc_info=True)
        try:
            tg.answer_callback(cb_id, "Got it, no feedback recorded.")
        except Exception:
            pass
        try:
            import forensic as _forensic
            _forensic.log_step(
                "bot.skip_reason_received",
                input={"chat_id": chat_id, "via": "skip_button"},
                output={"action": "user_canceled_via_button"},
                chat_id=chat_id,
            )
        except Exception:
            pass
        return

    # Stale-prompt guard for the skip-reason flow: if the user is parked in
    # STATE_AWAITING_SKIP_REASON and now taps another inline button (a job
    # action, settings, etc.) instead of typing a reason, treat that as an
    # implicit cancel and continue with the action they did press. Mirrors
    # the wizard escape pattern in _dispatch.
    try:
        if db.get_awaiting_state(chat_id) == STATE_AWAITING_SKIP_REASON:
            db.set_awaiting_state(chat_id, None)
    except Exception:
        # Reading awaiting_state should never break callback handling.
        pass

    # Onboarding wizard callbacks — delegate to the onboarding module. The
    # wizard owns its own state machine; this bot just forwards the button
    # press + hooks for the two cross-module actions (kick a job check,
    # re-attach the main-menu keyboard on completion).
    if kind == "ob":
        _onboarding.handle_callback(
            tg, db, cb, chat_id, msg_id, payload,
            on_complete=lambda cid: _attach_main_menu(tg, cid),
            on_run_search=lambda cid: trigger_job_check(tg, db, cid),
        )
        return

    # Settings sub-menu callbacks (`st:<action>`). Each branch reuses the
    # existing standalone handler so there's no duplicated logic.
    if kind == "st":
        _handle_settings_cb(tg, db, cb, chat_id, msg_id, payload)
        return

    # Min-match-score picker: `ms:<n>` where n ∈ 0..5.
    #
    # The same `ms:` prefix is also used INSIDE the onboarding wizard for
    # the step 6 picker — but onboarding.handle_callback routes those
    # through the `ob:` entry point. So if we see a bare `ms:` here, the
    # user is tweaking the score post-onboarding.
    if kind == "ms":
        try:
            score = int(payload)
        except ValueError:
            tg.answer_callback(cb_id, "Invalid score.")
            return
        # But: if the user happens to be mid-onboarding (e.g. the wizard
        # re-rendered the min-score picker and the callback came via the
        # legacy prefix rather than the wrapped ob: one), forward it.
        if _onboarding.is_in_progress(db, chat_id):
            _onboarding.handle_callback(
                tg, db, cb, chat_id, msg_id, f"ms:{payload}",
                on_complete=lambda cid: _attach_main_menu(tg, cid),
                on_run_search=lambda cid: trigger_job_check(tg, db, cid),
            )
            return
        _apply_min_score(tg, db, cb, chat_id, msg_id, score)
        return

    # Clean-my-data flow — three callback prefixes, none of which reference a
    # job row:
    #   cd:<kind>   → user picked a category from the menu; show confirm screen
    #   cdc:<kind>  → user confirmed; actually wipe
    #   cdx:        → user cancelled mid-confirm
    # Handled BEFORE the db.get_job() lookup because the payload here is a
    # cleanup-kind code (or empty for cdx), not a job_id. Each handler
    # answers the callback itself (so spinners stop promptly), so we hand
    # the full `cb` dict through rather than just the id.
    if kind == "cd":
        if payload not in _CLEAN_KIND_CODES:
            tg.answer_callback(cb_id, "Unknown category.")
            return
        _confirm_clean_data(tg, db, cb, chat_id, msg_id, payload)
        return
    if kind == "cdc":
        if payload not in _CLEAN_KIND_CODES:
            tg.answer_callback(cb_id, "Unknown category.")
            return
        _execute_clean_data(tg, db, cb, chat_id, msg_id, payload)
        return
    if kind == "cdx":
        _cancel_clean_data(tg, cb, chat_id, msg_id)
        return

    # Digest-header filter buttons (`flt:lwr:<run>:<floor>` and `flt:rse:<floor>`).
    # ⬇ "Lower" replays cached jobs at exactly the new floor for the same run
    #   and persists the new floor onto the user's profile for future digests.
    # ⬆ "Raise" only updates the profile floor — past digest contents are
    #   already on the chat and not retroactively trimmed (Telegram doesn't
    #   support deleting other people's messages, but more importantly the
    #   user might still want to scroll back through them).
    if kind == "flt":
        _handle_filter_button(tg, db, cb, chat_id, msg_id, payload)
        return

    job_id = payload
    row = db.get_job(job_id)
    job = _row_to_job(row)
    if job is None:
        tg.answer_callback(cb_id, "Job no longer in DB.")
        return

    if kind == "a":   # applied
        db.set_application_status(chat_id, job_id, "applied")
        try:
            new_text = format_job_mdv2(job, applied_status="applied")
            # We can't easily edit text + markup in one call for markdown; use
            # editMessageReplyMarkup which keeps the original text intact and just
            # flips the button state.
            tg.edit_reply_markup(chat_id, msg_id, job_keyboard(job_id, applied_status="applied", url=job.url or None))
        except Exception as e:
            log.warning("edit markup failed: %s", e)
        # Small celebration for hitting apply — fail-soft if no sticker
        # configured. Kept separate from the callback answer so both fire.
        _pigs.send_sticker(tg, chat_id, _pigs.THUMBS_UP)
        tg.answer_callback(cb_id, "Marked as applied ✅")

    elif kind == "n":  # not applied / skipped
        # We always persist the "skipped" status FIRST — that write is what
        # keeps the job from re-appearing in tomorrow's digest via
        # JobStore.filter_new_for(). Whatever happens to the message in the
        # UI after that is purely cosmetic.
        #
        # UX split based on whether the skip-reason capture flow is on:
        #
        #  • SKIP_FEEDBACK_ENABLED=1 → MORPH the job card into the
        #    "Why didn't this fit?" prompt in place. No new message; the
        #    chat order stays stable and the user reads the question
        #    exactly where the offending card was. The existing `sr:skip`
        #    handler deletes this same message_id when the user opts out,
        #    so the card disappears either way.
        #
        #  • SKIP_FEEDBACK_ENABLED unset → no question to ask, so just
        #    DELETE the card (default) or fall back to the strikethrough
        #    keyboard when deletion is blocked (>48h, SKIP_DELETES_MESSAGE=0).
        db.set_application_status(chat_id, job_id, "skipped")

        morphed = False
        deleted = False
        reason = "edit_fallback"
        feedback_on = _skip_feedback_enabled()

        if feedback_on:
            payload = {
                "job_id": job_id,
                "title": job.title or "",
                "company": job.company or "",
                "source": job.source or "",
                "url": job.url or "",
                "snippet": (job.snippet or "")[:600],
            }
            try:
                db.set_awaiting_state(chat_id, STATE_AWAITING_SKIP_REASON, payload)
            except Exception as e:
                log.warning("set_awaiting_state failed: %s", e)
                feedback_on = False  # treat as disabled — no prompt to render

        if feedback_on:
            try:
                tg.edit_message_text(
                    chat_id, msg_id,
                    SKIP_REASON_PROMPT_MDV2,
                    reply_markup=SKIP_REASON_INLINE_KB,
                )
                morphed = True
                reason = "morphed_in_place"
            except Exception as e:
                # Edit failed (e.g. message too old, or test fake without
                # edit_message_text). Fall back to legacy: send the prompt
                # as a NEW message and delete the card. State is already
                # set, so the next text reply still routes to the
                # skip-reason handler.
                log.debug("skip-reason morph failed; sending as new message: %s", e)
                try:
                    tg.send_message(
                        chat_id,
                        SKIP_REASON_PROMPT_MDV2,
                        reply_markup=SKIP_REASON_INLINE_KB,
                    )
                except Exception as e2:
                    log.warning("skip-reason new-message fallback also failed: %s", e2)
                    try:
                        db.set_awaiting_state(chat_id, None)
                    except Exception:
                        pass

            try:
                import forensic as _forensic
                _forensic.log_step(
                    "bot.skip_reason_prompted",
                    input={
                        "chat_id": chat_id,
                        "job_id": job_id,
                        "title": (job.title or ""),
                        "source": (job.source or ""),
                        "via": "card_morph" if morphed else "new_message",
                    },
                    chat_id=chat_id if isinstance(chat_id, int) else None,
                )
            except Exception:
                pass

        if not morphed:
            delete_enabled = os.environ.get("SKIP_DELETES_MESSAGE", "1") not in ("0", "false", "False", "")
            if delete_enabled:
                deleted = tg.delete_message(chat_id, msg_id)
                reason = "ok" if deleted else "edit_fallback"
            if not deleted:
                try:
                    tg.edit_reply_markup(
                        chat_id, msg_id,
                        job_keyboard(job_id, applied_status="skipped", url=job.url or None),
                    )
                except Exception as e:
                    log.warning("edit markup failed: %s", e)

        try:
            import forensic as _forensic
            _forensic.log_step(
                "bot.skip_message_deleted",
                input={"chat_id": chat_id, "job_id": job_id, "message_id": msg_id},
                output={"deleted": deleted, "morphed": morphed, "reason": reason},
                chat_id=chat_id if isinstance(chat_id, int) else None,
            )
        except Exception:
            pass

        if morphed:
            toast = "Tell me why?"
        elif deleted:
            toast = "✕ Removed"
        else:
            toast = "Hidden from future digests 🚫"
        tg.answer_callback(cb_id, toast)

    elif kind == "fit":  # Analyze fit — evaluate alignment & gaps, no rewrite
        user = db.get_user(chat_id)
        if not user or not user["resume_text"]:
            tg.answer_callback(cb_id, "Upload your CV first (PDF).", show_alert=True)
            return
        tg.answer_callback(cb_id, "Analyzing fit…")
        _start_fit_analysis(tg, db, chat_id, job_id, job, user["resume_text"])

    elif kind == "r":  # Tailor my resume — step 1: analyze and show suggestions
        user = db.get_user(chat_id)
        if not user or not user["resume_text"]:
            tg.answer_callback(cb_id, "Upload your CV first (PDF).", show_alert=True)
            return
        tg.answer_callback(cb_id, "Analyzing your resume vs. this role…")
        _start_tailor_dialog(tg, db, chat_id, job_id, job, dict(row), user["resume_text"])

    elif kind == "ra":  # tailor — Apply: send the rewritten resume as a document
        tg.answer_callback(cb_id, "Preparing your tailored resume…")
        _apply_tailor(tg, db, chat_id, msg_id, job_id, job)

    elif kind == "rd":  # tailor — Dismiss
        tg.answer_callback(cb_id, "Dismissed.")
        db.set_suggestion_status(chat_id, job_id, "dismissed")
        try:
            tg.edit_reply_markup(
                chat_id, msg_id,
                suggestions_keyboard(job_id, url=job.url or None, decided="dismissed"),
            )
        except Exception as e:
            log.warning("edit markup failed (rd): %s", e)

    elif kind == "noop":
        tg.answer_callback(cb_id, "")

    else:
        tg.answer_callback(cb_id, "Unknown action.")


# ---------- /marketresearch flow ----------
#
# Three-step UX:
#   1. /marketresearch (or /research, /mr, button) → _start_market_research.
#      If the user is missing a resume or a real profile we bail early; the
#      research subagents need both. Otherwise we hand off to
#      _ask_for_research_location which puts the user in
#      STATE_AWAITING_RESEARCH_LOCATION and shows the Cancel keyboard.
#   2. The next non-button, non-slash message in that state is interpreted
#      as the target location (or "." / "use profile" to reuse the profile's
#      location). After a safety-check pass it kicks a background thread.
#   3. The background thread runs `market_research.market_research_sync`,
#      renders the DOCX, sends it, logs to the `research_runs` audit table,
#      and releases the per-user lock.

_RESEARCH_LOCKS: dict[int, threading.Lock] = {}
_RESEARCH_LOCKS_GUARD = threading.Lock()

# Global cap on concurrent /marketresearch runs ACROSS users. One run is 10
# Opus workers + a manager — N parallel runs = N×11 Opus calls competing for
# one Anthropic API key, which trips 429s and burns the operator's quota.
# Default 2 (raise via env if you have headroom). Bounded so excess release()
# raises rather than silently incrementing past the cap.
_MAX_CONCURRENT_RESEARCH = max(1, int(os.environ.get("MAX_CONCURRENT_RESEARCH", "2")))
_RESEARCH_GLOBAL_SEM = threading.BoundedSemaphore(_MAX_CONCURRENT_RESEARCH)

# Per-user cooldown after a completed run. /marketresearch is expensive
# (~$0.50–$1 surrogate per run, 25–40 min wall clock); a single user
# triggering it on a loop drains the operator's budget. Default 1 hour.
_RESEARCH_COOLDOWN_SECONDS = max(0, int(os.environ.get("RESEARCH_COOLDOWN_SECONDS", "3600")))
_LAST_RESEARCH_FINISHED: dict[int, float] = {}
_LAST_RESEARCH_LOCK = threading.Lock()


def _research_lock(chat_id: int) -> threading.Lock:
    with _RESEARCH_LOCKS_GUARD:
        lk = _RESEARCH_LOCKS.get(chat_id)
        if lk is None:
            lk = _RESEARCH_LOCKS[chat_id] = threading.Lock()
        return lk


def _research_cooldown_remaining(chat_id: int) -> int:
    """Seconds until this user can run /marketresearch again. 0 = ready."""
    if _RESEARCH_COOLDOWN_SECONDS <= 0:
        return 0
    with _LAST_RESEARCH_LOCK:
        last = _LAST_RESEARCH_FINISHED.get(chat_id)
    if not last:
        return 0
    elapsed = int(time.time() - last)
    return max(0, _RESEARCH_COOLDOWN_SECONDS - elapsed)


def _mark_research_finished(chat_id: int) -> None:
    with _LAST_RESEARCH_LOCK:
        _LAST_RESEARCH_FINISHED[chat_id] = time.time()


def _has_real_profile(profile: dict | None) -> bool:
    """True if the user has a profile beyond the min_match_score stub."""
    if not profile:
        return False
    if set(profile.keys()) == {"min_match_score"}:
        return False
    return True


_RESEARCH_PROMPT = (
    "🔬 *Market research — deep scan*\n\n"
    "I'll run a \\~25\\-40 minute background job that fans out to *10 Opus "
    "sub\\-agents* \\(demand, salary at home & neighbours, current trends, "
    "historical shifts, skills match, projections, top employers, hiring bar, "
    "upskilling roadmap\\) and then hands their findings to a manager agent "
    "that writes a polished *\\.docx report* — cover, executive summary, key "
    "findings, sections, recommendations, risks, citations\\.\n\n"
    "📍 *Where should I scan?*\n"
    "Send a specific market like _Berlin, Germany_ or _Remote EU_ or _San "
    "Francisco Bay Area_\\. Send `\\.` or _use profile_ to reuse the location "
    "from your profile\\.\n\n"
    "⏳ One research run per user at a time\\. Send ✖️ Cancel to abort\\."
)


def _start_market_research(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Entry point for /marketresearch (and aliases / button).

    Gate-checks the user's resume and profile before handing off to the
    location-prompt step; the research subagents depend on both, so it's
    kinder to tell the user what's missing than to spin up a 25-minute run
    that produces a low-quality report.
    """
    db.upsert_user(chat_id)
    u = db.get_user(chat_id)
    has_resume = bool(u and u["resume_path"])
    if not has_resume:
        tg.send_message(
            chat_id,
            mdv2_escape(
                "📎 I need your resume first. Upload your CV as a PDF to this "
                "chat, then run /marketresearch again."
            ),
            reply_markup=REPLY_KEYBOARD,
        )
        return

    profile = profile_from_json(db.get_user_profile(chat_id))
    if not _has_real_profile(profile):
        tg.send_message(
            chat_id,
            mdv2_escape(
                "🤖 Run /prefs first so I know what role and constraints "
                "to research. Once your profile is built, come back to "
                "/marketresearch."
            ),
            reply_markup=REPLY_KEYBOARD,
        )
        return

    _ask_for_research_location(tg, db, chat_id)


def _ask_for_research_location(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Show the location prompt + Cancel keyboard; park the user in the
    STATE_AWAITING_RESEARCH_LOCATION state so the next non-button text
    message is interpreted as the research target."""
    db.upsert_user(chat_id)
    db.set_awaiting_state(chat_id, STATE_AWAITING_RESEARCH_LOCATION)
    tg.send_message(chat_id, _RESEARCH_PROMPT, reply_markup=PREFS_INPUT_KEYBOARD)


def _save_research_location_and_kick(
    tg: TelegramClient, db: DB, chat_id: int, text: str,
) -> None:
    """Validate the location text, resolve it (possibly falling back to the
    profile), and kick off the background research run.

    The location flows into Opus sub-agents equipped with WebSearch and
    WebFetch, so we run it through `safety_check.check_user_input` first —
    same gate as /prefs. On block we clear state and tell the user why.
    """
    verdict = check_user_input(text)
    if verdict.get("verdict") == "block":
        db.set_awaiting_state(chat_id, None)
        reason = str(verdict.get("reason") or "prompt-injection fingerprint")
        tg.send_message(
            chat_id,
            mdv2_escape(
                f"🛡️ I couldn't accept that location — it looked like a {reason}. "
                "Send a plain market name (e.g. 'Berlin, Germany' or 'Remote EU'), "
                "or re-run /marketresearch to try again."
            ),
            reply_markup=REPLY_KEYBOARD,
        )
        log.info("research location rejected by safety_check for chat %s: %s (method=%s)",
                 chat_id, reason, verdict.get("method"))
        return

    # Resolve the location. "." / "use profile" / whitespace-only all mean
    # "fall back to the profile's location".
    t = (text or "").strip()
    if t in (".", "") or t.lower() == "use profile":
        profile = profile_from_json(db.get_user_profile(chat_id)) or {}
        location = str(profile.get("location") or "").strip()
        # Profile schema has `locations` (list); fall back to the first entry
        # if the scalar `location` is missing.
        if not location:
            locs = profile.get("locations") or []
            if isinstance(locs, list) and locs:
                first = locs[0]
                if isinstance(first, str):
                    location = first.strip()
    else:
        location = t

    if not location:
        # Don't clear the state — give the user another try.
        tg.send_message(
            chat_id,
            mdv2_escape(
                "I couldn't find a location in your profile either. Send a "
                "specific market like 'Berlin, Germany' or 'Remote EU'."
            ),
            reply_markup=PREFS_INPUT_KEYBOARD,
        )
        return

    db.set_awaiting_state(chat_id, None)

    # Per-user cooldown gate. Same user can't burn /marketresearch back-to-back.
    cooldown_left = _research_cooldown_remaining(chat_id)
    if cooldown_left > 0:
        mins = (cooldown_left + 59) // 60
        tg.send_message(
            chat_id,
            mdv2_escape(
                f"⏳ /marketresearch is rate-limited per user. Try again in "
                f"~{mins} minute(s). One run is 10 Opus subagents — the cap "
                "keeps the operator's API budget intact."
            ),
            reply_markup=REPLY_KEYBOARD,
        )
        return

    lock = _research_lock(chat_id)
    if not lock.acquire(blocking=False):
        tg.send_message(
            chat_id,
            mdv2_escape(
                "⏳ A research run is already in progress — check back in ~25-40 min."
            ),
            reply_markup=REPLY_KEYBOARD,
        )
        return

    # Global concurrent-run cap across ALL users. Protects the operator's
    # single Anthropic API key from 10*N parallel Opus calls.
    if not _RESEARCH_GLOBAL_SEM.acquire(blocking=False):
        lock.release()
        tg.send_message(
            chat_id,
            mdv2_escape(
                f"⏳ {_MAX_CONCURRENT_RESEARCH} research runs are already "
                "active across users. Try again in ~25-40 minutes."
            ),
            reply_markup=REPLY_KEYBOARD,
        )
        return

    try:
        placeholder_id = tg.send_message(
            chat_id,
            mdv2_escape(
                f"🔬 Market research starting for {location} — fanning out to "
                "10 Opus subagents. Expect ~25-40 minutes. I'll post progress "
                "updates here and attach a .docx report at the end."
            ),
            reply_markup=REPLY_KEYBOARD,
        )
    except Exception:
        log.exception("market_research: placeholder send failed for chat=%s", chat_id)
        lock.release()
        try:
            _RESEARCH_GLOBAL_SEM.release()
        except ValueError:
            pass
        return

    threading.Thread(
        target=_run_market_research_work,
        args=(tg, db, chat_id, location, placeholder_id),
        daemon=True,
        name=f"market-research-{chat_id}",
    ).start()


def _run_market_research_work(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    location: str,
    placeholder_msg_id: int,
) -> None:
    """Background worker: run the orchestrator, render the DOCX, send it,
    log to research_runs, release the lock.

    The import of `market_research` / `market_research_render` is deferred
    to first-use here to avoid a top-level cycle (the same pattern
    `trigger_job_check` uses for `search_jobs`).
    """
    lock = _research_lock(chat_id)
    started_at = time.time()
    run = None
    try:
        # Lazy imports — mirrors the `from search_jobs import run as run_search`
        # pattern in trigger_job_check.
        import market_research as _mr
        import market_research_render as _mrr

        row = db.get_user(chat_id)
        resume_text = ""
        try:
            resume_text = (row["resume_text"] or "") if row is not None else ""
        except (IndexError, KeyError):
            resume_text = ""
        profile = profile_from_json(db.get_user_profile(chat_id)) or {}

        # Throttled progress updates — edit the placeholder at most once per
        # 15 s so we don't spam the chat during a 30-minute run.
        last_edit_ts = 0.0

        def progress_cb(completed: int, total: int) -> None:
            nonlocal last_edit_ts
            now = time.monotonic()
            if now - last_edit_ts < 15.0:
                return
            last_edit_ts = now
            body = mdv2_escape(
                f"🔬 Market research in progress… {completed}/{total} "
                "subagents done. This usually takes 25-40 min."
            )
            try:
                tg.edit_message_text(chat_id, placeholder_msg_id, body)
            except Exception as e:
                if "not modified" not in str(e):
                    log.debug("market_research: progress edit failed: %s", e)

        try:
            run = _mr.market_research_sync(
                chat_id, resume_text, profile, location,
                progress=progress_cb,
            )
        except Exception as e:
            log.exception("market_research_sync crashed for chat=%s", chat_id)
            finished_at = time.time()
            try:
                tg.send_message(
                    chat_id,
                    mdv2_escape(f"⚠️ Research crashed: {e}"),
                    reply_markup=REPLY_KEYBOARD,
                )
            except Exception:
                pass
            try:
                db.log_research_run(
                    chat_id,
                    status="exception",
                    location_used=location,
                    error_head=f"{e!r}"[:200],
                    started_at=started_at,
                    finished_at=finished_at,
                )
            except Exception:
                log.exception("log_research_run failed after exception")
            return

        finished_at = time.time()
        docx_path: Path | None = None

        if run.status in ("ok", "partial"):
            rdir = user_dir(chat_id) / "research"
            rdir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(finished_at))
            docx_path = rdir / f"market_research_{stamp}.docx"
            try:
                _mrr.render_research_docx(run, docx_path)
            except Exception as e:
                log.exception("market_research: render_research_docx failed")
                docx_path = None
                try:
                    tg.edit_message_text(
                        chat_id, placeholder_msg_id,
                        mdv2_escape(f"⚠️ Could not render the report: {e}"),
                    )
                except Exception:
                    pass

            if docx_path is not None:
                elapsed_min = max(1, int(round((run.elapsed_ms or 0) / 60000)))
                n_ok = len(run.workers_ok)
                n_total = n_ok + len(run.workers_failed)
                caption = (
                    f"Market research — {run.status} · "
                    f"{run.model or 'opus'} · {elapsed_min} min · "
                    f"{n_ok}/{n_total} subagents ok"
                )
                try:
                    tg.send_document(chat_id, docx_path, caption=caption)
                except Exception as e:
                    log.exception("market_research: send_document failed")
                    try:
                        tg.send_message(
                            chat_id,
                            mdv2_escape(f"⚠️ Could not attach the report: {e}"),
                            reply_markup=REPLY_KEYBOARD,
                        )
                    except Exception:
                        pass

                # Short MDv2 summary so the user gets a reading-ready snapshot
                # in-chat without opening the .docx.
                summary_lines: list[str] = []
                if run.status == "partial":
                    failed_topics = [
                        str(f.get("topic") or "?") for f in (run.workers_failed or [])
                    ]
                    manager_missing = not isinstance(run.manager_report, dict) or not run.manager_report
                    if failed_topics:
                        reason = (
                            f"failed subagents: {', '.join(failed_topics)}."
                        )
                    elif manager_missing:
                        reason = (
                            "synthesis step failed — delivering the raw outputs "
                            "from each worker agent instead of a manager-synthesized "
                            "report."
                        )
                    else:
                        reason = "some optional sections are missing."
                    summary_lines.append(
                        "⚠️ *" + mdv2_escape("Partial result") + "* — "
                        + mdv2_escape(reason)
                    )
                    summary_lines.append("")

                mr_report = run.manager_report or {}
                execs = mr_report.get("executive_summary") or []
                if isinstance(execs, list) and execs:
                    summary_lines.append("🧠 *" + mdv2_escape("Executive summary") + "*")
                    for item in execs[:3]:
                        if isinstance(item, str) and item.strip():
                            summary_lines.append("• " + mdv2_escape(item.strip()[:300]))
                    summary_lines.append("")
                else:
                    # No manager synthesis → surface the worker headlines so the
                    # in-chat summary isn't blank.
                    worker_results = run.worker_results or {}
                    head_lines: list[str] = []
                    for topic in ("demand", "current_trends", "skills_match",
                                   "projections", "salary_home"):
                        wr = worker_results.get(topic)
                        if not isinstance(wr, dict):
                            continue
                        for key in ("headline_summary", "headline", "narrative", "summary"):
                            v = wr.get(key)
                            if isinstance(v, str) and v.strip():
                                head_lines.append(v.strip()[:240])
                                break
                        if len(head_lines) >= 3:
                            break
                    if head_lines:
                        summary_lines.append("🧠 *" + mdv2_escape("Worker highlights") + "*")
                        for h in head_lines:
                            summary_lines.append("• " + mdv2_escape(h))
                        summary_lines.append("")
                summary_lines.append(mdv2_escape("Full report attached (.docx)."))

                try:
                    tg.send_message(
                        chat_id, "\n".join(summary_lines), reply_markup=REPLY_KEYBOARD,
                    )
                except Exception:
                    log.exception("market_research: summary send failed")

        else:
            # failed | exception | cli_missing — no DOCX.
            err_head = (run.error or "")[:200]
            if run.status == "cli_missing":
                body = (
                    "⚠️ Market research couldn't start — the `claude` CLI "
                    "isn't available. Ask the admin to install / log in "
                    "(then retry /marketresearch)."
                )
            elif run.status == "exception":
                body = (
                    "⚠️ Market research hit an unexpected error and couldn't "
                    "produce a report. Try again in a few minutes; if it "
                    "keeps failing, let the admin know."
                )
            else:
                body = (
                    "⚠️ Market research couldn't produce a report — too many "
                    "subagents failed. Network issues with the data sources "
                    "are the usual cause. Try /marketresearch again in a few "
                    "minutes."
                )
            if err_head:
                body += f"\n\nDetails: {err_head}"
            try:
                tg.edit_message_text(
                    chat_id, placeholder_msg_id, mdv2_escape(body),
                )
            except Exception:
                try:
                    tg.send_message(chat_id, mdv2_escape(body), reply_markup=REPLY_KEYBOARD)
                except Exception:
                    log.exception("market_research: failure message send failed")

        # Audit row — always written, regardless of outcome.
        try:
            db.log_research_run(
                chat_id,
                status=run.status,
                location_used=run.location_used or location,
                model=run.model,
                elapsed_ms=run.elapsed_ms,
                workers_ok=list(run.workers_ok or []),
                workers_failed=list(run.workers_failed or []),
                docx_path=str(docx_path) if docx_path is not None else None,
                resume_sha1=run.resume_sha1,
                prefs_sha1=run.prefs_sha1,
                error_head=run.error,
                started_at=started_at,
                finished_at=finished_at,
            )
        except Exception:
            log.exception("log_research_run failed for chat=%s", chat_id)

    except Exception as e:
        log.exception("market_research: background worker crashed")
        try:
            tg.send_message(
                chat_id, mdv2_escape(f"⚠️ Research crashed: {e}"),
                reply_markup=REPLY_KEYBOARD,
            )
        except Exception:
            pass
    finally:
        try:
            lock.release()
        except Exception:
            pass
        # Release the global concurrency slot so the next queued user can run.
        # BoundedSemaphore.release() raises ValueError on excess release —
        # swallow defensively so a buggy caller never crashes the worker.
        try:
            _RESEARCH_GLOBAL_SEM.release()
        except ValueError:
            pass
        _mark_research_finished(chat_id)


# ---------- fit-analysis helpers ----------
#
# Separate from the tailor flow (different button, different output). Shares
# the same "placeholder → background thread → edit in place" UX pattern but
# without the Apply/Dismiss step — fit analysis is read-only.
#
# Cache semantics (see db.fit_analyses):
#   - A cache hit with the CURRENT resume_sha1 → render instantly, no Claude call.
#   - A cache hit with a stale hash → ignore, run fresh.
#   - No cached row → run fresh and persist.

_FIT_LOCKS: dict[tuple[int, str], threading.Lock] = {}
_FIT_LOCKS_GUARD = threading.Lock()


def _fit_lock(chat_id: int, job_id: str) -> threading.Lock:
    """Per-(chat, job) lock so double-taps on Analyze fit don't spin two
    parallel Claude calls. Kept in-process (rare contention, restarts clear)."""
    key = (chat_id, job_id)
    with _FIT_LOCKS_GUARD:
        lk = _FIT_LOCKS.get(key)
        if lk is None:
            lk = _FIT_LOCKS[key] = threading.Lock()
        return lk


def _start_fit_analysis(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    job_id: str,
    job: Job,
    resume_text: str,
) -> None:
    """Run the fit analysis for this (user, job) and post the result.

    Cache-fast path: if we have an analysis in the DB whose resume_sha1
    matches the user's current resume, reply instantly with the stored
    render. No lock needed — cache reads are cheap.

    Cache-miss path: acquire the per-(chat, job) lock, send a placeholder,
    fire a background worker that calls the Claude CLI, render + store on
    success, edit the placeholder with the result.
    """
    current_sha1 = _fit.resume_sha1(resume_text)

    # --- Fast path: cache hit ---
    cached = db.get_fit_analysis(chat_id, job_id, current_resume_sha1=current_sha1)
    if cached is not None:
        try:
            analysis = _json.loads(cached["analysis_json"])
        except Exception:
            analysis = None
        if isinstance(analysis, dict):
            try:
                job_meta = {"title": job.title or "", "company": job.company or ""}
                body = _fit.render_analysis_mdv2(analysis, job_meta)
                # Note at the top of the bubble so users know it's instant-cached.
                body = mdv2_escape("Cached result — resume hasn't changed.") + "\n\n" + body
                tg.send_message(chat_id, body)
                return
            except Exception:
                log.exception("fit: cached render failed; falling through to fresh run")

    # --- Slow path: run the analysis ---
    lock = _fit_lock(chat_id, job_id)
    if not lock.acquire(blocking=False):
        tg.send_plain(chat_id, "Already analyzing this role — hold on a moment.")
        return

    try:
        placeholder_id = tg.send_message(
            chat_id,
            mdv2_escape(
                "Analyzing fit — comparing the posting against your resume. "
                "This usually takes under a minute."
            ),
        )
    except Exception:
        log.exception("fit: placeholder send failed")
        lock.release()
        return

    def _work():
        try:
            job_dict = {
                "title":    job.title or "",
                "company":  job.company or "",
                "location": job.location or "",
                "url":      job.url or "",
                "snippet":  job.snippet or "",
            }
            analysis = _fit.build_fit_analysis_ai(resume_text, job_dict)
            if analysis is None:
                try:
                    tg.edit_message_text(
                        chat_id, placeholder_id,
                        mdv2_escape(
                            "Couldn't analyze this posting. The `claude` CLI "
                            "may be missing or the model returned an "
                            "unparseable response. Try again in a minute."
                        ),
                    )
                except Exception:
                    pass
                return

            # Persist before sending so repeat taps hit the cache even if
            # the edit below fails.
            try:
                db.upsert_fit_analysis(
                    chat_id, job_id,
                    analysis_json=_json.dumps(analysis, ensure_ascii=False),
                    resume_sha1=_fit.resume_sha1(resume_text),
                )
            except Exception:
                log.exception("fit: cache upsert failed (non-fatal)")

            body = _fit.render_analysis_mdv2(analysis, job_dict)
            try:
                tg.edit_message_text(chat_id, placeholder_id, body)
            except Exception as e:
                log.exception("fit: edit_message_text failed: %s", e)
                # Last-ditch attempt — send as a new message.
                try:
                    tg.send_message(chat_id, body)
                except Exception:
                    pass
        except Exception as e:
            log.exception("fit analyze failed")
            try:
                tg.edit_message_text(
                    chat_id, placeholder_id,
                    mdv2_escape(f"Fit analysis failed: {e}"),
                )
            except Exception:
                pass
        finally:
            lock.release()

    threading.Thread(target=_work, daemon=True, name=f"fit-{chat_id}-{job_id}").start()


# ---------- tailor-dialog helpers ----------

_TAILOR_LOCKS: dict[tuple[int, str], threading.Lock] = {}
_TAILOR_LOCKS_GUARD = threading.Lock()


def _tailor_lock(chat_id: int, job_id: str) -> threading.Lock:
    key = (chat_id, job_id)
    with _TAILOR_LOCKS_GUARD:
        lk = _TAILOR_LOCKS.get(key)
        if lk is None:
            lk = _TAILOR_LOCKS[key] = threading.Lock()
        return lk


def _start_tailor_dialog(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    job_id: str,
    job: Job,
    row: dict,
    resume_text: str,
) -> None:
    """Send a placeholder message, then run the AI tailor in a background
    thread and edit the placeholder with the suggestions + Apply/Dismiss buttons.
    """
    lock = _tailor_lock(chat_id, job_id)
    if not lock.acquire(blocking=False):
        tg.send_plain(chat_id, "⏳ Already tailoring this role — hold on a moment.")
        return

    try:
        placeholder_id = tg.send_message(
            chat_id,
            "🤖 _Analyzing your resume vs\\. this role — this can take up to a minute\\._",
        )
    except Exception:
        log.exception("tailor: placeholder send failed")
        lock.release()
        return

    def _work():
        try:
            plan = build_tailor_plan_ai(resume_text, row)
            if plan is None:
                # Fallback: use the heuristic note and offer it as a direct attachment
                # (no Apply button — nothing to apply against).
                note = build_tailor_note(resume_text, row)
                out = user_dir(chat_id) / "tailored" / f"{job_id}.md"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(note)
                try:
                    tg.edit_message_text(
                        chat_id, placeholder_id,
                        mdv2_escape(
                            "AI tailor unavailable (is the `claude` CLI installed and logged in?). "
                            "Sending the heuristic tailoring note instead."
                        ),
                        reply_markup={"inline_keyboard": []},
                    )
                except Exception:
                    pass
                tg.send_document(chat_id, out, caption=f"Tailoring note: {job.title[:60]}")
                return

            db.upsert_suggestion(
                chat_id, job_id,
                plan_json=_json.dumps(plan, ensure_ascii=False),
                status="pending",
                message_id=placeholder_id,
            )
            body = render_suggestions_mdv2(job, plan)
            tg.edit_message_text(
                chat_id, placeholder_id, body,
                reply_markup=suggestions_keyboard(job_id, url=job.url or None),
            )
        except Exception as e:
            log.exception("tailor analyze failed")
            try:
                tg.edit_message_text(
                    chat_id, placeholder_id,
                    mdv2_escape(f"⚠️ Tailoring failed: {e}"),
                    reply_markup={"inline_keyboard": []},
                )
            except Exception:
                pass
        finally:
            lock.release()

    threading.Thread(target=_work, daemon=True, name=f"tailor-{chat_id}-{job_id}").start()


def _apply_tailor(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    msg_id: int,
    job_id: str,
    job: Job,
) -> None:
    row = db.get_suggestion(chat_id, job_id)
    if row is None:
        tg.send_plain(chat_id, "That tailor plan is no longer available. Click ✍️ Tailor my resume again.")
        return
    try:
        plan = _json.loads(row["plan_json"])
    except Exception:
        tg.send_plain(chat_id, "Could not read the stored tailor plan.")
        return

    md = (plan.get("tailored_resume_markdown") or "").strip()
    if not md:
        tg.send_plain(chat_id, "The plan didn't include a rewritten resume — try tailoring again.")
        return

    out = user_dir(chat_id) / "tailored" / f"{job_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# Tailored resume — {job.title or 'Role'}\n"
        f"**Company:** {job.company or ''}  \n"
        f"**Location:** {job.location or ''}  \n"
        f"**Posting:** {job.url or ''}\n\n"
        "---\n\n"
    )
    out.write_text(header + md)

    db.set_suggestion_status(chat_id, job_id, "applied")

    try:
        tg.edit_reply_markup(
            chat_id, msg_id,
            suggestions_keyboard(job_id, url=job.url or None, decided="applied"),
        )
    except Exception as e:
        log.warning("edit markup failed (ra): %s", e)

    try:
        caption = f"Tailored resume for {job.title[:60]}"
        tg.send_document(chat_id, out, caption=caption)
    except Exception as e:
        log.exception("tailor: send_document failed")
        tg.send_plain(chat_id, f"⚠️ Could not send the file: {e}")


# ---------- main loop ----------

def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN missing in .env")
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = DB(DB_PATH)
    tg = TelegramClient(token=token)

    log.info("Bot started. Polling for updates…")
    running = True

    def _stop(sig, frame):
        nonlocal running
        log.info("Signal %s received, stopping.", sig)
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    offset: int | None = None
    while running:
        try:
            updates = tg.get_updates(offset=offset, timeout=25)
        except Exception as e:
            log.error("getUpdates failed: %s", e)
            time.sleep(5)
            continue

        store = _get_store(db)
        for upd in updates:
            offset = upd["update_id"] + 1
            # Extract chat_id best-effort for the alert envelope; tolerate any
            # update shape (callback_query, message, edited_message).
            try:
                upd_chat_id = (
                    (upd.get("callback_query") or {}).get("message", {}).get("chat", {}).get("id")
                    or (upd.get("message") or {}).get("chat", {}).get("id")
                    or (upd.get("edited_message") or {}).get("chat", {}).get("id")
                )
            except Exception:
                upd_chat_id = None
            try:
                with error_capture(
                    store,
                    where="bot._dispatch",
                    chat_id=upd_chat_id,
                    alert_sink=lambda env, _tg=tg, _store=store: _ops_deliver_alert(_tg, _store, env),
                ):
                    _dispatch(tg, db, upd)
            except Exception:
                # error_capture already recorded + alerted; this just keeps the
                # main loop alive (mirrors prior behavior).
                log.exception("update handler crashed; continuing")

    log.info("Bot stopped cleanly.")
    return 0


def _dispatch(tg: TelegramClient, db: DB, upd: dict) -> None:
    if "callback_query" in upd:
        handle_callback(tg, db, upd["callback_query"])
        return
    msg = upd.get("message") or upd.get("edited_message")
    if not msg:
        return
    chat_id = msg.get("chat", {}).get("id")
    if not chat_id:
        return
    user = msg.get("from") or {}
    if "document" in msg:
        handle_document(tg, db, chat_id, msg["document"], user)
        return
    text = (msg.get("text") or "").strip()
    if not text:
        return

    # Cancel always wins, even mid-state. Accept both the new "✕ Cancel"
    # button and the legacy "✖️ Cancel" label to avoid stranding users on
    # cached keyboards.
    if text == BTN_CANCEL or _LEGACY_BUTTONS.get(text) == "cancel":
        prior_state = db.get_awaiting_state(chat_id)
        if prior_state:
            db.set_awaiting_state(chat_id, None)
            # Skip-reason has its own copy ("no feedback recorded") — the
            # generic "Cancelled." is too cold for a flow we just nudged
            # the user into. Other states keep the existing message.
            if prior_state == STATE_AWAITING_SKIP_REASON:
                tg.send_message(
                    chat_id,
                    mdv2_escape("Got it, no feedback recorded."),
                    reply_markup=REPLY_KEYBOARD,
                )
            else:
                tg.send_message(
                    chat_id,
                    f"{PIG}  " + mdv2_escape("Cancelled."),
                    reply_markup=REPLY_KEYBOARD,
                )
        else:
            tg.send_message(
                chat_id,
                f"{PIG}  " + mdv2_escape("Nothing to cancel."),
                reply_markup=REPLY_KEYBOARD,
            )
        return

    # --- ONBOARDING WIZARD: free-text capture ---
    #
    # The wizard uses inline buttons for most questions but falls back to
    # free text for Role and Location. When the user is parked in one of
    # those states, route their next text message to the matching handler.
    # Slash commands and main-menu buttons escape the state (implicit
    # cancel of the wizard question, not the whole wizard).
    onboarding_await = _onboarding.current_await_state(db, chat_id)
    if onboarding_await is not None:
        escape_labels = {BTN_CHECK_NOW, BTN_MY_APPS, BTN_PROFILE,
                         BTN_RESEARCH, BTN_SETTINGS} | set(_LEGACY_BUTTONS.keys())
        if text in escape_labels or text.startswith("/"):
            db.set_awaiting_state(chat_id, None)
            # Fall through to normal dispatch below — the wizard's inline
            # prompt stays in chat history but won't block further actions.
        else:
            if onboarding_await == _onboarding.AWAIT_ONBOARDING_ROLE:
                _onboarding.handle_text_role(tg, db, chat_id, text)
                return
            if onboarding_await == _onboarding.AWAIT_ONBOARDING_LOCATION:
                _onboarding.handle_text_location(tg, db, chat_id, text)
                return

    # --- CLASSIC /prefs and /marketresearch text flows ---
    state = db.get_awaiting_state(chat_id)
    if state == STATE_AWAITING_PREFS:
        escape_labels = {BTN_CHECK_NOW, BTN_MY_APPS, BTN_PROFILE,
                         BTN_RESEARCH, BTN_SETTINGS} | set(_LEGACY_BUTTONS.keys())
        if text in escape_labels or text.startswith("/"):
            db.set_awaiting_state(chat_id, None)
            # …then fall through to normal dispatch below.
        else:
            _save_prefs_from_text(tg, db, chat_id, text)
            return
    elif state == STATE_AWAITING_RESEARCH_LOCATION:
        escape_labels = {BTN_CHECK_NOW, BTN_MY_APPS, BTN_PROFILE,
                         BTN_RESEARCH, BTN_SETTINGS} | set(_LEGACY_BUTTONS.keys())
        if text in escape_labels or text.startswith("/"):
            db.set_awaiting_state(chat_id, None)
            # …fall through to normal dispatch below.
        else:
            _save_research_location_and_kick(tg, db, chat_id, text)
            return
    elif state == STATE_AWAITING_SKIP_REASON:
        # Stale-prompt guard mirroring the prefs/research escape: any main-menu
        # button or slash-command silently cancels the awaiting-skip-reason
        # state and lets the regular dispatcher handle whatever the user did
        # ask for. Cancel button was already handled above (BTN_CANCEL).
        escape_labels = {BTN_CHECK_NOW, BTN_MY_APPS, BTN_PROFILE,
                         BTN_RESEARCH, BTN_SETTINGS} | set(_LEGACY_BUTTONS.keys())
        if text in escape_labels or text.startswith("/"):
            db.set_awaiting_state(chat_id, None)
            # …fall through to normal dispatch below.
        else:
            _handle_skip_reason_text(tg, db, chat_id, text)
            return

    # --- NEW REPLY-KEYBOARD BUTTONS ---
    if text == BTN_CHECK_NOW:
        trigger_job_check(tg, db, chat_id)
        return
    if text == BTN_MY_APPS:
        _send_applied_list(tg, db, chat_id)
        return
    if text == BTN_PROFILE:
        _show_profile(tg, db, chat_id)
        return
    if text == BTN_RESEARCH:
        _start_market_research(tg, db, chat_id)
        return
    if text == BTN_SETTINGS:
        _show_settings_menu(tg, db, chat_id)
        return

    # --- LEGACY BUTTONS (users with cached old keyboards) ---
    legacy = _LEGACY_BUTTONS.get(text)
    if legacy == "search":
        trigger_job_check(tg, db, chat_id)
        return
    if legacy == "applied":
        _send_applied_list(tg, db, chat_id)
        return
    if legacy == "prefs":
        _ask_for_prefs(tg, db, chat_id)
        return
    if legacy == "minscore":
        _ask_min_score(tg, db, chat_id)
        return
    if legacy == "cleandata":
        _ask_clean_data(tg, db, chat_id)
        return

    if text.startswith("/"):
        handle_command(tg, db, chat_id, text, user)
        return

    # Free-form text — if the user has a stalled wizard, nudge them back
    # into it before giving up. Otherwise keep the fallback short.
    if _onboarding.maybe_resume(tg, db, chat_id):
        return
    tg.send_plain(
        chat_id,
        "I only act on buttons, commands, and PDF uploads. "
        "Send /help to see what's available.",
    )


if __name__ == "__main__":
    sys.exit(main())
