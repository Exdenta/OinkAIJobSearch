"""Guided onboarding wizard.

The old /start shipped one dense welcome bubble and expected the user to
figure out the rest (upload PDF → /prefs → /minscore → wait). This module
replaces that with a multi-step wizard that advances one question at a time,
shows progress, and prefers inline-button answers over free text wherever
possible.

Steps (total: 5)
----------------
1. seniority  — asked INSIDE the welcome message: short pitch + inline
                buttons (Junior / Mid / Senior / Staff+ / Any). No
                "Get started" interstitial — every observed drop-off
                happened on that tap, so the first message IS question 1.
2. role       — free text ("React Engineer", "DevRel", …) with [Skip].
3. remote     — inline buttons: Remote / Hybrid / Onsite / Any.
4. location   — free text with [Remote worldwide] shortcut.
5. resume     — PDF upload, deliberately LAST (heaviest ask, deferred until
                the user has invested four taps) and skippable.
done          — summary card (no buttons). Kicks off the Opus profile rebuild
                in the background AND auto-fires the first search so the payoff
                is immediate. Min-score is NOT asked here — a new user hasn't
                seen a scored job yet; it defaults to 3 and is tuned later
                via Settings → Min match score (STEP_MINSCORE/CB_MIN_SCORE
                remain only for in-flight legacy wizards).

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

`nudge_stalled(tg, db)` is the proactive counterpart: maybe_resume only fires
when the stalled user sends ANOTHER message — which stalled users never do.
bot.py runs this in a periodic daemon thread; it pings each stalled user
once (a few hours after their last step) and replays the pending question.
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

# Ordered for progress-dot rendering and re-entry. One-tap questions come
# first (instant engagement); the CV upload — the heaviest ask — is last,
# after the user has already invested five answers.
# Min-score is NOT here: a brand-new user can't pick a match threshold before
# they've seen a single scored job. It defaults to 3 in _finalize and is tuned
# post-onboarding via Settings → Min match score (/minscore). STEP_MINSCORE +
# CB_MIN_SCORE still exist for in-flight legacy wizards parked on that step.
_STEP_ORDER = [
    STEP_SENIORITY,  # 1
    STEP_ROLE,       # 2
    STEP_REMOTE,     # 3
    STEP_LOCATION,   # 4
    STEP_RESUME,     # 5
]
TOTAL_STEPS = len(_STEP_ORDER)

# Awaiting-state codes used by bot.py to route free-text messages to the
# right step handler. Keyed off the existing `users.awaiting_state` column
# so we don't have to add a second plumbing path.
AWAIT_ONBOARDING_ROLE     = "onboarding_role"
AWAIT_ONBOARDING_LOCATION = "onboarding_location"

# Callback-data prefixes. Kept short (Telegram caps callback_data at 64 bytes).
#
#   ob:start            — legacy "Get started" button (old welcome bubbles
#                         still on users' screens); routes to question 1
#   ob:sen:<level>      — junior | mid | senior | staff | any
#   ob:skiprole         — user tapped "Skip" on the role free-text step
#   ob:rmt:<policy>     — remote | hybrid | onsite | any
#   ob:locww            — shortcut for "Remote worldwide"
#   ob:ms:<n>           — min match score picked (0..5)
#   ob:skipcv           — resume step: finish without uploading a CV
#   ob:runsearch        — final step: trigger a live search
#   ob:wait             — final step: "I'll wait for tomorrow"
#   ob:cancel           — abort the wizard at any point
CB_START       = "start"
CB_KEEP_CV     = "keepcv"     # resume step: user chose to keep the existing CV on file
CB_SKIP_CV     = "skipcv"     # resume step: finish setup without a CV
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

# Proactive nudge window (see nudge_stalled): ping once when the wizard has
# sat idle for 4h+, but leave week-old corpses alone — a ping that late
# reads as spam, not help.
NUDGE_AFTER_SECONDS   = 4 * 3600
NUDGE_MAX_AGE_SECONDS = 7 * 86400

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


# ---------- localization (Telegram language_code) ----------
#
# Signups skew ES/RU but the wizard copy is authored in English. We translate
# the RAW English (before MarkdownV2 escaping) via the small Mistral model,
# cache each string once per language in the ui_translations table, and escape
# the result at the call site. Everything fails OPEN to English: missing lang,
# 'en*', empty text, LLM error, or cache error → the exact bytes we ship today.
#
# Plumbing is a `tr` callable threaded into the message/keyboard builders. For
# English (or unknown lang) `_translator` returns None and the builders use an
# identity function, so the English path is byte-identical and makes zero LLM
# calls. See project CLAUDE.md: the model does the translating, not a Python
# phrasebook.

import hashlib as _hashlib

_TRANSLATE_PROMPT = """\
Translate the text below into the language with IETF/BCP-47 code "{lang}".
Output ONLY the translated text — no quotes, no explanations, no markdown
fences, no commentary. Keep the tone friendly and concise. Preserve emoji,
URLs, numbers, and proper nouns as-is.

