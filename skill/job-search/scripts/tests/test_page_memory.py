"""Page-memory cursor tests (algorithm v2.7, P2 pipeline overhaul).

Two layers:

  1. Raw `DB` methods — `record_fetch`, `get_fetch`, `next_page_for`,
     `stale_fetches`, `source_novelty_ratio`, `count_existing_jobs`.

  2. One representative adapter (`sources.linkedin`) — exercises the
     cursor advancement end-to-end with the HTTP layer mocked out, and
     verifies that the `db=None` branch is unchanged.

Per-test DB lives on a tmp_path-rooted SQLite file via the `tmp_db`
fixture (same pattern as `test_job_scores_cache.py` /
`test_quality_buffer.py`).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import db as db_module  # noqa: E402
from dedupe import Job  # noqa: E402
from sources import linkedin  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path):
    return db_module.DB(tmp_path / "test.db")


# --------------------------------------------------------------------------- #
#                            raw DB-method tests                              #
# --------------------------------------------------------------------------- #


def test_record_fetch_then_get_fetch(tmp_db):
    """Round-trip: write a row, read it back, every field matches."""
    tmp_db.record_fetch(
        "linkedin", "react", page=1, location="Spain",
        jobs_seen=25, jobs_new=4,
    )
    row = tmp_db.get_fetch("linkedin", "react", page=1, location="Spain")
    assert row is not None
    assert row["source"]    == "linkedin"
    assert row["query"]     == "react"
    assert row["page"]      == 1
    assert row["location"]  == "Spain"
    assert row["jobs_seen"] == 25
    assert row["jobs_new"]  == 4
    # fetched_at populated with a recent timestamp.
    assert abs(row["fetched_at"] - time.time()) < 5

    # Cells we haven't recorded return None.
    assert tmp_db.get_fetch("linkedin", "react", page=2, location="Spain") is None
    assert tmp_db.get_fetch("justjoinit", "1", page=1, location="") is None


def test_record_fetch_replaces_on_same_key(tmp_db):
    """INSERT OR REPLACE: the same primary key keeps only the latest row."""
    tmp_db.record_fetch(
        "linkedin", "react", page=1, location="",
        jobs_seen=20, jobs_new=5,
    )
    first = tmp_db.get_fetch("linkedin", "react", page=1, location="")
    assert first is not None
    first_at = first["fetched_at"]

    time.sleep(0.02)  # measurable gap so a stray fresh-timestamp write is visible
    tmp_db.record_fetch(
        "linkedin", "react", page=1, location="",
        jobs_seen=10, jobs_new=1,
    )
    second = tmp_db.get_fetch("linkedin", "react", page=1, location="")
    assert second is not None
    # Latest counts win; only one row materially exists at this key.
    assert second["jobs_seen"] == 10
    assert second["jobs_new"]  == 1
    assert second["fetched_at"] > first_at

    # Hard verify the underlying row count.
    with tmp_db._conn() as c:  # noqa: SLF001
        n = c.execute(
            "SELECT COUNT(*) AS n FROM search_fetches "
            "WHERE source = 'linkedin' AND query = 'react' "
            "  AND page = 1 AND location = ''"
        ).fetchone()["n"]
    assert n == 1


def test_next_page_advances_when_page1_fresh(tmp_db):
    """Page 1 written 1h ago + min_revisit=6h → cursor returns 2."""
    # Backdate the row's fetched_at to 1h ago.
    tmp_db.record_fetch("linkedin", "react", 1, "", jobs_seen=10, jobs_new=2)
    one_hour_ago = time.time() - 3600
    with tmp_db._conn() as c:  # noqa: SLF001
        c.execute(
            "UPDATE search_fetches SET fetched_at = ? "
            "WHERE source = 'linkedin' AND query = 'react' "
            "  AND page = 1 AND location = ''",
            (one_hour_ago,),
        )

    next_p = tmp_db.next_page_for(
        "linkedin", "react", "",
        max_page=10, min_revisit_age_s=6 * 3600,
    )
    assert next_p == 2


def test_next_page_returns_1_when_stale(tmp_db):
    """Page 1 fetched 7h ago + min_revisit=6h → re-fetch page 1."""
    tmp_db.record_fetch("linkedin", "react", 1, "", jobs_seen=10, jobs_new=2)
    seven_hours_ago = time.time() - 7 * 3600
    with tmp_db._conn() as c:  # noqa: SLF001
        c.execute(
            "UPDATE search_fetches SET fetched_at = ? "
            "WHERE source = 'linkedin' AND query = 'react'",
            (seven_hours_ago,),
        )

    next_p = tmp_db.next_page_for(
        "linkedin", "react", "",
        max_page=10, min_revisit_age_s=6 * 3600,
    )
    assert next_p == 1


def test_next_page_returns_minus_1_when_all_covered_and_fresh(tmp_db):
    """All pages 1..max_page recorded within the window → -1 (skip)."""
    max_page = 5
    for p in range(1, max_page + 1):
        tmp_db.record_fetch("linkedin", "react", p, "Spain", 10, 1)

    next_p = tmp_db.next_page_for(
        "linkedin", "react", "Spain",
        max_page=max_page, min_revisit_age_s=6 * 3600,
    )
    assert next_p == -1


def test_next_page_wraps_after_max(tmp_db):
    """Highest recorded page == max_page (all fresh except page 1 also fresh) → -1.

    Note: the canonical "wrap" path tested here is the same all-covered
    case; when the cursor genuinely needs to wrap (page 1 expires), the
    "stale" test above already exercises the return-to-1 branch. The
    test name from the spec is preserved for cross-reference but the
    behaviour we check matches the `next_page_for` contract: all-fresh
    → -1, page-1-stale → 1. Wrap-with-page-1-stale: explicit setup."""
    max_page = 5
    for p in range(1, max_page + 1):
        tmp_db.record_fetch("linkedin", "react", p, "", 10, 1)

    # Backdate ONLY page 1 to make it stale; pages 2..max_page stay fresh.
    seven_hours_ago = time.time() - 7 * 3600
    with tmp_db._conn() as c:  # noqa: SLF001
        c.execute(
            "UPDATE search_fetches SET fetched_at = ? "
            "WHERE source = 'linkedin' AND query = 'react' AND page = 1",
            (seven_hours_ago,),
        )

    # Now page 1 is stale; the cursor wraps back to 1 (re-top).
    next_p = tmp_db.next_page_for(
        "linkedin", "react", "",
        max_page=max_page, min_revisit_age_s=6 * 3600,
    )
    assert next_p == 1


def test_stale_fetches_filters_by_age(tmp_db):
    """Three rows at varying ages; only the older-than-12h ones return."""
    now = time.time()
    for label, age_s in (
        ("very_old", 24 * 3600),
        ("old",      13 * 3600),
        ("recent",    1 * 3600),
    ):
        tmp_db.record_fetch("builtin", label, page=1, location="",
                            jobs_seen=5, jobs_new=1)
    with tmp_db._conn() as c:  # noqa: SLF001
        c.execute(
            "UPDATE search_fetches SET fetched_at = ? "
            "WHERE source = 'builtin' AND query = 'very_old'",
            (now - 24 * 3600,),
        )
        c.execute(
            "UPDATE search_fetches SET fetched_at = ? "
            "WHERE source = 'builtin' AND query = 'old'",
            (now - 13 * 3600,),
        )
        c.execute(
            "UPDATE search_fetches SET fetched_at = ? "
            "WHERE source = 'builtin' AND query = 'recent'",
            (now - 1 * 3600,),
        )

    stale = tmp_db.stale_fetches("builtin", max_age_seconds=12 * 3600)
    queries = [r["query"] for r in stale]
    assert "very_old" in queries
    assert "old" in queries
    assert "recent" not in queries
    # ASC order — oldest first.
    assert queries == sorted(queries, key=lambda q: -1 if q == "very_old" else 0)
    assert stale[0]["query"] == "very_old"


def test_source_novelty_ratio(tmp_db):
    """Window covers all 3 rows: ratio = sum(new) / sum(seen) = 14 / 30."""
    tmp_db.record_fetch("linkedin", "q1", 1, "", jobs_seen=10, jobs_new=3)
    tmp_db.record_fetch("linkedin", "q2", 1, "", jobs_seen=10, jobs_new=3)
    tmp_db.record_fetch("linkedin", "q3", 1, "", jobs_seen=10, jobs_new=8)

    ratio = tmp_db.source_novelty_ratio("linkedin", since_seconds_ago=86400)
    assert ratio == pytest.approx(14 / 30, rel=1e-6)


def test_source_novelty_ratio_zero_seen_returns_zero(tmp_db):
    """No rows OR jobs_seen sums to 0 → 0.0, never a ZeroDivisionError."""
    assert tmp_db.source_novelty_ratio("linkedin", 86400) == 0.0

    # Add a row whose jobs_seen is 0 — still no division.
    tmp_db.record_fetch("linkedin", "empty", 1, "", jobs_seen=0, jobs_new=0)
    assert tmp_db.source_novelty_ratio("linkedin", 86400) == 0.0


def test_count_existing_jobs(tmp_db):
    """Counts hits in `jobs`; new ids contribute 0."""
    tmp_db.upsert_job({
        "job_id": "linkedin:a", "source": "linkedin", "external_id": "a",
        "title": "T", "company": "C", "location": "", "url": "",
        "posted_at": "", "snippet": "", "salary": "",
    })
    tmp_db.upsert_job({
        "job_id": "linkedin:b", "source": "linkedin", "external_id": "b",
        "title": "T", "company": "C", "location": "", "url": "",
        "posted_at": "", "snippet": "", "salary": "",
    })
    assert tmp_db.count_existing_jobs([]) == 0
    assert tmp_db.count_existing_jobs(["linkedin:a", "linkedin:c"]) == 1
    assert tmp_db.count_existing_jobs(["linkedin:a", "linkedin:b"]) == 2
    assert tmp_db.count_existing_jobs(["linkedin:zzz"]) == 0


# --------------------------------------------------------------------------- #
#                       adapter test — linkedin cursor                        #
# --------------------------------------------------------------------------- #


class _FakeResp:
    """Minimal stand-in for `requests.Response` shaped to what
    linkedin._one_search reads off it."""

    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _fake_card_html(card_url: str, title: str = "Frontend Engineer",
                    company: str = "Acme") -> str:
    """Render one minimal LinkedIn search card. linkedin's _one_search reads
    <li>, an <a class="base-card__full-link">, an <h3>, <h4>, and the
    location <span> — that's the minimum surface area we need."""
    return f"""
    <li>
      <a class="base-card__full-link" href="{card_url}">click</a>
      <h3>{title}</h3>
      <h4>{company}</h4>
      <span class="job-search-card__location">Remote</span>
    </li>
    """.strip()


