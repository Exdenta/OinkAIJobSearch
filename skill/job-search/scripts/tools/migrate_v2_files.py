#!/usr/bin/env python3
"""Migrate existing users to algorithm v2's file-backed storage.

For every user in `users`:
  * write `users.resume_text` → state/users/<chat_id>/resume.txt
  * compose prefs.txt from `prefs_free_text` + appended `skip_notes_text`
  * lift `users.user_profile.min_match_score` (if non-zero) into the new
    `users.min_match_score` column.

Idempotent: running twice is a no-op (writes are full overwrites and the
column lift only fires when the col is currently 0).

Run from project root:
    python skill/job-search/scripts/tools/migrate_v2_files.py
    python skill/job-search/scripts/tools/migrate_v2_files.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from db import DB
import user_files


SKIP_HEADER = "[Recent 'not a fit' comments]"


def _compose_prefs(prefs_free_text: str | None, skip_notes_text: str | None) -> str:
    body = (prefs_free_text or "").strip()
    notes = (skip_notes_text or "").strip()
    if not notes:
        return body
    bullets = [
        ln if ln.lstrip().startswith("- ") else f"- {ln.strip()}"
        for ln in notes.splitlines()
        if ln.strip()
    ]
    if not bullets:
        return body
    parts = []
    if body:
        parts.append(body)
    parts.append(SKIP_HEADER)
    parts.extend(bullets)
    return "\n".join(parts) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would change but don't write.")
    ap.add_argument("--state-dir", default=None,
                    help="Override STATE_DIR (default: ./state)")
    args = ap.parse_args()

    state = Path(args.state_dir) if args.state_dir else (
        # tools → scripts → job-search → skill → project root
        Path(__file__).resolve().parent.parent.parent.parent.parent / "state"
    )
    db_path = state / "jobs.db"
    db = DB(db_path)

    with db._conn() as c:
        rows = c.execute(
            "SELECT chat_id, resume_text, prefs_free_text, skip_notes_text, "
            "user_profile, min_match_score FROM users"
        ).fetchall()

    changes_resume = 0
    changes_prefs = 0
    changes_score = 0

    for r in rows:
        chat_id = int(r["chat_id"])
        resume = r["resume_text"] or ""
        prefs_text = _compose_prefs(r["prefs_free_text"], r["skip_notes_text"])

        # Files
        rp = user_files.resume_path(chat_id)
        pp = user_files.prefs_path(chat_id)
        if resume and rp.read_text(encoding="utf-8", errors="ignore") if rp.exists() else "" != resume:
            if not args.dry_run:
                user_files.write_resume(chat_id, resume)
            changes_resume += 1
        elif resume and not rp.exists():
            if not args.dry_run:
                user_files.write_resume(chat_id, resume)
            changes_resume += 1

        if prefs_text:
            current = pp.read_text(encoding="utf-8", errors="ignore") if pp.exists() else ""
            if current != prefs_text:
                if not args.dry_run:
                    user_files.write_prefs(chat_id, prefs_text)
                changes_prefs += 1

        # Score lift: only when col is 0/NULL AND profile JSON has a non-zero score
        col_score = int(r["min_match_score"] or 0) if r["min_match_score"] is not None else 0
        if col_score == 0:
            try:
                profile = json.loads(r["user_profile"] or "{}")
                json_score = int(profile.get("min_match_score") or 0)
            except (TypeError, ValueError, json.JSONDecodeError):
                json_score = 0
            if json_score > 0:
                if not args.dry_run:
                    db.set_min_match_score(chat_id, json_score)
                changes_score += 1
                print(f"  chat={chat_id}: lifted min_match_score {json_score}")

    print(f"\nresume.txt writes:    {changes_resume}")
    print(f"prefs.txt writes:     {changes_prefs}")
    print(f"min_match_score lifts: {changes_score}")
    print(f"users scanned:        {len(rows)}")
    if args.dry_run:
        print("\n(dry-run — nothing persisted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
