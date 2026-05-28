#!/usr/bin/env python3
"""Smoke test for the Sonnet-pass batching knobs introduced 2026-05-28.

Forensic analysis of the worst 10 Sonnet rescore calls in the 7d window
2026-05-21..28 showed elapsed_ms 347-530s per batch (53s/job at
batch=10). The fix:

  1. defaults.ai_sonnet_max_jobs_per_call=5 — Sonnet RESCORE chunks are
     half the size of Haiku triage chunks. Per-batch input drops from
     ~25k → ~19k input tokens, capping worst-case batch wall time.
  2. defaults.ai_triage_ceiling=5 — Haiku verdicts AT OR ABOVE the
     ceiling skip the Sonnet rescore entirely. Haiku is rarely wrong
     at the top end; spending 50s/job to confirm a Haiku=5 is net
     negative.

This test pins both defaults AND the runtime behaviour of the knobs in
`enrich_jobs_ai` — so a regression to the old shape (Sonnet sees every
score>=2 in batches of 10) trips a CI failure rather than silently
re-introducing the 8-minute Sonnet stall.

It also exercises the split-retry path at the new batch size to confirm
the failure-recovery machinery still kicks in when Sonnet returns a
parse error on a batch=5 Sonnet call.

Run:
    cd <worktree-root>
    python3 skill/job-search/scripts/tests/smoke_enrich_sonnet_batch_knobs.py
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
    """Wrap a list of result dicts in the `claude -p --output-format json`
    envelope shape so extract_assistant_text + parse_json_block see a
    happy-path response."""
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": json.dumps({"results": items}),
    }
    return json.dumps(envelope)


def _haiku_response_for(present: list[_FakeJob], score_for) -> str:
    """Build a Haiku-shape happy response where each posting is scored by
    the `score_for(ext_id) -> int` callback. Used to control which jobs
    end up in the Sonnet rescore window."""
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


def _sonnet_response_for(present: list[_FakeJob]) -> str:
    """Sonnet always emits a fixed score=4 verdict — used so the test
    can detect WHICH jobs Sonnet actually saw (score=4 means Sonnet
    touched it; score from the Haiku fake means it didn't)."""
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


def _empty_envelope() -> str:
    return json.dumps({
        "type": "result", "subtype": "success",
        "is_error": False, "result": "",
    })


# ---------------------------------------------------------------------------
# Defaults pinning
# ---------------------------------------------------------------------------

def test_defaults_pin() -> None:
    section("0. defaults.py pins the new Sonnet knobs at the production values")

    # Reload defaults from a clean import — guards against test-order pollution.
    for mod in ("defaults",):
        if mod in sys.modules:
            del sys.modules[mod]
    from defaults import DEFAULTS

    _assert(
        DEFAULTS.get("ai_sonnet_max_jobs_per_call") == 5,
        f"ai_sonnet_max_jobs_per_call=5 in defaults "
        f"(got {DEFAULTS.get('ai_sonnet_max_jobs_per_call')!r})",
    )
    _assert(
        DEFAULTS.get("ai_triage_ceiling") == 5,
        f"ai_triage_ceiling=5 in defaults "
        f"(got {DEFAULTS.get('ai_triage_ceiling')!r})",
    )
    # The Haiku triage chunk size stays at 10 — we don't want to slow
    # Haiku triage too; only Sonnet is the bottleneck.
    _assert(
        DEFAULTS.get("ai_max_jobs_per_call") == 10,
        f"ai_max_jobs_per_call=10 in defaults "
        f"(got {DEFAULTS.get('ai_max_jobs_per_call')!r})",
    )
    # Two-pass scoring is still the default — the new knobs only matter in
    # that mode.
    _assert(
        DEFAULTS.get("ai_two_pass") is True,
        f"ai_two_pass=True in defaults (got {DEFAULTS.get('ai_two_pass')!r})",
    )


# ---------------------------------------------------------------------------
# Behavioural tests
# ---------------------------------------------------------------------------

def test_triage_ceiling_skips_top_end() -> None:
    section("1. triage_ceiling=5 — Haiku=5 verdicts skip the Sonnet rescore")

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 20 jobs. Haiku will score:
    #   ext-0000..ext-0009 → 5 (top end → SKIP Sonnet)
    #   ext-0010..ext-0014 → 3 (mid → Sonnet rescores)
    #   ext-0015..ext-0019 → 1 (below floor → no Sonnet)
    jobs = _make_jobs(20)
    top_ids = {j.external_id for j in jobs[0:10]}
    mid_ids = {j.external_id for j in jobs[10:15]}
    low_ids = {j.external_id for j in jobs[15:20]}

    def score_for(ext_id: str) -> int:
        if ext_id in top_ids:
            return 5
        if ext_id in mid_ids:
            return 3
        return 1

    sonnet_seen_ids: set[str] = set()

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "sonnet" in caller:
            sonnet_seen_ids.update(j.external_id for j in present)
            return _sonnet_response_for(present)
        return _haiku_response_for(present, score_for)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer, 8 years Python and AWS.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,           # production value
        sonnet_max_jobs_per_call=5, # production value
        workers=1,                  # deterministic ordering
    )

    # 1) Sonnet must have seen ONLY the mid-band ids — not the top end,
    #    not the below-floor band.
    _assert(
        sonnet_seen_ids == mid_ids,
        f"Sonnet saw exactly the mid-band ids "
        f"(extras={sonnet_seen_ids - mid_ids}, "
        f"missing={mid_ids - sonnet_seen_ids})",
    )
    # 2) The trusted-top Haiku verdicts must be returned UNTOUCHED
    #    (score=5, not the score=4 Sonnet stamp).
    for tid in top_ids:
        _assert(
            verdicts.get(tid, {}).get("match_score") == 5,
            f"Haiku=5 verdict {tid!r} passed through untouched "
            f"(got {verdicts.get(tid, {}).get('match_score')!r})",
        )
    # 3) Mid-band ids carry the Sonnet stamp (=4).
    for mid in mid_ids:
        _assert(
            verdicts.get(mid, {}).get("match_score") == 4,
            f"mid-band verdict {mid!r} carries Sonnet stamp "
            f"(got {verdicts.get(mid, {}).get('match_score')!r})",
        )
    # 4) Below-floor Haiku verdicts also pass through (score=1).
    for lid in low_ids:
        _assert(
            verdicts.get(lid, {}).get("match_score") == 1,
            f"below-floor verdict {lid!r} passed through "
            f"(got {verdicts.get(lid, {}).get('match_score')!r})",
        )


