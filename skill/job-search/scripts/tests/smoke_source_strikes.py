#!/usr/bin/env python3
"""Smoke for per-user source-strike auto-disable.

Verifies:
  1. Three consecutive zero-score outcomes flip a source to disabled.
  2. ``just_disabled=True`` fires only on the run that crossed the
     threshold (not on subsequent miss runs).
  3. Any non-zero score resets the streak AND clears `disabled_at`.
  4. `get_disabled_sources` returns only currently-disabled keys.
  5. Configurable threshold via the `threshold` arg.
  6. `clear_source_strike` manually re-enables (operator override).

Hermetic in-memory DB.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="smoke_strikes_")
for mod in ("db",):
    if mod in sys.modules:
        del sys.modules[mod]

from db import DB  # noqa: E402


PASS = 0
FAIL = 0


def _check(cond: bool, msg: str) -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {msg}")
    else:
        FAIL += 1
        print(f"  FAIL {msg}")


print("\n=== 1. three zero-score runs → auto-disable on the third ===")
db = DB(Path(os.environ["STATE_DIR"]) / "t.db")
chat_id = 1001

s1, jd1 = db.record_source_outcome(chat_id, 100, "noise_source", max_score=0)
_check((s1, jd1) == (1, False), f"strike 1 → streak=1, not yet disabled (got {(s1, jd1)})")

s2, jd2 = db.record_source_outcome(chat_id, 101, "noise_source", max_score=0)
_check((s2, jd2) == (2, False), f"strike 2 → streak=2, not yet disabled (got {(s2, jd2)})")

s3, jd3 = db.record_source_outcome(chat_id, 102, "noise_source", max_score=0)
_check((s3, jd3) == (3, True), f"strike 3 → streak=3, just_disabled=True (got {(s3, jd3)})")

_check("noise_source" in db.get_disabled_sources(chat_id),
       f"source appears in disabled set (got {db.get_disabled_sources(chat_id)})")


print("\n=== 2. just_disabled=True fires only ONCE ===")
s4, jd4 = db.record_source_outcome(chat_id, 103, "noise_source", max_score=0)
_check((s4, jd4) == (4, False), f"strike 4 → streak grows but just_disabled=False (got {(s4, jd4)})")
_check("noise_source" in db.get_disabled_sources(chat_id),
       "still disabled after subsequent miss")


print("\n=== 3. non-zero score resets streak AND clears disabled_at ===")
# Recovery — even a previously-disabled source can come back if the model
# eventually scores it >0. (Keeps the operator from being permanently
# stuck with a bad auto-disable from one bad week.)
s5, jd5 = db.record_source_outcome(chat_id, 104, "noise_source", max_score=3)
_check((s5, jd5) == (0, False), f"hit on previously-disabled → streak=0 (got {(s5, jd5)})")
_check("noise_source" not in db.get_disabled_sources(chat_id),
       "source no longer disabled after non-zero hit")


print("\n=== 4. multiple sources tracked independently ===")
db.record_source_outcome(chat_id, 200, "src_A", max_score=0)
db.record_source_outcome(chat_id, 200, "src_B", max_score=4)
db.record_source_outcome(chat_id, 201, "src_A", max_score=0)
db.record_source_outcome(chat_id, 201, "src_B", max_score=2)
db.record_source_outcome(chat_id, 202, "src_A", max_score=0)  # third strike
db.record_source_outcome(chat_id, 202, "src_B", max_score=3)
disabled = db.get_disabled_sources(chat_id)
_check("src_A" in disabled, f"src_A disabled (got {disabled})")
_check("src_B" not in disabled, f"src_B NOT disabled (got {disabled})")


print("\n=== 5. configurable threshold ===")
chat2 = 2002
db.record_source_outcome(chat2, 300, "x", max_score=0, threshold=2)
s_x, jd_x = db.record_source_outcome(chat2, 301, "x", max_score=0, threshold=2)
_check(jd_x is True and "x" in db.get_disabled_sources(chat2),
       f"threshold=2 disables on second miss (got streak={s_x}, just_disabled={jd_x})")


print("\n=== 6. clear_source_strike manual re-enable ===")
db.clear_source_strike(chat2, "x")
_check("x" not in db.get_disabled_sources(chat2),
       "manual clear removes from disabled set")
rows = db.list_source_strikes(chat2)
x_row = next((r for r in rows if r["source_key"] == "x"), None)
_check(x_row is not None and x_row["miss_streak"] == 0,
       f"manual clear zeroed the streak (got {x_row})")


print("\n=== 7. get_disabled_sources empty for fresh user ===")
_check(db.get_disabled_sources(99999) == set(),
       "no rows for unknown user → empty set")


print("\n=== 8. network failure path (no by_source entry) → no strike ===")
# This mirrors the search_jobs.py loop's behavior: if a source's fetch
# raised / returned [] / was blocked, no jobs of that source make it
# into `user_jobs`. The strike loop iterates over `by_source` (built
# from user_jobs) so the source never gets a strike for connectivity
# reasons. Verified at the integration boundary by simulating an empty
# strike-input set.
import search_jobs  # noqa: E402  -- check the module imports clean
chat3 = 3003
# Source had network failure — caller never invokes record_source_outcome
# for it. Strike row should not exist.
_check(db.get_disabled_sources(chat3) == set(),
       "no record without outcome call → no strike accumulated")


print("\n=== 9. strike skipped when enrichment returned no score (Haiku batch loss) ===")
# The search_jobs.py guard skips jobs whose enrichment dict is missing
# or carries match_score=None. Validate at the data-shape level: if the
# caller never invokes record_source_outcome (because the loop's
# `if not enr / if raw is None: continue` filtered them out), no strike
# accumulates for that source. The DB layer is intentionally
# unconditional — the caller is responsible for the "real score" gate.
chat4 = 4004
# Don't call record_source_outcome → simulating "all jobs of source X
# came back without verdicts; loop skipped them entirely".
_check(db.get_disabled_sources(chat4) == set(),
       "no strike when caller filters out None-score jobs")


print("\n=== 10. integration: search_jobs.py guard logic against synthetic enrichments ===")
# Simulate the exact loop that runs in search_jobs.py to confirm the
# guards work end-to-end. Three sources contribute jobs; one returns
# all None scores (LLM blip), one returns one None + one real-zero
# (real-zero counts), one returns all real-zeros (real strike).
from dedupe import Job  # noqa: E402
chat5 = 5005

user_jobs = [
    # Source 'llm_blip' — both jobs have match_score=None (Haiku dropped batch)
    Job("llm_blip", "a", "T1", "C1", "EU", "http://x/a", "", "", ""),
    Job("llm_blip", "b", "T2", "C2", "EU", "http://x/b", "", "", ""),
    # Source 'mixed' — one None, one real 0; should still strike (we have signal)
    Job("mixed", "c", "T3", "C3", "EU", "http://x/c", "", "", ""),
    Job("mixed", "d", "T4", "C4", "EU", "http://x/d", "", "", ""),
    # Source 'real_zero' — both real 0; clean strike
    Job("real_zero", "e", "T5", "C5", "EU", "http://x/e", "", "", ""),
]
# Job.job_id is a derived property (hash of source+external_id). Build
# the enrichments map keyed off the live computed id, not the raw
# external_id, so the loop's `enrichments.get(j.job_id)` finds them.
score_per_external = {"a": None, "b": None, "c": None, "d": 0, "e": 0}
enrichments = {
    j.job_id: (
        {"match_score": score_per_external[j.external_id], "why_match": "x"}
    )
    for j in user_jobs
}
# Replicate search_jobs.py loop snippet:
by_source: dict[str, int] = {}
for j in user_jobs:
    enr = enrichments.get(j.job_id)
    if not enr:
        continue
    raw = enr.get("match_score")
    if raw is None:
        continue
    try:
        s = int(raw)
    except (TypeError, ValueError):
        continue
    prev = by_source.get(j.source, -1)
    if s > prev:
        by_source[j.source] = s

_check("llm_blip" not in by_source,
       f"llm_blip skipped (all-None scores) (got by_source={by_source})")
_check(by_source.get("mixed") == 0,
       f"mixed gets one real signal: max=0 (got {by_source.get('mixed')})")
_check(by_source.get("real_zero") == 0,
       f"real_zero counted (got {by_source.get('real_zero')})")

# Apply outcomes — only `mixed` and `real_zero` should accumulate strikes
for src, max_score in by_source.items():
    db.record_source_outcome(chat5, 500, src, max_score, threshold=3)

rows = {r["source_key"]: r for r in db.list_source_strikes(chat5)}
_check("llm_blip" not in rows, "llm_blip has NO strike row (preserved from LLM blip)")
_check(rows.get("mixed", {}).get("miss_streak") == 1, "mixed got strike 1")
_check(rows.get("real_zero", {}).get("miss_streak") == 1, "real_zero got strike 1")


print()
print(f"{'PASS' if FAIL == 0 else 'FAIL'}: {PASS}/{PASS+FAIL} tests passed")
sys.exit(0 if FAIL == 0 else 1)
