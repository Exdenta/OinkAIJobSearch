#!/usr/bin/env python3
"""Live smoke test for the YC Work at a Startup source adapter.

Hits the real `/jobs/search` endpoint — no mocks. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_ycombinator_was_live.py

Asserts:
  - fetch() returns a list (no exception, parser didn't crash)
  - at least 1 Job is returned (or, if 0, prints a WARN and exits 0 — the
    network may be unavailable; we don't want CI noise on transient
    failures, matching the convention in `smoke_euraxess_live.py`)
  - every Job has the required fields populated (id, title, url, source)
  - every URL points to https://www.workatastartup.com/jobs/<id>
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

from sources import ycombinator_was  # noqa: E402


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> int:
    print("ycombinator_was: live fetch (max_per_source=12)")
    jobs = ycombinator_was.fetch({"max_per_source": 12})

    if not isinstance(jobs, list):
        _fail(f"fetch returned {type(jobs).__name__}, expected list")

    print(f"ycombinator_was: returned {len(jobs)} job(s)")

    if len(jobs) == 0:
        print("WARN: 0 jobs — re-run if this seems wrong; parser still passed.")
        print("PASS (empty result, no exceptions)")
        return 0

    for i, j in enumerate(jobs, 1):
        if j.source != "ycombinator_was":
            _fail(f"job[{i}].source = {j.source!r}, expected 'ycombinator_was'")
        if not j.external_id:
            _fail(f"job[{i}] has empty external_id")
        if not j.title:
            _fail(f"job[{i}] has empty title")
        if not j.url.startswith("https://www.workatastartup.com/jobs/"):
            _fail(f"job[{i}].url unexpected prefix: {j.url!r}")

    # external_ids should all be distinct (the adapter dedupes across
    # fan-out queries — assert that contract holds in production).
    ids = [j.external_id for j in jobs]
    if len(set(ids)) != len(ids):
        _fail(f"duplicate external_ids in result: {ids}")

    print()
    print("Sample postings:")
    for i, j in enumerate(jobs, 1):
        print(f"  {i}. {j.title}")
        print(f"     company:  {j.company or '<unknown>'}")
        print(f"     location: {j.location or '<unknown>'}")
        print(f"     salary:   {j.salary or '<unspecified>'}")
        print(f"     url:      {j.url}")
        if j.snippet:
            snippet = j.snippet if len(j.snippet) <= 160 else j.snippet[:157] + "..."
            print(f"     snippet:  {snippet}")
        print()

    print(f"PASS ({len(jobs)} jobs parsed cleanly)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
