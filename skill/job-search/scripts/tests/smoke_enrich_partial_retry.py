#!/usr/bin/env python3
"""Smoke test for `enrich_jobs_ai` partial-batch targeted re-ask.

Reproduces the failure mode observed in the 2026-05-02 10:00-10:30 CEST
cron run: Haiku consistently dropped exactly 1 verdict per 25-job batch
(every user, batch 4/5). The original code logged this as
`failure_reason=partial` and moved on without retrying — silently losing
1 verdict per user per run.

After the fix, a `partial` outcome triggers a single TARGETED re-ask
containing ONLY the missing external_ids. We verify:

  1. A simulated batch where the model omits 1 of 25 verdicts ends up
     with all 25 captured after the targeted re-ask.
  2. The retry call's prompt contains only the missing job (not all 25).
  3. The forensic log records a primary batch (retry_depth=0,
     reason=partial → reclassified ok) and a sub-call line at
     retry_depth=1 with batch_size=1.
  4. Retry is capped at 1 — if the re-ask itself returns nothing, we
     stop instead of looping.

Run with:
    cd <worktree-root> && python3 skill/job-search/scripts/tests/smoke_enrich_partial_retry.py
"""
from __future__ import annotations

import json
import logging
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
# Test fixtures
# ---------------------------------------------------------------------------

@dataclass
class _FakeJob:
    external_id: str
    title: str = "Senior MLOps Engineer"
    company: str = "Acme Corp"
    location: str = "Remote"
    salary: str = ""
    url: str = "https://example.com/job"
    snippet: str = "We need MLOps. Python, AWS, Docker, MLflow."

    @property
    def job_id(self) -> str:
        return f"jid-{self.external_id}"

    @property
    def source(self) -> str:
        return "smoke"


def _make_jobs(n: int) -> list[_FakeJob]:
    return [_FakeJob(external_id=f"ext-{i:04d}") for i in range(n)]


def _envelope(inner: dict) -> str:
    """Wrap an inner JSON payload in the CLI envelope shape."""
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(inner),
    })


def _full_response(present_jobs: list[_FakeJob]) -> str:
    return _envelope({
        "results": [
            {
                "id": j.external_id,
                "match_score": 3,
                "why_match": "ok",
                "key_details": {
                    "stack": "python", "seniority": "senior",
                    "remote_policy": "remote", "location": "",
                    "salary": "", "visa_support": "",
                    "language": "", "standout": "",
                },
            }
            for j in present_jobs
        ]
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_partial_recovered_via_targeted_reask() -> None:
    section("1. partial batch (1 verdict dropped) recovered via targeted re-ask")

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(25)  # one batch of 25 — same shape as the buggy run.
    dropped_id = jobs[17].external_id  # arbitrary mid-list omission

    call_log: list[list[str]] = []  # external_ids each call sees

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        call_log.append([j.external_id for j in present])
        # First call: simulate Haiku dropping `dropped_id` mid-list.
        # Subsequent calls: return verdicts for whatever is in the prompt.
        if len(call_log) == 1:
            return _full_response([j for j in present if j.external_id != dropped_id])
        return _full_response(present)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    captured: list[tuple[int, str]] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append((record.levelno, record.getMessage()))

    handler = _ListHandler()
    job_enrich.log.addHandler(handler)
    job_enrich.log.setLevel(logging.DEBUG)

    try:
        verdicts = job_enrich.enrich_jobs_ai(
            jobs=jobs,
            resume_text="MLOps engineer, 8 years Python and AWS.",
            timeout_s=60,
            projected_prefs={"keywords": ["mlops"]},
        )
    finally:
        job_enrich.log.removeHandler(handler)

    # --- Verdict map ---
    _assert(
        len(verdicts) == 25,
        f"all 25 verdicts captured after targeted re-ask (got {len(verdicts)})",
    )
    _assert(
        dropped_id in verdicts,
        f"the originally-dropped id {dropped_id!r} is now in the verdict map",
    )

    # --- Call shape ---
    _assert(
        len(call_log) == 2,
        f"exactly 2 CLI calls: 1 primary + 1 targeted re-ask (got {len(call_log)})",
    )
    _assert(
        len(call_log[0]) == 25,
        f"primary call saw all 25 jobs (got {len(call_log[0])})",
    )
    _assert(
        call_log[1] == [dropped_id],
        f"re-ask prompt contained ONLY the missing id (got {call_log[1]})",
    )

    # --- Forensic stream ---
    log_dir = Path(td) / "forensic_logs"
    raw = (log_dir / "log.0.jsonl").read_text().splitlines()
    batch_lines = [
        json.loads(line) for line in raw
        if json.loads(line).get("op") == "enrich_jobs_ai.batch"
    ]
    primary = [b for b in batch_lines if b["input"]["retry_depth"] == 0]
    retries = [b for b in batch_lines if b["input"]["retry_depth"] >= 1]
    _assert(
        len(primary) == 1 and primary[0]["input"]["batch_size"] == 25,
        f"1 primary forensic line for the 25-job batch (got {len(primary)})",
    )
    _assert(
        len(retries) == 1 and retries[0]["input"]["batch_size"] == 1,
        f"1 retry forensic line at batch_size=1 (got "
        f"{[r['input']['batch_size'] for r in retries]})",
    )
    _assert(
        retries[0]["output"]["verdicts_returned"] == 1,
        f"retry recovered 1 verdict (got "
        f"{retries[0]['output']['verdicts_returned']})",
    )

    # --- No spurious "batch failed" WARN ---
    failure_warnings = [m for lvl, m in captured
                        if lvl >= logging.WARNING and "batch(es) failed" in m]
    _assert(
        len(failure_warnings) == 0,
        f"no 'batch(es) failed' WARN after successful retry "
        f"(got {failure_warnings})",
    )


def test_partial_retry_is_capped_at_one() -> None:
    section("2. retry capped at 1: re-ask that also returns nothing stops")

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(25)
    dropped_id = jobs[5].external_id

    call_log: list[list[str]] = []

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        call_log.append([j.external_id for j in present])
        # Always omit `dropped_id` — even the targeted re-ask returns
        # nothing for it. Shouldn't loop.
        return _full_response([j for j in present if j.external_id != dropped_id])

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        timeout_s=60,
        projected_prefs={"keywords": ["mlops"]},
    )

    _assert(
        len(call_log) == 2,
        f"retry capped at 1 — exactly 2 calls (got {len(call_log)})",
    )
    _assert(
        len(verdicts) == 24 and dropped_id not in verdicts,
        f"24 verdicts captured; the unrecoverable one is missing "
        f"(got {len(verdicts)} verdicts, dropped present={dropped_id in verdicts})",
    )


def test_full_batch_no_retry() -> None:
    section("3. happy path: full 25/25 → no retry call")

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(25)
    call_count = [0]

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        call_count[0] += 1
        present = [j for j in jobs if j.external_id in prompt]
        return _full_response(present)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        timeout_s=60,
        projected_prefs={"keywords": ["mlops"]},
    )

    _assert(
        call_count[0] == 1,
        f"happy path uses 1 CLI call only (got {call_count[0]})",
    )
    _assert(
        len(verdicts) == 25,
        f"all 25 verdicts captured (got {len(verdicts)})",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        test_partial_recovered_via_targeted_reask()
        test_partial_retry_is_capped_at_one()
        test_full_batch_no_retry()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print("\nAll partial-retry smoke checks passed.")
