"""Telegram Bot API client + message formatting (with inline keyboards)."""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable

import requests

from dedupe import Job

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org/bot{token}/{method}"

# ---------- MarkdownV2 helpers ----------

_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"
_MDV2_RE = re.compile(f"([{re.escape(_MDV2_SPECIALS)}])")


def mdv2_escape(text: str) -> str:
    if not text:
        return ""
    return _MDV2_RE.sub(r"\\\1", text)


# Shared icon set. Kept small and functional — no decorative emoji. Each
# symbol has one consistent meaning across the bot so users learn to
# pattern-match after a couple of digests.
ICON_JOB       = "💼"   # role / posting
ICON_LOCATION  = "📍"
ICON_SALARY    = "💰"
ICON_COMPANY   = "🏢"
ICON_REMOTE    = "🌐"   # remote policy
ICON_STACK     = "⚙️"   # tech stack
ICON_SENIORITY = "📊"   # level
ICON_LANGUAGE  = "🗣"
ICON_VISA      = "🛂"
ICON_APPLIED   = "✅"
ICON_SKIPPED   = "⊘"    # lighter than 🚫 for the "not-applied" tag
ICON_NEW       = "•"    # neutral bullet, used for fresh-post emphasis


def _score_bar(score: int, cells: int = 5) -> str:
    """Return a horizontal match-score bar.

    Uses block elements ('▰' filled, '▱' empty) instead of stars — reads more
    like a progress/rating widget and less like a kids' game. Accepts any int;
    clamps to [0, cells].
    """
    n = max(0, min(cells, int(score)))
    return "▰" * n + "▱" * (cells - n)


# ---------- onboarding / layout primitives ----------

def progress_dots(step: int, total: int) -> str:
    """Render a 'Step N of M' progress indicator as filled/empty dots.

    Example: progress_dots(3, 6) → '●●●○○○  Step 3 of 6'. Cheap, scannable,
    no external assets. Clamps to sane bounds so off-by-one callers don't
    produce visual nonsense.
    """
    total = max(1, int(total))
    step = max(0, min(total, int(step)))
    dots = "●" * step + "○" * (total - step)
    return f"{dots}  Step {step} of {total}"


def hr_mdv2() -> str:
    """Horizontal rule made of MDv2-safe chars. Useful as a section divider."""
    return "─" * 22


def section_header_mdv2(title: str, subtitle: str | None = None) -> str:
    """Render a two-line section header: bold title + optional italic subtitle.

    Both lines are MDv2-escaped here so callers can hand in raw text.
    """
    out = [f"*{mdv2_escape(title)}*"]
    if subtitle:
        out.append(f"_{mdv2_escape(subtitle)}_")
    return "\n".join(out)


def chip_line_mdv2(chips: Iterable[tuple[str, str]]) -> str:
    """Render a row of 'icon  text' chips joined by ' · '.

    Each chip is a (icon, text) tuple. Empty/whitespace text values are
    dropped. The text is MDv2-escaped; the icon is emitted verbatim.
    """
    parts: list[str] = []
    for icon, text in chips:
        s = (text or "").strip()
        if not s:
            continue
        parts.append(f"{icon} {mdv2_escape(s)}")
    return "  ·  ".join(parts)


def _render_key_details_mdv2(d: dict) -> list[str]:
    """Compact two-line 'chip' block for the key_details dict, MDV2-escaped.

    Group 1 (role signal):  stack · seniority · remote policy
    Group 2 (logistics):    location · salary · visa · language

    Each group renders as one ' · '-joined line; empty fields drop out. Groups
    with zero surviving chips are omitted entirely so short cards stay short.
    The old per-field emoji list produced 5-8 lines of visual noise — this
    brings it down to ≤2 lines while keeping the same information density.
    """
    if not isinstance(d, dict):
        return []
    out: list[str] = []

    role_chips = [
        (ICON_STACK,     d.get("stack")),
        (ICON_SENIORITY, d.get("seniority")),
        (ICON_REMOTE,    d.get("remote_policy")),
    ]
    log_chips = [
        (ICON_LOCATION, d.get("location")),
        (ICON_SALARY,   d.get("salary")),
        (ICON_VISA,     _visa_label(d.get("visa_support"))),
        (ICON_LANGUAGE, d.get("language")),
    ]
    row1 = chip_line_mdv2((icon, (val or "")[:80]) for icon, val in role_chips)
    row2 = chip_line_mdv2((icon, (val or "")[:80]) for icon, val in log_chips)
    if row1:
        out.append(row1)
    if row2:
        out.append(row2)
    # Standout gets its own italic line if present — it's a free-text pitch,
    # not a tag, so it doesn't belong in the chip row.
    standout = (d.get("standout") or "").strip()
    if standout:
        out.append("_" + mdv2_escape(standout[:200]) + "_")
    return out