def test_sonnet_batch_size_drives_chunking() -> None:
    section(
        "2. sonnet_max_jobs_per_call=5 — Sonnet pool chunks at 5, "
        "Haiku at 10"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 12 jobs all in the Sonnet window so the rescore touches every one.
    jobs = _make_jobs(12)
    every_id = {j.external_id for j in jobs}

    haiku_batch_sizes: list[int] = []
    sonnet_batch_sizes: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "sonnet" in caller:
            sonnet_batch_sizes.append(len(present))
            return _sonnet_response_for(present)
        haiku_batch_sizes.append(len(present))
        return _haiku_response_for(present, lambda eid: 3)  # all mid

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        max_jobs_per_call=10,        # Haiku batch knob
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        sonnet_max_jobs_per_call=5,  # Sonnet batch knob — this is the test
        workers=1,
    )

    _assert(
        len(verdicts) == 12,
        f"all 12 jobs verdicted (got {len(verdicts)})",
    )
    # Haiku ran at the bigger batch knob: 12 jobs → 10 + 2.
    _assert(
        sorted(haiku_batch_sizes) == [2, 10],
        f"Haiku chunks at max_jobs_per_call=10 (got {haiku_batch_sizes})",
    )
    # Sonnet ran at the smaller knob: 12 jobs → 5 + 5 + 2.
    _assert(
        sorted(sonnet_batch_sizes) == [2, 5, 5],
        f"Sonnet chunks at sonnet_max_jobs_per_call=5 "
        f"(got {sonnet_batch_sizes})",
    )
    # Every Sonnet-rescored job carries the Sonnet stamp.
    _assert(
        all(verdicts[eid]["match_score"] == 4 for eid in every_id),
        "every job carries the Sonnet stamp after rescore",
    )


