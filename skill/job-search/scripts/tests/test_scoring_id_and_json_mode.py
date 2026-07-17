"""Defects that silently ate verdicts on the triage pass.

1. ID MISMATCH. The correlation id handed to the model was the posting URL.
   LinkedIn percent-encodes non-ASCII slugs ("t%C3%A9cnico"); the model echoes
   back the decoded form ("técnico"), so `ext_id not in valid_ids` dropped the
   verdict on the floor. Measured 6 of 10 lost on a real Spanish batch.
   Fix: send a short opaque id ("j1"…"jN") and map back.

"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from dedupe import Job  # noqa: E402
import job_enrich  # noqa: E402


def _jobs():
    # A percent-encoded non-ASCII URL — the exact shape that broke.
    return [
        Job(source="linkedin", external_id="https://es.linkedin.com/jobs/view/t%C3%A9cnico-4439148195",
            title="Técnico", company="Mecalux", location="Barcelona",
            url="https://es.linkedin.com/jobs/view/t%C3%A9cnico-4439148195",
            posted_at="", snippet="", salary=""),
        Job(source="linkedin", external_id="https://es.linkedin.com/jobs/view/plain-4440005288",
            title="Frontend Engineer", company="Blaine", location="Remote",
            url="https://es.linkedin.com/jobs/view/plain-4440005288",
            posted_at="", snippet="", salary=""),
    ]


def test_briefs_carry_short_ids_not_urls():
    jobs = _jobs()
    key_by_ext, lookup = job_enrich._id_lookup(jobs)
    briefs = job_enrich._briefs_with_short_ids(jobs, key_by_ext)

    assert [b["external_id"] for b in briefs] == ["j1", "j2"]
    # The model can echo any of these three and we still land on the right job.
    assert lookup["j1"] == jobs[0].external_id                       # short id
    assert lookup[jobs[0].external_id] == jobs[0].external_id        # verbatim
    decoded = "https://es.linkedin.com/jobs/view/técnico-4439148195"
    assert lookup[decoded] == jobs[0].external_id                    # decoded URL


def test_opaque_key_wins_when_an_external_id_collides_with_it():
    """An external_id may itself look like "j1". Since "j1" is what we SENT,
    our meaning must win — aliasing the other way shifted every verdict by one
    job (caught by test_audit_batched_critic, whose ids are "j0","j1",…)."""
    jobs = [
        Job(source="s", external_id="j0", title="A", company="", location="",
            url="", posted_at="", snippet="", salary=""),
        Job(source="s", external_id="j1", title="B", company="", location="",
            url="", posted_at="", snippet="", salary=""),
    ]
    _, lookup = job_enrich._id_lookup(jobs)
    assert lookup["j1"] == "j0"   # the key we sent for the FIRST job
    assert lookup["j2"] == "j1"   # the key we sent for the SECOND job


def test_decoded_url_verdict_is_no_longer_dropped(monkeypatch):
    """The regression: model answers with a decoded URL id → verdict kept."""
    jobs = _jobs()

    def fake_batch(caller, prefix, suffix, *, timeout_s, model):
        # Echo ids decoded, not encoded — the way small models actually do.
        return json.dumps({"result": json.dumps({"results": [
            {"id": "https://es.linkedin.com/jobs/view/técnico-4439148195",
             "match_score": 3, "why_match": "ok", "why_mismatch": "",
             "key_details": {}},
            {"id": "j2", "match_score": 5, "why_match": "great",
             "why_mismatch": "", "key_details": {}},
        ]})})

    monkeypatch.setattr(job_enrich, "_run_scoring_batch", fake_batch)
    out, reason = job_enrich._enrich_one_chunk(jobs, "resume", "prefs", 60)

    assert reason == job_enrich._BATCH_OK
    assert out[jobs[0].external_id]["match_score"] == 3   # was silently dropped
    assert out[jobs[1].external_id]["match_score"] == 5

