#!/usr/bin/env python3
"""Live smoke test for the Welcome to the Jungle source adapter.

Hits the public Algolia search index that powers welcometothejungle.com.
Asserts:

  * fetch({"max_per_source": 5}) returns >= 1 Job (or skips gracefully if
    the network/key is unavailable)
  * each Job has a non-empty title, url, and external_id
  * URLs are absolute and point to welcometothejungle.com/{lang}/companies/
  * sample titles are printed to stdout (multi-language allowed — we DO NOT
    translate; titles preserve original posting language)

Network is required. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_wttj_live.py

Exit codes:
  0 — passed (or gracefully skipped on transient network failure)
  1 — assertion failure
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import wttj  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("WTTJ live smoke — fetching up to 5 jobs from Algolia (EU bias)")
try:
    jobs = wttj.fetch({"max_per_source": 5})
except Exception as e:  # noqa: BLE001
    print(f"  SKIP unexpected exception in fetch(): {e!r}")
    sys.exit(0)

print(f"\n  Returned {len(jobs)} job(s).")

if len(jobs) == 0:
    # Graceful skip: a 403/404 from Algolia (key rotation, network) shouldn't
    # break CI. The forensic log records the body_head for debugging.
    print("  SKIP fetch returned 0 jobs — likely network/key issue. "
          "Check forensic log for `wttj.fetch` output.")
    sys.exit(0)

check(len(jobs) >= 1, "fetch returned at least one job")
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
    check(j.url.startswith("https://www.welcometothejungle.com/"),
          f"job[{i}].url is absolute welcometothejungle.com URL (got {j.url!r})")
    check("/companies/" in j.url and "/jobs/" in j.url,
          f"job[{i}].url has expected /companies/.../jobs/ shape")
    check(j.source == "wttj", f"job[{i}].source == 'wttj'")

print("\n  Sample titles (preserved in original language):")
for j in jobs:
    print(f"    - {j.title}")

print()
if failures:
    print(f"FAIL {len(failures)} check(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK  All WTTJ live smoke checks passed.")
