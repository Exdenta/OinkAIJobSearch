#!/usr/bin/env python3
"""Smoke test for `enrich_jobs_ai` batch-failure handling.

Reproduces the bug seen in pipeline_run #12: one of N batches returned an
empty result from Claude, the verdicts for those 25 jobs were silently
dropped, and the per-user "enriched X/Y" log line under-reported the loss
without flagging it as a failure.

What we assert here:

  1. When wrapped_run_p returns None for batch 2 of 4, enrich_jobs_ai
     still returns the verdicts for batches 1, 3, 4. The retry-on-split
     logic re-tries batch 2 as two halves; if those halves also fail
     (we wire the fake to keep returning None for the originally-failing
     postings), the batch is counted as failed and the postings are
     marked missing rather than silently filled.
  2. The function emits a per-batch forensic line (op =
     `enrich_jobs_ai.batch`) for each batch with the failure reason and
     missing_count, so post-hoc analysis is one-line-per-batch.
  3. The orchestrator-level WARN log fires when at least one batch failed.

Run with:
    cd <worktree-root> && python3 skill/job-search/scripts/tests/smoke_enrich_batch_failure.py
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
# Test setup
# ---------------------------------------------------------------------------

@dataclass
class _FakeJob:
    """Stand-in for `dedupe.Job` — only the attributes job_enrich reads."""
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


def _build_fake_response(chunk: list[_FakeJob]) -> str:
    """Build a CLI envelope JSON string with a happy-path `result` payload
    that matches the prompt contract: {"results": [{"id": ..., ...}]}."""
    inner = {
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
            for j in chunk
        ]
    }
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps(inner),
    }
    return json.dumps(envelope)


def _empty_result_envelope() -> str:
    """Mirror the failure mode observed in pipeline_run #12, claude_calls #23:
    CLI succeeded but `result` field was empty."""
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_one_batch_fails_others_succeed() -> None:
    section("1. batch 2 of 4 fails (empty result) — others succeed")

    # Isolate forensic logs to a temp dir so we can read the batch lines
    # written by this run.
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)
    os.environ.pop("FORENSIC_FULL", None)

    # Force re-import so STATE_DIR sticks.
    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]

    import job_enrich

    jobs = _make_jobs(96)  # 4 chunks of 25,25,25,21 with default chunk size 25.
    # Sanity check: at the default cap of 25, this gives us 4 batches —
    # the same shape as the buggy run.
    chunks_expected = [jobs[0:25], jobs[25:50], jobs[50:75], jobs[75:96]]
    assert len(chunks_expected) == 4

    # Track every prompt the fake sees so we can assert about retries.
    seen_prompts: list[str] = []

    # Identify which batch a prompt belongs to by scanning for the first
    # external_id it contains. Brittle but explicit; matches our fixture.
    def _which_batch(prompt: str) -> int:
        for idx, ch in enumerate(chunks_expected, start=1):
            if ch[0].external_id in prompt:
                return idx
        return -1

    # The set of external_ids that should ALWAYS fail — even when
    # retried as halves. Used to simulate "this batch's prompt is just
    # poisoned somehow" and confirm the orchestrator correctly counts
    # it as failed (vs. silently filling).
    poisoned_ids = {j.external_id for j in chunks_expected[1]}

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        seen_prompts.append(prompt)
        # Identify whether this prompt contains any poisoned id.
        if any(eid in prompt for eid in poisoned_ids):
            return _empty_result_envelope()
        # Otherwise build a happy-path response containing every id we
        # see in the prompt (covers both whole-batch and split-retry paths).
        present_jobs = [j for j in jobs if j.external_id in prompt]
        return _build_fake_response(present_jobs)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    # Capture log records to assert on the orchestrator-level WARN line.
    captured_warnings: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.levelno >= logging.WARNING:
                captured_warnings.append(record.getMessage())

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

    # --- Assertions on the verdict map ---
    expected_ids = {
        j.external_id for batch in (chunks_expected[0], chunks_expected[2], chunks_expected[3])
        for j in batch
    }
    got_ids = set(verdicts.keys())
    _assert(
        got_ids == expected_ids,
        f"verdicts cover exactly the 3 successful batches: "
        f"got={len(got_ids)} expected={len(expected_ids)} "
        f"missing={len(expected_ids - got_ids)} extra={len(got_ids - expected_ids)}",
    )
    _assert(
        not any(eid in got_ids for eid in poisoned_ids),
        "no verdicts for the poisoned (failing) batch",
    )

    # --- Assertions on the per-batch forensic stream ---
    log_dir = Path(td) / "forensic_logs"
    log_files = sorted(log_dir.glob("log.*.jsonl"))
    _assert(len(log_files) >= 1, f"forensic log file exists: {log_files}")
    raw = log_files[0].read_text().splitlines()
    batch_lines = [
        json.loads(line) for line in raw
        if json.loads(line).get("op") == "enrich_jobs_ai.batch"
    ]
    # 4 primary batches + 2 split-retry sub-batches for the failing one = 6.
    primary = [b for b in batch_lines if b["input"]["retry_depth"] == 0]
    retries = [b for b in batch_lines if b["input"]["retry_depth"] >= 1]
    _assert(
        len(primary) == 4,
        f"4 primary batch forensic lines (got {len(primary)})",
    )
    _assert(
        len(retries) == 2,
        f"2 split-retry forensic lines for the failing batch "
        f"(got {len(retries)})",
    )

    # The failing primary batch must declare its reason explicitly.
    failed_primary = [
        b for b in primary
        if b["output"]["failure_reason"] != "ok"
    ]
    _assert(
        len(failed_primary) == 1,
        f"exactly 1 primary batch flagged as failed (got {len(failed_primary)})",
    )
    _assert(
        failed_primary[0]["output"]["failure_reason"] == "empty_result",
        f"failure_reason='empty_result' surfaced in forensic "
        f"(got {failed_primary[0]['output']['failure_reason']!r})",
    )
    _assert(
        failed_primary[0]["output"]["missing_count"] == 25,
        f"missing_count=25 for the failed batch "
        f"(got {failed_primary[0]['output']['missing_count']})",
    )

    # The orchestrator-level summary should warn about the silent failure
    # — that's the difference between "log says 68/93" with no signal
    # vs "log says 68/93 AND 1/4 batch failed".
    failure_warnings = [
        m for m in captured_warnings
        if "batch(es) failed" in m
    ]
    _assert(
        len(failure_warnings) == 1,
        f"one summary WARN about silent batch failures "
        f"(got {failure_warnings})",
    )

    # --- Print a one-screen summary of the forensic batch lines ---
    print("\n  forensic per-batch summary:")
    for b in primary:
        print(f"    batch={b['input']['batch_idx']}/"
              f"{b['input']['total_batches']} "
              f"size={b['input']['batch_size']} "
              f"verdicts={b['output']['verdicts_returned']} "
              f"missing={b['output']['missing_count']} "
              f"reason={b['output']['failure_reason']}")
    for b in retries:
        print(f"    retry  size={b['input']['batch_size']} "
              f"verdicts={b['output']['verdicts_returned']} "
              f"missing={b['output']['missing_count']} "
              f"reason={b['output']['failure_reason']}")


def test_split_retry_recovers() -> None:
    section("2. split-retry recovers when the failure is prompt-size related")

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 50 jobs → 2 batches of 25.
    jobs = _make_jobs(50)

    # Fail any prompt containing >= 20 distinct external_ids; succeed
    # otherwise. Models the "prompt too big → empty result" theory:
    # halving the chunk fixes it.
    call_log: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        call_log.append(len(present))
        if len(present) >= 20:
            return _empty_result_envelope()
        return _build_fake_response(present)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        timeout_s=60,
        projected_prefs={"keywords": ["mlops"]},
    )

    _assert(
        len(verdicts) == 50,
        f"split retry recovers all 50 verdicts (got {len(verdicts)})",
    )
    # Each of the 2 primary batches fails (call_log includes both with
    # size 25), and each splits into 2 halves (~12 + ~13) which succeed.
    _assert(
        call_log.count(25) == 2,
        f"both primary batches were 25-job calls (got {call_log})",
    )
    _assert(
        sum(1 for n in call_log if n < 20) == 4,
        f"4 split-retry sub-calls under the failure threshold "
        f"(got {[n for n in call_log if n < 20]})",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        test_one_batch_fails_others_succeed()
        test_split_retry_recovers()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print("\nAll batch-failure smoke checks passed.")
