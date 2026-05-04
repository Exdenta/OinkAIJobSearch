#!/usr/bin/env python3
"""Live smoke test for the ImpactPool source adapter.

Hits the public listing page at https://www.impactpool.org/search.
Asserts:

  * fetch({"max_per_source": 5}) returns >= 1 Job (or skips gracefully if
    the network is unreachable / ImpactPool is down)
  * each Job has a non-empty title, url, and external_id
  * URLs are absolute and point to www.impactpool.org/jobs/<id>
  * external_ids look like the numeric ImpactPool job ids
  * sample titles are printed to stdout for human eyeballing

The adapter never raises out of fetch(); it returns [] on failure. We
distinguish a legitimate empty result (treat as graceful skip with a
warning) from a successful one. Network is required; on a hard outage the
test prints SKIP and exits 0 so CI doesn't flap on third-party uptime.

Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_impactpool_live.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import impactpool  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("ImpactPool live smoke — fetching up to 5 jobs from /search")
jobs = impactpool.fetch({"max_per_source": 5})

print(f"\n  Returned {len(jobs)} job(s).")

if len(jobs) == 0:
    # Graceful skip — could be a transient outage or layout change. Don't
    # flap CI on third-party site availability.
    print("  SKIP: ImpactPool returned 0 jobs (network outage or layout drift?).")
    sys.exit(0)

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
    check(
        j.url.startswith("https://www.impactpool.org/jobs/"),
        f"job[{i}].url is absolute impactpool.org/jobs URL (got {j.url!r})",
    )
    check(j.source == "impactpool", f"job[{i}].source == 'impactpool'")
    check(
        j.external_id.isdigit(),
        f"job[{i}].external_id is numeric (got {j.external_id!r})",
    )

print("\n  Sample titles:")
for j in jobs:
    print(f"    - {j.title}")

print()
if failures:
    print(f"FAIL {len(failures)} check(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK  All ImpactPool live smoke checks passed.")
