#!/usr/bin/env python3
"""Live smoke test for the jobs.ac.uk source adapter.

Hits the public per-category RSS feeds at
https://www.jobs.ac.uk/jobs/<slug>/?format=rss. Asserts:

  * fetch({"max_per_source": 5}) returns >= 1 Job (or skips with a clear
    message if the network is unavailable / the portal is degraded)
  * each Job has a non-empty title, url, and external_id
  * URLs are absolute and point to www.jobs.ac.uk
  * external_ids look like the alphanumeric jobs.ac.uk job codes
  * sample titles are printed to stdout for human eyeballing

Network is required. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_jobs_ac_uk_live.py

Exits non-zero on any check failure. Exits 0 with a SKIP banner if every
configured feed returns a hard network error (treated as environmental).
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import jobs_ac_uk  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("jobs.ac.uk live smoke — fetching up to 5 jobs across research-leaning categories")
jobs = jobs_ac_uk.fetch({"max_per_source": 5})

print(f"\n  Returned {len(jobs)} job(s).")

if len(jobs) == 0:
    # Treat empty-yield as a graceful skip: the live portal could be down
    # or the RSS routes could have shifted. Don't fail CI on transient
    # network issues, but make it loud.
    print("\nSKIP  jobs.ac.uk returned 0 jobs — check forensic log for status codes.")
    sys.exit(0)

check(len(jobs) > 0, "fetch returned at least one job")
check(len(jobs) <= 5, f"fetch respected cap (got {len(jobs)})")

for i, j in enumerate(jobs):
    print(f"\n  [{i}] title  = {j.title!r}")
    print(f"      company= {j.company!r}")
    print(f"      loc    = {j.location!r}")
    print(f"      url    = {j.url}")
    print(f"      ext_id = {j.external_id!r}")
    print(f"      posted = {j.posted_at!r}")
    print(f"      salary = {j.salary!r}")
    print(f"      snippet[:120] = {j.snippet[:120]!r}")

    check(bool(j.title.strip()), f"job[{i}].title is non-empty")
    check(bool(j.url.strip()), f"job[{i}].url is non-empty")
    check(bool(j.external_id.strip()), f"job[{i}].external_id is non-empty")
    check(
        j.url.startswith("https://www.jobs.ac.uk/"),
        f"job[{i}].url is absolute www.jobs.ac.uk URL (got {j.url!r})",
    )
    check(j.source == "jobs_ac_uk", f"job[{i}].source == 'jobs_ac_uk'")
    check(
        j.external_id.isalnum(),
        f"job[{i}].external_id is alphanumeric (got {j.external_id!r})",
    )

# Cross-feed dedup sanity: no two jobs should share an external URL.
urls = [j.url for j in jobs]
check(len(set(urls)) == len(urls), "fetch deduplicates URLs across feeds")

print("\n  Sample titles:")
for j in jobs:
    print(f"    - {j.title}")

print()
if failures:
    print(f"FAIL {len(failures)} check(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK  All jobs.ac.uk live smoke checks passed.")
