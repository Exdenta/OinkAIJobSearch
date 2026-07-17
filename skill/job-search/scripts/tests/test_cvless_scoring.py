"""CV-less users must still get scored.

The onboarding wizard lets people finish without uploading a PDF ("Skip for
now"). Before this, `enrich_jobs_ai` bailed on the empty resume and returned
{} — and since AI scoring is the SINGLE matching gate (see
`search_jobs.post_filter`), those users got zero jobs on every run, forever.

Two halves of the fix, one test each:
  * `profile_as_resume` renders the profile as a RESUME-block substitute.
  * `enrich_jobs_ai` bails only when resume AND prefs are both empty.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from dedupe import Job  # noqa: E402
import job_enrich  # noqa: E402
from user_profile import profile_as_resume  # noqa: E402


# Shaped like the real thing: chat 8726369991, who tapped through the wizard
# and skipped the CV step.
PROFILE = {
    "ideal_fit_paragraph": "Staff-level interior designer role with hybrid "
                           "work option in Bilbao or remote within Spain.",
    "primary_role": "interior designer",
    "target_levels": ["senior", "lead", "staff"],
    "years_experience": 8,
    "stack_primary": ["interior design", "autocad", "sketchup"],
    "stack_secondary": ["revit", "photoshop"],
    "title_must_match": ["interior designer", "interior architect"],
}
PREFS = "staff-level Interiorista, hybrid work OK, location: Bilbao."


def _job() -> Job:
    return Job(
        source="linkedin", external_id="1", title="Interior Designer",
        company="Studio", location="Bilbao", url="https://x/1",
        posted_at="", snippet="", salary="",
    )


def test_profile_as_resume_carries_the_scoring_signal():
    text = profile_as_resume(PROFILE)
    # The scorer needs role, level, experience and skills to grade a posting.
    assert "interior designer" in text.lower()
    assert "staff" in text.lower()
    assert "8" in text
    assert "autocad" in text.lower()
    # It must announce itself as a profile, not masquerade as a parsed CV.
    assert "No CV on file" in text
    # No profile → no substitute; the caller falls back to the PREFS-only path.
    assert profile_as_resume(None) == ""
    assert profile_as_resume({}) == ""


def test_enrich_bails_only_when_resume_and_prefs_are_both_empty(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_pool(jobs, resume_text, prefs_text, *a, **kw):
        calls.append((resume_text, prefs_text))
        return {j.external_id: {"match_score": 4} for j in jobs}

    monkeypatch.setattr(job_enrich, "_enrich_pool", fake_pool)

    # Nothing to score against → bail without calling the model.
    assert job_enrich.enrich_jobs_ai([_job()], "", "") == {}
    assert calls == []

    # Prefs alone (the wizard always writes them) → score.
    out = job_enrich.enrich_jobs_ai([_job()], "", PREFS)
    assert out and out["1"]["match_score"] == 4
    assert calls[-1][1] == PREFS

    # Profile standing in for the resume → score, with the profile as RESUME.
    out = job_enrich.enrich_jobs_ai([_job()], profile_as_resume(PROFILE), PREFS)
    assert out and out["1"]["match_score"] == 4
    assert "autocad" in calls[-1][0].lower()
