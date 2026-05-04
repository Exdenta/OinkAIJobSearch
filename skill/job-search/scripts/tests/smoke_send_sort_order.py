#!/usr/bin/env python3
"""Smoke test for ``send_per_job_digest`` sort order and UX direction.

Covers:
  1. Mixed-score / mixed-source set: the resulting send order respects
     the documented priority (match_score → posted_at → SOURCE_TIER →
     job_id), with best-LAST so the highest-scored job is the LAST
     ``send_message`` call (so it lands at the bottom of the Telegram
     chat closest to the input box).
  2. Equal scores → posted_at DESC tie-break (fresher last).
  3. Equal scores + equal posted_at → SOURCE_TIER tie-break.
  4. ``enrichments=None`` fallback: sort by source tier, posted_at, job_id.
  5. ``match_score=None`` (Haiku batch failure) — does NOT crash, sinks
     to the bottom of the priority list (sent FIRST under best-last).
  6. Determinism: shuffling inputs yields the identical send order.
  7. Best-last: highest-score job is the FINAL ``send_message`` call.
  8. ``top_n`` truncation keeps only the strongest N matches.

Uses a fake ``tg`` recorder + ``URL_VALIDATION_OFF=1`` so the test is
fully offline.
"""
from __future__ import annotations

import os
import random
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Force URL validation off and forensic off so the test is hermetic.
# Also disable the age + forum + rate-limit gates that landed alongside
# the sort logic — each gate has its own dedicated smoke test, and they
# would drop the synthetic test fixtures (old posted_at, repeated URLs)
# making sort-order assertions impossible to verify in isolation.
os.environ["URL_VALIDATION_OFF"] = "1"
os.environ["FORENSIC_OFF"] = "1"
os.environ["JOB_AGE_FILTER_OFF"] = "1"
os.environ["FORUM_FILTER_OFF"] = "1"
os.environ["TG_RATE_LIMIT_OFF"] = "1"
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="smoke_send_sort_")

# Reload to pick up env.
for mod in ("forensic", "telegram_client"):
    if mod in sys.modules:
        del sys.modules[mod]

import telegram_client as tc  # noqa: E402
from dedupe import Job  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


# ---------------------------------------------------------------------------

