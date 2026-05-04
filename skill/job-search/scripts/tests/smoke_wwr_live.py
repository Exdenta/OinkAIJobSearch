#!/usr/bin/env python3
"""Live smoke test for remote_boards._fetch_wwr.

Hits the real WeWorkRemotely RSS feeds and asserts that the adapter
returns a non-zero number of jobs. If the feeds are ever genuinely
deprecated (all 5 returning 0 with a network/parse error), the test
should fail loudly so we notice — silent zero is the bug we just fixed.

Run:
    python3 skill/job-search/scripts/tests/smoke_wwr_live.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Forensic logger writes under STATE_DIR; sandbox it for the smoke run.
os.environ.setdefault("STATE_DIR", tempfile.mkdtemp(prefix="wwr_smoke_"))
os.environ.pop("FORENSIC_OFF", None)

from sources.remote_boards import _fetch_wwr, WWR_FEEDS  # noqa: E402


def main() -> int:
    print(f"=== smoke_wwr_live: hitting {len(WWR_FEEDS)} WWR feeds ===")
    jobs = _fetch_wwr({})
    print(f"_fetch_wwr returned {len(jobs)} jobs")
    for j in jobs[:5]:
        print(f"  - [{j.company}] {j.title[:80]} -> {j.url}")

    if len(jobs) == 0:
        print("FAIL: WWR adapter returned 0 jobs across all feeds.")
        print("Diagnostic hints:")
        print(" - Run with verbose logging to see per-feed status_code / bozo_exception")
        print(" - SSL cert verify errors on macOS python.org builds: run")
        print("   `/Applications/Python 3.x/Install Certificates.command`")
        print(" - If feeds are genuinely dead, comment them out in WWR_FEEDS")
        return 1

    # Sanity: titles populated, urls look like wwr links
    bad = [j for j in jobs if not j.title or "weworkremotely.com" not in (j.url or "")]
    if bad:
        print(f"FAIL: {len(bad)} jobs with missing title or non-WWR url; first: {bad[0]!r}")
        return 1

    print(f"PASS: got {len(jobs)} WWR jobs with valid titles + urls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
