#!/usr/bin/env python3
"""Offline smoke test for P6-T2 — expanded LinkedIn query seeds (5 → 10).

Validates the user-facing contract of the bump:

  1. The operator-wide constant `_MAX_LINKEDIN_QUERIES` is the integer 10.
  2. The schema validator accepts a profile carrying exactly 10 queries.
  3. The schema validator rejects 11 with an error that names the new cap.
  4. The schema validator still accepts a legacy 5-query profile (back-
     compat: existing user profiles built before 2026-05-23 must NOT need
     a rebuild to keep validating).
  5. `linkedin.fetch_for_user` iterates ALL 10 queries when the HTTP layer
     is monkey-patched — i.e. the artificial `MAX_USER_QUERIES` cap that
     used to truncate to 5 was lifted.

No network, no Claude CLI. Exits non-zero on any assertion failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import profile_builder as pb  # noqa: E402
from profile_builder import profile_schema_validate  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


# ---------------------------------------------------------------------------
# Shared fixture — minimal valid v2 profile we can mutate per-test.
# ---------------------------------------------------------------------------

def _profile_with_queries(n: int) -> dict:
    """Synthetic v2 profile with `n` paired LinkedIn queries."""
    return {
        "schema_version": 2,
        "ideal_fit_paragraph": "Mid-level frontend engineer.",
        "primary_role": "frontend engineer",
        "target_levels": ["mid", "middle"],
        "years_experience": 5,
        "stack_primary": ["vue", "typescript"],
        "stack_secondary": ["react"],
        "stack_adjacent": ["node"],
        "stack_antipatterns": ["wordpress"],
        "title_must_match": ["frontend", "vue", "react"],
        "title_exclude": ["senior", "staff"],
        "exclude_keywords": ["wordpress"],
        "exclude_companies": [],
        "locations": ["spain", "europe"],
        "remote": "any",
        "time_zone_band": "UTC-1..UTC+3",
        "salary_min_usd": 0,
        "drop_if_salary_unknown": False,
        "language": "english",
        "max_age_hours": 0,
        "min_match_score": 0,
        "search_seeds": {
            "linkedin": {
                "queries": [
                    {"q": f"frontend variant {i}", "geo": "Spain", "f_TPR": "r86400"}
                    for i in range(n)
                ],
            },
            "web_search": {
                "seed_phrases": ["frontend europe"],
                "ats_domains": ["greenhouse.io"],
                "focus_notes": "EU TZ only.",
            },
        },
        "free_text": "",
    }


# ---------------------------------------------------------------------------
# 1. Constant is 10.
# ---------------------------------------------------------------------------
print("1. _MAX_LINKEDIN_QUERIES constant")
check(
    pb._MAX_LINKEDIN_QUERIES == 10,
    f"_MAX_LINKEDIN_QUERIES == 10 (got {pb._MAX_LINKEDIN_QUERIES!r})",
)


# ---------------------------------------------------------------------------
# 2. Validator accepts exactly 10 queries.
# ---------------------------------------------------------------------------
print("\n2. validator accepts 10 queries")
errs = profile_schema_validate(_profile_with_queries(10))
check(
    errs == [],
    f"10-query profile validates clean (got errs={errs})",
)


# ---------------------------------------------------------------------------
# 3. Validator rejects 11 with an error that mentions the new cap.
# ---------------------------------------------------------------------------
print("\n3. validator rejects 11 queries")
errs = profile_schema_validate(_profile_with_queries(11))
check(
    any("linkedin.queries length" in e for e in errs),
    f"11-query profile surfaces 'linkedin.queries length' error (got {errs})",
)
# And the error must NAME the new cap so future readers see the number.
check(
    any("10" in e for e in errs if "linkedin.queries length" in e),
    f"error mentions the new cap of 10 (got {errs})",
)


# ---------------------------------------------------------------------------
# 4. Back-compat: legacy 5-query profile still validates.
# ---------------------------------------------------------------------------
print("\n4. legacy 5-query profile still validates (back-compat)")
errs = profile_schema_validate(_profile_with_queries(5))
check(
    errs == [],
    f"5-query profile (pre-P6-T2) validates clean (got errs={errs})",
)


# ---------------------------------------------------------------------------
# 5. linkedin.fetch_for_user iterates ALL 10 queries — no artificial cap.
# ---------------------------------------------------------------------------
print("\n5. linkedin.fetch_for_user dispatches all 10 queries")

from sources import linkedin as li  # noqa: E402
from dedupe import Job  # noqa: E402

# Per-user 10-query profile in the PAIRED v2.0 shape — that's the shape
# whose dispatch count was previously capped at MAX_USER_QUERIES=5.
user_seeds = {
    "queries": [
        {"q": f"q{i}", "geo": "Spain", "f_TPR": "r86400"}
        for i in range(10)
    ]
}

# Capture every (q, geo) the adapter would have hit.
seen: list[tuple[str, str]] = []


def _stub_one_search(*, q, geo, f_TPR, remote, cap_remaining, filters,
                    seen_urls, start=0):
    """Return one fake Job per (q, geo) page-0 call, then 0 on subsequent
    pages so the runner moves on. Records the (q, geo) pair so we can
    assert all 10 queries were dispatched."""
    if start == 0:
        seen.append((q, geo))
        url = f"https://linkedin.example/jobs/{q}"
        if url in seen_urls:
            return [], True
        seen_urls.add(url)
        return [Job(
            source="linkedin",
            external_id=url,
            title=f"Job for {q}",
            company="Example",
            location=geo,
            url=url,
            posted_at="",
            snippet="",
        )], True
    return [], True


def _stub_fetch_detail_bodies(jobs):
    """Skip body-fetch in the test (avoids HTTP / sleeps)."""
    return jobs


orig_one_search = li._one_search
orig_fetch_bodies = li._fetch_detail_bodies
orig_pace = li.PACE_SECONDS
li._one_search = _stub_one_search
li._fetch_detail_bodies = _stub_fetch_detail_bodies
li.PACE_SECONDS = 0.0  # don't sleep between requests in the test

try:
    out = li.fetch_for_user({"remote": "any"}, user_seeds)
finally:
    li._one_search = orig_one_search
    li._fetch_detail_bodies = orig_fetch_bodies
    li.PACE_SECONDS = orig_pace

check(
    len(seen) == 10,
    f"all 10 queries dispatched (got {len(seen)}: {[q for q, _ in seen]})",
)
check(
    {q for q, _ in seen} == {f"q{i}" for i in range(10)},
    f"every query string reached _one_search exactly once (got {sorted(q for q, _ in seen)})",
)
check(
    len(out) == 10,
    f"10 jobs collected back from the adapter (got {len(out)})",
)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if failures:
    print(f"❌ {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ All test_expand_linkedin_seeds checks passed.")
