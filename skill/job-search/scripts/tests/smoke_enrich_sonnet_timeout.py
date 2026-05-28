#!/usr/bin/env python3
"""Smoke test for the Sonnet per-batch timeout + batch_size=1 retry path
introduced 2026-05-28.

Root-cause data (claude_calls table, last 7d, n=94 Sonnet rescore calls):

    p50: 117.1s   p90: 351.6s   p95: 407.9s   p99: 507.3s   max: 530.9s

The previous single `ai_enrich_timeout_s = 1200s` cap effectively
permitted a slow Sonnet call to monopolise one of the four enrich
workers for up to 20 minutes WITHOUT triggering the existing split-
retry path — split-retry fires on parse/empty failures, not on slow
SUCCESSES, and those are exactly the >5min Sonnet batches we observed.

The fix:
  * defaults.ai_sonnet_timeout_s = 300s — sized at p90 + headroom on
    the new batch=5 payload. Expected to fire on ~0% of healthy
    batches, ~100% of pathological slow-success ones.
  * enrich_jobs_ai now takes a dedicated `sonnet_timeout_s` kwarg
    plumbed only into the Sonnet rescore pool. The Haiku triage pool
    keeps the generous `timeout_s` cap.
  * On `_BATCH_TIMEOUT` (CLI returned None and elapsed ≈ timeout_s),
    the affected batch is retried ONCE at batch_size=1. Single-job
    batches that ALSO time out are LOGGED + DROPPED — we do not fall
    back to the Haiku verdict because that would mix verdict
    provenance in `job_scores`.

This test pins:
  1. The new `ai_sonnet_timeout_s = 300` default in defaults.py.
  2. The runtime behaviour: a Sonnet batch that times out (mocked via
     a fake wrapped_run_p that sleeps past `timeout_s` and returns
     None) is retried at batch_size=1 and the recovered verdicts
     carry the Sonnet stamp.
  3. The Haiku triage pass uses the bigger `timeout_s`, NOT the
     Sonnet-specific `sonnet_timeout_s`.
  4. A single-job retry that ALSO times out is dropped, NOT silently
     attributed to Haiku, and the drop is surfaced via the
     `enrich_jobs_ai.sonnet_timeout` forensic line.

Run:
    cd <worktree-root>
    python3 skill/job-search/scripts/tests/smoke_enrich_sonnet_timeout.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
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
# Fixtures
# ---------------------------------------------------------------------------

@dataclass
class _FakeJob:
    """Minimal stand-in for dedupe.Job — only the attributes job_enrich reads."""
    external_id: str
    title: str = "Senior MLOps Engineer"
    company: str = "Acme Corp"
    location: str = "Remote · EU"
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


def _envelope_with_results(items: list[dict]) -> str:
    envelope = {
        "type": "result", "subtype": "success",
        "is_error": False,
        "result": json.dumps({"results": items}),
    }
    return json.dumps(envelope)


def _haiku_response_for(present, score_for) -> str:
    items = []
    for j in present:
        items.append({
            "id": j.external_id,
            "match_score": int(score_for(j.external_id)),
            "why_match": "ok",
            "why_mismatch": "",
            "key_details": {
                "stack": "python", "seniority": "senior",
                "remote_policy": "remote", "location": "",
                "salary": "", "visa_support": "",
                "language": "", "standout": "",
            },
        })
    return _envelope_with_results(items)


def _sonnet_response_for(present) -> str:
    items = []
    for j in present:
        items.append({
            "id": j.external_id,
            "match_score": 4,
            "why_match": "sonnet-stamp",
            "why_mismatch": "",
            "key_details": {
                "stack": "python", "seniority": "senior",
                "remote_policy": "remote", "location": "",
                "salary": "", "visa_support": "",
                "language": "", "standout": "",
            },
        })
    return _envelope_with_results(items)


def _read_forensic_lines(state_dir: str) -> list[dict]:
    log_dir = Path(state_dir) / "forensic_logs"
    out: list[dict] = []
    if not log_dir.is_dir():
        return out
    for p in sorted(log_dir.glob("log.*.jsonl")):
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# ---------------------------------------------------------------------------
# Defaults pinning
# ---------------------------------------------------------------------------

def test_defaults_pin() -> None:
    section("0. defaults.py pins ai_sonnet_timeout_s at production value")

    for mod in ("defaults",):
        if mod in sys.modules:
            del sys.modules[mod]
    from defaults import DEFAULTS

    _assert(
        DEFAULTS.get("ai_sonnet_timeout_s") == 300,
        f"ai_sonnet_timeout_s=300 in defaults "
        f"(got {DEFAULTS.get('ai_sonnet_timeout_s')!r})",
    )
    # Slow-path / Haiku timeout stays at the legacy 1200s — only Sonnet
    # gets the tight cap.
    _assert(
        DEFAULTS.get("ai_enrich_timeout_s") == 1200,
        f"ai_enrich_timeout_s=1200 in defaults (Haiku/single-pass slow "
        f"path unchanged; got {DEFAULTS.get('ai_enrich_timeout_s')!r})",
    )


# ---------------------------------------------------------------------------
# Behavioural tests
# ---------------------------------------------------------------------------

def test_sonnet_timeout_triggers_batch_one_retry() -> None:
    section(
        "1. Sonnet batch times out → retried at batch_size=1; recovered "
        "verdicts carry the Sonnet stamp"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 10 jobs, all in the Sonnet window (all mid-band → Haiku scores 3).
    # With sonnet_max_jobs_per_call=5 that's 2 Sonnet batches.
    # The FIRST Sonnet batch (5 jobs) will time out on its first attempt;
    # the batch=1 retry path should re-issue 5 single-job calls and
    # those succeed instantly.
    jobs = _make_jobs(10)
    first_sonnet_batch_ids = {j.external_id for j in jobs[0:5]}

    sonnet_call_log: list[int] = []  # batch sizes seen by Sonnet calls

    # Use a SHORT timeout so the test actually completes in seconds.
    test_sonnet_timeout = 1  # 1s — easy to overshoot in a fake

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "sonnet" in caller:
            sonnet_call_log.append(len(present))
            # Time-out behaviour: the FULL 5-job batch sleeps past
            # timeout_s and returns None (mirrors subprocess.TimeoutExpired
            # → claude_cli.run_p returning None). Single-job calls (size=1)
            # succeed immediately.
            is_full_first_batch = (
                len(present) == 5
                and {j.external_id for j in present} == first_sonnet_batch_ids
            )
            if is_full_first_batch:
                # Sleep slightly longer than the timeout so the wrapper's
                # elapsed measurement crosses the _TIMEOUT_ELAPSED_THRESHOLD
                # (0.9 * timeout_s). 1.05 * timeout_s is enough margin.
                time.sleep((timeout_s or test_sonnet_timeout) * 1.05)
                return None
            return _sonnet_response_for(present)
        return _haiku_response_for(present, lambda eid: 3)  # all mid

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=10,             # Haiku budget (generous)
        sonnet_timeout_s=test_sonnet_timeout,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        sonnet_max_jobs_per_call=5,
        workers=1,                # deterministic order
    )

    # All 10 jobs verdicted (5 via successful 2nd-batch sonnet + 5 via
    # batch=1 retry).
    _assert(
        len(verdicts) == 10,
        f"all 10 verdicts present (got {len(verdicts)})",
    )
    # The originally-timed-out batch members must carry the Sonnet
    # stamp (=4) — NOT the Haiku verdict (=3). This pins that the retry
    # actually re-issued Sonnet calls; it did not fall back to Haiku.
    for jid in first_sonnet_batch_ids:
        _assert(
            verdicts.get(jid, {}).get("match_score") == 4,
            f"timeout-recovered job {jid!r} carries Sonnet stamp "
            f"(got {verdicts.get(jid, {}).get('match_score')!r}); "
            f"if 3, this means we silently fell back to Haiku — "
            f"verdict provenance would be corrupted",
        )

    # Sonnet call shape: 1 timed-out full-batch (size=5) + 5 single-job
    # retry calls (size=1 each) + 1 second full-batch success (size=5)
    # = 7 calls. Sorted: [1, 1, 1, 1, 1, 5, 5].
    _assert(
        sorted(sonnet_call_log) == [1, 1, 1, 1, 1, 5, 5],
        f"Sonnet call shape after timeout retry "
        f"(got {sorted(sonnet_call_log)})",
    )


def test_haiku_uses_separate_timeout() -> None:
    section(
        "2. Haiku triage uses the generous timeout_s, NOT the Sonnet cap "
        "— Haiku batches that take longer than sonnet_timeout_s still pass"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(5)

    haiku_call_timeouts: list[int] = []
    sonnet_call_timeouts: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "sonnet" in caller:
            sonnet_call_timeouts.append(timeout_s)
            return _sonnet_response_for(present)
        haiku_call_timeouts.append(timeout_s)
        return _haiku_response_for(present, lambda eid: 3)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=600,             # generous Haiku budget
        sonnet_timeout_s=120,      # tight Sonnet cap
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        sonnet_max_jobs_per_call=5,
        workers=1,
    )

    # Haiku calls must see the generous timeout, Sonnet calls the tight one.
    _assert(
        all(t == 600 for t in haiku_call_timeouts),
        f"Haiku calls received timeout_s=600 "
        f"(got {haiku_call_timeouts})",
    )
    _assert(
        all(t == 120 for t in sonnet_call_timeouts),
        f"Sonnet calls received sonnet_timeout_s=120 "
        f"(got {sonnet_call_timeouts})",
    )


def test_double_timeout_drops_jobs() -> None:
    section(
        "3. batch=1 retry that ALSO times out → job is DROPPED, "
        "NOT attributed to Haiku; forensic emits the drop count"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 6 jobs (one Sonnet batch of 5 + one of 1 with batch=5). The first
    # Sonnet batch times out; the batch=1 retry for the 5 jobs in that
    # batch ALSO times out for 2 of them, succeeds for 3.
    jobs = _make_jobs(6)
    first_batch_ids = {j.external_id for j in jobs[0:5]}
    # Of those 5, ids 0 and 1 will keep timing out even at size=1.
    unrecoverable_ids = {jobs[0].external_id, jobs[1].external_id}

    test_sonnet_timeout = 1

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "sonnet" in caller:
            is_full_first_batch = (
                len(present) == 5
                and {j.external_id for j in present} == first_batch_ids
            )
            is_unrecoverable_single = (
                len(present) == 1 and present[0].external_id in unrecoverable_ids
            )
            if is_full_first_batch or is_unrecoverable_single:
                time.sleep((timeout_s or test_sonnet_timeout) * 1.05)
                return None
            return _sonnet_response_for(present)
        return _haiku_response_for(present, lambda eid: 3)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=10,
        sonnet_timeout_s=test_sonnet_timeout,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        sonnet_max_jobs_per_call=5,
        workers=1,
    )

    # 4 verdicts survive: 3 recovered via batch=1 retry + 1 from the
    # second sonnet batch. 2 are dropped entirely (no fallback to Haiku).
    _assert(
        len(verdicts) == 4,
        f"4 jobs verdicted, 2 dropped on double timeout "
        f"(got {len(verdicts)} verdicts)",
    )
    for jid in unrecoverable_ids:
        _assert(
            jid not in verdicts,
            f"unrecoverable job {jid!r} is DROPPED (NOT silently filled "
            f"with Haiku verdict)",
        )
    for jid in first_batch_ids - unrecoverable_ids:
        _assert(
            verdicts.get(jid, {}).get("match_score") == 4,
            f"recovered job {jid!r} carries the Sonnet stamp "
            f"(got {verdicts.get(jid, {}).get('match_score')!r})",
        )

    # Forensic must surface the recovery split.
    all_lines = _read_forensic_lines(td)
    timeout_lines = [
        ln for ln in all_lines
        if ln.get("op") == "enrich_jobs_ai.sonnet_timeout"
    ]
    _assert(
        len(timeout_lines) == 1,
        f"exactly one sonnet_timeout forensic line "
        f"(got {len(timeout_lines)})",
    )
    outp = (timeout_lines[0].get("output") or {})
    inp = (timeout_lines[0].get("input") or {})
    _assert(
        outp.get("recovered_count") == 3,
        f"forensic.recovered_count=3 (got {outp.get('recovered_count')!r})",
    )
    _assert(
        outp.get("dropped_count") == 2,
        f"forensic.dropped_count=2 (got {outp.get('dropped_count')!r})",
    )
    _assert(
        inp.get("n_jobs") == 5,
        f"forensic.input.n_jobs=5 "
        f"(the 5 jobs in the timed-out batch; got {inp.get('n_jobs')!r})",
    )
    _assert(
        inp.get("timeout_s") == test_sonnet_timeout,
        f"forensic.input.timeout_s matches the configured cap "
        f"(got {inp.get('timeout_s')!r})",
    )
    _assert(
        inp.get("pass") == "sonnet",
        f"forensic.input.pass='sonnet' "
        f"(got {inp.get('pass')!r})",
    )


def test_no_timeout_no_retry_no_forensic() -> None:
    section(
        "4. Healthy path: no Sonnet timeouts → no batch=1 retry, "
        "no sonnet_timeout forensic line"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(8)

    sonnet_call_log: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "sonnet" in caller:
            sonnet_call_log.append(len(present))
            return _sonnet_response_for(present)
        return _haiku_response_for(present, lambda eid: 3)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        sonnet_timeout_s=30,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        sonnet_max_jobs_per_call=5,
        workers=1,
    )

    _assert(len(verdicts) == 8, f"all 8 verdicts present (got {len(verdicts)})")
    # Only the two primary Sonnet batches — no batch=1 retries.
    _assert(
        sorted(sonnet_call_log) == [3, 5],
        f"only primary Sonnet batches ran; no retry calls "
        f"(got {sorted(sonnet_call_log)})",
    )
    all_lines = _read_forensic_lines(td)
    timeout_lines = [
        ln for ln in all_lines
        if ln.get("op") == "enrich_jobs_ai.sonnet_timeout"
    ]
    _assert(
        timeout_lines == [],
        f"healthy path emits zero sonnet_timeout forensic lines "
        f"(got {len(timeout_lines)})",
    )


def test_cli_missing_not_misclassified_as_timeout() -> None:
    section(
        "5. CLI returning None FAST (true cli_missing, elapsed ≈ 0) is "
        "NOT treated as a timeout — split-retry path still fires, "
        "batch=1 retry path does NOT"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(10)
    first_batch_ids = {j.external_id for j in jobs[0:5]}

    sonnet_call_log: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "sonnet" in caller:
            sonnet_call_log.append(len(present))
            is_full_first_batch = (
                len(present) == 5
                and {j.external_id for j in present} == first_batch_ids
            )
            if is_full_first_batch:
                # Return None IMMEDIATELY — no sleep. This is the
                # "CLI literally missing / crashed instantly" case.
                # The wrapper's elapsed measurement will be ~0s; with
                # threshold = 0.9 * timeout_s, this is classified as
                # CLI-missing, NOT timeout. The split-retry path (the
                # legacy recovery for transient model glitches) should
                # fire; the new batch=1 retry path should NOT.
                return None
            return _sonnet_response_for(present)
        return _haiku_response_for(present, lambda eid: 3)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        sonnet_timeout_s=30,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        sonnet_max_jobs_per_call=5,
        workers=1,
    )

    # CLI-missing → split-retry path tries 2 + 3 sub-batches (those
    # SUCCEED in this fake because they no longer match the full-batch
    # poison condition). Plus the second primary batch (size 5).
    # Expected calls: [5 (poisoned), 2 (split half), 3 (split half), 5
    # (other primary)] = 4 calls total.
    _assert(
        sorted(sonnet_call_log) == [2, 3, 5, 5],
        f"split-retry fired on CLI-missing (NOT batch=1 retry) "
        f"(got {sorted(sonnet_call_log)})",
    )
    _assert(
        len(verdicts) == 10,
        f"split-retry recovered all 10 verdicts (got {len(verdicts)})",
    )
    # No sonnet_timeout forensic line.
    all_lines = _read_forensic_lines(td)
    timeout_lines = [
        ln for ln in all_lines
        if ln.get("op") == "enrich_jobs_ai.sonnet_timeout"
    ]
    _assert(
        timeout_lines == [],
        f"CLI-missing does NOT trigger sonnet_timeout forensic "
        f"(got {len(timeout_lines)})",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        test_defaults_pin()
        test_sonnet_timeout_triggers_batch_one_retry()
        test_haiku_uses_separate_timeout()
        test_double_timeout_drops_jobs()
        test_no_timeout_no_retry_no_forensic()
        test_cli_missing_not_misclassified_as_timeout()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print("\nAll Sonnet-timeout smoke checks passed.")
