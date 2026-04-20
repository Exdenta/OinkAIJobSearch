#!/usr/bin/env python3
"""Smoke test for fit_analyzer: normalization, rendering, and DB cache.

Covers:
  1. _normalize with valid payload → clean dict with all keys
  2. _normalize with invalid verdict → None (parse failure signal)
  3. _normalize clamps fit_score to [0, 5]
  4. _normalize coerces unknown severity → 'moderate'
  5. _normalize drops non-dict entries from strengths/gaps arrays
  6. render_analysis_mdv2 produces scannable MDv2 with expected sections
  7. render_analysis_mdv2 truncates at max_chars
  8. DB round-trip: upsert → hit with same sha → miss with different sha
  9. delete_fit_analyses wipes the cache

Run:  python skill/job-search/scripts/tests/smoke_fit_analyzer.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import fit_analyzer as fa    # noqa: E402
from db import DB            # noqa: E402


def _assert(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def _valid_payload() -> dict:
    return {
        "verdict": "solid_match",
        "fit_score": 3,
        "headline": "Solid stack match with moderate seniority gap.",
        "strengths": [
            {"area": "React / TypeScript", "evidence": "5 yrs at Acme."},
            {"area": "Design systems",     "evidence": "Built one from scratch."},
        ],
        "gaps": [
            {"area": "GraphQL", "severity": "moderate",
             "evidence": "JD lists it; resume omits.",
             "mitigation": "Do a weekend tutorial + cover in cover letter."},
            {"area": "Years", "severity": "critical",
             "evidence": "JD asks 8+; resume shows 5.",
             "mitigation": "Highlight tech lead moments to show scope."},
        ],
        "hidden_requirements": [
            "Regulated industry — background check likely.",
        ],
        "recommendation": "Apply. Emphasize design-system lead work. "
                          "Acknowledge the years gap directly in the cover letter.",
    }


def main() -> int:
    # ---- 1. valid payload normalizes cleanly ----
    a = fa._normalize(_valid_payload())
    _assert(isinstance(a, dict), "valid payload should normalize to dict")
    _assert(a["verdict"] == "solid_match", "verdict preserved")
    _assert(a["fit_score"] == 3, "fit_score preserved")
    _assert(len(a["strengths"]) == 2, "both strengths preserved")
    _assert(len(a["gaps"]) == 2, "both gaps preserved")
    _assert(a["hidden_requirements"] == ["Regulated industry — background check likely."],
            "hidden_requirements preserved")

    # ---- 2. invalid verdict → None ----
    bad = _valid_payload()
    bad["verdict"] = "probably_ok"   # not in _VALID_VERDICTS
    _assert(fa._normalize(bad) is None,
            "unknown verdict should yield None (caller shows error)")

    # ---- 3. fit_score clamp ----
    hi = _valid_payload(); hi["fit_score"] = 12
    lo = _valid_payload(); lo["fit_score"] = -3
    _assert(fa._normalize(hi)["fit_score"] == 5, "fit_score clamped to 5")
    _assert(fa._normalize(lo)["fit_score"] == 0, "fit_score clamped to 0")

    # ---- 4. unknown severity coerced to 'moderate' ----
    p = _valid_payload()
    p["gaps"][0]["severity"] = "catastrophic"   # not in _VALID_SEVERITIES
    norm = fa._normalize(p)
    _assert(norm["gaps"][0]["severity"] == "moderate",
            "unknown severity should coerce to 'moderate', not drop the row")

    # ---- 5. non-dict entries are dropped ----
    p = _valid_payload()
    p["strengths"] = ["string instead of dict", p["strengths"][0], 42]
    norm = fa._normalize(p)
    _assert(len(norm["strengths"]) == 1,
            f"only the dict entry should survive, got {len(norm['strengths'])}")

    # ---- 6. renderer produces scannable MDv2 with expected sections ----
    rendered = fa.render_analysis_mdv2(a, {"title": "Senior React Engineer",
                                           "company": "Acme Inc"})
    _assert("*Solid match*" in rendered, "verdict label rendered as bold header")
    _assert("3/5" in rendered, "fit score number rendered")
    _assert("*Strengths*" in rendered, "Strengths section present")
    _assert("*Gaps*" in rendered, "Gaps section present")
    _assert("*Watch for*" in rendered, "Watch-for (hidden reqs) section present")
    # Escaped slash should appear in "React / TypeScript"
    _assert("React / TypeScript" in rendered or "React \\/ TypeScript" in rendered,
            "strength text should appear in body")

    # Severity icons should appear somewhere
    _assert("●" in rendered or "◐" in rendered or "○" in rendered,
            "gap severity icons should appear")

    # ---- 7. truncation at max_chars ----
    big = _valid_payload()
    big["recommendation"] = "x" * 10_000
    big_rendered = fa.render_analysis_mdv2(fa._normalize(big),
                                            {"title": "t", "company": "c"},
                                            max_chars=500)
    _assert(len(big_rendered) <= 600,
            f"rendered should be near max_chars, got {len(big_rendered)}")
    _assert("trimmed" in big_rendered,
            "truncated body should note that it was trimmed")

    # ---- 8. DB cache round-trip ----
    with tempfile.TemporaryDirectory() as td:
        db = DB(Path(td) / "jobs.db")
        db.upsert_user(999, first_name="Test")

        # Fresh cache — miss.
        miss = db.get_fit_analysis(999, "job-42", current_resume_sha1="sha-A")
        _assert(miss is None, "empty cache should miss")

        # Insert and look up with matching sha.
        db.upsert_fit_analysis(999, "job-42", json.dumps(a), resume_sha1="sha-A")
        hit = db.get_fit_analysis(999, "job-42", current_resume_sha1="sha-A")
        _assert(hit is not None, "matching sha should hit")
        stored = json.loads(hit["analysis_json"])
        _assert(stored["verdict"] == a["verdict"], "stored analysis round-trips")

        # Look up with DIFFERENT sha → miss (cache invalidated).
        stale = db.get_fit_analysis(999, "job-42", current_resume_sha1="sha-B")
        _assert(stale is None, "different resume sha should miss (cache invalid)")

        # Look up without providing sha → hit (non-strict mode).
        loose = db.get_fit_analysis(999, "job-42")
        _assert(loose is not None, "loose lookup should hit regardless of sha")

        # ---- 9. delete_fit_analyses wipes the cache ----
        n = db.delete_fit_analyses(999)
        _assert(n == 1, f"should delete 1 row, got {n}")
        gone = db.get_fit_analysis(999, "job-42")
        _assert(gone is None, "cache should be empty after delete")

    # ---- 10. resume_sha1 is stable + differs on edits ----
    sha_a = fa.resume_sha1("Alex — React Engineer")
    sha_a2 = fa.resume_sha1("Alex — React Engineer")
    sha_b = fa.resume_sha1("Alex — React Engineer, now with GraphQL")
    _assert(sha_a == sha_a2, "resume_sha1 stable for same input")
    _assert(sha_a != sha_b, "resume_sha1 differs when resume changes")
    _assert(len(sha_a) == 40, "resume_sha1 is a full sha1 hex digest")

    print("PASS  — 22 assertions across normalize, render, truncation, "
          "DB cache round-trip, and resume-hash invalidation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
