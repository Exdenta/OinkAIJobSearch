#!/usr/bin/env python3
"""Live smoke test for the Ikerbasque source adapter.

Hits https://www.ikerbasque.net/en/calls. Asserts:

  * fetch({"max_per_source": 12}) returns 0 or more Jobs without raising.
  * If any Jobs are returned: each has non-empty title, url, external_id;
    source == "ikerbasque"; URL is absolute and points to ikerbasque.net;
    company == "Ikerbasque"; location contains "Bilbao".
  * If 0 Jobs (all calls closed and `ikerbasque_include_closed` not set) we
    PASS — Ikerbasque is a small foundation and may legitimately have no
    open calls on a given day.
  * Also re-runs with `ikerbasque_include_closed=True` and asserts that
    the resulting count is >= the open-only count, and is itself >= 1
    (the listing always retains historical Closed calls).

Network is required. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_ikerbasque_live.py

Exits non-zero on any failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import ikerbasque  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("Ikerbasque live smoke — fetching open calls (cap=12)")
jobs = ikerbasque.fetch({"max_per_source": 12})
print(f"\n  Returned {len(jobs)} open-call job(s).")

check(len(jobs) <= 12, f"fetch respected cap (got {len(jobs)})")

if not jobs:
    print("  NOTE: no Open calls today — treating as PASS for the open-only path.")

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
    check(j.url.startswith("https://www.ikerbasque.net/"),
          f"job[{i}].url is absolute ikerbasque.net URL (got {j.url!r})")
    check(j.source == "ikerbasque", f"job[{i}].source == 'ikerbasque'")
    check(j.company == "Ikerbasque", f"job[{i}].company == 'Ikerbasque'")
    check("Bilbao" in j.location, f"job[{i}].location contains 'Bilbao' (got {j.location!r})")

# Now exercise include_closed branch — listing should always have at least
# one historical entry, so we expect a non-empty result here.
print("\nIkerbasque live smoke — fetching with ikerbasque_include_closed=True")
all_jobs = ikerbasque.fetch({"max_per_source": 12, "ikerbasque_include_closed": True})
print(f"  Returned {len(all_jobs)} job(s) (open + closed).")

check(len(all_jobs) >= 1,
      "include_closed=True yields at least 1 job (listing is never truly empty)")
check(len(all_jobs) >= len(jobs),
      f"include_closed >= open-only (got {len(all_jobs)} vs {len(jobs)})")

print("\n  Sample titles (open + closed):")
for j in all_jobs[:5]:
    print(f"    - {j.title}")

print()
if failures:
    print(f"FAIL {len(failures)} check(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK  All Ikerbasque live smoke checks passed.")
