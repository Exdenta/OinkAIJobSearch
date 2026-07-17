#!/usr/bin/env python3
"""Offline smoke test for feedback_digest.py.

Covers the cases that don't need the real `claude` CLI:

  1. Below threshold → digest skipped, runner never called, notes untouched.
  2. Happy path — threshold met → runner called once, fresh notes stored,
     feedback_notes_updated_at bumped.
  3. Runner returns None (parse error / CLI missing) → None returned, the
     PREVIOUSLY stored notes are left byte-for-byte untouched.
  4. Malformed JSON shape (`notes` not a list) → same "untouched" guarantee.
  5. Model legitimately returns an empty `notes` array → NOT a failure;
     persisted as an empty string (distinct from "call failed").
  6. Kill switch (FEEDBACK_DIGEST_ENABLED=0) → no call even above threshold.
  7. force=True with only previous notes (no new feedback at all) → still
     calls the model (manual "regenerate" escape hatch).
  8. force=True on a genuinely empty account (no feedback, no previous
     notes) → still skipped; there is nothing to summarize.
  9. `augment_prefs_text` — appends the notes section when notes exist,
     returns prefs_text unchanged when there are none.

No Telegram, no network, no real Claude CLI. Exits non-zero on any
assertion failure so CI / the shell smoke step can rely on $?.
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

from db import DB  # noqa: E402
import feedback_digest as fd  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def _envelope(payload: dict) -> str:
    """Wrap a JSON dict in the `claude -p --output-format json` envelope
    that `extract_assistant_text` knows how to unwrap."""
    return json.dumps({"result": json.dumps(payload, ensure_ascii=False)})


def _make_runner(payload: dict | None):
    """Return a stub `_run_p` that records calls and returns the canned
    envelope (or None if payload is None to simulate CLI missing /
    parse failure upstream)."""
    calls: list[dict] = []

    def _runner(prompt: str, *, timeout_s: int, model: str | None = None) -> str | None:
        calls.append({
            "prompt_chars": len(prompt or ""),
            "prompt_head": (prompt or "")[:200],
            "timeout_s": timeout_s,
            "model": model,
        })
        if payload is None:
            return None
        return _envelope(payload)

    return _runner, calls


def _fresh_db() -> tuple[DB, int]:
    """Spin up a temp DB and pre-register a user. Returns (db, chat_id)."""
    td = tempfile.mkdtemp(prefix="feedback_digest_smoke_")
    db = DB(Path(td) / "jobs.db")
    chat_id = 99101
    db.upsert_user(chat_id=chat_id, username="t", first_name="T", last_name="")
    return db, chat_id


def _seed_job(db: DB, job_id: str, title: str, company: str) -> None:
    db.upsert_job({
        "job_id": job_id,
        "source": "linkedin",
        "title": title,
        "company": company,
        "location": "Remote",
        "url": f"https://example.com/{job_id}",
        "posted_at": None,
        "snippet": "A job.",
        "salary": None,
    })


def _seed_feedback(db: DB, chat_id: int, n: int, *, verdict: str = "down",
                    reason: str | None = "not a fit") -> None:
    """Create N distinct (job, feedback) rows for this user."""
    for i in range(n):
        job_id = f"job-{verdict}-{i:03d}"
        _seed_job(db, job_id, f"Some Role {i}", f"Company {i}")
        db.record_job_feedback(chat_id, job_id, verdict, reason)


NOTES_PAYLOAD = {
    "notes": [
        "Prefers backend roles, not frontend.",
        "Rejects fintech / crypto companies — mentioned twice.",
    ],
}


# ---------------------------------------------------------------------------
# Test 1 — below threshold → no call, no change
# ---------------------------------------------------------------------------
print("1. below threshold → digest skipped, no runner call")

db, cid = _fresh_db()
_seed_feedback(db, cid, 3)   # default threshold is 5

runner, calls = _make_runner(NOTES_PAYLOAD)
result = fd.maybe_run_feedback_digest(db, cid, _run_p=runner)

check(result is None, f"result is None (got {result!r})")
check(len(calls) == 0, f"runner NOT called (got {len(calls)} calls)")
notes, updated_at = db.get_feedback_notes(cid)
check(notes is None, "notes still unset")
check(updated_at is None, "updated_at still unset")


# ---------------------------------------------------------------------------
# Test 2 — happy path: threshold met → notes stored + timestamp bumped
# ---------------------------------------------------------------------------
print("\n2. threshold met → runner called once, notes persisted")

db, cid = _fresh_db()
_seed_feedback(db, cid, 5)

runner, calls = _make_runner(NOTES_PAYLOAD)
before = time.time()
result = fd.maybe_run_feedback_digest(db, cid, _run_p=runner)

check(len(calls) == 1, f"runner called exactly once (got {len(calls)})")
check(result is not None, "result is non-None")
if result:
    check("Prefers backend roles" in result, f"notes contain first line (got {result!r})")
    check("Rejects fintech" in result, f"notes contain second line (got {result!r})")

notes, updated_at = db.get_feedback_notes(cid)
check(notes == result, "get_feedback_notes matches returned notes")
check(updated_at is not None and updated_at >= before, "updated_at bumped to ~now")

# The prompt should have carried the feedback rows and NOT crashed on the
# f-string / template substitution.
if calls:
    prompt_head = calls[0]["prompt_head"]
    check(len(prompt_head) > 0, "prompt was non-empty")


# ---------------------------------------------------------------------------
# Test 3 — runner returns None → old notes untouched
# ---------------------------------------------------------------------------
print("\n3. runner returns None (CLI/parse failure) → notes NOT touched")

db, cid = _fresh_db()
db.set_feedback_notes(cid, "- Old note that must survive.")
prior_notes, prior_updated_at = db.get_feedback_notes(cid)
# Seed the NEW feedback AFTER the notes timestamp — otherwise these rows
# would predate `feedback_notes_updated_at` and the threshold gate would
# short-circuit before ever reaching the runner, which would test the
# wrong thing. The tiny sleep guarantees strictly-greater created_at
# despite low wall-clock resolution on some platforms.
time.sleep(0.01)
_seed_feedback(db, cid, 5)

runner, calls = _make_runner(None)
result = fd.maybe_run_feedback_digest(db, cid, _run_p=runner)

check(result is None, f"result is None (got {result!r})")
check(len(calls) == 1, f"runner was called once (got {len(calls)})")
after_notes, after_updated_at = db.get_feedback_notes(cid)
check(after_notes == prior_notes, f"notes byte-equal to prior (got {after_notes!r})")
check(after_updated_at == prior_updated_at, "updated_at unchanged")


# ---------------------------------------------------------------------------
# Test 4 — malformed model output (notes not a list) → same guarantee
# ---------------------------------------------------------------------------
print("\n4. malformed shape ('notes' missing/wrong type) → notes NOT touched")

db, cid = _fresh_db()
db.set_feedback_notes(cid, "- Another surviving note.")
prior_notes, prior_updated_at = db.get_feedback_notes(cid)
time.sleep(0.01)   # see comment in test 3 — must postdate feedback_notes_updated_at
_seed_feedback(db, cid, 5)

runner, calls = _make_runner({"notes": "not-a-list, a string instead"})
result = fd.maybe_run_feedback_digest(db, cid, _run_p=runner)

check(len(calls) == 1, f"runner was actually invoked (got {len(calls)})")
check(result is None, f"result is None (got {result!r})")
after_notes, after_updated_at = db.get_feedback_notes(cid)
check(after_notes == prior_notes, "notes unchanged on malformed shape")
check(after_updated_at == prior_updated_at, "updated_at unchanged on malformed shape")


# ---------------------------------------------------------------------------
# Test 5 — legitimate empty notes array is NOT a failure
# ---------------------------------------------------------------------------
print("\n5. empty notes array from model → persisted as '' (not a failure)")

db, cid = _fresh_db()
_seed_feedback(db, cid, 5)

runner, calls = _make_runner({"notes": []})
result = fd.maybe_run_feedback_digest(db, cid, _run_p=runner)

check(result == "", f"result is empty string, not None (got {result!r})")
notes, updated_at = db.get_feedback_notes(cid)
check(notes == "", f"stored notes is empty string (got {notes!r})")
check(updated_at is not None, "updated_at WAS bumped (a real digest ran, it just said nothing)")


# ---------------------------------------------------------------------------
# Test 6 — kill switch
# ---------------------------------------------------------------------------
print("\n6. FEEDBACK_DIGEST_ENABLED=0 → no call even above threshold")

db, cid = _fresh_db()
_seed_feedback(db, cid, 10)

runner, calls = _make_runner(NOTES_PAYLOAD)
os.environ["FEEDBACK_DIGEST_ENABLED"] = "0"
try:
    result = fd.maybe_run_feedback_digest(db, cid, _run_p=runner)
finally:
    del os.environ["FEEDBACK_DIGEST_ENABLED"]

check(result is None, f"result is None with kill switch on (got {result!r})")
check(len(calls) == 0, f"runner NOT called (got {len(calls)} calls)")


# ---------------------------------------------------------------------------
# Test 7 — force=True with only previous notes (no new feedback) still runs
# ---------------------------------------------------------------------------
print("\n7. force=True + only previous notes (no new feedback) → still calls model")

db, cid = _fresh_db()
db.set_feedback_notes(cid, "- Some carried-forward note.")

runner, calls = _make_runner({"notes": ["Refined carried-forward note."]})
result = fd.maybe_run_feedback_digest(db, cid, force=True, _run_p=runner)

check(len(calls) == 1, f"runner called once under force=True (got {len(calls)})")
check(result is not None and "Refined carried-forward note" in result,
      f"fresh notes reflect the model's output (got {result!r})")


# ---------------------------------------------------------------------------
# Test 8 — force=True on a genuinely empty account → still skipped
# ---------------------------------------------------------------------------
print("\n8. force=True + nothing at all (no feedback, no previous notes) → skipped")

db, cid = _fresh_db()
runner, calls = _make_runner(NOTES_PAYLOAD)
result = fd.maybe_run_feedback_digest(db, cid, force=True, _run_p=runner)

check(result is None, f"result is None (got {result!r})")
check(len(calls) == 0, f"runner NOT called — nothing to summarize (got {len(calls)})")


# ---------------------------------------------------------------------------
# Test 9 — augment_prefs_text: injection into the scoring prefs blob
# ---------------------------------------------------------------------------
print("\n9. augment_prefs_text appends the notes section iff notes exist")

db, cid = _fresh_db()
base_prefs = "Remote only. Backend roles. Min salary 90k."

# No notes yet → unchanged.
augmented = fd.augment_prefs_text(db, cid, base_prefs)
check(augmented == base_prefs, f"unaugmented when no notes set (got {augmented!r})")

# Set notes, then confirm the section is appended with the expected header
# and that the original prefs text is preserved verbatim as a prefix.
db.set_feedback_notes(cid, "- Prefers backend roles, not frontend.\n- Rejects fintech.")
augmented = fd.augment_prefs_text(db, cid, base_prefs)
check(augmented.startswith(base_prefs), "original prefs_text preserved as prefix")
check(fd.NOTES_SECTION_HEADER in augmented, "notes section header present")
check("Prefers backend roles" in augmented, "notes content appended")
check("Rejects fintech" in augmented, "notes content appended (2nd line)")

# Empty prefs_text (no /prefs set yet) still gets the section appended
# cleanly, no leading garbage.
augmented_empty_base = fd.augment_prefs_text(db, cid, "")
check(augmented_empty_base.strip().startswith(fd.NOTES_SECTION_HEADER),
      f"works with empty base prefs_text (got {augmented_empty_base!r})")

# None base prefs_text (defensive — read_prefs could theoretically return
# something falsy) does not raise.
augmented_none_base = fd.augment_prefs_text(db, cid, None)
check(fd.NOTES_SECTION_HEADER in augmented_none_base, "works with prefs_text=None")


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"FAIL {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK All feedback_digest smoke checks passed.")
