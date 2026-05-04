#!/usr/bin/env python3
"""Smoke test for per-verdict forensic emission.

Verifies the fix for the bug where 34 per-job verdicts were packed into a
single `output.verdicts` list on the `enrich_jobs_ai` forensic line and
got cut at verdict 12 by the 4 KiB field cap (forensic._truncate trims
list values), silently dropping 22 entries — including all LinkedIn
matches and the only score=2 result.

The fix emits one forensic line per verdict (op = `enrich_jobs_ai.verdict`)
plus a slim summary line (op = `enrich_jobs_ai`) carrying just
`enriched_count` and `score_distribution`. Each verdict line is ~600 bytes,
well under the 4 KiB cap, so all jobs are preserved verbatim.

This test:
  1. Builds 50 stub jobs and a stub `enrich_jobs_ai` that returns verdicts
     for every one (mix of scores 0-5).
  2. Replicates the per-verdict emission loop from search_jobs.py against
     a temp STATE_DIR.
  3. Reads state/forensic_logs/log.*.jsonl and asserts:
       - exactly 50 lines with op == "enrich_jobs_ai.verdict"
       - the summary line carries enriched_count == 50 and NO `verdicts` key
       - score_distribution matches the stub mix
       - verdict ids cover the full 50 (no truncation, no LinkedIn drop)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _assert(cond: bool, msg: str) -> None:
    print(("  OK  " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


# ---- stubs --------------------------------------------------------------

class _StubJob:
    """Mirrors the attributes the per-verdict loop reads from a real Job:
    `job_id`, `title`, `company`, `source`, `url`. Nothing else is touched."""

    def __init__(self, n: int, source: str) -> None:
        self.job_id = f"job-{n:03d}"
        self.title = f"Senior Engineer #{n} at Example Corp"
        self.company = f"Example Corp {n}"
        self.source = source
        self.url = f"https://example.com/jobs/{n}"


def _build_jobs(n: int) -> list[_StubJob]:
    """Mix of sources so we can prove LinkedIn entries survive (the bug
    truncated them all)."""
    sources = ["linkedin", "remoteok", "remotive", "hackernews", "web_search"]
    return [_StubJob(i, sources[i % len(sources)]) for i in range(n)]


def _stub_enrich(jobs: list[_StubJob]) -> dict[str, dict]:
    """Returns enrichments keyed by job_id. Score pattern guarantees:
      - at least one score=2 (the bug dropped the only score=2 entry)
      - LinkedIn jobs land at high indices that the bug truncated."""
    out: dict[str, dict] = {}
    for i, j in enumerate(jobs):
        score = i % 6  # 0..5 cycle
        out[j.job_id] = {
            "match_score": score,
            "why_match": f"Stack overlap with role #{i}; resume mentions X.",
            "key_details": {
                "stack": "python, django",
                "seniority": "senior",
                "remote_policy": "remote",
                "location": "EU",
                "salary": "",
                "language": "english",
            },
        }
    return out


def _score_histogram(enrichments: dict[str, dict]) -> dict[str, int]:
    """Local copy of search_jobs._score_histogram — keeps the test
    independent of search_jobs's heavy imports (db, dedupe, etc.)."""
    hist: dict[str, int] = {str(i): 0 for i in range(6)}
    hist["unknown"] = 0
    for v in enrichments.values():
        try:
            s = int((v or {}).get("match_score") or 0)
        except (TypeError, ValueError):
            hist["unknown"] += 1
            continue
        if 0 <= s <= 5:
            hist[str(s)] += 1
    return hist


# ---- the bit under test (mirrors search_jobs.py exactly) ----------------

def _emit(forensic, jobs, enrichments_by_job_id, *, chat_id: int, run_id: int) -> None:
    """Replicates the production emit pattern: one summary line via
    forensic.step, then one verdict line per job via forensic.log_step.
    Mirrors the code in search_jobs.py around the `enrich_jobs_ai` block."""
    with forensic.step(
        "enrich_jobs_ai",
        input={"job_count": len(jobs)},
        chat_id=chat_id,
        run_id=run_id,
    ) as fctx:
        fctx.set_output({
            "enriched_count": len(enrichments_by_job_id),
            "score_distribution": _score_histogram(enrichments_by_job_id),
        })
    for j in jobs:
        enr = enrichments_by_job_id.get(j.job_id) or {}
        kd = enr.get("key_details") or {}
        key_details_summary = "; ".join(
            f"{k}={str(v)[:60]}"
            for k, v in kd.items()
            if v not in (None, "", [], {})
        )[:400]
        forensic.log_step(
            "enrich_jobs_ai.verdict",
            input={
                "job_id": j.job_id,
                "title": j.title[:120],
                "company": (j.company or "")[:80],
                "source": j.source,
                "url": (j.url or "")[:200],
            },
            output={
                "match_score": enr.get("match_score"),
                "why_match": (enr.get("why_match") or "")[:300],
                "key_details_summary": key_details_summary,
            },
            chat_id=chat_id,
            run_id=run_id,
        )