Text:
{text}
"""


def _llm_translate(text: str, lang: str) -> str | None:
    """One-shot LLM translation of RAW English into `lang`. Disabled in this
    single-provider build — returns None so the caller keeps English
    (fail-open, same shape as an LLM failure). Monkeypatched in tests."""
    return None


def translate(db: DB, text: str, lang: str | None) -> str:
    """Return `text` translated into `lang`, cached per (lang, sha1(text)).

    Fail-open to the exact English `text` on: empty text, English/unknown lang,
    LLM failure, or any DB error. A successful translation is cached so the same
    string never hits the model twice for a language. A FAILED translation is
    NOT cached (so it retries next time)."""
    text = text or ""
    norm = (lang or "").strip().lower()
    if not text.strip() or not norm or norm.startswith("en"):
        return text
    sha1 = _hashlib.sha1(text.encode("utf-8")).hexdigest()
    try:
        cached = db.get_ui_translation(norm, sha1)
    except Exception:
        cached = None
    if cached is not None:
        return cached
    try:
        out = _llm_translate(text, norm)
    except Exception:
        out = None
    if not out:
        return text
    try:
        db.set_ui_translation(norm, sha1, out)
    except Exception:
        log.debug("ui-translation cache write failed", exc_info=True)
    return out


def _user_lang(db: DB, chat_id: int) -> str | None:
    """The user's Telegram language_code, or None (unknown / no row / no col)."""
    try:
        u = db.get_user(chat_id)
        return u["language_code"] if u is not None else None
    except Exception:
        return None


def _translator(db: DB, chat_id: int):
    """A `tr(text) -> str` callable bound to the user's language, or None for
    English/unknown langs. None signals builders to use the identity path —
    keeping the English output byte-identical and LLM-free."""
    lang = _user_lang(db, chat_id)
    norm = (lang or "").strip().lower()
    if not norm or norm.startswith("en"):
        return None
    return lambda s: translate(db, s, norm)


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
    now = time.time()
    state["last_step_at"] = now
    # Funnel breadcrumb: log each step the user actually lands on so the ops
    # problems report can see where drop-offs happen. Every state mutation
    # routes through _save, so appending here covers all paths. Bounded by
    # the wizard length — only a genuine step *change* appends (re-prompts
    # of the same step don't).
    step = state.get("step")
    hist = state.setdefault("history", [])
    if step is not None and (not hist or hist[-1].get("step") != step):
        hist.append({"step": step, "ts": now})
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

def _welcome_mdv2(first_name: str | None = None, *, tr=None) -> str:
    """Short pitch + question 1 in ONE message. There is deliberately no
    "Get started" interstitial and no step-preview list — every observed
    drop-off happened on that tap, so the first thing the user sees is a
    question they can answer with a single button press."""
    _t = tr or (lambda s: s)
    name = (first_name or "").strip()
    hello = mdv2_escape(_t("Welcome"))
    greeting = f"{PIG}  {hello}, {mdv2_escape(name)}" if name else f"{PIG}  {hello}"
    body = (
        "Every morning I scan LinkedIn, Indeed, Hacker News and remote "
        "boards, and send you the postings that actually match you.\n\n"
        "Five quick questions to tune your matches — most are one tap. "
        "First one:"
    )
    return f"*{greeting}*\n\n" + mdv2_escape(_t(body)) + "\n\n" + _seniority_prompt_mdv2(tr=tr)


def _resume_prompt_mdv2(*, tr=None) -> str:
    _t = tr or (lambda s: s)
    lines = [
        _progress_line(STEP_RESUME),
        "",
        "*" + mdv2_escape(_t("Last step — upload your CV")) + "*",
        "",
        mdv2_escape(_t(
            "Send your resume as a PDF to this chat. I score every posting "
            "against your actual experience, so matches get much sharper "
            "with it.\n\n"
            "No CV handy? Skip for now — you can send a PDF here any time "
            "later. Your file stays on the bot server; /cleardata wipes it."
        )),
    ]
    return "\n".join(lines)


def _resume_keyboard(*, tr=None) -> dict:
    _t = tr or (lambda s: s)
    return {"inline_keyboard": [
        [{"text": _t("Skip for now"),  "callback_data": f"ob:{CB_SKIP_CV}"}],
        [{"text": _t("Cancel setup"),  "callback_data": f"ob:{CB_CANCEL}"}],
    ]}


def _resume_review_prompt_mdv2(resume_filename: str, word_count: int, *, tr=None) -> str:
    """Shown when the user already has a CV on file.

    Re-asking someone to upload the same PDF is friction; silently skipping
    the step confuses them. Compromise: acknowledge the stored file, show
    its filename + parsed word count as a credibility check, and let them
    choose to keep it or upload a replacement.
    """
    _t = tr or (lambda s: s)
    # The filename is user data (not translated); only the surrounding copy is.
    lines = [
        _progress_line(STEP_RESUME),
        "",
        "*" + mdv2_escape(_t("Your CV")) + "*",
        "",
        mdv2_escape(
            _t("I already have a CV on file:") + f" {resume_filename} "
            + _t(f"({word_count} words parsed).") + "\n\n"
            + _t(
                "Keep using this one, or upload a replacement PDF now? "
                "Uploading a new file overwrites the old one."
            )
        ),
    ]
    return "\n".join(lines)


