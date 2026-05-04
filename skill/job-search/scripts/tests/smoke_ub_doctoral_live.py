#!/usr/bin/env python3
"""Live smoke test for sources/ub_doctoral.py.

Calls fetch({"max_per_source": 5, "ai_scrape_timeout_s": 180}) against the
real `claude` CLI + UB's website. The result list MAY legitimately be empty
when UB has no open PhD positions advertised that day — we treat that as a
pass and just print whatever reasoning the LLM returned.

What we assert
--------------
  - fetch returns a list (never None, never raises).
  - Every Job is well-formed: source/title/company/url all populated, url
    starts with "http".

Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_ub_doctoral_live.py

Exit code is 0 on success (including the zero-results case) and non-zero on
malformed Jobs or unexpected exceptions.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "sources"))

# Verbose logging so the adapter's internal reasoning shows up in the output.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)

from sources import ub_doctoral  # noqa: E402
from dedupe import Job  # noqa: E402


def main() -> int:
    print("=== ub_doctoral live smoke test ===")
    print(f"landing_url = {ub_doctoral.LANDING_URL}")
    print(f"hr_url      = {ub_doctoral.HR_URL}")
    print()

    # 180s per the brief, but the UB site is slow + Liferay-heavy and the LLM
    # may need to follow a sub-link, so allow override via env for live runs.
    import os
    timeout_s = int(os.environ.get("UB_SMOKE_TIMEOUT_S", "180"))
    filters = {"max_per_source": 5, "ai_scrape_timeout_s": timeout_s}
    print(f"calling fetch({filters})...")
    try:
        jobs = ub_doctoral.fetch(filters)
    except Exception as e:
        print(f"FAIL: fetch raised: {e!r}")
        return 1

    if not isinstance(jobs, list):
        print(f"FAIL: fetch returned {type(jobs).__name__}, expected list")
        return 1

    print(f"\nfetch returned {len(jobs)} job(s).")
    if not jobs:
        print("(zero results is acceptable — UB often has no open PhD positions; "
              "see log lines above for the LLM's reasoning.)")
        print("\nPASS")
        return 0

    failures: list[str] = []
    for i, j in enumerate(jobs, 1):
        if not isinstance(j, Job):
            failures.append(f"job #{i} is {type(j).__name__}, expected Job")
            continue
        print(f"\n--- job #{i} ---")
        print(f"  title    : {j.title}")
        print(f"  company  : {j.company}")
        print(f"  location : {j.location}")
        print(f"  url      : {j.url}")
        print(f"  posted_at: {j.posted_at!r}")
        print(f"  ext_id   : {j.external_id[:80]}")
        print(f"  snippet  : {j.snippet[:200]}")

        if j.source != "ub_doctoral":
            failures.append(f"job #{i} has source={j.source!r}, expected 'ub_doctoral'")
        if not j.title:
            failures.append(f"job #{i} has empty title")
        if j.company != "Universitat de Barcelona":
            failures.append(f"job #{i} has unexpected company={j.company!r}")
        if not j.url or not j.url.startswith("http"):
            failures.append(f"job #{i} has bad url={j.url!r}")

    print()
    if failures:
        print(f"FAIL: {len(failures)} issue(s):")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
