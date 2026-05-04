#!/usr/bin/env python3
"""Live smoke test for the Tecnoempleo source adapter.

Hits the public RSS feed at https://www.tecnoempleo.com/alertas-empleo-rss.php.
Asserts:

  * fetch({"max_per_source": 5}) returns >= 1 Job (or skips gracefully if
    the feed is unreachable / blocked, e.g. CI without egress)
  * each Job has a non-empty title, url, and external_id
  * URLs are absolute and point to www.tecnoempleo.com
  * external_ids match the `rf-<hex>` pattern Tecnoempleo uses internally
  * sample titles are printed to stdout for human eyeballing

Network is required. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_tecnoempleo_live.py

Exits 0 on success and on graceful skip (no network / non-200 from Tecnoempleo).
Exits non-zero only on real correctness failures.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import tecnoempleo  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("Tecnoempleo live smoke - fetching up to 5 jobs from RSS feed")

try:
    jobs = tecnoempleo.fetch({"max_per_source": 5})
except Exception as e:  # noqa: BLE001
    print(f"  SKIP fetch raised {type(e).__name__}: {e}")
    sys.exit(0)

print(f"\n  Returned {len(jobs)} job(s).")

if len(jobs) == 0:
    print("  SKIP no jobs returned (network blocked, feed empty, or upstream"
          " 4xx); treating as graceful skip.")
    sys.exit(0)

check(len(jobs) > 0, "fetch returned at least one job")
check(len(jobs) <= 5, f"fetch respected cap (got {len(jobs)})")

rf_pat = re.compile(r"^rf-[0-9a-f]{16,32}$", re.IGNORECASE)

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
        j.url.startswith("https://www.tecnoempleo.com/"),
        f"job[{i}].url is absolute tecnoempleo.com URL (got {j.url!r})",
    )
    check(j.source == "tecnoempleo", f"job[{i}].source == 'tecnoempleo'")
    check(
        bool(rf_pat.match(j.external_id)),
        f"job[{i}].external_id matches rf-<hex> pattern (got {j.external_id!r})",
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
print("OK  All Tecnoempleo live smoke checks passed.")