def _resume_review_keyboard(*, tr=None) -> dict:
    _t = tr or (lambda s: s)
    return {"inline_keyboard": [
        [{"text": _t("Keep this CV"), "callback_data": f"ob:{CB_KEEP_CV}"}],
        [{"text": _t("Cancel setup"), "callback_data": f"ob:{CB_CANCEL}"}],
    ]}


def _seniority_prompt_mdv2(*, tr=None) -> str:
    _t = tr or (lambda s: s)
    lines = [
        _progress_line(STEP_SENIORITY),
        "",
        f"{ICON_SENIORITY} *" + mdv2_escape(_t("Seniority level")) + "*",
        "",
        mdv2_escape(_t("Pick the bucket that best matches your experience.")),
    ]
    return "\n".join(lines)


def _seniority_keyboard(*, tr=None) -> dict:
    _t = tr or (lambda s: s)
    order = ["junior", "mid", "senior", "staff", "any"]
    rows: list[list[dict]] = []
    # Two-per-row layout for the four graded buckets, then "Any" solo.
    for i in range(0, 4, 2):
        pair = order[i:i+2]
        rows.append([
            {"text": _t(_SENIORITY_LABELS[k]), "callback_data": f"ob:{CB_SENIORITY}:{k}"}
            for k in pair
        ])
    rows.append([{"text": _t(_SENIORITY_LABELS["any"]), "callback_data": f"ob:{CB_SENIORITY}:any"}])
    rows.append([{"text": _t("Cancel setup"), "callback_data": f"ob:{CB_CANCEL}"}])
    return {"inline_keyboard": rows}


def _role_prompt_mdv2(*, tr=None) -> str:
    _t = tr or (lambda s: s)
    lines = [
        _progress_line(STEP_ROLE),
        "",
        f"{ICON_JOB} *" + mdv2_escape(_t("Target role")) + "*",
        "",
        mdv2_escape(_t(
            "Type the role title you want (e.g. 'React Engineer', 'Product "
            "Designer', 'DevRel'). Keep it short — I'll match against similar "
            "titles automatically.\n\n"
            "Tap Skip to let me infer the role from your CV."
        )),
    ]
    return "\n".join(lines)


def _role_keyboard(*, tr=None) -> dict:
    _t = tr or (lambda s: s)
    return {"inline_keyboard": [[
        {"text": _t("Skip"),         "callback_data": f"ob:{CB_SKIP_ROLE}"},
        {"text": _t("Cancel setup"), "callback_data": f"ob:{CB_CANCEL}"},
    ]]}


def _remote_prompt_mdv2(*, tr=None) -> str:
    _t = tr or (lambda s: s)
    lines = [
        _progress_line(STEP_REMOTE),
        "",
        f"{ICON_REMOTE} *" + mdv2_escape(_t("Work style")) + "*",
        "",
        mdv2_escape(_t("Which work arrangement are you open to?")),
    ]
    return "\n".join(lines)


def _remote_keyboard(*, tr=None) -> dict:
    _t = tr or (lambda s: s)
    return {"inline_keyboard": [
        [{"text": _t(_REMOTE_LABELS["remote"]), "callback_data": f"ob:{CB_REMOTE}:remote"},
         {"text": _t(_REMOTE_LABELS["hybrid"]), "callback_data": f"ob:{CB_REMOTE}:hybrid"}],
        [{"text": _t(_REMOTE_LABELS["onsite"]), "callback_data": f"ob:{CB_REMOTE}:onsite"},
         {"text": _t(_REMOTE_LABELS["any"]),    "callback_data": f"ob:{CB_REMOTE}:any"}],
        [{"text": _t("Cancel setup"), "callback_data": f"ob:{CB_CANCEL}"}],
    ]}


def _location_prompt_mdv2(*, tr=None) -> str:
    _t = tr or (lambda s: s)
    lines = [
        _progress_line(STEP_LOCATION),
        "",
        f"{ICON_LOCATION} *" + mdv2_escape(_t("Location")) + "*",
        "",
        mdv2_escape(_t(
            "Type the city, region, or country you want to work in — e.g. "
            "'Berlin', 'Spain', 'Bay Area', 'Remote EU'.\n\n"
            "Tap Remote worldwide if geography doesn't matter."
        )),
    ]
    return "\n".join(lines)


def _location_keyboard(*, tr=None) -> dict:
    _t = tr or (lambda s: s)
    return {"inline_keyboard": [
        [{"text": _t("Remote worldwide"),    "callback_data": f"ob:{CB_LOC_WW}"}],
        [{"text": _t("Cancel setup"),         "callback_data": f"ob:{CB_CANCEL}"}],
    ]}


def _minscore_prompt_mdv2(current: int = 3, *, tr=None) -> str:
    _t = tr or (lambda s: s)
    lines = [
        _progress_line(STEP_MINSCORE),
        "",
        "★ *" + mdv2_escape(_t("Minimum match score")) + "*",
        "",
        mdv2_escape(_t(
            "Each posting is scored 0–5 against your resume. Set the "
            "threshold below which I'll hide matches. Most users start at "
            "3+ — raise it once your digest feels noisy.\n\n"
            "You can change this any time from Settings."
        )),
    ]
    return "\n".join(lines)