class _FakeTG:
    """Minimal stand-in for TelegramClient — records every send."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, object]] = []
        self._next_id = 1000

    def send_message(self, chat_id, text, parse_mode="MarkdownV2",
                     reply_markup=None, disable_preview=True) -> int:
        self.calls.append((chat_id, text, reply_markup))
        self._next_id += 1
        return self._next_id


def _make_job(source: str, ext: str, posted_at: str = "", title: str | None = None) -> Job:
    return Job(
        source=source,
        external_id=ext,
        title=title or f"{source}-{ext}",
        company="Co",
        location="",
        url=f"https://example.com/{source}/{ext}",
        posted_at=posted_at,
    )


def _send_and_record(jobs, enrichments, *, top_n=None) -> tuple[_FakeTG, list[Job]]:
    """Run send_per_job_digest with a fake tg and return (tg, ordered_jobs).

    ``ordered_jobs`` is the post-sort list, derived from the sequence of
    titles seen by ``on_sent`` (skipping the header).
    """
    fake_tg = _FakeTG()
    captured: list[Job] = []

    def on_sent(_mid: int, j: Job) -> None:
        captured.append(j)

    tc.send_per_job_digest(
        fake_tg, chat_id=1, jobs=list(jobs), cfg={}, on_sent=on_sent,
        enrichments=enrichments, top_n=top_n,
    )
    return fake_tg, captured


# ---------------------------------------------------------------------------
# 1. Mixed scores across sources: full priority order
# ---------------------------------------------------------------------------

def test_mixed_scores_priority() -> None:
    section("1. Mixed scores → full priority order, best LAST")
    # 8 jobs across 4 sources, mixed scores, mixed dates
    jobs = [
        _make_job("hackernews",  "h1", "2026-04-29"),    # score 2
        _make_job("web_search",  "w1", "2026-04-25"),    # score 5
        _make_job("linkedin",    "l1", "2026-04-28"),    # score 4
        _make_job("remoteok",    "r1", "2026-04-30"),    # score 1
        _make_job("web_search",  "w2", "2026-04-30"),    # score 3
        _make_job("linkedin",    "l2", "2026-04-30"),    # score 5
        _make_job("hackernews",  "h2", "2026-04-30"),    # score 0
        _make_job("remoteok",    "r2", "2026-04-29"),    # score 4
    ]
    enr = {
        jobs[0].job_id: {"match_score": 2},
        jobs[1].job_id: {"match_score": 5},
        jobs[2].job_id: {"match_score": 4},
        jobs[3].job_id: {"match_score": 1},
        jobs[4].job_id: {"match_score": 3},
        jobs[5].job_id: {"match_score": 5},
        jobs[6].job_id: {"match_score": 0},
        jobs[7].job_id: {"match_score": 4},
    }
    _, captured = _send_and_record(jobs, enr)

    # Expected ascending-key order under best-last:
    # primary score ASC: 0,1,2,3,4,4,5,5
    # within score=4: posted_at ASC (29 then 28? no, ASC means older first)
    #   r2(2026-04-29) vs l1(2026-04-28) → l1 first (older), then r2
    # within score=5: posted_at ASC then -tier ASC
    #   w1(2026-04-25), l2(2026-04-30) → w1 first (older). then l2 last.
    #   actually w1 is older → w1 comes first; l2 is newer → l2 last.
    expected_titles = [
        "hackernews-h2",  # score 0
        "remoteok-r1",    # score 1
        "hackernews-h1",  # score 2
        "web_search-w2",  # score 3
        "linkedin-l1",    # score 4, posted 04-28 (older)
        "remoteok-r2",    # score 4, posted 04-29 (newer)
        "web_search-w1",  # score 5, posted 04-25 (older)
        "linkedin-l2",    # score 5, posted 04-30 (newer)
    ]
    got = [j.title for j in captured]
    _assert(got == expected_titles,
            f"send order matches priority\n      expected: {expected_titles}\n      got:      {got}")


# ---------------------------------------------------------------------------
# 2. Equal score, varying posted_at → posted_at DESC tie-break
# ---------------------------------------------------------------------------

def test_posted_at_tiebreak() -> None:
    section("2. Equal score → posted_at tie-break (fresher LAST)")
    jobs = [
        _make_job("linkedin", "a", "2026-04-15"),
        _make_job("linkedin", "b", "2026-04-30"),
        _make_job("linkedin", "c", "2026-04-20"),
        _make_job("linkedin", "d", "2026-04-25"),
    ]
    enr = {j.job_id: {"match_score": 3} for j in jobs}
    _, captured = _send_and_record(jobs, enr)
    titles = [j.title for j in captured]
    expected = ["linkedin-a", "linkedin-c", "linkedin-d", "linkedin-b"]
    _assert(titles == expected, f"posted_at ASC under best-last (got {titles})")


# ---------------------------------------------------------------------------
# 3. Equal score + posted_at → SOURCE_TIER tie-break
# ---------------------------------------------------------------------------

def test_source_tier_tiebreak() -> None:
    section("3. Equal score+date → SOURCE_TIER tie-break")
    jobs = [
        _make_job("hackernews", "x", "2026-04-30"),  # tier 7
        _make_job("web_search", "x", "2026-04-30"),  # tier 0 (best)
        _make_job("linkedin",   "x", "2026-04-30"),  # tier 1
    ]
    enr = {j.job_id: {"match_score": 4} for j in jobs}
    _, captured = _send_and_record(jobs, enr)
    titles = [j.title for j in captured]
    # Best LAST → web_search must be the final send.
    expected_last = "web_search-x"
    _assert(titles[-1] == expected_last,
            f"web_search (tier 0) sent last (got order {titles})")
    _assert(titles[0] == "hackernews-x",
            f"hackernews (tier 7) sent first (got order {titles})")


# ---------------------------------------------------------------------------
# 4. enrichments=None fallback
# ---------------------------------------------------------------------------

def test_no_enrichments_fallback() -> None:
    section("4. enrichments=None → tier+posted_at+job_id fallback")
    jobs = [
        _make_job("hackernews", "h", "2026-04-30"),  # tier 7
        _make_job("web_search", "w", "2026-04-15"),  # tier 0 — older but best tier
        _make_job("linkedin",   "l", "2026-04-25"),  # tier 1
    ]
    _, captured = _send_and_record(jobs, None)
    titles = [j.title for j in captured]
    # Under best-LAST + tier first: web_search must be LAST despite being
    # the oldest, because tier dominates in the no-enrichment fallback.
    _assert(titles[-1] == "web_search-w",
            f"fallback puts best tier last (got {titles})")
    _assert(titles[0] == "hackernews-h",
            f"fallback puts worst tier first (got {titles})")


# ---------------------------------------------------------------------------
# 5. match_score=None (Haiku batch failure) sinks
# ---------------------------------------------------------------------------

def test_none_score_sinks() -> None:
    section("5. match_score=None sinks (no crash, sent FIRST under best-last)")
    jobs = [
        _make_job("linkedin", "good", "2026-04-30"),
        _make_job("linkedin", "broken", "2026-04-30"),
        _make_job("linkedin", "ok", "2026-04-30"),
    ]
    enr = {
        jobs[0].job_id: {"match_score": 4},
        jobs[1].job_id: {"match_score": None},   # batch failure
        jobs[2].job_id: {"match_score": 2},
    }
    fake_tg, captured = _send_and_record(jobs, enr)
    titles = [j.title for j in captured]
    # None → -1, so "broken" must be sent FIRST (lowest priority); the
    # highest-score "good" must be LAST.
    _assert(titles[0] == "linkedin-broken",
            f"None-score sinks (sent first under best-last) — got {titles}")
    _assert(titles[-1] == "linkedin-good",
            f"highest score sent last (got {titles})")
    _assert(len(fake_tg.calls) == 4,  # 1 header + 3 jobs
            f"all 3 jobs sent (no crash) — calls={len(fake_tg.calls)}")


# ---------------------------------------------------------------------------
# 6. Determinism: shuffle → same result
# ---------------------------------------------------------------------------

def test_determinism() -> None:
    section("6. Determinism: shuffled input → same send order")
    jobs = [
        _make_job("hackernews",  "h1", "2026-04-29"),
        _make_job("web_search",  "w1", "2026-04-25"),
        _make_job("linkedin",    "l1", "2026-04-28"),
        _make_job("remoteok",    "r1", "2026-04-30"),
        _make_job("web_search",  "w2", "2026-04-30"),
        _make_job("linkedin",    "l2", "2026-04-30"),
        _make_job("hackernews",  "h2", "2026-04-30"),
        _make_job("remoteok",    "r2", "2026-04-29"),
    ]
    enr = {
        jobs[0].job_id: {"match_score": 2},
        jobs[1].job_id: {"match_score": 5},
        jobs[2].job_id: {"match_score": 4},
        jobs[3].job_id: {"match_score": 1},
        jobs[4].job_id: {"match_score": 3},
        jobs[5].job_id: {"match_score": 5},
        jobs[6].job_id: {"match_score": 0},
        jobs[7].job_id: {"match_score": 4},
    }
    _, baseline = _send_and_record(jobs, enr)
    baseline_titles = [j.title for j in baseline]

    rng = random.Random(42)
    for trial in range(5):
        shuffled = list(jobs)
        rng.shuffle(shuffled)
        _, captured = _send_and_record(shuffled, enr)
        titles = [j.title for j in captured]
        _assert(titles == baseline_titles,
                f"trial {trial}: shuffled input → identical order")


# ---------------------------------------------------------------------------
# 7. Best-LAST UX assertion
# ---------------------------------------------------------------------------

def test_best_last_ux() -> None:
    section("7. Highest-score job is the LAST send_message call")
    jobs = [
        _make_job("linkedin", "a", "2026-04-15"),
        _make_job("linkedin", "b", "2026-04-30"),
        _make_job("hackernews", "c", "2026-04-30"),
    ]
    enr = {
        jobs[0].job_id: {"match_score": 5},   # the best
        jobs[1].job_id: {"match_score": 1},
        jobs[2].job_id: {"match_score": 3},
    }
    fake_tg, captured = _send_and_record(jobs, enr)
    # 1 header + 3 jobs = 4 calls.
    _assert(len(fake_tg.calls) == 4, f"expected 4 calls (got {len(fake_tg.calls)})")
    # The LAST send_message text should reference the score-5 job.
    last_text = fake_tg.calls[-1][1]
    _assert("linkedin-a" in last_text or "a" in last_text,
            f"last message contains the best-match job (snippet: {last_text[:120]!r})")
    _assert(captured[-1].title == "linkedin-a",
            f"on_sent last == best (got {captured[-1].title!r})")


# ---------------------------------------------------------------------------
# 8. top_n truncation
# ---------------------------------------------------------------------------

def test_top_n_truncation() -> None:
    section("8. top_n=3 keeps the strongest 3, drops the rest")
    jobs = [
        _make_job("linkedin", f"j{i}", "2026-04-30") for i in range(6)
    ]
    enr = {
        jobs[0].job_id: {"match_score": 5},
        jobs[1].job_id: {"match_score": 4},
        jobs[2].job_id: {"match_score": 3},
        jobs[3].job_id: {"match_score": 2},
        jobs[4].job_id: {"match_score": 1},
        jobs[5].job_id: {"match_score": 0},
    }
    _, captured = _send_and_record(jobs, enr, top_n=3)
    # Strongest 3 are scores 5/4/3 → titles j0/j1/j2 (in best-last order:
    # j2 first, then j1, then j0).
    titles = [j.title for j in captured]
    _assert(len(titles) == 3, f"only 3 jobs sent (got {len(titles)})")
    _assert(titles == ["linkedin-j2", "linkedin-j1", "linkedin-j0"],
            f"top-3 in ascending-priority order (got {titles})")


# ---------------------------------------------------------------------------

def main() -> int:
    tests = [
        test_mixed_scores_priority,
        test_posted_at_tiebreak,
        test_source_tier_tiebreak,
        test_no_enrichments_fallback,
        test_none_score_sinks,
        test_determinism,
        test_best_last_ux,
        test_top_n_truncation,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"  ASSERT FAILED in {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR in {t.__name__}: {type(e).__name__}: {e}")
    print()
    if failed:
        print(f"FAIL: {failed}/{len(tests)} test(s) failed")
        return 1
    print(f"PASS: {len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