def _visa_label(v) -> str:
    s = (v or "").strip().lower()
    return {"yes": "visa support", "no": "no visa support"}.get(s, "")


def format_job_mdv2(
    job: Job,
    include_snippet: bool = True,
    snippet_chars: int = 240,
    applied_status: str | None = None,
    enrichment: dict | None = None,
) -> str:
    """Render one job as a Telegram MarkdownV2 card.

    If `enrichment` is provided (from job_enrich.enrich_jobs_ai), we render:
      - a ⭐ score bar right under the title
      - a resume-aware `why_match` line
      - a compact list of key_details (stack, seniority, remote, salary, …)

    Without enrichment the card falls back to title / company / snippet / source.
    """
    title = mdv2_escape(job.title or "Untitled role")
    company = mdv2_escape(job.company or "Unknown company")
    location = mdv2_escape(job.location or "")
    salary = mdv2_escape(job.salary or "")
    source = mdv2_escape(job.source)
    url = (job.url or "").replace(")", "\\)").replace("(", "\\(")

    # Line 1 — prominent role title (linked).
    lines = [f"*[{title}]({url})*"]
    # Line 2 — company · location [· salary]. Drop salary here when enrichment
    # is present because it re-appears in the logistics chip row.
    meta_bits = [company]
    if location:
        meta_bits.append(location)
    if salary and not enrichment:
        meta_bits.append(salary)
    lines.append("  ·  ".join(meta_bits))

    # Match score + resume-aware rationale come next so users can triage in
    # one glance. The score bar uses block elements (▰▱) so it reads like a
    # progress bar rather than a star rating.
    if enrichment:
        score = int(enrichment.get("match_score") or 0)
        lines.append("")
        lines.append(f"{_score_bar(score)}  *{score}/5 match*")
        why = (enrichment.get("why_match") or "").strip()
        if why:
            lines.append("_" + mdv2_escape(why[:260]) + "_")
        detail_lines = _render_key_details_mdv2(enrichment.get("key_details") or {})
        if detail_lines:
            lines.append("")
            lines.extend(detail_lines)

    if include_snippet and job.snippet:
        snip = job.snippet.strip()
        if len(snip) > snippet_chars:
            snip = snip[:snippet_chars].rstrip() + "…"
        lines.append("")
        lines.append(f"_{mdv2_escape(snip)}_")

    # Status chip + source footer. "Applied" / "Skipped" use the canonical
    # icon pair from the top of the module; "Saved" stays as a plain
    # star-flavoured tag. Source lives in monospace so it reads as metadata,
    # not body text.
    badge_map = {
        "applied":    f"{ICON_APPLIED} *Applied*",
        "skipped":    f"{ICON_SKIPPED} *Skipped*",
        "interested": "★ *Saved*",
    }
    badge = badge_map.get(applied_status or "", "")
    footer = f"`via {source}`"
    if badge:
        footer = f"{badge}  ·  {footer}"
    lines.append("")
    lines.append(footer)
    return "\n".join(lines)


# ---------- Inline keyboards ----------

