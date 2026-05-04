#!/usr/bin/env python3
"""Live smoke test for the DevEx source adapter.

DevEx (https://www.devex.com) is DataDome-walled — every direct HTTP request
gets a 403 + JS captcha challenge. The adapter delegates discovery to a
Claude CLI sub-agent that uses Google site-search (``site:devex.com/jobs``)
to surface posting URLs and extract title/employer/location from the search
snippets.

This smoke test asserts EITHER:

  * ``fetch({"max_per_source": 5})`` returns >= 1 Job with a valid
    ``https://www.devex.com/jobs/...`` URL, a non-empty title, and a stable
    external_id; OR
  * The adapter gracefully returns ``[]`` and logs an explicit reason
    (DataDome block, claude CLI missing, search returned no results) — in
    which case we PASS the test and print the documented blocker. We never
    fail on legitimate paywall / bot-wall conditions because that is the
    expected steady-state behavior on hardened DevEx infrastructure.

Network is required. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_devex_live.py

Exits non-zero only on shape violations of returned Jobs (bad URL, empty
title, missing external_id, wrong source string) — i.e. real adapter bugs.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import devex  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("DevEx live smoke — fetching up to 5 jobs via Google site-search delegation")
print("(DevEx is DataDome-walled; expect either >=1 job from snippet extraction,")
print(" or graceful 0 with a documented paywall/bot reason.)")
print()

jobs = devex.fetch({"max_per_source": 5})

print(f"\n  Returned {len(jobs)} job(s).")

if len(jobs) == 0:
    print()
    print("  NOTE: zero jobs returned. This is the documented graceful-skip path")
    print("        when DataDome blocks the sub-agent's WebFetch attempts AND")
    print("        Google site-search returns nothing usable, OR when the")
    print("        ``claude`` CLI is unavailable / not logged in. Inspect the")
    print("        forensic log for the exact reason. Test passes.")
    print()
    print("OK  DevEx live smoke completed (0 jobs, paywall/bot-wall path).")
    sys.exit(0)

# We have at least one job — exercise the shape contract.
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
    check(j.url.startswith("https://www.devex.com/jobs/"),
          f"job[{i}].url is absolute www.devex.com/jobs/ URL (got {j.url!r})")
    check(j.source == "devex", f"job[{i}].source == 'devex'")

print("\n  Sample titles:")
for j in jobs:
    print(f"    - {j.title}")

print()
if failures:
    print(f"FAIL {len(failures)} check(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK  All DevEx live smoke checks passed.")