def _fake_listing_page(start: int, n_cards: int = 3) -> str:
    """Wrap N fake cards in a >500-byte HTML envelope (linkedin's
    body_resolved check trips at 500 bytes — under that the geo is
    treated as "didn't resolve" and we don't want that path here)."""
    cards = "\n".join(
        _fake_card_html(
            card_url=f"https://www.linkedin.com/jobs/view/{start + i}",
            title=f"Frontend Engineer #{start + i}",
            company=f"Acme {i}",
        )
        for i in range(n_cards)
    )
    pad = "<!-- " + ("padding " * 100) + " -->"
    return f"<html><body>{cards}{pad}</body></html>"


@pytest.fixture
def linkedin_no_sleep(monkeypatch):
    """LinkedIn paces 1.5s between requests; disable in tests."""
    monkeypatch.setattr(linkedin, "PACE_SECONDS", 0.0)
    # Skip the detail-body second pass — that hits a different endpoint
    # we'd otherwise need to also mock.
    monkeypatch.setattr(linkedin, "_fetch_detail_bodies", lambda jobs: jobs)


@pytest.fixture
def fake_requests_get(monkeypatch):
    """Intercept `requests.get` inside the linkedin module. Each call
    is recorded; the fixture exposes a list of (params, headers) and
    returns one fresh fake response per call."""
    calls: list[dict[str, Any]] = []

    def _stub(url: str, params=None, headers=None, timeout=20):
        calls.append({"url": url, "params": dict(params or {})})
        start = int((params or {}).get("start", 0))
        return _FakeResp(_fake_listing_page(start))

    monkeypatch.setattr(linkedin.requests, "get", _stub)
    return calls


