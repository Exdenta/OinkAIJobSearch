#!/usr/bin/env python3
"""Live smoke test for the EURAXESS source adapter.

Hits the real portal — no mocks. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_euraxess_live.py

Asserts:
  - fetch() returns a list (no exception, parser didn't crash)
  - if the portal has any jobs today, every Job has the required fields
    populated (id, title, url, source)

Prints sample titles + organisations + locations so a human can sanity-check
that the scrape is still aligned with the live markup.
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

from sources import euraxess  # noqa: E402


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> int:
    print("euraxess: live fetch (max_per_source=5)")
    jobs = euraxess.fetch({"max_per_source": 5})

    if not isinstance(jobs, list):
        _fail(f"fetch returned {type(jobs).__name__}, expected list")

    print(f"euraxess: returned {len(jobs)} job(s)")

    if len(jobs) == 0:
        # Acceptable per the spec: portal could legitimately be empty after
        # filters, but we should at least confirm the parse didn't blow up.
        print("WARN: 0 jobs — re-run if this seems wrong; parser still passed.")
        print("PASS (empty result, no exceptions)")
        return 0

    for i, j in enumerate(jobs, 1):
        if j.source != "euraxess":
            _fail(f"job[{i}].source = {j.source!r}, expected 'euraxess'")
        if not j.external_id:
            _fail(f"job[{i}] has empty external_id")
        if not j.title:
            _fail(f"job[{i}] has empty title")
        if not j.url.startswith("https://euraxess.ec.europa.eu/"):
            _fail(f"job[{i}].url not absolute https euraxess: {j.url!r}")

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