def test_sonnet_split_retry_still_fires_at_batch_5() -> None:
    section(
        "3. split-retry still recovers when a Sonnet batch=5 call fails — "
        "the smaller batch knob doesn't disable the failure-recovery path"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # 10 jobs, all in the Sonnet window. With sonnet_max_jobs_per_call=5
    # that's 2 Sonnet batches. We poison the FIRST Sonnet batch so it
    # returns an empty-result envelope on the first attempt, but only at
    # batch_size >= 5. Splitting it into 2 + 3 then succeeds — the same
    # shape as the original batch-failure test but at the new batch size.
    jobs = _make_jobs(10)
    first_sonnet_batch_ids = {j.external_id for j in jobs[0:5]}

    sonnet_call_log: list[int] = []

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "sonnet" in caller:
            sonnet_call_log.append(len(present))
            # Empty-result on the FULL first sonnet batch (size 5,
            # contains every one of first_sonnet_batch_ids); on the
            # split halves return success.
            is_full_first_batch = (
                len(present) == 5
                and {j.external_id for j in present} == first_sonnet_batch_ids
            )
            if is_full_first_batch:
                return _empty_envelope()
            return _sonnet_response_for(present)
        return _haiku_response_for(present, lambda eid: 3)  # all mid

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        sonnet_max_jobs_per_call=5,
        workers=1,
    )

    # Every job should still come back via the Haiku verdict (score=3)
    # OR the Sonnet stamp (=4). The retry-recovered first batch should
    # carry the Sonnet stamp.
    _assert(
        len(verdicts) == 10,
        f"all 10 verdicts present (got {len(verdicts)})",
    )
    for jid in first_sonnet_batch_ids:
        _assert(
            verdicts.get(jid, {}).get("match_score") == 4,
            f"split-retry recovered Sonnet stamp for {jid!r} "
            f"(got {verdicts.get(jid, {}).get('match_score')!r})",
        )
    # Sonnet call shape: 1 full-batch failure (size=5) + 2 split-retry
    # subcalls (sizes 2 + 3) + 1 second-batch success (size=5) = 4 calls.
    _assert(
        sorted(sonnet_call_log) == [2, 3, 5, 5],
        f"Sonnet split-retry produced expected call shape (got "
        f"{sonnet_call_log})",
    )


def _read_forensic_lines(state_dir: str) -> list[dict]:
    """Slurp every forensic JSONL line written under STATE_DIR. The
    forensic writer rotates files but the test never hits the rotation
    threshold, so log.0.jsonl typically contains everything."""
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


