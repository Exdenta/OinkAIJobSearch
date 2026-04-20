#!/usr/bin/env python3
"""Offline smoke test for profile_builder.py.

Covers every piece that doesn't need the real `claude` CLI:

  1. profile_schema_validate — good fixture passes
  2. profile_schema_validate — 10 hand-written bad profiles each surface
     a specific error
  3. build_profile_sync — fixture-backed Opus stub returns OK
  4. build_profile_sync — stub that returns malformed text → parse_error
  5. build_profile_sync — stub that returns a schema-invalid profile →
     validation_error
  6. build_profile_sync — stub that simulates CLI missing → cli_missing_or_timeout
  7. ProfileBuilderQueue — 5 rapid prefs_change enqueues debounce into 1 call
  8. ProfileBuilderQueue — resume_upload runs immediately (no debounce)
  9. ProfileBuilderQueue — coalesces a trigger arriving during in-flight
 10. _clip_profile — truncates overlong seed_phrases / drops bad ATS domains

No Telegram calls, no network, no real Claude CLI. Exits non-zero on any
assertion failure so CI / the shell smoke step can rely on $?.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import profile_builder as pb  # noqa: E402
from profile_builder import (  # noqa: E402
    BuildResult,
    ProfileBuilderQueue,
    _clip_profile,
    build_profile_sync,
    profile_schema_validate,
    sha1_hex,
)
from db import DB  # noqa: E402


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


# ---------------------------------------------------------------------------
# Good-profile fixture (matches the Example A output from the prompt)
# ---------------------------------------------------------------------------

def _good_profile() -> dict:
    return {
        "schema_version": 2,
        "ideal_fit_paragraph": "Mid-level frontend engineer.",
        "primary_role": "frontend engineer",
        "target_levels": ["mid", "middle"],
        "years_experience": 5,
        "stack_primary": ["vue", "typescript"],
        "stack_secondary": ["react", "jest"],
        "stack_adjacent": ["node"],
        "stack_antipatterns": ["wordpress", "drupal"],
        "title_must_match": ["frontend", "vue", "react"],
        "title_exclude": ["senior", "staff", "backend"],
        "exclude_keywords": ["wordpress", "drupal"],
        "exclude_companies": ["Crossover"],
        "locations": ["bilbao", "spain", "europe"],
        "remote": "remote",
        "time_zone_band": "UTC-1..UTC+3",
        "salary_min_usd": 0,
        "drop_if_salary_unknown": False,
        "language": "english",
        "max_age_hours": 0,
        "min_match_score": 0,
        "search_seeds": {
            "linkedin": {
                "queries": [
                    {"q": "frontend vue developer", "geo": "Spain", "f_TPR": "r86400"},
                    {"q": "react typescript remote", "geo": "European Union", "f_TPR": "r86400"},
                ],
            },
            "web_search": {
                "seed_phrases": ["remote vue frontend europe", "site:greenhouse.io vue"],
                "ats_domains": ["greenhouse.io", "lever.co"],
                "focus_notes": "Prefer EU timezones.",
            },
        },
        "free_text": "remote EU, Vue or React",
    }


# ---------------------------------------------------------------------------
# 1. Good fixture passes
# ---------------------------------------------------------------------------
print("1. profile_schema_validate — good fixture passes")
errs = profile_schema_validate(_good_profile())
check(errs == [], f"good profile is valid (got errs={errs})")


# ---------------------------------------------------------------------------
# 2. Bad profiles (10 cases), each surfaces a specific error
# ---------------------------------------------------------------------------
print("\n2. profile_schema_validate — 10 bad profiles")


def _with(**overrides) -> dict:
    base = _good_profile()
    for k, v in overrides.items():
        if v is pb:  # sentinel for "delete key"
            base.pop(k, None)
        else:
            base[k] = v
    return base


bad_cases = [
    # (label, mutator_fn, substring expected in at least one error)
    ("schema_version wrong",
     lambda: _with(schema_version=1),
     "schema_version"),
    ("remote not in enum",
     lambda: _with(remote="sometimes"),
     "remote must be one of"),
    ("title_must_match not lowercase",
     lambda: _with(title_must_match=["Frontend"]),
     "must be all-lowercase"),
    ("title_exclude item too long",
     lambda: _with(title_exclude=["x" * 50]),
     "40 chars"),
    ("salary out of range",
     lambda: _with(salary_min_usd=-5),
     "salary_min_usd out of sane range"),
    ("stack_primary wrong type",
     lambda: _with(stack_primary="vue, react"),
     "stack_primary must be a list of strings"),
    ("min_match_score out of range",
     lambda: _with(min_match_score=9),
     "min_match_score must be in"),
    ("ats_domains off-allowlist",
     lambda: _with(search_seeds={
         "linkedin": {"queries": []},
         "web_search": {"seed_phrases": ["x"], "ats_domains": ["evil.example"], "focus_notes": ""},
     }),
     "ats_domains contains disallowed"),
    ("linkedin.queries too many",
     lambda: _with(search_seeds={
         "linkedin": {"queries": [
             {"q": "a", "geo": "Spain", "f_TPR": "r86400"},
             {"q": "b", "geo": "Spain", "f_TPR": "r86400"},
             {"q": "c", "geo": "Spain", "f_TPR": "r86400"},
             {"q": "d", "geo": "Spain", "f_TPR": "r86400"},
         ]},
         "web_search": {"seed_phrases": [], "ats_domains": [], "focus_notes": ""},
     }),
     "linkedin.queries length"),
    ("seed_phrase too long",
     lambda: _with(search_seeds={
         "linkedin": {"queries": []},
         "web_search": {"seed_phrases": ["y" * 200], "ats_domains": [], "focus_notes": ""},
     }),
     "seed_phrases length"),
]

for label, mutate, expect_sub in bad_cases:
    errs = profile_schema_validate(mutate())
    check(
        any(expect_sub in e for e in errs),
        f"{label} → error contains {expect_sub!r} (got {errs})",
    )


# ---------------------------------------------------------------------------
# 3. build_profile_sync — happy path with stubbed run_p
# ---------------------------------------------------------------------------
print("\n3. build_profile_sync — happy-path stub")


def _fake_run_p_ok(prompt: str, timeout_s: int, model: str | None = None) -> str:
    # claude_cli expects the CLI's JSON envelope; we return the same shape.
    body = json.dumps(_good_profile(), ensure_ascii=False)
    return json.dumps({"result": body})


res = build_profile_sync(
    "Candidate — 5y Vue/React/TS frontend dev, remote EU.",
    "remote EU or Bilbao, Vue or React, no senior roles",
    _run_p=_fake_run_p_ok,
)
check(res.status == "ok", f"status is ok (got {res.status!r})")
check(res.profile is not None, "profile is present")
if res.profile is not None:
    check(res.profile.get("schema_version") == 2, "schema_version stamped")
    check("built_at" in res.profile, "built_at stamped")
    check(res.profile.get("built_from", {}).get("resume_sha1") == sha1_hex(
        "Candidate — 5y Vue/React/TS frontend dev, remote EU."
    ), "resume_sha1 stamped by us, not the model")
    check(res.profile.get("built_from", {}).get("model") == "opus", "model stamped")


# ---------------------------------------------------------------------------
# 4. build_profile_sync — unparseable response
# ---------------------------------------------------------------------------
print("\n4. build_profile_sync — garbage response → parse_error")


def _fake_run_p_garbage(prompt: str, timeout_s: int, model: str | None = None) -> str:
    return json.dumps({"result": "<html>nope</html>"})


res = build_profile_sync("resume", "prefs", _run_p=_fake_run_p_garbage)
check(res.status == "parse_error", f"status parse_error (got {res.status!r})")
check(res.profile is None, "no profile returned")


# ---------------------------------------------------------------------------
# 5. build_profile_sync — schema-invalid profile
# ---------------------------------------------------------------------------
print("\n5. build_profile_sync — schema-invalid response → validation_error")


def _fake_run_p_bad_schema(prompt: str, timeout_s: int, model: str | None = None) -> str:
    bad = _good_profile()
    bad["remote"] = "sometimes"
    return json.dumps({"result": json.dumps(bad)})


res = build_profile_sync("resume", "prefs", _run_p=_fake_run_p_bad_schema)
check(res.status == "validation_error", f"status validation_error (got {res.status!r})")
check(res.error and "remote" in res.error, f"error surfaces 'remote' (got {res.error!r})")


# ---------------------------------------------------------------------------
# 6. build_profile_sync — run_p returns None (CLI missing / timeout)
# ---------------------------------------------------------------------------
print("\n6. build_profile_sync — run_p returns None → cli_missing_or_timeout")


def _fake_run_p_none(prompt: str, timeout_s: int, model: str | None = None):
    return None


res = build_profile_sync("resume", "prefs", _run_p=_fake_run_p_none)
check(res.status == "cli_missing_or_timeout",
      f"status cli_missing_or_timeout (got {res.status!r})")


# ---------------------------------------------------------------------------
# 7. ProfileBuilderQueue — debounce coalescing
# ---------------------------------------------------------------------------
print("\n7. ProfileBuilderQueue — 5 rapid prefs_change → 1 build call")


def _make_queue(tmpdir: Path, build_calls: list[dict], debounce_s: float = 0.2):
    db = DB(tmpdir / "jobs.db")
    # Pre-register the user so log_profile_build + set_user_profile work.
    db.upsert_user(chat_id=42, username="t", first_name="Test", last_name="")

    def _stub_build(resume_text, free_text, *, timeout_s, model):
        build_calls.append({
            "resume_text": resume_text,
            "free_text": free_text,
            "timeout_s": timeout_s,
            "model": model,
            "at": time.monotonic(),
        })
        return BuildResult(
            status="ok",
            profile=_good_profile() | {"free_text": free_text[:500]},
            elapsed_ms=5,
            resume_sha1=sha1_hex(resume_text),
            prefs_sha1=sha1_hex(free_text),
            model=model,
        )

    q = ProfileBuilderQueue(
        db=db,
        tg=None,
        debounce_s=debounce_s,
        sync_builder=_stub_build,
    )
    return q, db


with tempfile.TemporaryDirectory() as td:
    calls: list[dict] = []
    q, db = _make_queue(Path(td), calls, debounce_s=0.2)
    for i in range(5):
        q.enqueue(42, "resume-text", f"pref #{i}", trigger="prefs_change")
        time.sleep(0.05)  # within the 0.2s debounce window
    ok = q.wait_idle(timeout_s=5.0)
    check(ok, "queue drained within 5s")
    check(len(calls) == 1, f"exactly 1 build call (got {len(calls)})")
    if calls:
        check(calls[0]["free_text"] == "pref #4",
              f"latest inputs won (got {calls[0]['free_text']!r})")


# ---------------------------------------------------------------------------
# 8. ProfileBuilderQueue — resume_upload runs immediately
# ---------------------------------------------------------------------------
print("\n8. ProfileBuilderQueue — resume_upload skips debounce")

with tempfile.TemporaryDirectory() as td:
    calls = []
    q, db = _make_queue(Path(td), calls, debounce_s=5.0)
    t0 = time.monotonic()
    q.enqueue(42, "RESUME v2", "", trigger="resume_upload")
    ok = q.wait_idle(timeout_s=2.0)
    check(ok, "resume_upload drained in <2s despite 5s debounce setting")
    check(len(calls) == 1, f"exactly 1 build (got {len(calls)})")
    if calls:
        check(calls[0]["resume_text"] == "RESUME v2", "immediate build used fresh inputs")


# ---------------------------------------------------------------------------
# 9. ProfileBuilderQueue — trigger arriving during in-flight is coalesced
# ---------------------------------------------------------------------------
print("\n9. ProfileBuilderQueue — in-flight coalescing produces a follow-up build")

with tempfile.TemporaryDirectory() as td:
    calls = []
    db = DB(Path(td) / "jobs.db")
    db.upsert_user(chat_id=42)

    release = threading.Event()

    def _slow_build(resume_text, free_text, *, timeout_s, model):
        # First call blocks until the second enqueue arrives.
        if len(calls) == 0:
            release.wait(timeout=2.0)
        calls.append({"free_text": free_text})
        return BuildResult(
            status="ok",
            profile=_good_profile() | {"free_text": free_text[:500]},
            elapsed_ms=5,
            model=model,
        )

    q = ProfileBuilderQueue(
        db=db, tg=None, debounce_s=0.0,
        sync_builder=_slow_build,
    )
    q.enqueue(42, "resume", "first", trigger="manual")   # starts immediately, blocks
    time.sleep(0.1)
    q.enqueue(42, "resume", "second", trigger="manual")  # coalesced as pending
    release.set()
    ok = q.wait_idle(timeout_s=5.0)
    check(ok, "queue drained")
    check(len(calls) == 2, f"two builds ran sequentially (got {len(calls)})")
    if len(calls) == 2:
        check(calls[0]["free_text"] == "first", "first build had first inputs")
        check(calls[1]["free_text"] == "second", "follow-up used latest inputs")


# ---------------------------------------------------------------------------
# 10. _clip_profile — truncates overlong fields, drops bad ATS, no raise
# ---------------------------------------------------------------------------
print("\n10. _clip_profile — sanitization")

dirty = copy.deepcopy(_good_profile())
dirty["free_text"] = "x" * 1000
dirty["search_seeds"]["web_search"]["seed_phrases"] = ["phrase"] * 20
dirty["search_seeds"]["web_search"]["ats_domains"] = [
    "greenhouse.io", "evil.example", "lever.co", "also-evil.example",
]
dirty["search_seeds"]["linkedin"]["queries"] = [
    {"q": "a", "geo": "Spain", "f_TPR": "r86400"},
    {"q": "b", "geo": "Spain", "f_TPR": "r86400"},
    {"q": "c", "geo": "Spain", "f_TPR": "r86400"},
    {"q": "d", "geo": "Spain", "f_TPR": "r86400"},
]

clean = _clip_profile(dirty)
check(len(clean["free_text"]) <= 500, "free_text clipped to 500 chars")
check(len(clean["search_seeds"]["web_search"]["seed_phrases"]) <= 8,
      "seed_phrases clipped to 8")
check(
    clean["search_seeds"]["web_search"]["ats_domains"] == ["greenhouse.io", "lever.co"],
    "bad ATS domains stripped",
)
check(len(clean["search_seeds"]["linkedin"]["queries"]) == 3,
      "linkedin.queries clipped to 3")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if failures:
    print(f"❌ {len(failures)} failure(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ All profile_builder smoke checks passed.")