def job_keyboard(job_id: str, applied_status: str | None = None, url: str | None = None) -> dict:
    """Build the inline keyboard under each job message.

    callback_data is capped at 64 bytes; our job_id is 16 hex chars → plenty of room.
    Prefixes:
        a:<job_id>  → mark applied
        n:<job_id>  → mark not applied / skipped
        r:<job_id>  → rewrite resume for this position

    The top row is a direct URL button (Telegram opens the posting in-browser).
    """
    rows: list[list[dict]] = []
    if url:
        rows.append([{"text": "View posting ↗", "url": url}])
    if applied_status == "applied":
        status_row = [{"text": "✓ Applied", "callback_data": f"n:{job_id}"}]
    elif applied_status == "skipped":
        status_row = [{"text": "⊘ Skipped", "callback_data": f"a:{job_id}"}]
    else:
        status_row = [
            {"text": "✓ Applied", "callback_data": f"a:{job_id}"},
            {"text": "⊘ Not a fit", "callback_data": f"n:{job_id}"},
        ]
    rows.append(status_row)
    # Two AI actions on one row — paired because they tackle the same job
    # but serve different intents:
    #   fit:<id>  → evaluate alignment & surface gaps (this doesn't rewrite anything)
    #   r:<id>    → produce a tailored resume draft
    # Keeping them adjacent makes the tradeoff visible: read before rewrite.
    rows.append([
        {"text": "Analyze fit →",   "callback_data": f"fit:{job_id}"},
        {"text": "Tailor resume →", "callback_data": f"r:{job_id}"},
    ])
    return {"inline_keyboard": rows}


def min_score_keyboard(current: int = 0) -> dict:
    """Inline keyboard for picking the minimum match score (0 = any, 1..5 = gate).

    Callback data shape:  ms:<n>   where n ∈ {0, 1, 2, 3, 4, 5}

    The currently-selected tier is marked with a dot in its label so the user
    can see what they already have. The layout is two rows of three so it
    stays readable on narrow phone screens.
    """
    def _lbl(n: int) -> str:
        # Use '●' as the "selected" marker so the UI stays tonally consistent
        # with the onboarding progress dots. The empty/filled block pattern
        # doubles as a miniature score bar so users can see the tier at a
        # glance without reading the number.
        marker = "● " if n == current else ""
        if n == 0:
            return f"{marker}Any"
        return f"{marker}{n}+  {_score_bar(n)}"

    rows = [
        [{"text": _lbl(0), "callback_data": "ms:0"},
         {"text": _lbl(1), "callback_data": "ms:1"},
         {"text": _lbl(2), "callback_data": "ms:2"}],
        [{"text": _lbl(3), "callback_data": "ms:3"},
         {"text": _lbl(4), "callback_data": "ms:4"},
         {"text": _lbl(5), "callback_data": "ms:5"}],
    ]
    return {"inline_keyboard": rows}


# Clean-my-data cleanup categories. Kept here (rather than in bot.py) so the
# inline keyboard and the handler reference the same canonical labels +
# callback codes. Ordered for rendering — lightest first, destructive "all"
# last. The code value ends up in the callback_data after the `cd:` / `cdc:`
# prefix, so keep it URL-safe and short (Telegram caps callback_data at 64
# bytes).
CLEAN_DATA_KINDS: tuple[tuple[str, str, str], ...] = (
    # (code,       emoji + label,               one-line description — used on confirm screen)
    ("resume",    "📄 Resume",                  "Your uploaded CV (PDF + extracted text)."),
    ("history",   "📋 Job history",             "Applied/skipped marks + digest sent-log."),
    ("tailored",  "✍️ Tailored resumes",        "Markdown files from ✍️ Tailor my resume."),
    ("profile",   "🤖 Profile",                 "Your /prefs free-text + AI-built profile + ⭐ min-score."),
    ("research",  "🔬 Research",                "Market-research history and saved .docx files."),
    ("all",       "⚠️ Everything",              "Full wipe — starts you back at /start."),
)


def clean_data_menu_keyboard() -> dict:
    """Inline keyboard for the 🧹 Clean my data menu.

    Layout: two-per-row for the five scoped options, then the destructive
    "Everything" on its own row (visual separation — tapping it shouldn't
    feel like a small-adjustment button), then a Cancel row.

    Callback data shape:  cd:<code>  where <code> is one of CLEAN_DATA_KINDS.
    """
    scoped = [k for k in CLEAN_DATA_KINDS if k[0] != "all"]
    all_btn = next(k for k in CLEAN_DATA_KINDS if k[0] == "all")

    rows: list[list[dict]] = []
    for i in range(0, len(scoped), 2):
        pair = scoped[i:i + 2]
        rows.append([
            {"text": label, "callback_data": f"cd:{code}"}
            for (code, label, _desc) in pair
        ])
    # Destructive row — alone, with the warning emoji the label carries.
    rows.append([{"text": all_btn[1], "callback_data": f"cd:{all_btn[0]}"}])
    rows.append([{"text": "✖️ Cancel", "callback_data": "cdx:"}])
    return {"inline_keyboard": rows}


