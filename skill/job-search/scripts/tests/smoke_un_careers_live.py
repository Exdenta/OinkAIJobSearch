#!/usr/bin/env python3
"""Live smoke test for the ``un_careers`` source adapter.

Runs the real adapter against the real ``claude`` CLI (or its absence) and
asserts only that the call DOES NOT RAISE and returns a list. Everything else
— number of jobs, freshness, formatting — is informational.

The adapter delegates to the Claude CLI which delegates to WebFetch. Any of
those layers can legitimately yield zero results on a given day (CLI not on
PATH in CI, WebFetch blocked by CloudFront, the page genuinely empty during a
deploy window). Treating those as failures would make the test flake on
infrastructure conditions we can't control, so the contract here is the
narrowest one that still proves wiring: ``fetch`` returns a ``list[Job]`` for
the given filter shape.

Invoke:

    cd <worktree-root>
    python3 skill/job-search/scripts/tests/smoke_un_careers_live.py

Exit code: 0 = wiring OK; 1 = exception escaped fetch().
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from sources import un_careers  # noqa: E402
from dedupe import Job  # noqa: E402


def main() -> int:
    filters = {
        "max_per_source": 5,
        "ai_scrape_timeout_s": 180,
    }

    print("[un_careers smoke] calling fetch(filters=%r)..." % filters, flush=True)
    try:
        jobs = un_careers.fetch(filters)
    except Exception:
        print("[un_careers smoke] FAIL — fetch() raised:", file=sys.stderr)
        traceback.print_exc()
        return 1

    if not isinstance(jobs, list):
        print("[un_careers smoke] FAIL — fetch() returned %s (not a list)"
              % type(jobs).__name__, file=sys.stderr)
        return 1

    print("[un_careers smoke] OK — fetch() returned %d Job(s)" % len(jobs))

    if not jobs:
        # Zero results is an allowed outcome. We surface diagnostics so a human
        # can tell whether the CLI was missing, the page was blocked, or the
        # model genuinely came back empty — but we do NOT fail the test.
        print("[un_careers smoke] note: zero results — see warnings above for"
              " whether the CLI was missing, WebFetch was blocked, or the LLM"
              " returned {\"jobs\": []} on purpose.")
        return 0

    # Sample preview. Only print up to 5 to stay readable.
    for i, j in enumerate(jobs[:5], 1):
        if not isinstance(j, Job):
            print("[un_careers smoke] FAIL — element %d is %s, expected Job"
                  % (i, type(j).__name__), file=sys.stderr)
            return 1
        title = (j.title or "").strip() or "<no title>"
        loc = (j.location or "").strip() or "<no location>"
        company = (j.company or "").strip() or "<no company>"
        print("  %d. %s — %s (%s)" % (i, title, company, loc))
        print("     url: %s" % (j.url or "<no url>"))
        if j.posted_at:
            print("     posted_at: %s" % j.posted_at)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