def test_trusted_top_emits_per_job_forensic() -> None:
    section(
        "4. trusted-top Haiku verdicts emit a per-job forensic line — "
        "audit trail for any 5/5 alert that bypassed Sonnet"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    # Same shape as test 1: top band skips Sonnet, mid band goes to
    # Sonnet. We only care that the top band shows up in the forensic
    # log here.
    jobs = _make_jobs(8)
    top_ids = {j.external_id for j in jobs[0:5]}
    mid_ids = {j.external_id for j in jobs[5:8]}

    def score_for(ext_id: str) -> int:
        if ext_id in top_ids:
            return 5
        if ext_id in mid_ids:
            return 3
        return 1

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "sonnet" in caller:
            return _sonnet_response_for(present)
        return _haiku_response_for(present, score_for)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer, 8 years Python and AWS.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=5,
        sonnet_max_jobs_per_call=5,
        workers=1,
    )

    all_lines = _read_forensic_lines(td)
    trusted_lines = [
        ln for ln in all_lines
        if ln.get("op") == "enrich_jobs_ai.trusted_top"
    ]

    # 1) Exactly one forensic line per trusted-top job.
    _assert(
        len(trusted_lines) == len(top_ids),
        f"one trusted_top forensic line per Haiku-5 job "
        f"(expected {len(top_ids)}, got {len(trusted_lines)})",
    )

    # 2) Every top-band ext_id is represented exactly once.
    logged_ext_ids = sorted(
        (ln.get("input", {}) or {}).get("ext_id") for ln in trusted_lines
    )
    _assert(
        logged_ext_ids == sorted(top_ids),
        f"trusted_top lines cover every Haiku-5 ext_id "
        f"(missing={set(top_ids) - set(logged_ext_ids)}, "
        f"extras={set(logged_ext_ids) - set(top_ids)})",
    )

    # 3) Shape contract: each line carries job_id, ext_id, title,
    #    haiku_score, triage_ceiling. This is what the operator will
    #    `jq` over when chasing a bad 5/5 alert — if any of these go
    #    missing the audit trail breaks.
    sample = trusted_lines[0]
    inp = sample.get("input", {}) or {}
    outp = sample.get("output", {}) or {}
    for key in ("job_id", "ext_id", "title", "company"):
        _assert(
            key in inp and inp[key] not in (None, ""),
            f"trusted_top.input carries {key!r} (sample={inp!r})",
        )
    _assert(
        outp.get("haiku_score") == 5,
        f"trusted_top.output.haiku_score==5 (got {outp.get('haiku_score')!r})",
    )
    _assert(
        outp.get("triage_ceiling") == 5,
        f"trusted_top.output.triage_ceiling==5 "
        f"(got {outp.get('triage_ceiling')!r})",
    )

    # 4) Mid-band ids must NOT show up as trusted_top — they went to
    #    Sonnet and should be auditable through enrich_jobs_ai.batch
    #    forensic lines instead.
    for mid in mid_ids:
        _assert(
            mid not in logged_ext_ids,
            f"mid-band id {mid!r} is NOT logged as trusted_top",
        )


def test_trusted_top_no_emit_when_ceiling_open() -> None:
    section(
        "5. triage_ceiling=6 (open / legacy default) emits zero "
        "trusted_top lines — every Haiku verdict still goes to Sonnet"
    )

    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)

    for mod in ("forensic", "job_enrich"):
        if mod in sys.modules:
            del sys.modules[mod]
    import job_enrich

    jobs = _make_jobs(6)

    def fake_wrapped_run_p(store, caller, prompt, **kwargs):
        present = [j for j in jobs if j.external_id in prompt]
        if "sonnet" in caller:
            return _sonnet_response_for(present)
        # All Haiku=5 — yet ceiling=6 means none are "trusted".
        return _haiku_response_for(present, lambda eid: 5)

    job_enrich.wrapped_run_p = fake_wrapped_run_p

    job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="MLOps engineer.",
        prefs_text="MLOps engineer prefs",
        timeout_s=60,
        max_jobs_per_call=10,
        two_pass=True,
        triage_floor=2,
        triage_ceiling=6,            # legacy / backwards-compat default
        sonnet_max_jobs_per_call=5,
        workers=1,
    )

    all_lines = _read_forensic_lines(td)
    trusted_lines = [
        ln for ln in all_lines
        if ln.get("op") == "enrich_jobs_ai.trusted_top"
    ]
    _assert(
        trusted_lines == [],
        f"ceiling=6 emits zero trusted_top lines (got {len(trusted_lines)})",
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        test_defaults_pin()
        test_triage_ceiling_skips_top_end()
        test_sonnet_batch_size_drives_chunking()
        test_sonnet_split_retry_still_fires_at_batch_5()
        test_trusted_top_emits_per_job_forensic()
        test_trusted_top_no_emit_when_ceiling_open()
    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    print("\nAll Sonnet-batch-knob smoke checks passed.")
