"""Cross-source pre-enrichment dedupe (algorithm v2.8, P4 pipeline overhaul).

`dedupe_cross_source` collapses identical postings that appear across
multiple feeds before the LLM scorer runs. Operates at the transport
layer — normalisation rules are closed-set facts about how feeds format
the same posting, not heuristics about job fit. See the
`dedupe.dedupe_cross_source` docstring for the full design-principle
justification.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from dedupe import Job, dedupe_cross_source  # noqa: E402


def _job(
    *,
    source: str,
    ext: str,
    title: str = "Frontend Developer",
    company: str = "Acme",
    location: str = "Remote",
    snippet: str = "",
) -> Job:
    return Job(
        source=source,
        external_id=ext,
        title=title,
        company=company,
        location=location,
        url=f"https://example.com/{source}/{ext}",
        posted_at="2026-05-20",
        snippet=snippet,
    )


def test_empty_input_returns_empty():
    assert dedupe_cross_source([]) == []


def test_collapses_identical_company_title_location():
    """Three feeds report the same role — keep the row with the longest
    snippet, drop the other two."""
    jobs = [
        _job(source="justjoinit",  ext="j1", snippet="short"),
        _job(source="nofluffjobs", ext="n1", snippet="x" * 200),  # longest
        _job(source="linkedin",    ext="l1", snippet="medium-length"),
    ]
    out = dedupe_cross_source(jobs)
    assert len(out) == 1
    assert out[0].source == "nofluffjobs"
    assert out[0].external_id == "n1"


def test_preserves_distinct_jobs():
    """Three distinct titles — all three survive, in input order."""
    jobs = [
        _job(source="justjoinit",  ext="a", title="Frontend Developer"),
        _job(source="nofluffjobs", ext="b", title="Backend Developer"),
        _job(source="linkedin",    ext="c", title="Staff Engineer"),
    ]
    out = dedupe_cross_source(jobs)
    assert len(out) == 3
    assert [j.external_id for j in out] == ["a", "b", "c"]


def test_normalization_strips_parenthetical():
    """"Frontend Developer (Remote)" and "Frontend Developer" must
    collapse — the parenthesised suffix is an ATS-feed artifact, not a
    different role."""
    jobs = [
        _job(source="justjoinit",  ext="p", title="Frontend Developer (Remote)", snippet="aa"),
        _job(source="nofluffjobs", ext="q", title="Frontend Developer", snippet="b"),
    ]
    out = dedupe_cross_source(jobs)
    assert len(out) == 1
    # Longest snippet wins (the (Remote)-suffixed one).
    assert out[0].external_id == "p"


def test_normalization_strips_multiple_parenthetical_suffixes():
    """A title with multiple trailing parens (gendered hiring tags +
    location) must still collapse against the plain version."""
    jobs = [
        _job(source="justjoinit",  ext="x", title="Frontend Developer (m/f/d) (Remote)", snippet="aa"),
        _job(source="nofluffjobs", ext="y", title="Frontend Developer", snippet="b"),
    ]
    out = dedupe_cross_source(jobs)
    assert len(out) == 1


def test_normalization_is_case_insensitive():
    """ACME INC vs Acme Inc should be the same cluster — feeds vary
    casing freely."""
    jobs = [
        _job(source="justjoinit",  ext="u", company="Acme Inc", snippet="aa"),
        _job(source="nofluffjobs", ext="v", company="ACME INC", snippet="b"),
    ]
    out = dedupe_cross_source(jobs)
    assert len(out) == 1
    # Insertion-order keeps "Acme Inc" (justjoinit) because its snippet
    # is longer.
    assert out[0].external_id == "u"


def test_normalization_collapses_whitespace():
    """Double-spaced titles must collapse against the single-spaced
    canonical version."""
    jobs = [
        _job(source="justjoinit",  ext="w1", title="Frontend  Developer", snippet="bb"),
        _job(source="nofluffjobs", ext="w2", title="Frontend Developer", snippet="a"),
    ]
    out = dedupe_cross_source(jobs)
    assert len(out) == 1
    assert out[0].external_id == "w1"


def test_tie_on_snippet_length_keeps_first_occurrence():
    """When two postings have equal-length snippets, the first one stays.
    This makes ordering deterministic for tests and for users — feeds
    earlier in the SOURCES dict win on ties."""
    jobs = [
        _job(source="justjoinit",  ext="t1", snippet="abc"),
        _job(source="nofluffjobs", ext="t2", snippet="xyz"),
    ]
    out = dedupe_cross_source(jobs)
    assert len(out) == 1
    assert out[0].external_id == "t1"


def test_preserves_relative_order_when_mixed():
    """Distinct + duplicate rows interleaved: kept entries appear in the
    order they were first seen in the input."""
    jobs = [
        _job(source="justjoinit",  ext="a", title="Alpha"),
        _job(source="nofluffjobs", ext="b", title="Beta",  snippet="long-snippet-for-beta"),
        _job(source="linkedin",    ext="c", title="Beta",  snippet="x"),       # dup of b → dropped
        _job(source="builtin",     ext="d", title="Delta"),
    ]
    out = dedupe_cross_source(jobs)
    assert [j.external_id for j in out] == ["a", "b", "d"]