def clean_data_confirm_keyboard(kind: str) -> dict:
    """Inline keyboard for the second-step confirm after picking a category.

    Callback data shape:
        cdc:<code>  → execute the deletion
        cdx:        → cancel / back to menu
    """
    return {"inline_keyboard": [[
        {"text": "✅ Yes, delete",   "callback_data": f"cdc:{kind}"},
        {"text": "✖️ Cancel",        "callback_data": "cdx:"},
    ]]}


def suggestions_keyboard(job_id: str, url: str | None = None,
                         decided: str | None = None) -> dict:
    """Inline keyboard for the tailor-suggestions dialog.

    Callback data prefixes:
        ra:<job_id>  → user accepted; attach the tailored resume file
        rd:<job_id>  → user dismissed the suggestions
    """
    rows: list[list[dict]] = []
    if url:
        rows.append([{"text": "🔗 View posting", "url": url}])
    if decided == "applied":
        rows.append([{"text": "✅ Applied — see attachment", "callback_data": f"noop:{job_id}"}])
    elif decided == "dismissed":
        rows.append([{"text": "✖️ Dismissed", "callback_data": f"noop:{job_id}"}])
    else:
        rows.append([
            {"text": "✅ Apply", "callback_data": f"ra:{job_id}"},
            {"text": "✖️ Dismiss", "callback_data": f"rd:{job_id}"},
        ])
    return {"inline_keyboard": rows}


# Per-change-type emoji. Keys are normalized lower-case verbs.
_CHANGE_EMOJI = {
    "add":      "➕",
    "remove":   "➖",
    "rephrase": "✏️",
    "reorder":  "🔀",
    "reframe":  "🎯",
    "rewrite":  "✏️",
    "edit":     "✏️",
    "update":   "✏️",
}
_DEFAULT_CHANGE_EMOJI = "✏️"
_RULE = "─" * 22  # visual divider between suggestions — contains no MDv2-reserved chars


def _change_emoji(change: str) -> str:
    return _CHANGE_EMOJI.get((change or "").strip().lower(), _DEFAULT_CHANGE_EMOJI)


def _balance_mdv2_entities(text: str) -> str:
    """Strip UNESCAPED trailing `*`, `_`, backtick tokens if their count is odd.

    Telegram rejects MDv2 messages with an unterminated bold/italic/code entity.
    If we had to truncate mid-block, we may be left with an odd number of a
    given marker — walk the text, count only unescaped occurrences, and if the
    count is odd, strip the LAST unescaped occurrence. Escaped markers (`\\*`,
    `\\_`, `\\\\``) don't open/close entities so they don't count.

    This is a last-resort safety net; the primary mitigation is truncating at
    `_RULE` boundaries (which contain no reserved chars) so entities can't be
    split in the first place.
    """
    for tok in ("*", "_", "`"):
        # Find positions of UNESCAPED tok (preceding char is not a backslash).
        positions: list[int] = []
        for i, ch in enumerate(text):
            if ch != tok:
                continue
            if i > 0 and text[i - 1] == "\\":
                continue
            positions.append(i)
        if len(positions) % 2 == 1:
            last = positions[-1]
            text = text[:last] + text[last + 1:]
    return text


