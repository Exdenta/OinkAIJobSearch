#!/usr/bin/env python3
"""Smoke: ⊘ Not a fit MORPHS the job card into the "Why?" prompt in place.

When SKIP_FEEDBACK_ENABLED=1, tapping ⊘ Not a fit no longer deletes the
card and appends a fresh prompt message at the bottom — instead it edits
the original card's text + keyboard so the question replaces the job
right where the user's eye already is. This keeps chat order stable and
makes the cause/effect relationship obvious.

Verifies:
  1. edit_message_text called with the prompt body + sr:skip keyboard.
  2. delete_message NOT called on the morph path.
  3. No NEW send_message added.
  4. STATE_AWAITING_SKIP_REASON set with the job payload.
  5. Toast = "Tell me why?".
  6. Fallback path: when edit_message_text raises, the prompt is still
     delivered as a new message (legacy behavior preserved).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

os.environ["URL_VALIDATION_OFF"] = "1"
os.environ["FORENSIC_OFF"] = "1"
os.environ["SKIP_FEEDBACK_ENABLED"] = "1"
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="smoke_morph_")

for mod in ("forensic", "telegram_client", "db", "bot"):
    if mod in sys.modules:
        del sys.modules[mod]

import bot  # noqa: E402
from db import DB  # noqa: E402


PASS = 0
FAIL = 0


def _check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


class FakeTG:
    """FakeTG with `edit_message_text` available — exercises the morph path."""
    def __init__(self, edit_raises: bool = False) -> None:
        self.sends: list[tuple] = []
        self.edits: list[tuple] = []
        self.edit_keyboards: list[tuple] = []
        self.deletes: list[tuple] = []
        self.toasts: list[tuple] = []
        self._edit_raises = edit_raises
        self._next_id = 5000

    def send_message(self, chat_id, text, reply_markup=None, parse_mode=None,
                     disable_preview=True) -> int:
        self.sends.append((chat_id, text, reply_markup))
        self._next_id += 1
        return self._next_id

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None,
                          parse_mode=None, disable_preview=True) -> None:
        if self._edit_raises:
            raise RuntimeError("simulated edit failure (>48h)")
        self.edits.append((chat_id, message_id, text, reply_markup))

    def edit_reply_markup(self, chat_id, message_id, reply_markup) -> None:
        self.edit_keyboards.append((chat_id, message_id, reply_markup))

    def delete_message(self, chat_id, message_id) -> bool:
        self.deletes.append((chat_id, message_id))
        return True

    def answer_callback(self, cb_id, text="", show_alert=False) -> None:
        self.toasts.append((cb_id, text))


def _seed_job(db: DB, chat_id: int, job_id: str = "job-morph-1") -> None:
    db.upsert_user(chat_id)
    with db._conn() as c:
        c.execute(
            "INSERT INTO jobs (job_id, source, external_id, title, company, location, url, "
            "posted_at, snippet, salary, first_seen_at) "
            "VALUES (?, 'linkedin', 'ext', 'Senior X', 'Acme', 'EU', "
            "'https://example.com/x', '2026-04-30', 'snip', '', 0)",
            (job_id,),
        )


def _cb(chat_id: int, msg_id: int, job_id: str) -> dict:
    return {
        "id": f"cbq-{job_id}",
        "data": f"n:{job_id}",
        "message": {
            "message_id": msg_id,
            "chat": {"id": chat_id},
        },
    }


# ---------------------------------------------------------------------------

print("\n=== 1. SKIP_FEEDBACK_ENABLED=1 + edit_message_text works → MORPH ===")

db = DB(Path(os.environ["STATE_DIR"]) / "t.db")
chat_id = 7777
_seed_job(db, chat_id, "job-morph-1")
tg = FakeTG(edit_raises=False)
bot.handle_callback(tg, db, _cb(chat_id, msg_id=42, job_id="job-morph-1"))

_check(len(tg.edits) == 1, f"edit_message_text called once (got {len(tg.edits)})")
if tg.edits:
    _, _, text, kb = tg.edits[0]
    _check("Why didn't this fit?" in text, "edit text contains the prompt body")
    _check(kb == bot.SKIP_REASON_INLINE_KB, "edit replaces keyboard with sr:skip")
_check(tg.deletes == [], f"delete_message NOT called on morph path (got {tg.deletes})")
_check(tg.sends == [], f"NO new message sent on morph path (got {tg.sends})")
state = db.get_awaiting_state(chat_id)
_check(state == bot.STATE_AWAITING_SKIP_REASON,
       f"awaiting state set (got {state!r})")
payload = db.get_awaiting_state_payload(chat_id) or {}
_check(payload.get("job_id") == "job-morph-1",
       f"awaiting payload carries job_id (got {payload.get('job_id')!r})")
_check(any(t == "Tell me why?" for _, t in tg.toasts),
       f"toast = 'Tell me why?' (got {tg.toasts})")

# Verify the DB row was still flipped to skipped.
status_row = db.get_application_status(chat_id, "job-morph-1") if hasattr(db, "get_application_status") else None
# Fallback if helper name differs:
with db._conn() as c:
    row = c.execute(
        "SELECT status FROM applications WHERE chat_id = ? AND job_id = ?",
        (chat_id, "job-morph-1"),
    ).fetchone()
_check(row and row["status"] == "skipped",
       f"DB applications row marked skipped (got {row['status'] if row else None})")


# ---------------------------------------------------------------------------

print("\n=== 2. edit_message_text raises → fallback to new prompt + delete ===")

_seed_job(db, chat_id, "job-morph-2")
tg = FakeTG(edit_raises=True)
bot.handle_callback(tg, db, _cb(chat_id, msg_id=99, job_id="job-morph-2"))

_check(len(tg.sends) == 1, f"prompt re-delivered as new send_message (got {len(tg.sends)})")
if tg.sends:
    _, txt, kb = tg.sends[0]
    _check("Why didn't this fit?" in txt, "fallback send carries the prompt body")
    _check(kb == bot.SKIP_REASON_INLINE_KB, "fallback send carries sr:skip keyboard")
_check(len(tg.deletes) == 1, f"original card deleted in fallback (got {tg.deletes})")
_check(db.get_awaiting_state(chat_id) == bot.STATE_AWAITING_SKIP_REASON,
       "awaiting state still set on fallback path")


# ---------------------------------------------------------------------------

print()
print(f"{'PASS' if FAIL == 0 else 'FAIL'}: {PASS}/{PASS+FAIL} tests passed")
sys.exit(0 if FAIL == 0 else 1)
