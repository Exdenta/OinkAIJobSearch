#!/usr/bin/env python3
"""Live smoke test for the EURES source adapter.

EURES is fetched via the public, unauthenticated Job-Vacancy-Search REST
API (`/eures/api/jv-searchengine/public/jv-search/search`) — no EU Login
required (the earlier ECAS-gated probe tested the wrong endpoint). We
expect real rows back and assert the canonical Job fields.

Network is required. A hard network error / portal outage degrades to a
graceful SKIP (exit 0) rather than failing CI.

Run from worktree root:

    python3 skill/job-search/scripts/tests/smoke_eures_live.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import eures  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("EURES live smoke — fetching up to 6 jobs via the public jv-search API")
jobs = eures.fetch({
    "max_per_source": 6,
    "eures_keywords": ["software engineer", "frontend developer"],
})

print(f"\n  Returned {len(jobs)} job(s).")

if not jobs:
    # The public API is anonymous and normally returns rows; 0 means the
    # portal is degraded or the network is unavailable. Don't fail CI on a
    # transient outage, but make it loud.
    print("\nSKIP  EURES returned 0 jobs — portal degraded or network "
          "unavailable. Check forensic log for the per-keyword status codes.")
    sys.exit(0)

check(len(jobs) <= 6, f"fetch respected cap (got {len(jobs)})")
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
    check(j.source == "eures", f"job[{i}].source == 'eures'")
    check(
        j.url.startswith("https://europa.eu/eures/portal/jv-se/jv-details/"),
        f"job[{i}].url is a EURES jv-details URL (got {j.url!r})",
    )
    # posted_at should be an ISO date (YYYY-MM-DD), never raw epoch ms.
    check(
        (not j.posted_at) or (len(j.posted_at) == 10 and j.posted_at[4] == "-"),
        f"job[{i}].posted_at is ISO date or empty (got {j.posted_at!r})",
    )

# Dedup sanity: no two jobs share an id.
ids = [j.external_id for j in jobs]
check(len(set(ids)) == len(ids), "fetch deduplicates vacancy ids")

print("\n  Sample titles:")
for j in jobs:
    print(f"    - {j.title}")

print()
if failures:
    print(f"FAIL {len(failures)} check(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK  All EURES live smoke checks passed.")
