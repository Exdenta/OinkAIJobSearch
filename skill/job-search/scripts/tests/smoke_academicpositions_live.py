#!/usr/bin/env python3
"""Live smoke test for the AcademicPositions source adapter.

Hits the real site — no mocks. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_academicpositions_live.py

Asserts:
  - fetch() returns a list (never raises)
  - if Cloudflare lets us through, every Job has the required fields populated
  - if Cloudflare blocks (the typical case at the moment), we still PASS
    because the adapter is meant to degrade gracefully

Prints sample titles + organisations + countries so a human can sanity-check
that any successful parse looks reasonable.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "sources"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from sources import academicpositions  # noqa: E402


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> int:
    print("academicpositions: live fetch (max_per_source=5)")
    jobs = academicpositions.fetch({
        "max_per_source": 5,
        "academicpositions_search": "qualitative research",
    })

    if not isinstance(jobs, list):
        _fail(f"fetch returned {type(jobs).__name__}, expected list")

    print(f"academicpositions: returned {len(jobs)} job(s)")

    if len(jobs) == 0:
        # Cloudflare's Bot Fight Mode currently 403s every server-side request
        # to academicpositions.com. The adapter logs this in forensic and
        # returns [] cleanly — that's a PASS for the smoke harness.
        print("WARN: 0 jobs — typically Cloudflare 403. Adapter degraded gracefully.")
        print("PASS (empty result, no exceptions)")
        return 0

    for i, j in enumerate(jobs, 1):
        if j.source != "academicpositions":
            _fail(f"job[{i}].source = {j.source!r}, expected 'academicpositions'")
        if not j.external_id:
            _fail(f"job[{i}] has empty external_id")
        if not j.title:
            _fail(f"job[{i}] has empty title")
        if not (j.url.startswith("https://academicpositions.com/")
                or j.url.startswith("http://academicpositions.com/")):
            _fail(f"job[{i}].url not on academicpositions.com: {j.url!r}")

    print()
    print("Sample postings:")
    for i, j in enumerate(jobs, 1):
        print(f"  {i}. {j.title}")
        print(f"     org:      {j.company or '<unknown>'}")
        print(f"     location: {j.location or '<unknown>'}")
        print(f"     posted:   {j.posted_at or '<unknown>'}")
        print(f"     url:      {j.url}")
        if j.snippet:
            snippet = j.snippet if len(j.snippet) <= 160 else j.snippet[:157] + "..."
            print(f"     snippet:  {snippet}")
        print()

    print(f"PASS ({len(jobs)} jobs parsed cleanly)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
