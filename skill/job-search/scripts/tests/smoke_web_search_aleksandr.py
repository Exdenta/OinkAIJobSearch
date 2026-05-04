#!/usr/bin/env python3
"""Live smoke test for sources.web_search.fetch with Aleksandr's profile.

Reproduces the 2026-04 zero-runs incident on chat 169016071. Before the
fix, the discovery agent ran ~15s, hit `permission_denials` for every
WebSearch tool_use, and returned `{"jobs": []}` — see
state/forensic_logs/log.0.jsonl.

After the fix (web_search now uses run_p_with_tools with
allowed_tools="WebSearch,WebFetch"), the agent should return >0 jobs.

Run:
    python3 skill/job-search/scripts/tests/smoke_web_search_aleksandr.py

Exit code:
    0 = success (>0 jobs returned)
    1 = failure (0 jobs returned, agent likely still permission-denied)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from sources import web_search  # noqa: E402


CHAT_ID = 169016071
# Project root: <root>/skill/job-search/scripts/tests/<this>
PROJECT_ROOT = Path(__file__).resolve().parents[4]
DB_PATH = PROJECT_ROOT / os.environ.get("STATE_DIR", "state") / "jobs.db"


def _load_profile_seeds_from_db() -> dict | None:
    """Pull Aleksandr's profile.search_seeds.web_search from the live DB."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(
            "SELECT json_extract(user_profile, '$.search_seeds.web_search') "
            "FROM users WHERE chat_id = ?",
            (CHAT_ID,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


# Filters mirror what search_jobs.py would assemble for a senior MLOps user.
# We turn web_search on explicitly (the adapter no-ops otherwise).
FILTERS = {
    "sources": {"web_search": True},
    "max_per_source": 12,
    "ai_web_search_timeout_s": 240,
    "keywords": [
        "mlops", "machine learning", "ml platform",
        "python", "kubernetes", "aws", "databricks",
    ],
    "title_must_match": ["mlops", "machine learning engineer", "ml platform"],
    "title_exclude": ["intern", "frontend", "ios", "android"],
    "locations": ["Remote", "Europe", "Spain", "EU"],
    "remote": "yes",
    "seniority": "senior",
    "salary": {"min_usd": 0},
    "language": "English",
    "max_age_hours": 168,
}

# The user's free-text from /prefs (Aleksandr's actual phrasing).
USER_FREE_TEXT = (
    "senior-level roles similar to my resume, remote only, location: Remote EU."
)

# Fallback seeds if the DB isn't available (e.g. running in CI).
FALLBACK_SEEDS = {
    "seed_phrases": [
        "senior mlops engineer remote europe",
        "machine learning engineer python remote eu",
        "ml platform engineer databricks remote",
        "mlops aws kubernetes remote europe",
    ],
    "ats_domains": ["greenhouse.io", "lever.co", "ashbyhq.com", "workable.com"],
    "focus_notes": (
        "Prefer EU-timezone-friendly fully remote senior MLOps and ML "
        "platform roles. De-prioritize pure data science research, people "
        "management, and frontend or mobile-first positions."
    ),
}


def main() -> int:
    seeds = _load_profile_seeds_from_db() or FALLBACK_SEEDS
    print(f"[smoke] Using seeds source: "
          f"{'live DB' if _load_profile_seeds_from_db() else 'fallback'}")
    print(f"[smoke] Seed phrases: {seeds.get('seed_phrases', [])}")
    print(f"[smoke] Calling web_search.fetch (this may take 30-90s)...")

    jobs = web_search.fetch(
        FILTERS,
        user_free_text=USER_FREE_TEXT,
        profile_seeds=seeds,
    )

    print(f"[smoke] web_search.fetch returned {len(jobs)} jobs")
    for j in jobs[:10]:
        print(f"  - {j.title[:70]:<70} | {j.company[:25]:<25} | {j.location[:25]:<25}")
        print(f"    {j.url}")

    if len(jobs) == 0:
        print("[smoke] FAIL: 0 jobs — the bug may still be present.", file=sys.stderr)
        print("[smoke] Inspect state/forensic_logs/log.0.jsonl for the latest "
              "claude_cli.run_p_with_tools entry; look for permission_denials.",
              file=sys.stderr)
        return 1

    print(f"[smoke] PASS: {len(jobs)} jobs returned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
