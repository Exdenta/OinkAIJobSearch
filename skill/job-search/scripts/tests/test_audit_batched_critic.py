"""Tests for the batched-audit + runtime-critic flow in
`reanalyze_scoring_ai` (v2.6).

Covered:

  * `jobs` of size 44 → 5 batches of 10/10/10/10/4 (split arithmetic).
  * critic approves on round 1 for every batch → exactly N_batches
    scorer calls + N_batches critic calls.
  * critic rejects on round 1 then approves on round 2 → 2 scorer
    calls and 2 critic calls for the batch; the round-2 scorer prompt
    contains the previous round's critic feedback verbatim.
  * critic ALWAYS disagrees → after critic_rounds the function
    returns the last scorer output AND logs the documented warning.
  * scorer emits an out-of-range revised_score → critic flags it,
    scorer fixes it on round 2 → final output is clamped.
  * 3 batches × workers=2 — function still completes correctly under
    the ThreadPoolExecutor (proves the wiring isn't accidentally serial).
  * zero jobs → empty list, ZERO claude calls.
  * critic CLI returns None → audit accepts the scorer's output for
    that batch (and logs the warning).

All tests run hermetically — we monkeypatch
`job_enrich.wrapped_run_p` and decide via the `caller` arg whether
the call is a scorer or a critic. Mocks return JSON-strings shaped
like the real `claude -p --output-format json` envelope so the same
`extract_assistant_text` parser is exercised.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import job_enrich  # noqa: E402
from dedupe import Job  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(ext_id: str, *, title: str = "Engineer",
              company: str = "Acme", snippet: str = "snippet body") -> Job:
    return Job(
        source="linkedin",
        external_id=ext_id,
        title=title,
        company=company,
        location="Remote",
        url=f"https://example.com/{ext_id}",
        posted_at="2026-05-20",
        snippet=snippet,
    )


def _envelope(payload: dict | list) -> str:
    """Mimic the `claude -p --output-format json` envelope so
    `extract_assistant_text` round-trips through the `result` field."""
    return json.dumps({"result": json.dumps(payload, ensure_ascii=False)})


def _scorer_reviews(jobs_or_ids, *, score: int = 3,
                    delta: int = 0) -> list[dict]:
    """Build a list of scorer reviews for `jobs_or_ids`.

    The audit prompt hands the model OPAQUE per-batch ids ("j1"…"jN"), not
    external_ids (a percent-encoded URL doesn't survive the round trip — see
    `job_enrich._id_lookup`). A real model echoes back what it was given, so
    the fake does too: ids parsed from the prompt pass through verbatim, and
    a list of Jobs becomes its positional keys.

    `score` is the original score the audit sees in
    `enrichments_by_external_id`; `delta` shifts the revised_score
    so we can drive the verdict direction.
    """
    ids = [x if isinstance(x, str) else f"j{i}"
           for i, x in enumerate(jobs_or_ids, 1)]
    out = []
    revised = max(0, min(5, score + delta))
    if revised > score:
        verdict = "raise"
    elif revised < score:
        verdict = "lower"
    else:
        verdict = "agree"
    for jid in ids:
        out.append({
            "id": jid,
            "verdict": verdict,
            "revised_score": revised,
            "comment": "ok" if verdict == "agree" else "drift caught",
        })
    return out


def _make_enrichments(jobs: list[Job], score: int = 3) -> dict[str, dict]:
    return {
        j.external_id: {
            "match_score": score,
            "why_match": "stack match",
            "why_mismatch": "",
            "key_details": {},
        }
        for j in jobs
    }


class FakeCLI:
    """Stateful stand-in for `wrapped_run_p`.

    Routes calls to a scorer- or critic-handler based on the `caller`
    string the production code passes ("scoring_audit:b{idx}r{round}" vs
    "scoring_audit_critic:b{idx}r{round}").

    Records (caller, prompt) per call so tests can assert call counts,
    prompt contents, and parallelism (via the thread-id set).
    """

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        # Per-call model alias the production code passed (proves the
        # scorer/critic model knobs thread through to the CLI invocation).
        self.models: list[tuple[str, str | None]] = []
        self.thread_ids: set[int] = set()
        self._lock = threading.Lock()
        self.scorer_handler = None
        self.critic_handler = None

    def __call__(self, store, caller, prompt, *, timeout_s=None,
                 model=None, **kwargs):
        with self._lock:
            self.calls.append((caller, prompt))
            self.models.append((caller, model))
            self.thread_ids.add(threading.get_ident())
        if caller.startswith("scoring_audit_critic"):
            return self._dispatch(self.critic_handler, caller, prompt)
        if caller.startswith("scoring_audit"):
            return self._dispatch(self.scorer_handler, caller, prompt)
        raise AssertionError(f"unexpected caller {caller!r}")

    @staticmethod
    def _dispatch(handler, caller, prompt):
        if handler is None:
            raise AssertionError(f"no handler set for caller={caller!r}")
        return handler(caller, prompt)

    def n_scorer_calls(self) -> int:
        return sum(1 for c, _ in self.calls if c.startswith("scoring_audit:"))

    def n_critic_calls(self) -> int:
        return sum(1 for c, _ in self.calls
                   if c.startswith("scoring_audit_critic"))

    def scorer_models(self) -> set[str | None]:
        return {m for c, m in self.models
                if c.startswith("scoring_audit:")}

    def critic_models(self) -> set[str | None]:
        return {m for c, m in self.models
                if c.startswith("scoring_audit_critic")}


@pytest.fixture
def fake_cli(monkeypatch):
    cli = FakeCLI()
    monkeypatch.setattr(job_enrich, "wrapped_run_p", cli)
    return cli


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def _extract_batch_ext_ids(prompt: str) -> list[str]:
    """Parse the JSON items payload out of a scorer prompt and
    return its external_ids in order.

    The audit-scorer prompt embeds the items list as JSON between the
    untrusted-data delimiters; we pull the first JSON array we find
    after `BEGIN UNTRUSTED DATA` and read its `id` fields. Robust to
    duplicate `jN` substrings (e.g. `j1` vs `j10` collisions).
    """
    marker = "BEGIN UNTRUSTED DATA"
    start = prompt.find(marker)
    assert start != -1, "scorer prompt missing untrusted-data marker"
    array_start = prompt.find("[", start)
    array_end = prompt.find("]", array_start)
    assert array_start != -1 and array_end != -1
    items = json.loads(prompt[array_start:array_end + 1])
    return [str(it.get("id")) for it in items]


def test_batch_split_44_items_into_5_batches(fake_cli):
    """44 jobs at batch_size=10 → batches of 10/10/10/10/4."""
    jobs = [_make_job(f"j{n}") for n in range(44)]
    enr = _make_enrichments(jobs, score=3)

    batch_sizes_observed: list[int] = []

    def scorer(caller, prompt):
        ext_ids = _extract_batch_ext_ids(prompt)
        batch_sizes_observed.append(len(ext_ids))
        sub = [j for j in jobs if j.external_id in set(ext_ids)]
        return _envelope({"reviews": _scorer_reviews(
            sub, score=3, delta=0
        )})

    def critic(caller, prompt):
        return _envelope({"approve": True, "issues": []})

    fake_cli.scorer_handler = scorer
    fake_cli.critic_handler = critic

    out = job_enrich.reanalyze_scoring_ai(
        jobs, enr, resume_text="r", prefs_text="p",
        batch_size=10, workers=1, critic_rounds=3,
    )

    # 5 scorer calls + 5 critic calls (one round each).
    assert fake_cli.n_scorer_calls() == 5
    assert fake_cli.n_critic_calls() == 5
    # Batches are 10/10/10/10/4 (order may vary because the parallel
    # path is serial here; we sort to compare as a multiset).
    assert sorted(batch_sizes_observed) == [4, 10, 10, 10, 10]
    # All 44 ids returned, deduped, in input order.
    assert [r["id"] for r in out] == [j.external_id for j in jobs]


def test_critic_approves_round_1_all_batches(fake_cli):
    jobs = [_make_job(f"j{n}") for n in range(25)]
    enr = _make_enrichments(jobs, score=2)

    def scorer(caller, prompt):
        return _envelope({"reviews": _scorer_reviews(
            [j for j in jobs if j.external_id in prompt], score=2, delta=0
        )})

    def critic(caller, prompt):
        return _envelope({"approve": True, "issues": []})

    fake_cli.scorer_handler = scorer
    fake_cli.critic_handler = critic

    out = job_enrich.reanalyze_scoring_ai(
        jobs, enr, resume_text="r", prefs_text="p",
        batch_size=10, workers=2, critic_rounds=3,
    )

    # 25 / 10 → 3 batches.
    assert fake_cli.n_scorer_calls() == 3
    assert fake_cli.n_critic_calls() == 3
    assert len(out) == 25
    # Every review is 'agree' since delta=0.
    assert all(r["verdict"] == "agree" for r in out)


def test_critic_rejects_then_approves(fake_cli):
    """Round 1: scorer mis-labels verdict; critic flags; scorer fixes.

    Asserts that round-2 scorer prompt contains the critic's feedback
    verbatim per the spec.
    """
    jobs = [_make_job(f"j{n}") for n in range(3)]
    enr = _make_enrichments(jobs, score=3)

    rounds_seen = {"scorer": 0, "critic": 0}

    def scorer(caller, prompt):
        rounds_seen["scorer"] += 1
        if rounds_seen["scorer"] == 1:
            # Bad output: verdict='raise' but revised_score == original.
            return _envelope({"reviews": [
                {"id": j.external_id, "verdict": "raise",
                 "revised_score": 3, "comment": "drifty"}
                for j in jobs
            ]})
        # Round 2: prompt MUST carry the critic's feedback block.
        assert "CRITIC FEEDBACK FROM PREVIOUS ROUND" in prompt
        assert "verdict==raise but score unchanged" in prompt
        # Now emit the clean output.
        return _envelope({"reviews": _scorer_reviews(jobs, score=3, delta=0)})

    def critic(caller, prompt):
        rounds_seen["critic"] += 1
        if rounds_seen["critic"] == 1:
            return _envelope({
                "approve": False,
                "issues": [
                    {"id": jobs[0].external_id,
                     "problem": "verdict==raise but score unchanged"},
                ],
            })
        return _envelope({"approve": True, "issues": []})

    fake_cli.scorer_handler = scorer
    fake_cli.critic_handler = critic

    out = job_enrich.reanalyze_scoring_ai(
        jobs, enr, resume_text="r", prefs_text="p",
        batch_size=10, workers=1, critic_rounds=3,
    )

    assert fake_cli.n_scorer_calls() == 2
    assert fake_cli.n_critic_calls() == 2
    # Final output is the round-2 clean reviews.
    assert len(out) == 3
    assert all(r["verdict"] == "agree" for r in out)


def test_max_rounds_falls_back_to_last_scorer_output(fake_cli, caplog):
    jobs = [_make_job(f"j{n}") for n in range(2)]
    enr = _make_enrichments(jobs, score=2)

    def scorer(caller, prompt):
        return _envelope({"reviews": _scorer_reviews(jobs, score=2, delta=1)})

    def critic(caller, prompt):
        return _envelope({
            "approve": False,
            "issues": [{"id": jobs[0].external_id, "problem": "still wrong"}],
        })

    fake_cli.scorer_handler = scorer
    fake_cli.critic_handler = critic

    with caplog.at_level(logging.WARNING, logger=job_enrich.log.name):
        out = job_enrich.reanalyze_scoring_ai(
            jobs, enr, resume_text="r", prefs_text="p",
            batch_size=10, workers=1, critic_rounds=3,
        )

    # Exactly 3 scorer + 3 critic calls (critic_rounds=3 → 3 rounds).
    assert fake_cli.n_scorer_calls() == 3
    assert fake_cli.n_critic_calls() == 3
    # No exception, last scorer output flows through.
    assert len(out) == 2
    # Documented warning fired.
    assert any(
        "critic disagreement persisted after 3 rounds" in rec.message
        for rec in caplog.records
    ), caplog.records


def test_format_violation_caught_and_fixed(fake_cli):
    """Scorer returns revised_score=7 on round 1 (out of [0,5]);
    critic flags it; scorer fixes on round 2; final output clamped/
    corrected so revised_score is in range.
    """
    jobs = [_make_job(f"j{n}") for n in range(1)]
    enr = _make_enrichments(jobs, score=3)

    rounds = {"scorer": 0}

    def scorer(caller, prompt):
        rounds["scorer"] += 1
        if rounds["scorer"] == 1:
            return _envelope({"reviews": [
                {"id": jobs[0].external_id, "verdict": "raise",
                 "revised_score": 7, "comment": "way too high"},
            ]})
        return _envelope({"reviews": [
            {"id": jobs[0].external_id, "verdict": "raise",
             "revised_score": 4, "comment": "fixed"},
        ]})

    critic_rounds_seen = {"n": 0}

    def critic(caller, prompt):
        critic_rounds_seen["n"] += 1
        if critic_rounds_seen["n"] == 1:
            return _envelope({"approve": False, "issues": [
                {"id": jobs[0].external_id,
                 "problem": "revised_score=7 is outside [0,5]"},
            ]})
        return _envelope({"approve": True, "issues": []})

    fake_cli.scorer_handler = scorer
    fake_cli.critic_handler = critic

    out = job_enrich.reanalyze_scoring_ai(
        jobs, enr, resume_text="r", prefs_text="p",
        batch_size=10, workers=1, critic_rounds=3,
    )

    assert len(out) == 1
    r = out[0]
    assert 0 <= r["revised_score"] <= 5
    assert r["revised_score"] == 4
    assert r["verdict"] == "raise"


def test_concurrent_batches(fake_cli):
    """Three batches × workers=2 — function still returns the full set.

    Counts unique thread ids touched by the fake CLI to prove the
    ThreadPoolExecutor wiring is real and not silently serial.
    """
    jobs = [_make_job(f"j{n}") for n in range(30)]
    enr = _make_enrichments(jobs, score=3)

    # Tiny blocking gate so two batches genuinely overlap on the pool.
    barrier = threading.Barrier(2, timeout=5)

    def scorer(caller, prompt):
        # Round 1 only — synchronise two workers to ensure parallel
        # dispatch is real. Third worker reaches the gate as a relief
        # batch — gate is only sized for 2, but timeout drops it
        # through after 5s on the third call.
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return _envelope({"reviews": _scorer_reviews(
            [j for j in jobs if j.external_id in prompt], score=3, delta=0
        )})

    def critic(caller, prompt):
        return _envelope({"approve": True, "issues": []})

    fake_cli.scorer_handler = scorer
    fake_cli.critic_handler = critic

    out = job_enrich.reanalyze_scoring_ai(
        jobs, enr, resume_text="r", prefs_text="p",
        batch_size=10, workers=2, critic_rounds=3,
    )

    assert len(out) == 30
    assert fake_cli.n_scorer_calls() == 3
    assert fake_cli.n_critic_calls() == 3
    # At least two distinct threads dispatched batches — proves the
    # pool isn't silently serial (workers=2 → at least 2 threads).
    assert len(fake_cli.thread_ids) >= 2, fake_cli.thread_ids


def test_returns_empty_on_no_jobs(fake_cli):
    fake_cli.scorer_handler = lambda *_: pytest.fail(
        "scorer should NOT be called for empty jobs"
    )
    fake_cli.critic_handler = lambda *_: pytest.fail(
        "critic should NOT be called for empty jobs"
    )

    out = job_enrich.reanalyze_scoring_ai(
        [], {}, resume_text="r", prefs_text="p",
        batch_size=10, workers=4, critic_rounds=3,
    )
    assert out == []
    assert fake_cli.calls == []


def test_critic_cli_failure_returns_scorer_output(fake_cli, caplog):
    jobs = [_make_job(f"j{n}") for n in range(2)]
    enr = _make_enrichments(jobs, score=3)

    def scorer(caller, prompt):
        return _envelope({"reviews": _scorer_reviews(jobs, score=3, delta=0)})

    def critic(caller, prompt):
        # CLI failure: real run_p returns None for missing-cli / timeout
        # / non-zero exit. Production wraps that to None and our hook
        # returns None to match.
        return None

    fake_cli.scorer_handler = scorer
    fake_cli.critic_handler = critic

    with caplog.at_level(logging.WARNING, logger=job_enrich.log.name):
        out = job_enrich.reanalyze_scoring_ai(
            jobs, enr, resume_text="r", prefs_text="p",
            batch_size=10, workers=1, critic_rounds=3,
        )

    # One scorer + one critic call — critic None → treated as approve
    # so the loop exits at round 1.
    assert fake_cli.n_scorer_calls() == 1
    assert fake_cli.n_critic_calls() == 1
    assert len(out) == 2
    assert any("critic CLI returned None" in rec.message
               for rec in caplog.records), caplog.records


def test_model_knobs_thread_to_cli_scorer_and_critic(fake_cli):
    """The `model` and `critic_model` args reach the right CLI calls.

    Proves the cost knobs are honored end-to-end: every scorer call is
    dispatched with `model=`, every critic call with `critic_model=`,
    and the two are kept distinct (no cross-wiring). Drives a round-2
    rerun so BOTH rounds' scorer + critic models are checked.
    """
    jobs = [_make_job(f"j{n}") for n in range(2)]
    enr = _make_enrichments(jobs, score=3)

    rounds = {"critic": 0}

    def scorer(caller, prompt):
        return _envelope({"reviews": _scorer_reviews(jobs, score=3, delta=0)})

    def critic(caller, prompt):
        rounds["critic"] += 1
        if rounds["critic"] == 1:
            # Reject round 1 so a round 2 (re-dispatching both models)
            # actually happens.
            return _envelope({"approve": False, "issues": [
                {"id": jobs[0].external_id, "problem": "recheck"},
            ]})
        return _envelope({"approve": True, "issues": []})

    fake_cli.scorer_handler = scorer
    fake_cli.critic_handler = critic

    out = job_enrich.reanalyze_scoring_ai(
        jobs, enr, resume_text="r", prefs_text="p",
        batch_size=10, workers=1, critic_rounds=3,
        model="sonnet", critic_model="sonnet",
    )

    assert len(out) == 2
    # Two rounds fired (critic rejected round 1).
    assert fake_cli.n_scorer_calls() == 2
    assert fake_cli.n_critic_calls() == 2
    # Scorer ran on the scorer model; critic ran on the critic model.
    # No None leaked (would mean the knob never reached wrapped_run_p).
    assert fake_cli.scorer_models() == {"sonnet"}
    assert fake_cli.critic_models() == {"sonnet"}


def test_distinct_scorer_and_critic_models(fake_cli):
    """When scorer and critic models differ, each CLI call gets the
    correct one — guards against the two knobs being cross-wired."""
    jobs = [_make_job(f"j{n}") for n in range(3)]
    enr = _make_enrichments(jobs, score=2)

    def scorer(caller, prompt):
        return _envelope({"reviews": _scorer_reviews(jobs, score=2, delta=0)})

    def critic(caller, prompt):
        return _envelope({"approve": True, "issues": []})

    fake_cli.scorer_handler = scorer
    fake_cli.critic_handler = critic

    out = job_enrich.reanalyze_scoring_ai(
        jobs, enr, resume_text="r", prefs_text="p",
        batch_size=10, workers=1, critic_rounds=2,
        model="opus", critic_model="sonnet",
    )

    assert len(out) == 3
    assert fake_cli.scorer_models() == {"opus"}
    assert fake_cli.critic_models() == {"sonnet"}


def test_default_audit_knobs_are_sonnet_and_two_rounds():
    """defaults.py wires the post-send audit to the cheaper models +
    fewer rounds. Locks the Tier-1 cost knobs so a future edit can't
    silently bump the audit back onto Opus."""
    import defaults  # noqa: PLC0415

    d = defaults.DEFAULTS
    assert d["ai_scoring_audit_model"] == "sonnet"
    assert d["ai_scoring_audit_critic_model"] == "sonnet"
    assert d["ai_scoring_audit_critic_rounds"] == 2