# The wizard no longer asks for a min-score, so a fresh flow's answers omit
# the key entirely — default it to 3 (the standard "start here" gate). An
# in-flight legacy wizard that DID pick a score (including an explicit 0 =
# "any") keeps its choice: only an absent key falls back to 3.
DEFAULT_MIN_SCORE = 3


def _default_min_score(answers: dict[str, Any]) -> int:
    v = answers.get("min_score")
    if v is None:
        return DEFAULT_MIN_SCORE
    try:
        return max(0, min(5, int(v)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_SCORE


def _summary_mdv2(answers: dict[str, Any], *, tr=None) -> str:
    """Rendered at the 'done' step — a clean recap of everything collected."""
    _t = tr or (lambda s: s)

    def _fmt(v: Any, dash: str = "—") -> str:
        s = (v or "").strip() if isinstance(v, str) else v
        return str(s) if s else dash

    # Our labels are translated; role/location are the user's own words (kept).
    seniority = _t(_SENIORITY_LABELS.get(answers.get("seniority") or "any", "Any level"))
    remote    = _t(_REMOTE_LABELS.get(answers.get("remote") or "any", "Any"))
    role      = _fmt(answers.get("role"), _t("(inferred from CV)"))
    location  = _fmt(answers.get("location"), _t("(any)"))
    min_score = _default_min_score(answers)
    # "out of 5", not "/5" — Telegram auto-links "/5" as a bot command (blue).
    gate      = _t("Any score") if min_score == 0 else f"{min_score}+ " + _t("out of 5")

    # No column alignment (proportional fonts collapse padding) and no
    # horizontal rule (wraps onto a second line on phones): one "Label: value"
    # per line reads clean at every width.
    def _row(icon: str, label: str, value: str) -> str:
        return f"{icon} *" + mdv2_escape(_t(label) + ":") + f"* {mdv2_escape(value)}"

    body_lines = [
        f"{PIG}  *" + mdv2_escape(_t("Setup complete")) + "*",
        "",
        _row(ICON_JOB,       "Role",       role),
        _row(ICON_SENIORITY, "Seniority",  seniority),
        _row(ICON_REMOTE,    "Work style", remote),
        _row(ICON_LOCATION,  "Location",   location),
        _row("★",            "Match gate", gate),
        "",
        mdv2_escape(_t(
            "I'm already scanning every source for you — a full scan with AI "
            "scoring usually takes 20–40 minutes, and I'll send your first "
            "matches here the moment they're ready. After that, a fresh "
            "digest arrives every morning."
        )),
        "",
        mdv2_escape(_t(
            "Tune the match gate any time in Settings → Min match score (or "
            "/minscore)."
        )),
    ]
    return "\n".join(body_lines)


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
        if step in (STEP_WELCOME, _STEP_ORDER[0]):
            _send_welcome(tg, db, chat_id, first_name)
        else:
            _send_step_prompt(tg, db, chat_id, step)
        return

    # Fresh start (either brand new user, or forced restart after /cleardata all).
    state = _new_state()
    _save(db, chat_id, state)
    _send_welcome(tg, db, chat_id, first_name)


def nudge_stalled(tg: TelegramClient, db: DB) -> int:
    """One proactive re-engagement pass over every stalled wizard.

    maybe_resume only fires when the stalled user messages the bot again —
    which stalled users, by definition, rarely do. This is the push side:
    for each user whose wizard has sat idle for NUDGE_AFTER_SECONDS..
    NUDGE_MAX_AGE_SECONDS, send ONE friendly ping and replay the pending
    question. The `nudged` flag in the state JSON guarantees at most one
    ping per wizard session. Returns the number of users nudged.
    """
    nudged = 0
    for chat_id, raw in db.get_stalled_onboarding():
        try:
            state = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        step = state.get("step")
        if step in (None, STEP_DONE) or state.get("nudged"):
            continue
        age = time.time() - float(state.get("last_step_at") or 0)
        if age < NUDGE_AFTER_SECONDS or age > NUDGE_MAX_AGE_SECONDS:
            continue
        # Mark BEFORE sending — a crash mid-send must not turn into a
        # spam loop on the next pass.
        state["nudged"] = True
        _save(db, chat_id, state)
        # Lead with a real job when we can find a plausible one — a concrete
        # posting is a demo, "Still with me?" is just a reminder. Fail-open:
        # empty jobs table / no LLM pick / any error → today's generic copy.
        teaser = None
        try:
            jobs = [dict(r) for r in db.recent_jobs(25)]
            teaser = _pick_teaser_job(state.get("answers") or {}, jobs)
        except Exception:
            log.debug("teaser pick failed for %s", chat_id, exc_info=True)
        tr = _translator(db, chat_id)
        try:
            if teaser:
                tg.send_message(chat_id, _teaser_nudge_mdv2(teaser, tr=tr))
            else:
                tg.send_message(
                    chat_id,
                    f"{PIG}  " + mdv2_escape((tr or (lambda s: s))(
                        "Still with me? Your daily job matches are a couple of "
                        "taps away — here's where we left off:"
                    )),
                )
            _send_step_prompt(tg, db, chat_id, step)
        except Exception as e:
            log.warning("onboarding nudge failed for %s: %s", chat_id, e)
            continue
        nudged += 1
    return nudged


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
    tr = _translator(db, chat_id)
    try:
        tg.send_message(
            chat_id,
            mdv2_escape((tr or (lambda s: s))("Picking up where you left off — "))
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
    """Send the merged welcome: pig sticker + (pitch + question 1) bubble.

    The state jumps straight to the first real step — the welcome is not a
    screen of its own anymore, so there is no zero-progress state to stall in.
    """
    state = _load(db, chat_id) or _new_state()
    state["step"] = _STEP_ORDER[0]
    _save(db, chat_id, state)
    db.set_awaiting_state(chat_id, None)
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
    tr = _translator(db, chat_id)
    tg.send_message(
        chat_id,
        _welcome_mdv2(first_name, tr=tr),
        reply_markup=_seniority_keyboard(tr=tr),
    )


def _send_step_prompt(tg: TelegramClient, db: DB, chat_id: int, step: str) -> None:
    """Send the prompt for `step`. Centralized so re-entry and first-send
    share identical copy + keyboards."""
    if step == STEP_WELCOME:
        # Legacy rows persisted before the welcome screen was merged into
        # question 1 — route them to the first real step.
        step = _STEP_ORDER[0]
    state = _load(db, chat_id) or _new_state()
    state["step"] = step
    _save(db, chat_id, state)
    tr = _translator(db, chat_id)

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
                _resume_review_prompt_mdv2(filename, word_count, tr=tr),
                reply_markup=_resume_review_keyboard(tr=tr),
            )
        else:
            tg.send_message(chat_id, _resume_prompt_mdv2(tr=tr), reply_markup=_resume_keyboard(tr=tr))
    elif step == STEP_SENIORITY:
        db.set_awaiting_state(chat_id, None)
        tg.send_message(chat_id, _seniority_prompt_mdv2(tr=tr), reply_markup=_seniority_keyboard(tr=tr))
    elif step == STEP_ROLE:
        db.set_awaiting_state(chat_id, AWAIT_ONBOARDING_ROLE)
        tg.send_message(chat_id, _role_prompt_mdv2(tr=tr), reply_markup=_role_keyboard(tr=tr))
    elif step == STEP_REMOTE:
        db.set_awaiting_state(chat_id, None)
        tg.send_message(chat_id, _remote_prompt_mdv2(tr=tr), reply_markup=_remote_keyboard(tr=tr))
    elif step == STEP_LOCATION:
        db.set_awaiting_state(chat_id, AWAIT_ONBOARDING_LOCATION)
        tg.send_message(chat_id, _location_prompt_mdv2(tr=tr), reply_markup=_location_keyboard(tr=tr))
    elif step == STEP_MINSCORE:
        db.set_awaiting_state(chat_id, None)
        # Reuse the existing min-score keyboard but wrap it in an
        # onboarding-flavoured prompt. Callbacks come back as `ms:<n>` —
        # bot.py routes those to our handler when the user is mid-wizard.
        tg.send_message(
            chat_id,
            _minscore_prompt_mdv2(current=3, tr=tr),
            reply_markup=min_score_keyboard(current=3),
        )
    elif step == STEP_DONE:
        state = _load(db, chat_id) or {}
        tg.send_message(chat_id, _summary_mdv2(state.get("answers") or {}, tr=tr))
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
        tr = _translator(db, chat_id)
        try:
            tg.edit_message_text(
                chat_id, msg_id,
                f"{PIG}  " + mdv2_escape((tr or (lambda s: s))(
                    "Setup cancelled. Run /start any time to try again."
                )),
                reply_markup={"inline_keyboard": []},
            )
        except Exception as e:
            log.debug("onboarding cancel edit failed: %s", e)
        tg.answer_callback(cb_id, "Cancelled")
        return

    # --- START (legacy "Get started" buttons on old welcome bubbles) ---
    if code == CB_START:
        if state is None:
            state = _new_state()
        state["step"] = _STEP_ORDER[0]
        _save(db, chat_id, state)
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _send_step_prompt(tg, db, chat_id, _STEP_ORDER[0])
        tg.answer_callback(cb_id, "")
        return

    # --- KEEP EXISTING CV (from resume-review step, the final step) ---
    if code == CB_KEEP_CV:
        # User already has a CV and chose to keep it. Same forward motion
        # as handle_resume_uploaded, just without the file-save side effect.
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _finalize(tg, db, chat_id, on_complete=on_complete, on_run_search=on_run_search)
        tg.answer_callback(cb_id, "Keeping your CV")
        return

    # --- SKIP CV (from resume step, the final step) ---
    if code == CB_SKIP_CV:
        # Finish without a resume — the profile builds from the button
        # answers alone; a PDF sent later triggers the normal rebuild.
        db.set_awaiting_state(chat_id, None)
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _finalize(tg, db, chat_id, on_complete=on_complete, on_run_search=on_run_search)
        tg.answer_callback(cb_id, "You can send a PDF any time")
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
        state["step"] = STEP_RESUME
        _save(db, chat_id, state)
        db.set_awaiting_state(chat_id, None)
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _send_step_prompt(tg, db, chat_id, STEP_RESUME)
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
        state["step"] = STEP_RESUME
        _save(db, chat_id, state)
        # Strip the picker keyboard in place so the user can't change it
        # post-commit without a fresh Settings visit.
        try:
            tg.edit_reply_markup(chat_id, msg_id, {"inline_keyboard": []})
        except Exception:
            pass
        _send_step_prompt(tg, db, chat_id, STEP_RESUME)
        tg.answer_callback(cb_id, f"Gate set to {'any' if score == 0 else f'{score}+/5'}")
        return

    tg.answer_callback(cb_id, "")


# ---------- free-text handlers ----------

# Which answers-key each step captures. Steps whose progress is not a plain
# answer (resume = a CV upload, not a stored value) are absent, so
# _first_unanswered treats them as terminal. This is a schema map, not a
# content heuristic.
_STEP_ANSWER_KEY = {
    STEP_SENIORITY: "seniority",
    STEP_ROLE:      "role",
    STEP_REMOTE:    "remote",
    STEP_LOCATION:  "location",
}

# LLM prompt for parsing a free-form multi-field answer typed at any wizard
# step. Small/cheap model, JSON-only. Fail-open: any error → no fields
# extracted → callers behave exactly as they did before (re-prompt / verbatim
# capture). See project CLAUDE.md: the model decides content, not Python.
_FREE_TEXT_PROMPT = """\
You extract job-search preferences from a single chat message during onboarding.
The user may write in ANY language.

Their answers so far (JSON): {current}

Their new message:
{text}

Return ONLY a JSON object containing the fields the user CLEARLY stated in this
message. Omit any field they did not clearly state — do not guess or infer.

Allowed fields and value sets:
- "seniority": one of junior | mid | senior | staff | any
- "remote": one of remote | hybrid | onsite | any
- "role": a short free-text job title (e.g. "React Engineer")
- "location": a short free-text place (e.g. "Berlin", "Remote EU")

Output STRICT JSON only — no prose, no markdown fences.
Example: {{"seniority": "senior", "role": "React Engineer", "remote": "remote", "location": "EU"}}
If the message states nothing usable, return {{}}.
"""


def _extract_json(s: str) -> dict | None:
    """Parse the first {...} object out of a model reply, tolerating fences /
    prose around it. Returns None on failure."""
    s = (s or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        obj = json.loads(s[i:j + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _llm_parse_answer(text: str, answers: dict[str, Any]) -> dict[str, Any]:
    """Ask a small model which of {seniority, role, remote, location} the
    user just stated. Disabled in this single-provider build — returns {}
    (fail-open: the wizard keeps its button-driven flow)."""
    return {}


# LLM prompt for the re-engagement teaser: pick the single most plausible job
# to dangle in front of a stalled user. Small/cheap model, JSON-only. Fail-open:
# any error → no pick → the nudge falls back to today's generic copy. The model
# does the matching against partial (possibly empty) wizard answers — no Python
# keyword filtering (see project CLAUDE.md).
_TEASER_PROMPT = """\
A user started a job-search setup wizard but stopped partway. Pick ONE job from
the list below to show them as a teaser — the single most appealing, plausible
match to lure them back to finish setup.

Their partial answers so far (JSON, may be empty): {answers}

Candidate jobs (numbered):
{jobs}

Return ONLY a JSON object: {{"pick": N}} where N is the number of the best job,
or {{"pick": null}} if none is a reasonable teaser. If the answers are empty,
pick a broadly appealing, reputable-looking posting. No prose, no markdown.
"""


def _pick_teaser_job(answers: dict[str, Any], jobs: list[dict]) -> dict | None:
    """Pick the single most plausible teaser job. Disabled in this
    single-provider build — returns None (fail-open: the caller uses the
    generic nudge copy)."""
    return None


def _teaser_nudge_mdv2(job: dict, *, tr=None) -> str:
    """Nudge copy that LEADS with a real job (title — company — location, linked
    if the row has a URL), then the come-back line. Job fields are the posting's
    own words (never translated); only our come-back copy is."""
    _t = tr or (lambda s: s)
    bits = [b for b in (
        (job.get("title") or "").strip(),
        (job.get("company") or "").strip(),
        (job.get("location") or "").strip(),
    ) if b]
    headline = mdv2_escape(" — ".join(bits) or _t("A fresh match"))
    url = (job.get("url") or "").strip()
    if url:
        safe = url.replace(")", "\\)").replace("(", "\\(")
        headline = f"[{headline}]({safe})"
    return (
        f"{PIG}  *{headline}*\n\n"
        + mdv2_escape(_t(
            "Jobs like this are waiting — a couple of taps to finish setup. "
            "Here's where we left off:"
        ))
    )


def _first_unanswered(answers: dict[str, Any]) -> str:
    """First step in _STEP_ORDER whose answer key is missing. Steps with no
    answer key (resume) are terminal."""
    for step in _STEP_ORDER:
        key = _STEP_ANSWER_KEY.get(step)
        if key is None:
            return step
        if key not in answers:
            return step
    return STEP_RESUME


def _captured_line(captured: dict[str, Any], *, tr=None) -> str:
    """Human-readable recap of the fields we just captured (plain text; the
    caller escapes it for MarkdownV2). Our labels are translated; role/location
    are the user's own words (kept verbatim)."""
    _t = tr or (lambda s: s)
    parts: list[str] = []
    if "seniority" in captured:
        parts.append(_t(_SENIORITY_LABELS[captured["seniority"]]))
    if "role" in captured:
        parts.append(captured["role"])
    if "remote" in captured:
        parts.append(f"{_t(_REMOTE_LABELS[captured['remote']])} " + _t("work"))
    if "location" in captured:
        parts.append(captured["location"])
    return ", ".join(parts)


def handle_free_text(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    text: str,
    *,
    require: str | None = None,
) -> bool:
    """Mid-wizard: LLM-parse a free-form answer (possibly multi-field, any
    language), apply whatever the model clearly extracted, advance to the first
    unanswered step, and confirm what was captured.

    Returns True if at least one field was applied (caller stops). Returns
    False when the LLM failed or extracted nothing usable — the caller then
    re-prompts (buttons-only steps) or falls back to verbatim single-field
    capture (role/location). When `require` is set, only that field being
    present counts as success (used so the role/location handlers fall back to
    today's verbatim capture when the model didn't produce their own field)."""
    state = _load(db, chat_id)
    if not state or state.get("step") in (None, STEP_DONE):
        return False
    if not (text or "").strip():
        return False
    answers = state.get("answers") or {}
    captured = _llm_parse_answer(text, answers)
    if not captured:
        return False
    if require is not None and require not in captured:
        return False

    answers.update(captured)
    state["answers"] = answers
    next_step = _first_unanswered(answers)
    state["step"] = next_step
    _save(db, chat_id, state)
    db.set_awaiting_state(chat_id, None)

    tr = _translator(db, chat_id)
    line = _captured_line(captured, tr=tr)
    if line:
        try:
            tg.send_message(
                chat_id,
                f"{PIG}  " + mdv2_escape((tr or (lambda s: s))("Got it —") + f" {line}."),
            )
        except Exception:
            log.debug("free-text confirmation send failed", exc_info=True)
    _send_step_prompt(tg, db, chat_id, next_step)
    return True


def reprompt_current(tg: TelegramClient, db: DB, chat_id: int) -> None:
    """Re-send the current step's prompt once. Used when a free-text answer at
    a buttons-only step couldn't be parsed — better than today's silent ignore,
    and non-looping (one re-prompt per inbound message)."""
    state = _load(db, chat_id)
    if not state or state.get("step") in (None, STEP_DONE):
        return
    _send_step_prompt(tg, db, chat_id, state["step"])


def handle_text_role(tg: TelegramClient, db: DB, chat_id: int, text: str) -> bool:
    """Consume a free-text role title. Returns True if consumed.

    Routes through the multi-field parser first so a rich answer ("senior react
    dev, remote, EU") fills everything and jumps ahead. Falls back to today's
    verbatim single-field capture whenever the model didn't yield a role."""
    state = _load(db, chat_id)
    if not state or state.get("step") != STEP_ROLE:
        return False
    if handle_free_text(tg, db, chat_id, text, require="role"):
        return True
    role = (text or "").strip()[:120]
    state["answers"]["role"] = role
    state["step"] = STEP_REMOTE
    _save(db, chat_id, state)
    db.set_awaiting_state(chat_id, None)
    _send_step_prompt(tg, db, chat_id, STEP_REMOTE)
    return True


def handle_text_location(tg: TelegramClient, db: DB, chat_id: int, text: str) -> bool:
    """Consume a free-text location. Returns True if consumed.

    Parser-first (a multi-field answer fills everything), else today's verbatim
    single-field capture."""
    state = _load(db, chat_id)
    if not state or state.get("step") != STEP_LOCATION:
        return False
    if handle_free_text(tg, db, chat_id, text, require="location"):
        return True
    location = (text or "").strip()[:120]
    if not location:
        # Empty input — re-prompt rather than advance.
        _send_step_prompt(tg, db, chat_id, STEP_LOCATION)
        return True
    state["answers"]["location"] = location
    state["step"] = STEP_RESUME
    _save(db, chat_id, state)
    db.set_awaiting_state(chat_id, None)
    _send_step_prompt(tg, db, chat_id, STEP_RESUME)
    return True


def handle_resume_uploaded(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    *,
    on_complete: Callable[[int], None] | None = None,
    on_run_search: Callable[[int], None] | None = None,
) -> bool:
    """Called by bot.py AFTER a successful resume save, while mid-wizard.

    The resume step is the wizard's last question, so a successful upload
    finalizes the whole flow. Returns True if we consumed the upload (the
    caller should suppress its own "resume saved" confirmations in favor
    of ours).
    """
    state = _load(db, chat_id)
    if not state:
        return False
    if state.get("step") != STEP_RESUME:
        # The user uploaded a CV outside the wizard's resume step — leave
        # state alone and let the caller do its normal thing.
        return False
    try:
        tr = _translator(db, chat_id)
        tg.send_message(
            chat_id,
            f"{PIG}  " + mdv2_escape((tr or (lambda s: s))(
                "Got your CV — sniffed through it in a second."
            )),
        )
    except Exception:
        pass
    _finalize(tg, db, chat_id, on_complete=on_complete, on_run_search=on_run_search)
    return True


# ---------- finalization ----------

def _finalize(
    tg: TelegramClient,
    db: DB,
    chat_id: int,
    *,
    on_complete: Callable[[int], None] | None = None,
    on_run_search: Callable[[int], None] | None = None,
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

    # Persist the min-score to the DB column — that's the AUTHORITATIVE ⭐
    # gate the search reads (`db.get_min_match_score`). It lives in its own
    # column precisely so it survives the Opus rebuild kicked below. The old
    # code wrote it into the profile JSON via set_min_match_score(profile,…),
    # which the score gate does NOT read and the rebuild overwrites — so the
    # user's chosen floor silently never applied (DB column stayed 0 → gate
    # fell back to the default). Also write a minimal skeleton profile so the
    # user counts as "onboarded" during the ~150-290s build window.
    min_score = _default_min_score(answers)
    try:
        db.set_min_match_score(chat_id, min_score)
    except Exception:
        log.exception("finalize: set_min_match_score (DB column) failed")
    try:
        from user_profile import profile_from_json, profile_to_json, set_min_match_score
        profile = profile_from_json(db.get_user_profile(chat_id))
        new_profile = set_min_match_score(profile, min_score)
        db.set_user_profile(chat_id, profile_to_json(new_profile))
    except Exception:
        log.exception("finalize: persist skeleton profile failed")

    # Stamp completion BEFORE the summary send so the next /start falls
    # through to the welcome-back flow even if the summary send fails.
    try:
        db.mark_onboarding_complete(chat_id)
    except Exception:
        log.exception("finalize: mark_onboarding_complete failed")

    # Kick the Opus profile build the wizard DEFERRED to this final step.
    # handle_document skips the resume_upload build while the user is mid-
    # wizard ("so we only run it once the user has given all their signals"),
    # delegating to finalize — but historically finalize never actually did
    # it, leaving every wizard-onboarded user with the skeleton profile above:
    # no search_seeds, so LinkedIn + seeded web_search never ran for them and
    # they got almost no matches. This is the build that gives them a real
    # profile (role, stack, search seeds) from their resume + synthesized
    # prefs. Runs in the background; the queue notifies the user on completion.
    try:
        from bot import _enqueue_profile_rebuild
        _enqueue_profile_rebuild(tg, db, chat_id, trigger="onboarding")
    except Exception:
        log.exception("finalize: profile build enqueue failed")

    # Live-spawn the continuous searcher for this newly-onboarded user.
    # No-op when OINK_CONTINUOUS_MODE is off, or when the operator pinned a
    # specific chat_id list that excludes this user (see bot.start_continuous_searcher_for).
    # Lazy import: keeps onboarding.py importable in tests that don't load bot.py.
    try:
        from bot import start_continuous_searcher_for as _live_spawn
        _live_spawn(db, chat_id)
    except Exception:
        log.exception("finalize: live-spawn of continuous searcher failed")

    # Summary card. No buttons — on_complete re-attaches the main menu and the
    # first search auto-fires below, so there's nothing left to tap.
    tg.send_message(chat_id, _summary_mdv2(answers, tr=_translator(db, chat_id)))

    if on_complete is not None:
        try:
            on_complete(chat_id)
        except Exception:
            log.exception("on_complete hook crashed")

    # Auto-fire the first search so the payoff is immediate instead of asking.
    # ponytail: this fires on the SKELETON profile (real search_seeds land when
    # the Opus rebuild enqueued above completes ~150-290s later). We accept that
    # tradeoff for instant feedback — the continuous searcher live-spawned above
    # and the next scheduled run pick up seed-driven matches once the build
    # lands. No per-onboarding completion hook exists on ProfileBuilderQueue
    # (its on_done is a process-global singleton that would over-fire on every
    # prefs_change/resume rebuild), so sequencing after the build isn't clean.
    # Fail-open: a search crash must never block onboarding completion.
    if on_run_search is not None:
        try:
            on_run_search(chat_id)
        except Exception:
            log.exception("on_run_search hook crashed")


# ---------- utility: is the user expecting an onboarding text input? ----------

def current_await_state(db: DB, chat_id: int) -> str | None:
    """Return the AWAIT_* constant if the user's awaiting_state is one of
    ours, else None. Useful for bot.py's dispatch to decide whether to
    route a text message here."""
    raw = db.get_awaiting_state(chat_id)
    if raw in (AWAIT_ONBOARDING_ROLE, AWAIT_ONBOARDING_LOCATION):
        return raw
    return None
