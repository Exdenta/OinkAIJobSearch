#!/usr/bin/env python3
"""Live smoke test for the Built In source adapter.

Hits the public category pages on builtin.com (HTML scrape; Built In does
not publish a real RSS feed). Asserts:

  * fetch({"max_per_source": 5}) returns >= 1 Job, OR — if Built In is
    unreachable / Cloudflare-challenged from this network — exits 0 with a
    skip message. We don't fail CI for an external dependency outage.
  * each Job has non-empty title, url, company, external_id
  * URLs are absolute and point to builtin.com
  * external_ids are numeric Built In job ids
  * sample titles printed for human eyeballing

Network is required. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_builtin_live.py

Exits non-zero on any *assertion* failure (not on transient network).
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import builtin  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("Built In live smoke — fetching up to 5 jobs from /jobs/remote/* category pages")
jobs = builtin.fetch({"max_per_source": 5})

print(f"\n  Returned {len(jobs)} job(s).")

if len(jobs) == 0:
    # Treat as a skip rather than a hard fail: Built In sits behind
    # Cloudflare and may bot-challenge from CI / unfamiliar egress IPs.
    # The forensic log will record the page_status for diagnosis.
    print("\n  SKIP  Built In returned 0 jobs — likely Cloudflare challenge or "
          "transient outage. Not failing.")
    sys.exit(0)

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
    check(j.url.startswith("https://builtin.com/"),
          f"job[{i}].url is absolute builtin.com URL (got {j.url!r})")
    check(j.source == "builtin", f"job[{i}].source == 'builtin'")
    check(j.external_id.isdigit(),
          f"job[{i}].external_id is numeric (got {j.external_id!r})")
    check(bool(j.company.strip()), f"job[{i}].company is non-empty")

print("\n  Sample titles:")
for j in jobs:
    print(f"    - {j.title}")

print()
if failures:
    print(f"FAIL {len(failures)} check(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK  All Built In live smoke checks passed.")