def test_linkedin_with_db_advances_page(tmp_db, linkedin_no_sleep,
                                        fake_requests_get):
    """First call fetches page 1 (start=0). After the cursor records
    that, a second call fetches page 2 (start=10) — the cursor advances
    instead of re-fetching page 1."""
    user_seeds = {
        "queries": ["react typescript"],
        "geos":    ["Spain"],
        "f_TPR":   "r86400",
    }
    filters = {"remote": "any"}

    # ---- iteration 1 ---------------------------------------------------- #
    jobs_1 = linkedin.fetch_for_user(
        filters, user_seeds, db=tmp_db, min_revisit_age_s=21600,
    )
    assert jobs_1, "first call should return some jobs"
    # Exactly one HTTP request happened, against start=0 (page 1).
    assert len(fake_requests_get) == 1
    assert int(fake_requests_get[0]["params"]["start"]) == 0

    # Cursor cell exists for page 1.
    row = tmp_db.get_fetch("linkedin", "react typescript", page=1, location="Spain")
    assert row is not None
    assert row["jobs_seen"] >= 1

    # ---- iteration 2 ---------------------------------------------------- #
    fake_requests_get.clear()
    jobs_2 = linkedin.fetch_for_user(
        filters, user_seeds, db=tmp_db, min_revisit_age_s=21600,
    )
    assert jobs_2, "second call should still return jobs (different page)"
    assert len(fake_requests_get) == 1
    # Page 2 → start=10 per _LINKEDIN_PAGE_TO_START.
    assert int(fake_requests_get[0]["params"]["start"]) == 10

    # Cursor cells now exist for pages 1 AND 2.
    assert tmp_db.get_fetch("linkedin", "react typescript", 1, "Spain") is not None
    assert tmp_db.get_fetch("linkedin", "react typescript", 2, "Spain") is not None


def test_linkedin_without_db_unchanged(linkedin_no_sleep, fake_requests_get,
                                       tmp_path):
    """db=None preserves the legacy 4-page sweep per (q, geo) combo —
    starts at 0, then 10, 25, 50. No `search_fetches` writes occur (and
    can't, since there's no DB)."""
    user_seeds = {
        "queries": ["react"],
        "geos":    ["Spain"],
        "f_TPR":   "r86400",
    }
    filters = {"remote": "any"}

    jobs = linkedin.fetch_for_user(filters, user_seeds)
    # Legacy mode walks (0, 10, 25, 50) = 4 requests per (q, geo).
    starts = [int(c["params"]["start"]) for c in fake_requests_get]
    assert starts == [0, 10, 25, 50]
    assert jobs, "legacy mode should still produce jobs"