def render_suggestions_mdv2(job, plan: dict, max_chars: int = 3500) -> str:
    """Render the AI plan as a MarkdownV2 dialog body.

    Goal: make this scannable. Three-line header, one suggestion block per
    change, horizontal separators between blocks, and emoji-tagged labels for
    Current / Suggested / Why so the user can pattern-match without reading
    every word.

    The full rewritten resume is intentionally omitted — it arrives as a
    sendDocument after the user clicks Apply. Truncates to stay under
    Telegram's 4096-char cap for editMessageText.
    """
    title = mdv2_escape(job.title or "Role")
    company = mdv2_escape(job.company or "")

    # Three-line header: prominent, easy to skim.
    lines: list[str] = [
        "🎯 *Tailor plan*",
        f"📄 *{title}*",
    ]
    if company:
        lines.append(f"🏢 {company}")
    lines.append("")

    summary = (plan.get("summary") or "").strip()
    if summary:
        lines.append("💬 _" + mdv2_escape(summary) + "_")
        lines.append("")

    suggestions = plan.get("suggestions") or []
    if not suggestions:
        lines.append("✨ " + mdv2_escape("Your resume already aligns — no concrete edits suggested."))
    else:
        n = len(suggestions)
        plural = "s" if n != 1 else ""
        lines.append(f"📝 *{n} suggested change{plural}*")
        lines.append("")
        for i, s in enumerate(suggestions, 1):
            section = str(s.get("section") or "").strip() or "Resume"
            change = str(s.get("change") or "Rephrase").strip() or "Rephrase"
            emoji = _change_emoji(change)
            # Block header: "─────  1. Experience  ➕ Add"
            lines.append(f"{_RULE}")
            lines.append(
                f"*{i}\\.* *{mdv2_escape(section)}*  ·  {emoji} _{mdv2_escape(change)}_"
            )

            before = (s.get("before") or "").strip()
            after = (s.get("after") or "").strip()
            why = (s.get("why") or "").strip()

            if before:
                lines.append("")
                lines.append("❌ *Current*")
                lines.append("> " + mdv2_escape(before[:280]))
            if after:
                lines.append("")
                lines.append("✅ *Suggested*")
                lines.append("> *" + mdv2_escape(after[:280]) + "*")
            if why:
                lines.append("")
                lines.append("💡 _" + mdv2_escape(why[:240]) + "_")
            lines.append("")
        lines.append(_RULE)
        lines.append("")

    lines.append("👇 " + mdv2_escape("Tap ✅ Apply to receive the rewritten resume as a file."))
    body = "\n".join(lines)
    if len(body) > max_chars:
        # Safe-boundary truncation: slice at the last `_RULE` (the horizontal
        # separator between suggestion blocks). Since `_RULE` contains no
        # MDv2-reserved chars, we're guaranteed the prefix has no mid-entity
        # cut. Leave room for a trailing note + the closing footer.
        note = "…\n\n" + mdv2_escape(
            "Plan trimmed to fit Telegram's size limit — the full rewritten "
            "resume will still include every change when you tap ✅ Apply."
        )
        room = max_chars - len(note) - 16
        cutoff = body.rfind(_RULE, 0, room)
        if cutoff > 0:
            body = body[:cutoff].rstrip() + "\n\n" + note
        else:
            # Fallback: cut at the last newline before `room` so at least we
            # don't split a line in half, then let the balancer fix stragglers.
            nl = body.rfind("\n", 0, room)
            body = body[: nl if nl > 0 else room].rstrip() + "\n\n" + note
        # Defensive: if any odd-count bold/italic/code marker survived, strip it.
        body = _balance_mdv2_entities(body)
    return body


# ---------- Client ----------

