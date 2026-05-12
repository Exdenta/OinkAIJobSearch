"""Per-user file storage for algorithm v2.

Algorithm v2 replaces the structured `user_profile` JSON (locations,
title_exclude, etc.) with two plain-text files per user:

    state/users/<chat_id>/resume.txt    — extracted resume body
    state/users/<chat_id>/prefs.txt     — verbatim preferences + appended
                                          'not a fit' comments

Scoring (`job_enrich`) feeds those two blobs straight to Haiku alongside
each posting. The model itself does the constraint extraction. No
projection layer, no schema drift, no Opus rebuild on every prefs tweak.

The DB still keeps a `users.resume_text` / `users.prefs_free_text` /
`users.skip_notes_text` mirror — file IS the source of truth, but we
write back to DB on every change so legacy code paths and the web UI
keep working until they're cut over.

Path layout:
    <STATE_DIR>/users/<chat_id>/resume.txt
    <STATE_DIR>/users/<chat_id>/prefs.txt

`STATE_DIR` defaults to `<project>/state` and is overridable via the
env var of the same name (matches search_jobs / bot conventions).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


# Header inserted before the appended skip-reason block. Picked so the
# scoring prompt reads it as "below this line is rolling rejection
# feedback the user wrote after pressing 'not a fit'".
_SKIP_HEADER = "[Recent 'not a fit' comments]"

# Cap on the on-disk prefs.txt size so we don't blow Haiku's prompt
# budget over time. Skip-reasons FIFO-rotate when the file would exceed
# this. The scoring prompt clips to ~3000 chars when reading too —
# this is the *storage* cap.
_MAX_PREFS_CHARS = 4000


def _project_root() -> Path:
    here = Path(__file__).resolve().parent
    # scripts/ → job-search/ → skill/ → project root
    return here.parent.parent.parent


def state_dir() -> Path:
    """Same `STATE_DIR` resolution rule as search_jobs.py / bot.py.

    Returns an absolute path; does NOT create the directory.
    """
    raw = os.environ.get("STATE_DIR", "state")
    p = Path(raw)
    if not p.is_absolute():
        p = _project_root() / p
    return p


def user_dir(chat_id: int) -> Path:
    """Per-user directory; created on first call."""
    d = state_dir() / "users" / str(int(chat_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def resume_path(chat_id: int) -> Path:
    return user_dir(chat_id) / "resume.txt"


def prefs_path(chat_id: int) -> Path:
    return user_dir(chat_id) / "prefs.txt"


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

def write_resume(chat_id: int, text: str) -> None:
    """Persist the extracted resume body. Empty / None text wipes the file."""
    p = resume_path(chat_id)
    if not text:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return
    p.write_text(text, encoding="utf-8")


def read_resume(chat_id: int) -> str:
    p = resume_path(chat_id)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        log.exception("user_files: failed to read %s", p)
        return ""


# ---------------------------------------------------------------------------
# Prefs (verbatim user description + appended skip-reasons)
# ---------------------------------------------------------------------------

def write_prefs(chat_id: int, text: str) -> None:
    """Replace the entire prefs.txt with `text`. Use for /prefs save —
    the user is restating their preferences from scratch.

    Empty / None text wipes the file.
    """
    p = prefs_path(chat_id)
    if not text:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        return
    p.write_text(text[:_MAX_PREFS_CHARS], encoding="utf-8")


def read_prefs(chat_id: int) -> str:
    p = prefs_path(chat_id)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        log.exception("user_files: failed to read %s", p)
        return ""


def append_skip_note(chat_id: int, reason: str) -> None:
    """Append a `not a fit` comment to prefs.txt under a stable header.

    Format on disk:

        <existing prefs body>

        [Recent 'not a fit' comments]
        - reason 1
        - reason 2
        - ...

    FIFO-trims oldest comments when the file would exceed `_MAX_PREFS_CHARS`.
    Empty `reason` is a no-op.
    """
    reason = (reason or "").strip()
    if not reason:
        return
    p = prefs_path(chat_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = p.read_text(encoding="utf-8") if p.exists() else ""

    # Split into (preamble, header, comments) so we can grow the
    # comments block without touching the user's original prefs.
    if _SKIP_HEADER in existing:
        head, _, tail = existing.partition(_SKIP_HEADER)
        head = head.rstrip()
        comments = [
            ln for ln in tail.splitlines()
            if ln.strip().startswith("- ")
        ]
    else:
        head = existing.rstrip()
        comments = []

    comments.append(f"- {reason[:400]}")

    def _render(comment_lines: list[str]) -> str:
        body_parts: list[str] = []
        if head:
            body_parts.append(head)
        body_parts.append(_SKIP_HEADER)
        body_parts.extend(comment_lines)
        return "\n".join(body_parts) + "\n"

    rendered = _render(comments)
    while len(rendered) > _MAX_PREFS_CHARS and len(comments) > 1:
        comments.pop(0)
        rendered = _render(comments)

    # If even the head + 1 comment is too long, hard-truncate at storage cap.
    p.write_text(rendered[:_MAX_PREFS_CHARS], encoding="utf-8")


def clear_user_files(chat_id: int) -> None:
    """Wipe both files. Used by the clean-data flow."""
    for fn in (resume_path, prefs_path):
        try:
            fn(chat_id).unlink()
        except FileNotFoundError:
            pass
