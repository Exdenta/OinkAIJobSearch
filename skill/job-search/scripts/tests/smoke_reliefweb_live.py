#!/usr/bin/env python3
"""Live smoke test for the ReliefWeb source adapter.

Hits the public RSS feed at https://reliefweb.int/jobs/rss.xml. Asserts:

  * fetch({"max_per_source": 5}) returns >= 1 Job
  * each Job has a non-empty title, url, and external_id
  * URLs are absolute and point to reliefweb.int
  * external_ids look like the numeric ReliefWeb job ids
  * sample titles are printed to stdout for human eyeballing

Network is required. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_reliefweb_live.py

Exits non-zero on any failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import reliefweb  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("ReliefWeb live smoke — fetching up to 5 jobs from RSS feed")
jobs = reliefweb.fetch({"max_per_source": 5})

print(f"\n  Returned {len(jobs)} job(s).")
check(len(jobs) > 0, "fetch returned at least one job")
check(len(jobs) <= 5, f"fetch respected cap (got {len(jobs)})")

for i, j in enumerate(jobs):
    print(f"\n  [{i}] title  = {j.title!r}")
    print(f"      company= {j.company!r}")
    print(f"      loc    = {j.location!r}")
    print(f"      url    = {j.url}")
    print(f"      ext_id = {j.external_id!r}")
    print(f"      posted = {j.posted_at!r}")
    print(f"      snippet[:120] = {j.snippet[:120]!r}")

    check(bool(j.title.strip()), f"job[{i}].title is non-empty")
    check(bool(j.url.strip()), f"job[{i}].url is non-empty")
    check(bool(j.external_id.strip()), f"job[{i}].external_id is non-empty")
    check(j.url.startswith("https://reliefweb.int/"),
          f"job[{i}].url is absolute reliefweb.int URL (got {j.url!r})")
    check(j.source == "reliefweb", f"job[{i}].source == 'reliefweb'")
    check(j.external_id.isdigit(),
          f"job[{i}].external_id is numeric (got {j.external_id!r})")

print("\n  Sample titles:")
for j in jobs:
    print(f"    - {j.title}")

print()
if failures:
    print(f"FAIL {len(failures)} check(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK  All ReliefWeb live smoke checks passed.")
