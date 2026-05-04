#!/usr/bin/env python3
"""Offline smoke test for skip_feedback.py.

Covers the cases that don't need the real `claude` CLI:

  1. Fresh profile (None) → all four lists merged in, set_user_profile called.
  2. Pre-existing duplicate → dedupe, added list is empty, profile unchanged
     for that field.
  3. Runner returns None (parse error / CLI missing) → fallback summary,
     profile NOT modified.
  4. List cap (50 items) — pre-existing 50 items, new addition kicks out
     the oldest (FIFO).
  5. Case-insensitive dedupe — pre-existing ['FinTech'], new 'fintech' →
     not added.

No Telegram, no network, no real Claude CLI. Exits non-zero on any
assertion failure so CI / the shell smoke step can rely on $?.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

from db import DB  # noqa: E402
import skip_feedback as sf  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


def _envelope(payload: dict) -> str:
    """Wrap a JSON dict in the `claude -p --output-format json` envelope
    that `extract_assistant_text` knows how to unwrap."""
    return json.dumps({"result": json.dumps(payload, ensure_ascii=False)})


def _make_runner(payload: dict | None):
    """Return a stub `_run_p` that records calls and returns the canned
    envelope (or None if payload is None to simulate CLI missing)."""
    calls: list[dict] = []

    def _runner(prompt: str, *, timeout_s: int, model: str | None = None) -> str | None:
        calls.append({
            "prompt_chars": len(prompt or ""),
            "prompt_head": (prompt or "")[:200],
            "timeout_s": timeout_s,
            "model": model,
        })
        if payload is None:
            return None
        return _envelope(payload)

    return _runner, calls


def _fresh_db() -> tuple[DB, int]:
    """Spin up a temp DB and pre-register a user. Returns (db, chat_id).
    The temp dir is intentionally NOT cleaned up so we can inspect on
    failure — the OS cleans /tmp eventually."""
    td = tempfile.mkdtemp(prefix="skip_feedback_smoke_")
    db = DB(Path(td) / "jobs.db")
    chat_id = 99001
    db.upsert_user(chat_id=chat_id, username="t", first_name="T", last_name="")
    return db, chat_id


JOB_CTX = {
    "job_id":  "job-abc-123",
    "title":   "Senior Backend Engineer (Fintech)",
    "company": "Acme Crypto Bank",
    "source":  "linkedin",
    "url":     "https://www.linkedin.com/jobs/view/9999999/",
    "snippet": "Crypto-friendly fintech building a stablecoin payments rail.",
}


# ---------------------------------------------------------------------------
# Test 1 — fresh profile, all four lists merge correctly
# ---------------------------------------------------------------------------
print("1. fresh profile → all four lists merged in")

db, cid = _fresh_db()
runner, calls = _make_runner({
    "title_excludes_to_add":     ["fintech"],
    "exclude_keywords_to_add":   ["crypto", "stablecoin"],
    "exclude_companies_to_add":  ["Acme Crypto Bank"],
    "stack_antipatterns_to_add": ["solidity"],
    "summary": "Excluding fintech / crypto roles.",
})

# Manually set a profile so set_user_profile has something to bump revision off.
db.set_user_profile(cid, json.dumps({
    "schema_version": 2,
    "title_exclude": [],
    "exclude_keywords": [],
    "exclude_companies": [],
    "stack_antipatterns": [],
    "primary_role": "backend",
}))

result = sf.apply_skip_feedback(
    db, cid, JOB_CTX,
    "I'm not interested in fintech / crypto roles, and Acme Crypto Bank in particular.",
    _run_p=runner,
)

check(len(calls) == 1, f"runner called once (got {len(calls)})")
check(result["added_title_excludes"]    == ["fintech"],          f"title excludes (got {result['added_title_excludes']})")
check(result["added_exclude_keywords"]  == ["crypto", "stablecoin"], f"keywords (got {result['added_exclude_keywords']})")
check(result["added_exclude_companies"] == ["Acme Crypto Bank"], f"companies (got {result['added_exclude_companies']})")
check(result["added_stack_antipatterns"]== ["solidity"],         f"antipatterns (got {result['added_stack_antipatterns']})")
check("fintech" in (result["summary"] or "").lower(),            f"summary mentions excluded thing (got {result['summary']!r})")

raw = db.get_user_profile(cid)
check(raw is not None, "profile persisted")
if raw:
    saved = json.loads(raw)
    check(saved["title_exclude"]      == ["fintech"],          f"saved title_exclude {saved['title_exclude']}")
    check(saved["exclude_keywords"]   == ["crypto", "stablecoin"], f"saved exclude_keywords {saved['exclude_keywords']}")
    check(saved["exclude_companies"]  == ["Acme Crypto Bank"], f"saved exclude_companies {saved['exclude_companies']}")
    check(saved["stack_antipatterns"] == ["solidity"],         f"saved stack_antipatterns {saved['stack_antipatterns']}")
    # Other fields preserved
    check(saved.get("primary_role") == "backend", "primary_role preserved (we only touched excludes)")


# ---------------------------------------------------------------------------
# Test 2 — pre-existing 'fintech' → dedupe; added_title_excludes == []
# ---------------------------------------------------------------------------
print("\n2. existing 'fintech' → no addition (dedupe)")

db, cid = _fresh_db()
db.set_user_profile(cid, json.dumps({
    "schema_version": 2,
    "title_exclude": ["fintech"],
    "exclude_keywords": ["crypto"],
    "exclude_companies": [],
    "stack_antipatterns": [],
}))
runner, calls = _make_runner({
    "title_excludes_to_add":     ["fintech"],     # duplicate
    "exclude_keywords_to_add":   ["crypto", "defi"],
    "exclude_companies_to_add":  [],
    "stack_antipatterns_to_add": [],
    "summary": "Excluding fintech.",
})

result = sf.apply_skip_feedback(db, cid, JOB_CTX, "fintech please no", _run_p=runner)
check(result["added_title_excludes"]    == [],         f"title_excludes empty (got {result['added_title_excludes']})")
check(result["added_exclude_keywords"]  == ["defi"],   f"only 'defi' added (got {result['added_exclude_keywords']})")
check(result["added_exclude_companies"] == [],         "no companies")
check(result["added_stack_antipatterns"]== [],         "no antipatterns")

saved = json.loads(db.get_user_profile(cid))
check(saved["title_exclude"]    == ["fintech"],         f"title_exclude unchanged (got {saved['title_exclude']})")
check(saved["exclude_keywords"] == ["crypto", "defi"],  f"exclude_keywords got 'defi' appended (got {saved['exclude_keywords']})")


# ---------------------------------------------------------------------------
# Test 3 — runner returns None → parse-error fallback, profile untouched
# ---------------------------------------------------------------------------
print("\n3. runner returns None → fallback summary, profile NOT modified")

db, cid = _fresh_db()
prior = {
    "schema_version": 2,
    "title_exclude": ["aaa"],
    "exclude_keywords": ["bbb"],
    "exclude_companies": ["Ccc"],
    "stack_antipatterns": ["ddd"],
    "primary_role": "frontend",
}
db.set_user_profile(cid, json.dumps(prior))

runner, calls = _make_runner(None)
result = sf.apply_skip_feedback(db, cid, JOB_CTX, "I just don't like this", _run_p=runner)

check(result["added_title_excludes"]    == [], "no title additions")
check(result["added_exclude_keywords"]  == [], "no keyword additions")
check(result["added_exclude_companies"] == [], "no company additions")
check(result["added_stack_antipatterns"]== [], "no antipattern additions")
check(isinstance(result["summary"], str) and len(result["summary"]) > 0,
      f"non-empty fallback summary (got {result['summary']!r})")

saved = json.loads(db.get_user_profile(cid))
check(saved == prior, "profile JSON byte-equal to prior (untouched)")


# ---------------------------------------------------------------------------
# Test 4 — list cap (50) with FIFO eviction
# ---------------------------------------------------------------------------
print("\n4. list cap (50) — FIFO eviction kicks out oldest item")

db, cid = _fresh_db()
seed_keywords = [f"kw{i:02d}" for i in range(50)]   # 50 items, kw00..kw49
db.set_user_profile(cid, json.dumps({
    "schema_version": 2,
    "title_exclude": [],
    "exclude_keywords": list(seed_keywords),
    "exclude_companies": [],
    "stack_antipatterns": [],
}))

runner, calls = _make_runner({
    "title_excludes_to_add":     [],
    "exclude_keywords_to_add":   ["kw99"],
    "exclude_companies_to_add":  [],
    "stack_antipatterns_to_add": [],
    "summary": "Adding kw99.",
})

result = sf.apply_skip_feedback(db, cid, JOB_CTX, "skip plz", _run_p=runner)
check(result["added_exclude_keywords"] == ["kw99"], f"new addition reported (got {result['added_exclude_keywords']})")

saved = json.loads(db.get_user_profile(cid))
saved_kw = saved["exclude_keywords"]
check(len(saved_kw) == 50, f"length still 50 (got {len(saved_kw)})")
check(saved_kw[-1] == "kw99",  f"new item at tail (got {saved_kw[-1]!r})")
check("kw00" not in saved_kw,  f"oldest 'kw00' evicted (full list head: {saved_kw[:3]})")
check(saved_kw[0] == "kw01",   f"new head is 'kw01' (got {saved_kw[0]!r})")


# ---------------------------------------------------------------------------
# Test 5 — case-insensitive dedupe
# ---------------------------------------------------------------------------
print("\n5. case-insensitive dedupe — 'FinTech' vs 'fintech'")

db, cid = _fresh_db()
db.set_user_profile(cid, json.dumps({
    "schema_version": 2,
    "title_exclude": ["FinTech"],   # mixed case
    "exclude_keywords": [],
    "exclude_companies": ["Acme Corp"],   # company case-preserve
    "stack_antipatterns": [],
}))

runner, calls = _make_runner({
    "title_excludes_to_add":     ["fintech"],   # would dup case-insensitive
    "exclude_keywords_to_add":   [],
    "exclude_companies_to_add":  ["acme corp"], # case-different dup
    "stack_antipatterns_to_add": [],
    "summary": "Already excluded.",
})

result = sf.apply_skip_feedback(db, cid, JOB_CTX, "no fintech", _run_p=runner)
check(result["added_title_excludes"]    == [], f"no title addition (got {result['added_title_excludes']})")
check(result["added_exclude_companies"] == [], f"no company addition (got {result['added_exclude_companies']})")

saved = json.loads(db.get_user_profile(cid))
check(saved["title_exclude"]     == ["FinTech"],    f"title_exclude unchanged (got {saved['title_exclude']})")
check(saved["exclude_companies"] == ["Acme Corp"],  f"exclude_companies unchanged (got {saved['exclude_companies']})")


# ---------------------------------------------------------------------------
# Bonus: empty reason short-circuits without calling the runner
# ---------------------------------------------------------------------------
print("\n6. empty reason → no runner call, generic summary")

db, cid = _fresh_db()
db.set_user_profile(cid, json.dumps({
    "schema_version": 2,
    "title_exclude": [],
    "exclude_keywords": [],
    "exclude_companies": [],
    "stack_antipatterns": [],
}))
runner, calls = _make_runner({"title_excludes_to_add": ["should-not-fire"]})
result = sf.apply_skip_feedback(db, cid, JOB_CTX, "   ", _run_p=runner)
check(len(calls) == 0, f"runner NOT called for empty reason (got {len(calls)})")
check(result["added_title_excludes"] == [], "no additions")


# ---------------------------------------------------------------------------
# Bonus: no profile in DB at all → stub created with the new excludes
# ---------------------------------------------------------------------------
print("\n7. no prior profile → stub built with new excludes, persisted")

db, cid = _fresh_db()   # no set_user_profile call → users.user_profile is NULL
runner, calls = _make_runner({
    "title_excludes_to_add":     ["fintech"],
    "exclude_keywords_to_add":   [],
    "exclude_companies_to_add":  [],
    "stack_antipatterns_to_add": [],
    "summary": "Excluding fintech.",
})
result = sf.apply_skip_feedback(db, cid, JOB_CTX, "no fintech", _run_p=runner)
check(result["added_title_excludes"] == ["fintech"], "addition reported")
raw = db.get_user_profile(cid)
check(raw is not None, "stub profile written")
if raw:
    saved = json.loads(raw)
    check(saved.get("title_exclude") == ["fintech"], f"stub has the addition (got {saved.get('title_exclude')})")


# ---------------------------------------------------------------------------
# Test 8 — structured-intent override: location reason → /prefs hint
# ---------------------------------------------------------------------------
print("\n8. structured intent (location) + zero extracts → /prefs hint surfaces")

db, cid = _fresh_db()
db.set_user_profile(cid, json.dumps({
    "schema_version": 2,
    "title_exclude": [],
    "exclude_keywords": [],
    "exclude_companies": [],
    "stack_antipatterns": [],
}))
# Haiku correctly returns nothing (rule 4: location belongs to profile).
# Without the override, the user would see a confusing model-summary
# paraphrase. With it, they see an actionable /prefs pointer.
runner, calls = _make_runner({
    "title_excludes_to_add":     [],
    "exclude_keywords_to_add":   [],
    "exclude_companies_to_add":  [],
    "stack_antipatterns_to_add": [],
    "summary": "Location belongs to profile, not excludes.",
})
result = sf.apply_skip_feedback(
    db, cid, JOB_CTX,
    "I do not want jobs in Berlin",
    _run_p=runner,
)
check(result["added_title_excludes"] == [], "no excludes added (location feedback)")
check("/prefs" in (result["summary"] or ""),
      f"summary points user to /prefs (got {result['summary']!r})")
check("location" in (result["summary"] or "").lower(),
      f"summary names the structured field (got {result['summary']!r})")


print("\n9. structured intent (salary) + zero extracts → salary hint")
db, cid = _fresh_db()
db.set_user_profile(cid, json.dumps({"schema_version": 2}))
runner, calls = _make_runner({
    "title_excludes_to_add":     [],
    "exclude_keywords_to_add":   [],
    "exclude_companies_to_add":  [],
    "stack_antipatterns_to_add": [],
    "summary": "Salary feedback isn't an exclude.",
})
result = sf.apply_skip_feedback(
    db, cid, JOB_CTX,
    "Salary too low",
    _run_p=runner,
)
check("salary" in (result["summary"] or "").lower(),
      f"salary hint surfaces (got {result['summary']!r})")
check("/prefs" in (result["summary"] or ""), "salary hint links /prefs")


print("\n9b. real-world phrasing 'office jobs in usa' → hint fires (regression)")
db, cid = _fresh_db()
db.set_user_profile(cid, json.dumps({"schema_version": 2}))
runner, calls = _make_runner({
    "title_excludes_to_add":     [],
    "exclude_keywords_to_add":   [],
    "exclude_companies_to_add":  [],
    "stack_antipatterns_to_add": [],
    "summary": "No filter tokens added - location preference belongs to the location profile field, not body-text excludes.",
})
result = sf.apply_skip_feedback(
    db, cid, JOB_CTX,
    "i don't want office jobs in usa",
    _run_p=runner,
)
check("/prefs" in (result["summary"] or ""),
      f"hint fires for real-world reason (got {result['summary']!r})")


print("\n9c. unrecognized reason + zero extracts → generic /prefs fallback")
db, cid = _fresh_db()
db.set_user_profile(cid, json.dumps({"schema_version": 2}))
runner, calls = _make_runner({
    "title_excludes_to_add":     [],
    "exclude_keywords_to_add":   [],
    "exclude_companies_to_add":  [],
    "stack_antipatterns_to_add": [],
    "summary": "Got it - feedback noted but no specific filter to add.",
})
result = sf.apply_skip_feedback(
    db, cid, JOB_CTX,
    "this just isn't quite right somehow",
    _run_p=runner,
)
check("/prefs" in (result["summary"] or ""),
      f"generic fallback still points to /prefs (got {result['summary']!r})")


print("\n10. structured intent does NOT override when extracts succeed")
db, cid = _fresh_db()
db.set_user_profile(cid, json.dumps({"schema_version": 2}))
runner, calls = _make_runner({
    "title_excludes_to_add":     ["fintech"],
    "exclude_keywords_to_add":   [],
    "exclude_companies_to_add":  [],
    "stack_antipatterns_to_add": [],
    "summary": "Excluding fintech.",
})
# Reason mentions "remote" (intent trigger) but the extract fired anyway.
# Override must NOT clobber a successful extract — user got real value.
result = sf.apply_skip_feedback(
    db, cid, JOB_CTX,
    "no fintech, also prefer remote roles",
    _run_p=runner,
)
check(result["added_title_excludes"] == ["fintech"], "extract still applied")
check("/prefs" not in (result["summary"] or ""),
      f"override skipped because extracts succeeded (got {result['summary']!r})")


# ---------------------------------------------------------------------------
print()
if failures:
    print(f"FAIL {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK All skip_feedback smoke checks passed.")
