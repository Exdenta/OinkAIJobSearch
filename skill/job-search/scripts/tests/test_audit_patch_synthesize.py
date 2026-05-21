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
import shutil
import subprocess
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


def _git_apply_check(patch_path: Path, job_enrich_path: Path) -> tuple[int, str]:
    """Build a hermetic sandbox and run `git apply --check` for real.

    Mirrors what `audit_patch_synthesize.anchors_match_prompt` does
    internally, but lets the test assert the same property end-to-end on
    the file the synthesizer wrote. Returns (exit_code, stderr).
    """
    assert shutil.which("git"), "git binary required for these tests"
    sandbox = Path(__import__("tempfile").mkdtemp(prefix="audit_patch_test_"))
    try:
        target = sandbox / "skill/job-search/scripts/job_enrich.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(job_enrich_path.read_text(encoding="utf-8"), encoding="utf-8")
        proc = subprocess.run(
            ["git", "apply", "--check", str(patch_path)],
            cwd=str(sandbox),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, (proc.stderr or proc.stdout or "")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_patch_anchors_match_current_prompt(tmp_path, monkeypatch):
    """End-to-end safety property: a well-formed additions-only diff is
    accepted AND the persisted .patch file applies cleanly with
    `git apply --check`. A diff with bogus anchors is rejected (no .patch
    file written).

    This is the key correctness contract: the operator must be able to
    `git apply` whatever the synthesizer wrote, without manual fixup.
    """
    # ----- positive case: valid diff → .patch persisted → applies cleanly.
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
    runner = _make_canned_runner([cluster_json, _make_well_formed_diff()])
    monkeypatch.setattr(aps, "run_p", runner)

    result = aps.synthesize(
        forensic_dir=forensic_dir,
        output_root=output_root,
        job_enrich_path=stub_enrich,
        min_cluster_size=5,
        notify=False,
    )
    assert result.n_patches_written == 1
    patches = list(result.output_dir.glob("*.patch"))
    assert len(patches) == 1
    rc, stderr = _git_apply_check(patches[0], stub_enrich)
    assert rc == 0, (
        "synthesizer persisted a diff that `git apply --check` rejected:\n"
        f"  patch: {patches[0]}\n"
        f"  stderr: {stderr}"
    )

    # ----- negative case: bogus anchors → cluster skipped, nothing written.
    output_root2 = tmp_path / "audit_patches_2"
    bad_diff = (
        "--- a/skill/job-search/scripts/job_enrich.py\n"
        "+++ b/skill/job-search/scripts/job_enrich.py\n"
        "@@ -1,3 +1,4 @@\n"
        " THIS LINE IS NOT IN THE PROMPT\n"
        " NEITHER IS THIS\n"
        " OR THIS\n"
        "+But we propose to ADD this line.\n"
    )
    runner2 = _make_canned_runner([
        json.dumps({"clusters": [{
            "cluster_name": "bogus-anchors",
            "doctrine_violated": "other",
            "n_disagreements": 6,
            "sample_ids": [f"j{i}" for i in range(6)],
            "root_cause_hypothesis": "Hypothesis text.",
        }]}),
        bad_diff,
    ])
    monkeypatch.setattr(aps, "run_p", runner2)
    result2 = aps.synthesize(
        forensic_dir=forensic_dir,
        output_root=output_root2,
        job_enrich_path=stub_enrich,
        min_cluster_size=5,
        notify=False,
    )
    assert result2.n_patches_written == 0
    assert result2.output_dir is None
    assert not output_root2.exists()

    # ----- direct unit-check of the helper.
    assert aps.anchors_match_prompt(_make_well_formed_diff(), stub_enrich) is True
    assert aps.anchors_match_prompt(bad_diff, stub_enrich) is False


def test_anchor_reorder_rejected(tmp_path, monkeypatch):
    """COUNTER-EXAMPLE A: anchor lines are present in the file but in the
    WRONG ORDER. The old set-membership check accepted such diffs; the
    real `git apply --check` rejects them. The synthesizer must too.
    """
    forensic_dir = tmp_path / "forensic_logs"
    _write_forensic(forensic_dir, [
        _audit_record(f"j{i}") for i in range(6)
    ])
    output_root = tmp_path / "audit_patches"
    stub_enrich = _write_job_enrich_stub(tmp_path, PROMPT_SAMPLE)

    # DOCTRINE A, B, C are all present in PROMPT_SAMPLE — but here we
    # swap B and C. Every anchor line individually appears in the file,
    # so a set-membership test would pass. `git apply --check` rejects.
    reorder_diff = (
        "--- a/skill/job-search/scripts/job_enrich.py\n"
        "+++ b/skill/job-search/scripts/job_enrich.py\n"
        "@@ -7,6 +7,8 @@\n"
        " DOCTRINE A — NEVER penalize \"overqualification\". Anywhere.\n"
        " DOCTRINE C — Generic titles describing data labeling → score=0.\n"
        " DOCTRINE B — NEVER stack penalties on the SAME underlying fact.\n"
        "+\n"
        "+DOCTRINE D — added rule.\n"
        " \n"
        " ═════════════════════════════════════════════════════════════════════\n"
        " \n"
    )
    runner = _make_canned_runner([
        json.dumps({"clusters": [{
            "cluster_name": "reorder",
            "doctrine_violated": "other",
            "n_disagreements": 6,
            "sample_ids": [f"j{i}" for i in range(6)],
            "root_cause_hypothesis": "Hypothesis.",
        }]}),
        reorder_diff,
    ])
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
    # Direct helper call.
    assert aps.anchors_match_prompt(reorder_diff, stub_enrich) is False


def test_non_contiguous_anchors_rejected(tmp_path, monkeypatch):
    """COUNTER-EXAMPLE B: anchor lines pulled from FAR-APART regions of
    the file. Each line is verbatim in `_PROMPT`, but they're not
    adjacent, so the hunk header's positional claim can't be satisfied
    and `git apply --check` rejects.
    """
    forensic_dir = tmp_path / "forensic_logs"
    _write_forensic(forensic_dir, [
        _audit_record(f"j{i}") for i in range(6)
    ])
    output_root = tmp_path / "audit_patches"
    stub_enrich = _write_job_enrich_stub(tmp_path, PROMPT_SAMPLE)

    # Lines 1, 7, and 13 of PROMPT_SAMPLE — far apart. Each is real, but
    # they don't sit on consecutive lines, so the hunk can't apply.
    noncontig_diff = (
        "--- a/skill/job-search/scripts/job_enrich.py\n"
        "+++ b/skill/job-search/scripts/job_enrich.py\n"
        "@@ -1,5 +1,6 @@\n"
        " OUTPUT STYLE — caveman english\n"
        " DOCTRINE A — NEVER penalize \"overqualification\". Anywhere.\n"
        " For each posting, you must:\n"
        "+DOCTRINE D — Added rule.\n"
        "   1. Score how well it matches THIS candidate, on an integer 0-5 scale.\n"
        "   4. Extract `key_details`.\n"
    )
    runner = _make_canned_runner([
        json.dumps({"clusters": [{
            "cluster_name": "non-contig",
            "doctrine_violated": "other",
            "n_disagreements": 6,
            "sample_ids": [f"j{i}" for i in range(6)],
            "root_cause_hypothesis": "Hypothesis.",
        }]}),
        noncontig_diff,
    ])
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
    # Direct helper call.
    assert aps.anchors_match_prompt(noncontig_diff, stub_enrich) is False


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


def test_extract_diff_rejects_deletion_lines():
    """The patch-proposer prompt says additions-only, but trusting the
    prompt isn't enough — the parser must REJECT any candidate whose
    hunk body contains a `-`-prefixed line. Otherwise an operator
    running `git apply` would silently mutate `_PROMPT`.
    """
    with_deletion = (
        "--- a/skill/job-search/scripts/job_enrich.py\n"
        "+++ b/skill/job-search/scripts/job_enrich.py\n"
        "@@ -7,4 +7,4 @@\n"
        " DOCTRINE A — NEVER penalize \"overqualification\". Anywhere.\n"
        "-DOCTRINE B — NEVER stack penalties on the SAME underlying fact.\n"
        "+DOCTRINE B — Never EVER stack penalties on the SAME upward gap.\n"
        " DOCTRINE C — Generic titles describing data labeling → score=0.\n"
        " \n"
    )
    assert aps._extract_diff(with_deletion) is None

    # Sanity: the same diff but with the `-` line removed parses fine.
    additions_only = (
        "--- a/skill/job-search/scripts/job_enrich.py\n"
        "+++ b/skill/job-search/scripts/job_enrich.py\n"
        "@@ -7,3 +7,4 @@\n"
        " DOCTRINE A — NEVER penalize \"overqualification\". Anywhere.\n"
        " DOCTRINE B — NEVER stack penalties on the SAME underlying fact.\n"
        " DOCTRINE C — Generic titles describing data labeling → score=0.\n"
        "+DOCTRINE D — additional rule.\n"
    )
    assert aps._extract_diff(additions_only) is not None