class TelegramClient:
    def __init__(self, token: str, timeout: int = 20):
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is empty")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, payload: dict | None = None, files: dict | None = None,
              http_timeout: int | None = None) -> dict:
        url = API_BASE.format(token=self.token, method=method)
        effective_timeout = http_timeout if http_timeout is not None else self.timeout
        if files:
            resp = requests.post(url, data=payload or {}, files=files, timeout=effective_timeout)
        else:
            resp = requests.post(url, json=payload or {}, timeout=effective_timeout)
        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(f"Telegram {method} non-JSON response: {resp.text[:200]}")
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data.get("result", {})

    # ----- sending -----

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: str = "MarkdownV2",
        reply_markup: dict | None = None,
        disable_preview: bool = True,
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        res = self._call("sendMessage", payload)
        return int(res.get("message_id", 0))

    def send_plain(self, chat_id: int | str, text: str) -> int:
        return self.send_message(chat_id, text, parse_mode="")

    def send_document(
        self,
        chat_id: int | str,
        path: Path,
        caption: str | None = None,
    ) -> int:
        path = Path(path)
        with path.open("rb") as f:
            files = {"document": (path.name, f)}
            payload = {"chat_id": str(chat_id)}
            if caption:
                payload["caption"] = caption
            res = self._call("sendDocument", payload, files=files)
        return int(res.get("message_id", 0))

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        parse_mode: str = "MarkdownV2",
        reply_markup: dict | None = None,
        disable_preview: bool = True,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._call("editMessageText", payload)

    def edit_reply_markup(self, chat_id: int | str, message_id: int, reply_markup: dict | None) -> None:
        self._call("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup or {"inline_keyboard": []},
        })

    def answer_callback(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        self._call("answerCallbackQuery", payload)

    # ----- receiving (used by bot.py) -----

    def get_updates(self, offset: int | None = None, timeout: int = 25) -> list[dict]:
        """Long-poll for updates. The HTTP read timeout MUST be longer than the
        long-poll timeout — otherwise the client gives up before Telegram has
        anything to return.
        """
        payload = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        # +10s buffer for the server's own request processing.
        res = self._call("getUpdates", payload, http_timeout=timeout + 10)
        return res if isinstance(res, list) else []

    def get_file_path(self, file_id: str) -> str:
        res = self._call("getFile", {"file_id": file_id})
        return res["file_path"]

    def download_file(self, file_path: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
        return dest


# ---------- High-level digest helpers (for search_jobs.py) ----------

def digest_header_mdv2() -> str:
    today = mdv2_escape(time.strftime("%A, %d %B %Y"))
    # Two lines: primary heading + date subtitle. Reads cleaner than one
    # dense dash-joined line on narrow phone screens.
    return f"*Daily Job Digest*\n_{today}_"


def send_per_job_digest(
    tg: TelegramClient,
    chat_id: int,
    jobs: list[Job],
    cfg: dict,
    on_sent,  # callable(message_id:int, job:Job) -> None
    enrichments: dict[str, dict] | None = None,
    min_score: int = 0,
) -> int:
    """Send one message per job, each with its own inline keyboard.

    `enrichments`, if provided, is a map keyed by Job.job_id → {match_score,
    why_match, key_details}. Each matching job's message will include the
    resume-aware card (⭐ score, why-match line, key details).

    `min_score` (0-5) is informational — when >0 the count line mentions the
    active gate so the user knows their digest is filtered. The actual score
    filtering happens upstream in search_jobs.py.

    Calls `on_sent` after every successful send so the caller can persist the
    message_id → job_id mapping in the DB.

    Returns number of messages sent.
    """
    msg_cfg = cfg.get("message", {})
    inc_snip = bool(msg_cfg.get("include_snippet", True))
    snip_chars = int(msg_cfg.get("snippet_chars", 240))
    enrichments = enrichments or {}

    # Header first; when enriched, sort by descending match score so the
    # strongest fits land at the top of the feed.
    if enrichments:
        jobs = sorted(
            jobs,
            key=lambda j: int((enrichments.get(j.job_id) or {}).get("match_score") or 0),
            reverse=True,
        )

    tg.send_message(chat_id, digest_header_mdv2() + "\n\n" + _count_line(jobs, min_score=min_score))
    if not jobs:
        return 1
    sent = 1
    for job in jobs:
        enr = enrichments.get(job.job_id)
        text = format_job_mdv2(
            job, include_snippet=inc_snip, snippet_chars=snip_chars, enrichment=enr,
        )
        kb = job_keyboard(job.job_id, url=job.url or None)
        try:
            msg_id = tg.send_message(chat_id, text, reply_markup=kb)
            on_sent(msg_id, job)
            sent += 1
            time.sleep(0.35)  # 30 msg/sec global cap → stay well under
        except Exception as e:
            log.error("send_message failed for %s: %s", job.job_id, e)
    return sent


def _count_line(jobs: Iterable[Job], min_score: int = 0) -> str:
    jobs = list(jobs)
    gate = ""
    if min_score and min_score > 0:
        # MDv2 allows '≥' literally but we escape parens explicitly.
        gate = f" \\(filtered to ≥ {int(min_score)}/5\\)"
    if not jobs:
        return f"_No new postings today{gate}\\._"
    by_src: dict[str, int] = {}
    for j in jobs:
        by_src[j.source] = by_src.get(j.source, 0) + 1
    # Sort by source name for deterministic output across runs.
    parts = "  ·  ".join(f"{mdv2_escape(k)} {v}" for k, v in sorted(by_src.items()))
    noun = "posting" if len(jobs) == 1 else "postings"
    return f"*{len(jobs)}* new {noun}{gate}\n`{parts}`"
