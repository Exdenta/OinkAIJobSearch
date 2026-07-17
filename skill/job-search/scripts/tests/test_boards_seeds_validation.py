#!/usr/bin/env python3
"""Coverage for search_seeds.boards.keywords — the 2026-07-02 addition that
feeds per-user queries into the Apify query-array/single-keyword actors.

Regression this guards against: `boards` was only added to
`seeds_schema_validate` (the dead v4 seeds-only path) on the first pass;
`profile_schema_validate` (the LIVE v2/v3 validator every real profile goes
through) was missed. That bug would silently accept `boards` in v4 test
fixtures while rejecting it (or just never validating it) for real profiles.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import profile_builder as pb  # noqa: E402
from profile_builder import profile_schema_validate  # noqa: E402


def _v3_profile(boards=None) -> dict:
    seeds = {
        "linkedin": {"queries": [{"q": "frontend engineer", "geo": "Spain", "f_TPR": "r86400"}]},
        "web_search": {"seed_phrases": ["frontend europe"], "ats_domains": ["greenhouse.io"],
                        "focus_notes": "EU TZ only."},
    }
    if boards is not None:
        seeds["boards"] = boards
    return {
        "schema_version": 3,
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
        "onsite_locations": ["bilbao"],
        "remote_regions": ["spain", "europe"],
        "remote": "any",
        "time_zone_band": "UTC-1..UTC+3",
        "salary_min_usd": 0,
        "drop_if_salary_unknown": False,
        "language": "english",
        "max_age_hours": 0,
        "min_match_score": 0,
        "search_seeds": seeds,
        "free_text": "",
    }


def test_v3_profile_without_boards_still_validates_clean():
    """Back-compat: profiles built before 2026-07-02 have no boards block."""
    errs = profile_schema_validate(_v3_profile(boards=None))
    assert errs == [], f"boards-less v3 profile should validate clean (got {errs})"


def test_v3_profile_with_valid_boards_validates_clean():
    errs = profile_schema_validate(_v3_profile(boards={"keywords": ["react", "frontend"]}))
    assert errs == [], f"valid boards block should validate clean (got {errs})"


def test_v3_profile_rejects_too_many_board_keywords():
    kws = [f"kw{i}" for i in range(pb._MAX_BOARD_KEYWORDS + 1)]
    errs = profile_schema_validate(_v3_profile(boards={"keywords": kws}))
    assert any("boards.keywords" in e for e in errs), errs


def test_v3_profile_rejects_long_board_keyword():
    long_kw = "x" * (pb._MAX_BOARD_KEYWORD + 1)
    errs = profile_schema_validate(_v3_profile(boards={"keywords": [long_kw]}))
    assert any("boards.keywords" in e for e in errs), errs


def test_v3_profile_rejects_non_dict_boards():
    errs = profile_schema_validate(_v3_profile(boards="not a dict"))
    assert any("search_seeds.boards must be an object" in e for e in errs), errs


def test_v3_profile_rejects_non_string_list_keywords():
    errs = profile_schema_validate(_v3_profile(boards={"keywords": [1, 2]}))
    assert any("boards.keywords must be a list of strings" in e for e in errs), errs


def test_clip_profile_trims_excess_board_keywords():
    kws = [f"kw{i}" for i in range(pb._MAX_BOARD_KEYWORDS + 3)]
    clipped = pb._clip_profile(_v3_profile(boards={"keywords": kws}))
    assert len(clipped["search_seeds"]["boards"]["keywords"]) == pb._MAX_BOARD_KEYWORDS


def test_single_keyword_rollout_matches_probed_sources():
    """Locks the 2026-07-02 --query-ab probe verdict (chat 433775883):
    wttj + infojobs showed clean relevance gains and are ON; the NGO/academic
    single-keyword sources were only tested off-domain (frontend keywords on
    an NGO board) and stay OFF pending a probe with a matching-domain
    profile. If this test needs updating, a new probe backs the change.
    """
    import defaults
    assert set(defaults.DEFAULTS["apify_query_sources"]) == {"wttj", "infojobs"}
    assert set(defaults.DEFAULTS["apify_query_sources"]) <= set(
        __import__("apify_fetch").QUERY_SINGLE_PARAM
    )


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
