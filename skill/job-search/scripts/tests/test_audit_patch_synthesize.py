"""Tests for the audit-patch synthesizer.

Covers the offline patch proposer's safety contract:

  * empty forensic → no output dir created.
  * disagreement count below threshold → no patches written.
  * a well-formed Opus response (mocked) → patch + briefing both land
    on disk, the patch is structurally a unified diff.
  * malformed Opus output → script exits 0, nothing written.
  * proposed patches whose context lines don't exist verbatim in the
    current `_PROMPT` body are REJECTED (this is the safety property:
    the operator must be able to `git apply` cleanly).
  * --dry-run flag never touches the filesystem.

All tests run hermetically — they patch `audit_patch_synthesize.run_p`
to return canned JSON / diff text and point the script at a tmp_path
forensic dir with a single hand-rolled log line.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
TOOLS_DIR = SCRIPTS_DIR / "tools"
for p in (str(SCRIPTS_DIR), str(TOOLS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import audit_patch_synthesize as aps  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A trimmed but realistic-ish slice of the _PROMPT body. Tests anchor
# their canned diffs against THIS text — when the real `_PROMPT` body
# is loaded from job_enrich.py the same anchors are used (we point the
# synthesizer at a hand-built `job_enrich.py` stub via job_enrich_path).
PROMPT_SAMPLE = (
    "OUTPUT STYLE — caveman english\n"
    "==============================\n"
    "Some style rules go here.\n"
    "\n"
    "═══ TOP-LEVEL SCORING DOCTRINE — read before applying any rule ═══\n"
    "\n"
    "DOCTRINE A — NEVER penalize \"overqualification\". Anywhere.\n"
    "DOCTRINE B — NEVER stack penalties on the SAME underlying fact.\n"
    "DOCTRINE C — Generic titles describing data labeling → score=0.\n"
    "\n"
    "═════════════════════════════════════════════════════════════════════\n"
    "\n"
    "For each posting, you must:\n"
    "\n"
    "  1. Score how well it matches THIS candidate, on an integer 0-5 scale.\n"
    "  2. Write `why_match`.\n"
    "  3. Write `why_mismatch`.\n"
    "  4. Extract `key_details`.\n"
)


def _write_job_enrich_stub(tmp_path: Path, prompt_body: str) -> Path:
    """Build a fake `job_enrich.py` containing only a triple-quoted
    `_PROMPT = "..."` assignment so the prompt-body scraper has something
    to read. Tests anchor canned diffs against `prompt_body`.
    """
    src = (
        '"""stub for tests"""\n'
        '_PROMPT = """' + prompt_body + '""".strip()\n'
        'def enrich_jobs_ai():\n    return {}\n'
    )
    p = tmp_path / "job_enrich.py"
    p.write_text(src, encoding="utf-8")
    return p


def _write_forensic(forensic_dir: Path, records: list[dict]) -> None:
    forensic_dir.mkdir(parents=True, exist_ok=True)
    path = forensic_dir / "log.0.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _audit_record(
    ext_id: str,
    *,
    title: str = "Senior React Developer",
    company: str = "Acme",
    source: str = "linkedin",
    verdict: str = "raise",
    original: int = 3,
    revised: int = 4,
    comment: str = "Stacked seniority + years penalty on the same upward gap.",
    ts: float | None = None,
) -> dict:
    """Construct one scoring_audit.review forensic record."""
    return {
        "ts": ts if ts is not None else time.time(),
        "op": "scoring_audit.review",
        "phase": "single",
        "chat_id": 12345,
        "run_id": 99,
        "input": {
            "external_id": ext_id,
            "title": title,
            "company": company,
            "source": source,
        },
        "output": {
            "original_score": original,
            "revised_score": revised,
            "verdict": verdict,
            "comment": comment,
        },
    }


def _make_well_formed_diff() -> str:
    """A diff that anchors on lines verbatim present in PROMPT_SAMPLE."""
    return (
        "--- a/skill/job-search/scripts/job_enrich.py\n"
        "+++ b/skill/job-search/scripts/job_enrich.py\n"
        "@@ -7,6 +7,9 @@\n"
        " DOCTRINE A — NEVER penalize \"overqualification\". Anywhere.\n"
        " DOCTRINE B — NEVER stack penalties on the SAME underlying fact.\n"
        " DOCTRINE C — Generic titles describing data labeling → score=0.\n"
        "+\n"
        "+DOCTRINE D — Soft language phrasing (\"proficient\", \"professional\n"
        "+working\", \"fluent\") DOES NOT trigger the CEFR penalty.\n"
        " \n"
        " ═════════════════════════════════════════════════════════════════════\n"
        " \n"
    )


def _wrap_cli_envelope(text: str) -> str:
    """Mimic `claude -p --output-format json` envelope."""
    return json.dumps({"result": text})


def _make_canned_runner(responses: list[str | None]):
    """Build a run_p stub that pops one response per call.

    Each entry is the raw assistant text the model would have produced;
    we wrap it in the CLI envelope automatically so
    `extract_assistant_text` finds it.
    """
    calls: list[dict] = []
    queue: list[str | None] = list(responses)

    def _runner(prompt: str, *, model: str | None = None, timeout_s: int = 60):
        calls.append({"prompt": prompt, "model": model, "timeout_s": timeout_s})
        if not queue:
            return None
        item = queue.pop(0)
        if item is None:
            return None
        return _wrap_cli_envelope(item)

    _runner.calls = calls  # type: ignore[attr-defined]
    return _runner


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_no_disagreements_writes_nothing(tmp_path, monkeypatch):
    """Empty forensic dir → no output dir is created."""
    forensic_dir = tmp_path / "forensic_logs"
    forensic_dir.mkdir()  # exists but empty
    output_root = tmp_path / "audit_patches"
    stub_enrich = _write_job_enrich_stub(tmp_path, PROMPT_SAMPLE)

    # Runner that would crash if invoked — proving we never even called Opus.
    def _crash(*_args, **_kwargs):
        raise AssertionError("Opus must not be called when there are no disagreements")
    monkeypatch.setattr(aps, "run_p", _crash)

    result = aps.synthesize(
        forensic_dir=forensic_dir,
        output_root=output_root,
        job_enrich_path=stub_enrich,
        notify=False,
    )

    assert result.n_disagreements == 0
    assert result.n_patches_written == 0
    assert result.output_dir is None
    assert not output_root.exists()


def test_below_threshold_skipped(tmp_path, monkeypatch):
    """3 disagreements, min_cluster_size=5 → cluster filtered out."""
    forensic_dir = tmp_path / "forensic_logs"
    _write_forensic(forensic_dir, [
        _audit_record(f"j{i}") for i in range(3)
    ])
    output_root = tmp_path / "audit_patches"
    stub_enrich = _write_job_enrich_stub(tmp_path, PROMPT_SAMPLE)

    # Opus returns ONE cluster of size 3 — below the 5 threshold.
    cluster_json = json.dumps({
        "clusters": [{
            "cluster_name": "doctrine-b-stacked-penalties",
            "doctrine_violated": "B",
            "n_disagreements": 3,
            "sample_ids": [f"j{i}" for i in range(3)],
            "root_cause_hypothesis": "Scorer stacks seniority + years penalties.",
        }]
    })
    runner = _make_canned_runner([cluster_json])
    monkeypatch.setattr(aps, "run_p", runner)

    result = aps.synthesize(
        forensic_dir=forensic_dir,
        output_root=output_root,
        job_enrich_path=stub_enrich,
        min_cluster_size=5,
        notify=False,
    )

    assert result.n_disagreements == 3
    assert result.n_clusters_considered == 1
    assert result.n_patches_written == 0
    # No write should have happened — only the clustering call was made.
    assert len(runner.calls) == 1
    assert not output_root.exists()


def test_cluster_above_threshold_writes_patch_and_md(tmp_path, monkeypatch):
    """Threshold met → .patch + .md + summary land on disk, .patch is a unified diff."""
    forensic_dir = tmp_path / "forensic_logs"
    _write_forensic(forensic_dir, [
        _audit_record(f"j{i}") for i in range(6)
    ])
    output_root = tmp_path / "audit_patches"
    stub_enrich = _write_job_enrich_stub(tmp_path, PROMPT_SAMPLE)

    cluster_json = json.dumps({
        "clusters": [{
            "cluster_name": "doctrine-b-stacked-penalties",
            "doctrine_violated": "B",
            "n_disagreements": 6,
            "sample_ids": [f"j{i}" for i in range(6)],
            "root_cause_hypothesis": "Scorer stacks seniority + years penalties.",
        }]
    })
    diff_text = _make_well_formed_diff()
    runner = _make_canned_runner([cluster_json, diff_text])
    monkeypatch.setattr(aps, "run_p", runner)

    result = aps.synthesize(
        forensic_dir=forensic_dir,
        output_root=output_root,
        job_enrich_path=stub_enrich,
        min_cluster_size=5,
        notify=False,
    )

    assert result.n_patches_written == 1
    assert result.output_dir is not None and result.output_dir.exists()

    patches = list(result.output_dir.glob("*.patch"))
    mds = list(result.output_dir.glob("*.md"))
    assert len(patches) == 1
    # Per-cluster md + summary.md
    assert {p.name for p in mds} >= {"doctrine-b-stacked-penalties.md", "summary.md"}

    patch_body = patches[0].read_text(encoding="utf-8")
    assert patch_body.startswith("--- a/skill/job-search/scripts/job_enrich.py\n")
    assert "+++ b/skill/job-search/scripts/job_enrich.py\n" in patch_body
    assert "\n@@ " in patch_body or patch_body.startswith("@@ "), patch_body[:120]
    # The added line should be present.
    assert "+DOCTRINE D — Soft language phrasing" in patch_body

    md_body = (result.output_dir / "doctrine-b-stacked-penalties.md").read_text(encoding="utf-8")
    assert "doctrine-b-stacked-penalties" in md_body
    assert "git apply" in md_body
    assert "Stacked seniority + years penalty" in md_body  # comment surfaced


def test_malformed_opus_output_skipped(tmp_path, monkeypatch, caplog):
    """Garbage Opus output → script exits 0, no files written, log line emitted."""
    forensic_dir = tmp_path / "forensic_logs"
    _write_forensic(forensic_dir, [
        _audit_record(f"j{i}") for i in range(6)
    ])
    output_root = tmp_path / "audit_patches"
    stub_enrich = _write_job_enrich_stub(tmp_path, PROMPT_SAMPLE)

    # First call: clustering response is completely unparseable.
    runner = _make_canned_runner(["this is not JSON at all 🤷"])
    monkeypatch.setattr(aps, "run_p", runner)

    caplog.set_level("INFO", logger="audit_patch_synthesize")
    result = aps.synthesize(
        forensic_dir=forensic_dir,
        output_root=output_root,
        job_enrich_path=stub_enrich,
        min_cluster_size=5,
        notify=False,
    )

    assert result.n_patches_written == 0
    assert result.output_dir is None
    assert not output_root.exists()
    # The CLI ran exactly once — patch-proposer was never invoked.
    assert len(runner.calls) == 1

    # Now exercise the per-cluster malformed-diff path: clusters parse OK,
    # but the patch text isn't a real diff. We still expect zero writes
    # and a graceful exit (not a crash).
    runner2 = _make_canned_runner([
        json.dumps({
            "clusters": [{
                "cluster_name": "doctrine-b-stacked-penalties",
                "doctrine_violated": "B",
                "n_disagreements": 6,
                "sample_ids": [f"j{i}" for i in range(6)],
                "root_cause_hypothesis": "Scorer stacks penalties.",
            }]
        }),
        # Diff response is junk: no `--- a/...` header.
        "sorry, I cannot help with this request",
    ])
    monkeypatch.setattr(aps, "run_p", runner2)
    result2 = aps.synthesize(
        forensic_dir=forensic_dir,
        output_root=output_root,
        job_enrich_path=stub_enrich,
        min_cluster_size=5,
        notify=False,
    )
    assert result2.n_clusters_considered == 1
    assert result2.n_patches_written == 0
    assert result2.output_dir is None
    assert not output_root.exists()


def test_patch_anchors_match_current_prompt(tmp_path, monkeypatch):
    """A diff whose context lines DON'T appear verbatim in _PROMPT is REJECTED.

    This is the safety property: the operator must be able to `git
    apply` the patch without manual fixup. We never write a diff whose
    anchors won't apply.
    """
    forensic_dir = tmp_path / "forensic_logs"
    _write_forensic(forensic_dir, [
        _audit_record(f"j{i}") for i in range(6)
    ])
    output_root = tmp_path / "audit_patches"
    stub_enrich = _write_job_enrich_stub(tmp_path, PROMPT_SAMPLE)

    # A diff with context lines that do NOT appear in PROMPT_SAMPLE.
    bad_diff = (
        "--- a/skill/job-search/scripts/job_enrich.py\n"
        "+++ b/skill/job-search/scripts/job_enrich.py\n"
        "@@ -1,3 +1,4 @@\n"
        " THIS LINE IS NOT IN THE PROMPT\n"
        " NEITHER IS THIS\n"
        " OR THIS\n"
        "+But we propose to ADD this line.\n"
    )
    cluster_json = json.dumps({
        "clusters": [{
            "cluster_name": "bogus-anchors",
            "doctrine_violated": "other",
            "n_disagreements": 6,
            "sample_ids": [f"j{i}" for i in range(6)],
            "root_cause_hypothesis": "Hypothesis text.",
        }]
    })
    runner = _make_canned_runner([cluster_json, bad_diff])
    monkeypatch.setattr(aps, "run_p", runner)

    result = aps.synthesize(
        forensic_dir=forensic_dir,
        output_root=output_root,
        job_enrich_path=stub_enrich,
        min_cluster_size=5,
        notify=False,
    )

    assert result.n_patches_written == 0
    assert result.output_dir is None
    assert not output_root.exists()

    # Also unit-check the helper directly.
    assert aps.anchors_match_prompt(_make_well_formed_diff(), PROMPT_SAMPLE) is True
    assert aps.anchors_match_prompt(bad_diff, PROMPT_SAMPLE) is False


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    """--dry-run path: runs the model, never touches the filesystem."""
    forensic_dir = tmp_path / "forensic_logs"
    _write_forensic(forensic_dir, [
        _audit_record(f"j{i}") for i in range(6)
    ])
    output_root = tmp_path / "audit_patches"
    stub_enrich = _write_job_enrich_stub(tmp_path, PROMPT_SAMPLE)

    cluster_json = json.dumps({
        "clusters": [{
            "cluster_name": "doctrine-b-stacked-penalties",
            "doctrine_violated": "B",
            "n_disagreements": 6,
            "sample_ids": [f"j{i}" for i in range(6)],
            "root_cause_hypothesis": "Scorer stacks penalties.",
        }]
    })
    runner = _make_canned_runner([cluster_json, _make_well_formed_diff()])
    monkeypatch.setattr(aps, "run_p", runner)

    result = aps.synthesize(
        forensic_dir=forensic_dir,
        output_root=output_root,
        job_enrich_path=stub_enrich,
        min_cluster_size=5,
        dry_run=True,
        notify=False,
    )

    # dry-run "would write" 1 patch but result.output_dir must be None.
    assert result.n_patches_written == 1
    assert result.output_dir is None
    assert not output_root.exists()


def test_extract_diff_rejects_no_patch_sentinel():
    """`@@ -0,0 +0,0 @@` is the model's documented 'no patch' sentinel."""
    sentinel = (
        "--- a/skill/job-search/scripts/job_enrich.py\n"
        "+++ b/skill/job-search/scripts/job_enrich.py\n"
        "@@ -0,0 +0,0 @@\n"
    )
    assert aps._extract_diff(sentinel) is None


def test_extract_diff_accepts_fenced_diff():
    """Some models wrap the diff in ```diff ... ```. Strip and accept."""
    fenced = "```diff\n" + _make_well_formed_diff() + "```"
    out = aps._extract_diff(fenced)
    assert out is not None
    assert out.startswith("--- a/skill/job-search/scripts/job_enrich.py")
