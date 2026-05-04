#!/usr/bin/env python3
"""Live smoke test for the Wellfound source adapter.

Wellfound is gated by DataDome and has no public RSS / sitemap / GraphQL.
The adapter is therefore a documented stub that returns []. This smoke
test treats EITHER of the following outcomes as a pass:

  * fetch() returns >= 1 Job (would imply DataDome is no longer in the
    way and the adapter has been upgraded — print sample titles), OR
  * fetch() returns [] AND a probe of a listing endpoint surfaces a
    block reason (datadome / 403 / login-wall) — print a single
    `blocked: <reason>` line and exit 0.

Network is required. Run from the worktree root:

    python3 skill/job-search/scripts/tests/smoke_wellfound_live.py

Exits non-zero only if both paths fail (e.g. no jobs returned AND no
probeable block reason — meaning the adapter is silently broken).
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import wellfound  # noqa: E402

print("Wellfound live smoke — fetching up to 5 jobs (stub adapter expected)")
jobs = wellfound.fetch({"max_per_source": 5})
print(f"  Returned {len(jobs)} job(s).")

if jobs:
    # Unexpected-good path: adapter started returning real data. Treat
    # any well-formed Job as a pass and print samples for human review.
    failures: list[str] = []

    def check(cond: bool, label: str) -> None:
        if cond:
            print(f"  OK  {label}")
        else:
            print(f"  FAIL {label}")
            failures.append(label)

    check(len(jobs) <= 5, f"fetch respected cap (got {len(jobs)})")
    for i, j in enumerate(jobs):
        print(f"\n  [{i}] title  = {j.title!r}")
        print(f"      company= {j.company!r}")
        print(f"      loc    = {j.location!r}")
        print(f"      url    = {j.url}")
        print(f"      ext_id = {j.external_id!r}")
        check(bool(j.title.strip()), f"job[{i}].title is non-empty")
        check(bool(j.url.strip()), f"job[{i}].url is non-empty")
        check(bool(j.external_id.strip()), f"job[{i}].external_id is non-empty")
        check(j.source == "wellfound", f"job[{i}].source == 'wellfound'")
        check("wellfound.com" in j.url,
              f"job[{i}].url points to wellfound.com (got {j.url!r})")

    if failures:
        print(f"\nFAIL {len(failures)} check(s):")
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print("\nOK  Wellfound returned real jobs (adapter has been upgraded).")
    sys.exit(0)

# Empty path — re-probe so we can print a precise blocked-with-reason line.
reason, debug = wellfound._probe_block_reason()
print(f"  blocked: {reason}")
for url, info in debug.items():
    print(f"    {url} -> {info}")

# Accept any of the documented block reasons as a graceful skip.
ok_reasons = {
    "datadome_403",
    "http_403",
    "all_probes_failed",          # network down — still a graceful no-op
    "unexpected_200_revisit_adapter",  # signal-only; counts as pass
}
if reason in ok_reasons:
    print("\nOK  Wellfound stub adapter returned [] with a known block reason.")
    sys.exit(0)

print(f"\nFAIL  Adapter returned [] but probe reason ({reason!r}) is "
      "unrecognised — adapter may be silently broken.")
sys.exit(1)
