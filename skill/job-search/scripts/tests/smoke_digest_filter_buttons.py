#!/usr/bin/env python3
"""Smoke for the digest-header filter-buttons flow (⬇ Lower / ⬆ Raise).

Covers the three pieces that have to compose for the feature to work:

  1. ``digest_header_keyboard`` button-shape rules
       — floor 0 hides ⬇; floor 5 hides ⬆; lower_count==0 hides ⬇.
  2. ``DB.record_digest_run_jobs`` + ``fetch_unsent_at_score`` round-trip
       — ``mark_digest_jobs_sent`` flips rows so re-fetch returns []]
  3. ``send_per_job_digest(skip_header=True)`` re-sends per-job cards with
     no new header — what the bot's ``flt:lwr`` callback uses to append
     dropped postings to an existing digest.

Hermetic: forensic off, URL/age/forum gates off, in-memory DB.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

os.environ["URL_VALIDATION_OFF"] = "1"
os.environ["FORENSIC_OFF"] = "1"
os.environ["JOB_AGE_FILTER_OFF"] = "1"
os.environ["FORUM_FILTER_OFF"] = "1"
os.environ["TG_RATE_LIMIT_OFF"] = "1"
os.environ["STATE_DIR"] = tempfile.mkdtemp(prefix="smoke_filter_btn_")

for mod in ("forensic", "telegram_client", "db"):
    if mod in sys.modules:
        del sys.modules[mod]

import telegram_client as tc  # noqa: E402
from db import DB  # noqa: E402
from dedupe import Job  # noqa: E402


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


def section(label: str) -> None:
    print(f"\n=== {label} ===")


class _FakeTG:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 1000

    def send_message(self, chat_id, text, parse_mode="MarkdownV2",
                     reply_markup=None, disable_preview=True) -> int:
        self.calls.append(("send", {"chat_id": chat_id, "text": text, "kb": reply_markup}))
        self._next_id += 1
        return self._next_id


def _job(jid: str, source: str = "linkedin") -> Job:
    j = Job(source, jid, f"Title {jid}", "Co", "EU", f"https://x/{jid}", "2026-04-30", "snip", "")
    # Job dataclass has no job_id field in __init__; the dedupe layer derives
    # job_id from external_id+source. Verify by reading the attribute.
    return j


# ---------------------------------------------------------------------------

section("1. digest_header_keyboard button-shape rules")

kb = tc.digest_header_keyboard(run_id=42, current_floor=3, lower_count=5)
buttons = kb["inline_keyboard"][0] if kb else []
_check(len(buttons) == 2, f"floor=3, lower=5 → 2 buttons (got {len(buttons)})")
_check(buttons[0]["text"] == "⬇ ≥2 (+5)", f"⬇ label correct (got {buttons[0]['text']})")
_check(buttons[0]["callback_data"] == "flt:lwr:42:2", f"⬇ callback (got {buttons[0]['callback_data']})")
_check(buttons[1]["text"] == "⬆ ≥4", f"⬆ label correct (got {buttons[1]['text']})")
_check(buttons[1]["callback_data"] == "flt:rse:4", f"⬆ callback (got {buttons[1]['callback_data']})")

kb = tc.digest_header_keyboard(run_id=42, current_floor=0, lower_count=0)
btns = kb["inline_keyboard"][0] if kb else []
_check(len(btns) == 1 and btns[0]["text"] == "⬆ ≥1", "floor=0 hides ⬇, shows only ⬆ ≥1")

kb = tc.digest_header_keyboard(run_id=42, current_floor=5, lower_count=2)
btns = kb["inline_keyboard"][0] if kb else []
_check(len(btns) == 1 and btns[0]["text"].startswith("⬇"),
       "floor=5 hides ⬆, shows only ⬇")

kb = tc.digest_header_keyboard(run_id=42, current_floor=3, lower_count=0)
btns = kb["inline_keyboard"][0] if kb else []
_check(len(btns) == 1 and btns[0]["text"] == "⬆ ≥4",
       "lower_count=0 hides ⬇")

kb = tc.digest_header_keyboard(run_id=None, current_floor=3, lower_count=5)
btns = kb["inline_keyboard"][0] if kb else []
_check(len(btns) == 1 and btns[0]["text"] == "⬆ ≥4",
       "run_id=None hides ⬇ (no run to replay against)")


# ---------------------------------------------------------------------------

section("2. DB record/fetch/mark round-trip")

db_path = Path(os.environ["STATE_DIR"]) / "t.db"
db = DB(db_path)

enrichments = {
    "j_a": {"match_score": 4, "why_match": "good"},
    "j_b": {"match_score": 3, "why_match": "ok"},
    "j_c": {"match_score": 2, "why_match": "borderline"},
    "j_d": {"match_score": 2, "why_match": "borderline2"},
    "j_e": {"match_score": 0, "why_match": "no"},
}
n = db.record_digest_run_jobs(chat_id=99, run_id=7, scored_enrichments=enrichments)
_check(n == 5, f"recorded all 5 enrichments (got {n})")

# `>=` semantics: count includes everything at or above the floor that's
# still unsent. The button label "⬇ ≥N (+M)" must equal what a click admits.
_check(db.unsent_count_at_score(99, 7, 2) == 4,
       "count at score≥2 → 4 (j_a:4, j_b:3, j_c:2, j_d:2)")
_check(db.unsent_count_at_score(99, 7, 4) == 1, "count at score≥4 → 1 (j_a)")
_check(db.unsent_count_at_score(99, 7, 1) == 4,
       "count at score≥1 → 4 (j_e is score 0 → excluded)")
_check(db.unsent_count_at_score(99, 7, 5) == 0, "count at score≥5 → 0")
_check(db.latest_digest_run_id(99) == 7, "latest_digest_run_id(99) == 7")

# Live digest at floor=3 delivered j_a and j_b (the only ≥3 jobs).
db.mark_digest_jobs_sent(99, 7, ["j_a", "j_b"], floor=3)

# ⬇ to floor=2 must admit ALL still-unsent jobs at score ≥ 2 — that's
# j_c and j_d (score 2). j_a and j_b are stamped sent_floor=3 so they
# stay filtered out (idempotent: the user already saw them).
rows_2 = db.fetch_unsent_at_score(99, 7, 2)
_check(len(rows_2) == 2, f"fetch at score≥2 → 2 unsent (got {len(rows_2)})")
ids_2 = sorted(r[0] for r in rows_2)
_check(ids_2 == ["j_c", "j_d"], f"unsent at ≥2 → j_c+j_d (got {ids_2})")

# Marked rows don't reappear at their own score either.
rows_4 = db.fetch_unsent_at_score(99, 7, 4)
_check(rows_4 == [], "marked rows are gone from fetch_unsent_at_score")

# Regression for the Alena bug (chat 433775883, run 24): with multiple
# unsent score tiers, the count and fetch must agree. Add an unsent
# score-3 row alongside the two existing unsent score-2 rows; ⬇ ≥2 must
# admit all three (not just exact-score-2 matches).
db.record_digest_run_jobs(
    chat_id=99, run_id=7,
    scored_enrichments={"j_late": {"match_score": 3, "why_match": "late enrich"}},
)
_check(db.unsent_count_at_score(99, 7, 2) == 3,
       "count at score≥2 with late j_late(score 3) → 3 (j_c, j_d, j_late)")
rows_2b = db.fetch_unsent_at_score(99, 7, 2)
ids_2b = sorted(r[0] for r in rows_2b)
_check(ids_2b == ["j_c", "j_d", "j_late"],
       f"fetch at ≥2 includes higher tiers (got {ids_2b})")


# ---------------------------------------------------------------------------

section("3. send_per_job_digest(skip_header=True) does not send a header")

ftg = _FakeTG()
jobs = [_job("aaa"), _job("bbb")]
enrich = {jobs[0].job_id: {"match_score": 2}, jobs[1].job_id: {"match_score": 2}}

sent = tc.send_per_job_digest(
    ftg, chat_id=99, jobs=jobs,
    cfg={"message": {"include_snippet": True, "snippet_chars": 100}},
    on_sent=lambda mid, j: None,
    enrichments=enrich,
    min_score=2,
    run_id=7,
    skip_header=True,
)
texts = [c[1]["text"] for c in ftg.calls]
_check(len(ftg.calls) == 2, f"replay sends 2 messages (got {len(ftg.calls)})")
_check(not any("Daily Job Digest" in t for t in texts),
       "no header sent in skip_header=True path")
_check(sent == 2, f"send_per_job_digest returned per-job count (got {sent})")


section("4. send_per_job_digest(skip_header=False) carries the new keyboard")

ftg2 = _FakeTG()
jobs = [_job("ccc")]
enrich = {jobs[0].job_id: {"match_score": 4}}
tc.send_per_job_digest(
    ftg2, chat_id=99, jobs=jobs,
    cfg={"message": {"include_snippet": True, "snippet_chars": 100}},
    on_sent=lambda mid, j: None,
    enrichments=enrich,
    min_score=3,
    run_id=7,
    enriched_count=10,
    dropped_below_score=9,
    lower_count_at_step=4,
)
header_call = ftg2.calls[0][1]
_check("Daily Job Digest" in header_call["text"], "header text present")
_check("filter ≥ *3*/5" in header_call["text"], "header shows current floor")
_check("9 below floor" in header_call["text"], "header shows below-floor count")
_check(header_call["kb"] is not None, "header has keyboard attached")
hb = header_call["kb"]["inline_keyboard"][0] if header_call["kb"] else []
_check(any(b["callback_data"] == "flt:lwr:7:2" for b in hb),
       f"header has ⬇ ≥2 button (got {[b.get('callback_data') for b in hb]})")
_check(any(b["callback_data"] == "flt:rse:4" for b in hb),
       f"header has ⬆ ≥4 button (got {[b.get('callback_data') for b in hb]})")


# ---------------------------------------------------------------------------

print()
print(f"{'PASS' if FAIL == 0 else 'FAIL'}: {PASS}/{PASS+FAIL} tests passed")
sys.exit(0 if FAIL == 0 else 1)
