#!/usr/bin/env python3
"""Algorithm-v2 smoke checks: file IO, score floor on DB column, enrich
prompt accepts raw resume + raw prefs and emits why_mismatch.

Run from project root:
    python skill/job-search/scripts/tests/smoke_v2_files.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _assert(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


# ---------------------------------------------------------------------------
# 1. user_files: write/read/append round-trip
# ---------------------------------------------------------------------------
section("1. user_files: round-trip resume.txt + prefs.txt + skip-note append")

with tempfile.TemporaryDirectory() as td:
    os.environ["STATE_DIR"] = td
    # Force re-import so user_files picks up the new STATE_DIR
    for mod in ("user_files",):
        sys.modules.pop(mod, None)
    import user_files

    user_files.write_resume(123, "alex resume body")
    _assert(user_files.read_resume(123) == "alex resume body",
            "resume round-trip")

    user_files.write_prefs(123, "wants frontend remote eu")
    _assert(user_files.read_prefs(123) == "wants frontend remote eu",
            "prefs round-trip")

    user_files.append_skip_note(123, "no senior")
    user_files.append_skip_note(123, "no german")
    body = user_files.read_prefs(123)
    _assert("wants frontend remote eu" in body, "prefs body preserved")
    _assert("[Recent 'not a fit' comments]" in body, "skip header inserted")
    _assert("- no senior" in body and "- no german" in body,
            "both skip notes appended")

    # Empty append is a no-op
    pre = user_files.read_prefs(123)
    user_files.append_skip_note(123, "")
    user_files.append_skip_note(123, "   ")
    _assert(user_files.read_prefs(123) == pre, "empty/whitespace skip is no-op")


# ---------------------------------------------------------------------------
# 2. DB col min_match_score: clamp + survives profile JSON rewrites
# ---------------------------------------------------------------------------
section("2. DB col min_match_score: clamp + persists across profile rewrites")

with tempfile.TemporaryDirectory() as td:
    os.environ["STATE_DIR"] = td
    for mod in ("db",):
        sys.modules.pop(mod, None)
    from db import DB

    db = DB(Path(td) / "jobs.db")
    db.upsert_user(42)

    db.set_min_match_score(42, 4)
    _assert(db.get_min_match_score(42) == 4, "score=4 persisted")

    db.set_min_match_score(42, 99)  # over-clamp
    _assert(db.get_min_match_score(42) == 5, "score clamps to 5")

    db.set_min_match_score(42, -3)  # under-clamp
    _assert(db.get_min_match_score(42) == 0, "score clamps to 0")

    # Restore to 4, then rewrite profile JSON — score column must NOT be
    # touched by that rewrite.
    db.set_min_match_score(42, 4)
    db.set_user_profile(42, json.dumps({
        "schema_version": 4,
        "primary_role": "frontend engineer",
        "search_seeds": {"linkedin": {"queries": []},
                         "web_search": {"seed_phrases": [], "ats_domains": [], "focus_notes": ""}},
    }))
    _assert(db.get_min_match_score(42) == 4,
            "score survives profile JSON rewrite (the v1 leak we fixed)")


# ---------------------------------------------------------------------------
# 3. enrich_jobs_ai: new signature with raw prefs_text + why_mismatch passthrough
# ---------------------------------------------------------------------------
section("3. enrich_jobs_ai: raw prefs_text + why_mismatch in verdict")


@dataclass
class _FakeJob:
    external_id: str
    title: str = "Senior Backend Engineer"
    company: str = "Acme"
    location: str = "Remote · USA"
    salary: str = ""
    url: str = "https://example.com"
    snippet: str = "Backend Python; 8+ years required."

    @property
    def job_id(self) -> str:
        return f"jid-{self.external_id}"

    @property
    def source(self) -> str:
        return "smoke"


def _envelope(inner: dict) -> str:
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(inner),
    })


with tempfile.TemporaryDirectory() as td:
    os.environ["STATE_DIR"] = td
    for mod in ("forensic", "job_enrich"):
        sys.modules.pop(mod, None)
    import job_enrich

    captured_prompts: list[str] = []

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        captured_prompts.append(prompt)
        return _envelope({
            "results": [{
                "id": "ext-1",
                "match_score": 0,
                "why_match": "Backend role; no overlap with frontend ask.",
                "why_mismatch": "title-exclude hit: 'senior'; remote USA outside EU bands",
                "key_details": {
                    "stack": "Python", "seniority": "senior",
                    "remote_policy": "remote", "location": "USA",
                    "salary": "", "visa_support": "",
                    "language": "English", "standout": "",
                },
            }]
        })

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    jobs = [_FakeJob(external_id="ext-1")]
    verdicts = job_enrich.enrich_jobs_ai(
        jobs,
        resume_text="alex frontend react typescript bilbao",
        prefs_text=("Mid-level frontend EU remote.\n"
                    "[Recent 'not a fit' comments]\n"
                    "- no senior"),
        timeout_s=60,
    )

    _assert(len(verdicts) == 1, "one verdict back")
    v = verdicts["ext-1"]
    _assert(v["match_score"] == 0, "score 0 (HARD VETO)")
    _assert("title-exclude hit" in v["why_mismatch"],
            "why_mismatch carries the veto reason")
    _assert("Backend" in v["why_match"],
            "why_match still populated (separate field)")

    # Prompt should embed both the resume and the prefs verbatim.
    _assert(captured_prompts, "CLI was called")
    p = captured_prompts[0]
    _assert("alex frontend react typescript bilbao" in p,
            "resume blob present in prompt")
    _assert("Recent 'not a fit' comments" in p,
            "prefs.txt skip-block present in prompt")
    _assert("=== CANDIDATE PREFS" in p,
            "prefs section header present (raw text, no JSON projection)")


# ---------------------------------------------------------------------------
# 4. Default config: two_pass=False, batch=5
# ---------------------------------------------------------------------------
section("4. defaults.py: v2 toggles")

for mod in ("defaults",):
    sys.modules.pop(mod, None)
from defaults import DEFAULTS  # noqa: E402

_assert(DEFAULTS.get("ai_two_pass") is False, "ai_two_pass defaults False in v2")
_assert(DEFAULTS.get("ai_max_jobs_per_call") == 10,
        f"ai_max_jobs_per_call=10 (got {DEFAULTS.get('ai_max_jobs_per_call')})")
_assert(DEFAULTS.get("ai_pre_enrich_liveness") is False,
        "ai_pre_enrich_liveness defaults off (liveness moved to send-time in v2.1)")


print("\nAll algorithm-v2 smoke checks passed.")
