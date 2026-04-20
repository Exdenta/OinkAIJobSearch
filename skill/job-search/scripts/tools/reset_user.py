#!/usr/bin/env python3
"""Reset one user's history in state/jobs.db.

Wipes `sent_messages` and `applications` rows for a given chat_id so the next
`search_jobs.py` run treats every currently-known posting as brand-new again.
Preserves the `users` row (resume stays) and the global `jobs` table (so other
users' dedupe and button callbacks keep working).

Usage:
    # Default: dry run, only report counts.
    python skill/job-search/scripts/tools/reset_user.py --chat-id 123456789

    # Commit the deletes.
    python skill/job-search/scripts/tools/reset_user.py --chat-id 123456789 --yes

    # Also forget the resume (rare — you'll need to re-upload).
    python skill/job-search/scripts/tools/reset_user.py --chat-id 123456789 --yes --drop-resume

Stop bot.py before running this — SQLite writes compete with the long-poll loop.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def default_db_path() -> Path:
    here = Path(__file__).resolve()
    return here.parents[3] / "state" / "jobs.db"  # skill/job-search/scripts/tools/..


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chat-id", type=int, required=True, help="Telegram chat_id to reset.")
    ap.add_argument("--db", type=Path, default=None, help="Path to jobs.db (default: state/jobs.db).")
    ap.add_argument("--yes", action="store_true", help="Actually delete (otherwise dry-run).")
    ap.add_argument("--drop-resume", action="store_true",
                    help="Also clear the user's resume_path/resume_text.")
    args = ap.parse_args()

    db_path = args.db or default_db_path()
    if not db_path.exists():
        print(f"DB not found at {db_path}", file=sys.stderr)
        return 2

    con = sqlite3.connect(db_path, timeout=15)
    con.execute("PRAGMA busy_timeout = 15000")
    cur = con.cursor()

    def count(sql: str) -> int:
        return cur.execute(sql, (args.chat_id,)).fetchone()[0]

    sent_n = count("SELECT COUNT(*) FROM sent_messages WHERE chat_id = ?")
    app_n  = count("SELECT COUNT(*) FROM applications  WHERE chat_id = ?")
    usr    = cur.execute("SELECT username, first_name FROM users WHERE chat_id = ?",
                         (args.chat_id,)).fetchone()

    print(f"chat_id={args.chat_id}  user={usr}")
    print(f"  sent_messages: {sent_n}")
    print(f"  applications:  {app_n}")

    if not args.yes:
        print("\n(dry-run — pass --yes to commit)")
        return 0

    cur.execute("DELETE FROM sent_messages WHERE chat_id = ?", (args.chat_id,))
    cur.execute("DELETE FROM applications  WHERE chat_id = ?", (args.chat_id,))
    if args.drop_resume:
        cur.execute(
            "UPDATE users SET resume_path = NULL, resume_text = NULL WHERE chat_id = ?",
            (args.chat_id,),
        )
    con.commit()
    con.close()
    print(f"\ndeleted: sent_messages={sent_n}  applications={app_n}"
          + ("  resume=cleared" if args.drop_resume else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
