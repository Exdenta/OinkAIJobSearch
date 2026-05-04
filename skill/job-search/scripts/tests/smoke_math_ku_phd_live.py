#!/usr/bin/env python3
"""Live smoke test for the math_ku_phd source adapter.

Hits https://employment.ku.dk/phd/?get_rss=1 over the network and verifies:
  1. fetch() does not raise.
  2. It returns a list (possibly empty — we tolerate zero in case the RSS
     feed is briefly empty or the network is restricted, but we print it
     loudly so the operator can investigate).
  3. Every Job has the expected source/company/location, an absolute
     employment.ku.dk URL, and a non-empty title + external_id.

Run:
    cd <worktree-root>
    python3 skill/job-search/scripts/tests/smoke_math_ku_phd_live.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS / "sources"))

from sources import math_ku_phd  # noqa: E402


def _assert(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def main() -> int:
    print("Calling math_ku_phd.fetch({'max_per_source': 5}) ...")
    jobs = math_ku_phd.fetch({"max_per_source": 5})

    _assert(isinstance(jobs, list), f"fetch should return a list, got {type(jobs)!r}")
    print(f"  got {len(jobs)} jobs (cap=5)")

    if not jobs:
        # Document the zero-result reason so the human reviewer can decide
        # whether this is expected (no PhD postings open right now) or a
        # silent break (network blocked, RSS schema drifted, etc.).
        print(
            "WARN: fetch returned 0 jobs.\n"
            "      Likely causes: (a) employment.ku.dk RSS empty,\n"
            "      (b) outbound network blocked,\n"
            "      (c) RSS schema drifted (no <title>/show=NNNN ids).\n"
            "      Check skill/job-search/scripts/sources/math_ku_phd.py\n"
            "      forensic log for status_code + error fields."
        )
    else:
        # Validate the shape of every job we got back.
        for j in jobs:
            _assert(j.source == "math_ku_phd", f"source slug mismatch: {j.source}")
            _assert(j.title, "title should not be empty")
            _assert(j.external_id, f"external_id missing for {j.title!r}")
            _assert(j.url.startswith("https://employment.ku.dk/"),
                    f"url should be absolute KU URL, got {j.url!r}")
            _assert(j.company.startswith("University of Copenhagen"),
                    f"company should mention KU, got {j.company!r}")
            _assert(j.location == "Copenhagen, Denmark",
                    f"location should be Copenhagen, got {j.location!r}")
            _assert(len(j.snippet) <= 401,
                    f"snippet should be capped near 400 chars, got {len(j.snippet)}")

        print("\nSample titles:")
        for j in jobs[:5]:
            print(f"  - [{j.external_id}] {j.title}")
            print(f"      url:       {j.url}")
            print(f"      posted_at: {j.posted_at!r}")
            print(f"      snippet:   {j.snippet[:120]}...")

    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