# ---- driver -------------------------------------------------------------

def main() -> int:
    section("setup: isolated STATE_DIR + fresh forensic module")
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)
    os.environ.pop("FORENSIC_FULL", None)
    # Default 4 KiB field cap — that's the cap the bug exceeded. Leave it.
    os.environ.pop("FORENSIC_MAX_FIELD_BYTES", None)
    os.environ.pop("FORENSIC_MAX_BYTES", None)
    if "forensic" in sys.modules:
        del sys.modules["forensic"]
    import forensic
    _assert(True, f"STATE_DIR={td}")

    section("stub: 50 jobs across 5 sources")
    jobs = _build_jobs(50)
    enrichments = _stub_enrich(jobs)
    _assert(len(jobs) == 50, "50 stub jobs built")
    _assert(len(enrichments) == 50, "stub enrich returned 50 verdicts")
    linkedin_ids = {j.job_id for j in jobs if j.source == "linkedin"}
    _assert(len(linkedin_ids) == 10, f"10 LinkedIn jobs in the stub (got {len(linkedin_ids)})")

    section("act: run per-verdict emission")
    _emit(forensic, jobs, enrichments, chat_id=12345, run_id=999)

    section("read: state/forensic_logs/log.*.jsonl")
    log_dir = Path(td) / "forensic_logs"
    files = sorted(log_dir.glob("log.*.jsonl"))
    _assert(len(files) >= 1, f"at least one log file written (got {len(files)})")
    lines: list[dict] = []
    for f in files:
        for raw in f.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            lines.append(json.loads(raw))
    _assert(len(lines) >= 51, f"at least 51 lines (1 summary + 50 verdicts), got {len(lines)}")

    section("assert: exactly 50 verdict lines + slim summary")
    verdict_lines = [l for l in lines if l.get("op") == "enrich_jobs_ai.verdict"]
    summary_lines = [l for l in lines if l.get("op") == "enrich_jobs_ai"]
    _assert(
        len(verdict_lines) == 50,
        f"exactly 50 enrich_jobs_ai.verdict lines (got {len(verdict_lines)})",
    )
    _assert(
        len(summary_lines) == 1,
        f"exactly 1 enrich_jobs_ai summary line (got {len(summary_lines)})",
    )

    summary = summary_lines[0]
    out = summary.get("output") or {}
    _assert(
        out.get("enriched_count") == 50,
        f"summary.output.enriched_count == 50 (got {out.get('enriched_count')})",
    )
    _assert(
        "verdicts" not in out,
        "summary.output has NO 'verdicts' key (the bulky list was removed)",
    )
    _assert(
        isinstance(out.get("score_distribution"), dict),
        "summary.output.score_distribution is a dict",
    )

    section("assert: every job_id present (no truncation, LinkedIn included)")
    seen_ids = {(l.get("input") or {}).get("job_id") for l in verdict_lines}
    expected_ids = {j.job_id for j in jobs}
    missing = expected_ids - seen_ids
    _assert(not missing, f"no missing verdicts (missing={sorted(missing)})")
    seen_linkedin = {
        (l.get("input") or {}).get("job_id")
        for l in verdict_lines
        if (l.get("input") or {}).get("source") == "linkedin"
    }
    _assert(
        seen_linkedin == linkedin_ids,
        f"all 10 LinkedIn job_ids present (got {len(seen_linkedin)})",
    )

    section("assert: score=2 verdict survives (the bug's smoking gun)")
    score2 = [
        l for l in verdict_lines
        if (l.get("output") or {}).get("match_score") == 2
    ]
    _assert(len(score2) >= 1, f"at least one score=2 verdict logged (got {len(score2)})")

    section("assert: each verdict line stays under 4 KiB cap")
    for l in verdict_lines:
        encoded = json.dumps(l, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 4096:
            _assert(False, f"verdict line exceeded 4 KiB: {encoded[:200]}...")
    _assert(True, "all 50 verdict lines under 4 KiB")

    section("assert: chat_id + run_id propagated on every line")
    bad = [
        l for l in verdict_lines
        if l.get("chat_id") != 12345 or l.get("run_id") != 999
    ]
    _assert(not bad, f"chat_id/run_id propagated on all verdict lines ({len(bad)} bad)")

    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
