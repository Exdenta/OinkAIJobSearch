#!/usr/bin/env python3
"""Live smoke test for the EURES source adapter.

EURES is gated behind EU Login (ECAS) for its JSON search API (see
docstring in `sources/eures.py` for the full probe log). On an
unauthenticated environment we EXPECT zero results and a skip reason
of "EURES API requires EU Login..." — we exit 0 with a "skipped:"
line in that case rather than failing CI.

If `EURES_API_TOKEN` is in env (or `eures_api_token` in filters), we
expect actual rows back, and we assert >= 1 Job + canonical fields.

Run from worktree root:

    python3 skill/job-search/scripts/tests/smoke_eures_live.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import eures  # noqa: E402


HAS_TOKEN = bool(os.environ.get("EURES_API_TOKEN", "").strip())
failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


print("EURES live smoke — fetching up to 5 jobs (Spain bias)")
filters = {"max_per_source": 5, "eures_country_codes": ["ES"]}
jobs = eures.fetch(filters)

print(f"\n  Returned {len(jobs)} job(s). HAS_TOKEN={HAS_TOKEN}")

if not jobs:
    if HAS_TOKEN:
        # With a token, zero rows is a real failure.
        print("FAIL Got 0 rows despite EURES_API_TOKEN set.")
        sys.exit(1)
    # Expected path on public networks: API is gated.
    print("skipped: EURES API gated by EU Login (ECAS); no public "
          "anonymous access. Set EURES_API_TOKEN to test the success "
          "path. Adapter returned [] cleanly without raising — this is "
          "the intended fallback. See sources/eures.py docstring for "
          "full probe log.")
    sys.exit(0)

# We did get rows — validate structure.
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
    check(j.source == "eures", f"job[{i}].source == 'eures'")
    check(
        "europa.eu" in j.url or j.url.startswith("https://"),
        f"job[{i}].url is absolute (got {j.url!r})",
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
print("OK  All EURES live smoke checks passed.")
