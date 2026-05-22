#!/usr/bin/env python3
"""Pytest coverage for P6-T2 — expanded LinkedIn query seeds (5 → 10).

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

No network, no Claude CLI. Pytest-collectable: each invariant is a
standalone `test_*` function so `pytest -v` discovers it.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import profile_builder as pb  # noqa: E402
from profile_builder import profile_schema_validate  # noqa: E402


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

def test_max_queries_constant_is_10():
    """`profile_builder._MAX_LINKEDIN_QUERIES` must be the integer 10."""
    assert pb._MAX_LINKEDIN_QUERIES == 10, (
        f"_MAX_LINKEDIN_QUERIES != 10 (got {pb._MAX_LINKEDIN_QUERIES!r})"
    )


# ---------------------------------------------------------------------------
# 2. Validator accepts exactly 10 queries.
# ---------------------------------------------------------------------------

def test_validator_accepts_10_queries():
    """A v2 profile with exactly 10 LinkedIn queries must validate clean."""
    errs = profile_schema_validate(_profile_with_queries(10))
    assert errs == [], f"10-query profile should validate clean (got errs={errs})"


# ---------------------------------------------------------------------------
# 3. Validator rejects 11 with an error that mentions the new cap.
# ---------------------------------------------------------------------------

def test_validator_rejects_11_queries():
    """11-query profile must surface a `linkedin.queries length` error."""
    errs = profile_schema_validate(_profile_with_queries(11))
    assert any("linkedin.queries length" in e for e in errs), (
        f"11-query profile should surface 'linkedin.queries length' error (got {errs})"
    )


def test_validator_error_names_new_cap_of_10():
    """The rejection error must name the new cap of 10 so future readers
    see the number in the message — not just a generic 'too long'."""
    errs = profile_schema_validate(_profile_with_queries(11))
    assert any("10" in e for e in errs if "linkedin.queries length" in e), (
        f"error should mention the new cap of 10 (got {errs})"
    )


# ---------------------------------------------------------------------------
# 4. Back-compat: legacy 5-query profile still validates.
# ---------------------------------------------------------------------------

def test_validator_accepts_legacy_5_query_profile():
    """A legacy v2 profile built before P6-T2 (5 queries) must still
    validate clean — existing users should NOT need a rebuild."""
    errs = profile_schema_validate(_profile_with_queries(5))
    assert errs == [], (
        f"5-query (pre-P6-T2) profile should validate clean (got errs={errs})"
    )


# ---------------------------------------------------------------------------
# 5. linkedin.fetch_for_user iterates ALL 10 queries — no artificial cap.
#
# These three tests share a fixture that stubs the HTTP layer of the
# linkedin adapter, runs `fetch_for_user` against a 10-query paired
# profile once, and exposes the captured (q, geo) pairs + the returned
# Job list. Each test asserts ONE invariant about that single run.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.fixture
def linkedin_10_query_dispatch(monkeypatch):
    """Run `linkedin.fetch_for_user` once with a 10-query paired profile
    against a stubbed HTTP layer. Returns (seen_pairs, returned_jobs).

    Stubs:
      * `_one_search` — records each (q, geo) and returns one fake Job per
        page-0 call.
      * `_fetch_detail_bodies` — passthrough, no body-fetch HTTP.
      * `PACE_SECONDS` — 0, so the adapter doesn't sleep.
    """
    from sources import linkedin as li
    from dedupe import Job

    user_seeds = {
        "queries": [
            {"q": f"q{i}", "geo": "Spain", "f_TPR": "r86400"}
            for i in range(10)
        ]
    }

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

    monkeypatch.setattr(li, "_one_search", _stub_one_search)
    monkeypatch.setattr(li, "_fetch_detail_bodies", _stub_fetch_detail_bodies)
    monkeypatch.setattr(li, "PACE_SECONDS", 0.0)

    out = li.fetch_for_user({"remote": "any"}, user_seeds)
    return seen, out


def test_linkedin_adapter_dispatches_all_10_queries(linkedin_10_query_dispatch):
    """Adapter must call `_one_search` once per query (no MAX_USER_QUERIES=5
    truncation)."""
    seen, _ = linkedin_10_query_dispatch
    assert len(seen) == 10, (
        f"all 10 queries should dispatch (got {len(seen)}: {[q for q, _ in seen]})"
    )


def test_linkedin_adapter_dispatches_every_query_string(linkedin_10_query_dispatch):
    """Every distinct query string `q0..q9` must reach `_one_search` exactly
    once — no silent skipping or dedupe-collapse mid-loop."""
    seen, _ = linkedin_10_query_dispatch
    assert {q for q, _ in seen} == {f"q{i}" for i in range(10)}, (
        f"every query string should reach _one_search exactly once "
        f"(got {sorted(q for q, _ in seen)})"
    )


def test_linkedin_adapter_returns_10_jobs(linkedin_10_query_dispatch):
    """The 10 dispatched queries (each yielding one fake Job) must produce
    a 10-Job return list — no late truncation in the merge / dedupe path."""
    _, out = linkedin_10_query_dispatch
    assert len(out) == 10, (
        f"10 jobs should be collected back from the adapter (got {len(out)})"
    )
