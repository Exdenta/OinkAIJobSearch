"""Tier-2 tests: Anthropic-SDK prompt-caching scoring path.

The SDK+caching path (skill/job-search/scripts/sdk_scoring.py) is GATED
behind ANTHROPIC_API_KEY because it bills real dollars, whereas the live
bot runs on the user's Claude Code subscription (no API key). The most
important guarantee here is the NO-KEY one: with the key unset, behavior
is byte-identical to today's `claude -p` CLI path.

NO real `claude` CLI, NO real Anthropic API calls — the `anthropic`
package is replaced by an in-process fake injected into sys.modules, and
the scoring transport is asserted by inspecting which fake got called.

These run under the canonical `pytest skill/job-search/scripts/tests -q`
command. A `pytest.fixture(autouse=True)` guarantees the fake module and
the ANTHROPIC_API_KEY env var are torn down after EVERY test (pass or
fail) so this file can never pollute the rest of the suite.

Pins:
  1. ANTHROPIC_API_KEY UNSET -> sdk_available() False; _run_scoring_batch
     takes the CLI path. SDK never touched.
  2. Model-ID mapping: haiku->claude-haiku-4-5, sonnet->claude-sonnet-4-6;
     concrete IDs pass through; None/"" -> None (forces CLI fallback).
  3. Prompt-split identity: stable_prefix + volatile_suffix == _PROMPT.format(...).
  4. Key SET + mocked anthropic -> SDK path; cache_control:{type:ephemeral}
     on the rubric+profile prefix, ABSENT on the per-job content; mapped
     model id; verdict shape == CLI contract.
  5. SDK error -> score_batch None -> CLI fallback (never crashes).
  6. enrich_jobs_ai end-to-end via the SDK returns CLI-shape verdicts.
  7. Telemetry: cache_read/creation tokens + token-derived cost recorded;
     degrades gracefully when record_claude_call lacks Tier-3 columns.
"""
from __future__ import annotations

import json
import re
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

@dataclass
class _FakeJob:
    """Minimal stand-in for dedupe.Job — only attributes job_enrich reads."""
    external_id: str
    title: str = "Mid Frontend Engineer"
    company: str = "Acme"
    location: str = "Remote · EU"
    salary: str = ""
    url: str = "https://example.com/job"
    snippet: str = "React, TypeScript, Vue. Remote EU."

    @property
    def job_id(self) -> str:
        return f"jid-{self.external_id}"

    @property
    def source(self) -> str:
        return "smoke"


def _make_jobs(n: int) -> list[_FakeJob]:
    return [_FakeJob(external_id=f"ext-{i:04d}") for i in range(n)]


def _brief(j) -> dict:
    """Tiny brief so the job id lands in the suffix text the fake inspects."""
    return {"external_id": j.external_id, "title": j.title}


def _results_json(present_ids) -> str:
    """The model's assistant text: strict-JSON {"results":[...]}.

    `present_ids` are the ids the PROMPT handed us — since the id-mismatch fix
    those are opaque per-batch keys ("j1"…"jN"), not external_ids. A real model
    echoes back what it was given, so the fake does too.
    """
    items = [{
        "id": jid,
        "match_score": 4,
        "why_match": "react + ts match",
        "why_mismatch": "",
        "key_details": {
            "stack": "React, TS", "seniority": "middle",
            "remote_policy": "remote", "location": "",
            "salary": "", "visa_support": "",
            "language": "", "standout": "",
        },
    } for jid in present_ids]
    return json.dumps({"results": items})


class _FakeUsage:
    def __init__(self, **kw):
        self.input_tokens = kw.get("input_tokens", 0)
        self.output_tokens = kw.get("output_tokens", 0)
        self.cache_creation_input_tokens = kw.get("cache_creation_input_tokens", 0)
        self.cache_read_input_tokens = kw.get("cache_read_input_tokens", 0)


class _FakeTextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMessage:
    def __init__(self, text, usage):
        self.content = [_FakeTextBlock(text)]
        self.usage = usage
        self.stop_reason = "end_turn"


class _FakeMessages:
    """Captures create() kwargs and returns a canned message (or raises)."""
    def __init__(self, parent):
        self._parent = parent

    def create(self, **kwargs):
        self._parent.create_calls.append(kwargs)
        if self._parent.raise_exc is not None:
            raise self._parent.raise_exc
        msgs = kwargs.get("messages") or []
        blocks = (msgs[0].get("content") if msgs else []) or []
        suffix_text = blocks[1]["text"] if len(blocks) > 1 else ""
        # Echo back the ids the prompt actually carried (opaque "j1"…"jN"),
        # exactly as the model would.
        present = re.findall(r'"external_id":\s*"([^"]+)"', suffix_text)
        usage = _FakeUsage(
            input_tokens=120,                  # uncached remainder
            output_tokens=80,
            cache_creation_input_tokens=9000,  # rubric written to cache
            cache_read_input_tokens=0,
        )
        return _FakeMessage(_results_json(present), usage)


class _FakeClient:
    def __init__(self, parent):
        self._parent = parent
        self.messages = _FakeMessages(parent)

    def with_options(self, **kw):
        self._parent.with_options_calls.append(kw)
        return self


class _FakeAnthropicModule(types.ModuleType):
    """Drop-in replacement for `import anthropic`."""
    def __init__(self, parent):
        super().__init__("anthropic")

        def _ctor(**kw):
            parent.ctor_calls.append(kw)
            return _FakeClient(parent)

        self.Anthropic = _ctor


class _MockSpec:
    """Bundles the fake-module state for one test."""
    def __init__(self, all_jobs, raise_exc=None):
        self.all_jobs = all_jobs
        self.raise_exc = raise_exc
        self.create_calls: list[dict] = []
        self.with_options_calls: list[dict] = []
        self.ctor_calls: list[dict] = []


# ---------------------------------------------------------------------------
# Fixtures — guarantee env + sys.modules cleanup after EVERY test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Each test starts with no key + no fake anthropic, and any state it
    installs is torn down afterwards (pass OR fail), so this file cannot
    leak into the rest of the suite. `monkeypatch.delenv` handles the env
    var; we restore sys.modules['anthropic'] and reset sdk_scoring's
    client cache by hand on teardown."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    had_anthropic = sys.modules.get("anthropic")
    yield
    if had_anthropic is None:
        sys.modules.pop("anthropic", None)
    else:
        sys.modules["anthropic"] = had_anthropic
    if "sdk_scoring" in sys.modules:
        sys.modules["sdk_scoring"]._CLIENT = None


def _install_fake(spec: _MockSpec, monkeypatch):
    """Inject the fake `anthropic` and reset sdk_scoring's client cache."""
    monkeypatch.setitem(sys.modules, "anthropic", _FakeAnthropicModule(spec))
    import sdk_scoring
    sdk_scoring._CLIENT = None


# ---------------------------------------------------------------------------
# 1. No key -> CLI path (the no-risk production default)
# ---------------------------------------------------------------------------

def test_no_key_takes_cli_path(monkeypatch):
    # Even with a fake anthropic importable, no key MUST mean CLI path.
    spec = _MockSpec(all_jobs=_make_jobs(1))
    _install_fake(spec, monkeypatch)

    import sdk_scoring
    import job_enrich

    assert sdk_scoring.sdk_available() is False

    cli_calls: list[str] = []

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kw):
        cli_calls.append(prompt)
        return json.dumps({"result": json.dumps({"results": []})})

    monkeypatch.setattr(job_enrich, "wrapped_run_p", fake_wrapped_run_p)

    out = job_enrich._run_scoring_batch(
        "job_enrich:haiku", "PREFIX", "SUFFIX", timeout_s=240, model="haiku",
    )
    assert out is not None
    # CLI path reassembles the single prompt = prefix + suffix (the
    # byte-identical-to-before contract).
    assert cli_calls == ["PREFIXSUFFIX"]
    # SDK never invoked.
    assert spec.create_calls == []


# ---------------------------------------------------------------------------
# 2. Model-ID mapping
# ---------------------------------------------------------------------------

def test_model_id_mapping():
    import sdk_scoring
    assert sdk_scoring._resolve_sdk_model("haiku") == "claude-haiku-4-5"
    assert sdk_scoring._resolve_sdk_model("sonnet") == "claude-sonnet-4-6"
    assert sdk_scoring._resolve_sdk_model("claude-opus-4-8") == "claude-opus-4-8"
    # SDK needs an explicit model — None/"" force the CLI fallback.
    assert sdk_scoring._resolve_sdk_model(None) is None
    assert sdk_scoring._resolve_sdk_model("") is None


# ---------------------------------------------------------------------------
# 3. Prompt-split identity (quality-preservation invariant)
# ---------------------------------------------------------------------------

def test_prompt_split_identity():
    import job_enrich
    resume = "Mid frontend, React/TS/Vue, Bilbao."
    prefs = "Remote EU. No senior."
    jobs_json = json.dumps([{"external_id": "ext-0", "title": "Dev"}])
    prefix, suffix = job_enrich._build_scoring_prompt(resume, prefs, jobs_json)

    full = job_enrich._PROMPT.format(
        resume=resume, prefs_text=prefs, jobs_json=jobs_json,
    )
    assert prefix + suffix == full
    assert prefix.endswith("=== JOBS (JSON array) ===\n")
    assert suffix == jobs_json
    assert resume in prefix and prefs in prefix
    # The resume cap still applies inside the prefix.
    long_resume = "X" * (job_enrich._MAX_RESUME_PROMPT_CHARS + 5000)
    p2, _ = job_enrich._build_scoring_prompt(long_resume, "", "[]")
    assert ("X" * job_enrich._MAX_RESUME_PROMPT_CHARS) in p2
    assert ("X" * (job_enrich._MAX_RESUME_PROMPT_CHARS + 1)) not in p2


# ---------------------------------------------------------------------------
# 4. Key set + mock -> SDK path, cache_control placement, verdict shape
# ---------------------------------------------------------------------------

def test_sdk_path_caches_prefix_only(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    jobs = _make_jobs(3)
    spec = _MockSpec(all_jobs=jobs)
    _install_fake(spec, monkeypatch)

    import sdk_scoring
    import job_enrich

    assert sdk_scoring.sdk_available() is True

    # If the SDK path were skipped, this CLI fake would fire — it must NOT.
    cli_called = {"n": 0}

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kw):
        cli_called["n"] += 1
        return json.dumps({"result": json.dumps({"results": []})})

    monkeypatch.setattr(job_enrich, "wrapped_run_p", fake_wrapped_run_p)

    prefix, suffix = job_enrich._build_scoring_prompt(
        "resume", "prefs", json.dumps([_brief(j) for j in jobs]),
    )
    envelope = job_enrich._run_scoring_batch(
        "job_enrich:sonnet", prefix, suffix, timeout_s=200, model="sonnet",
    )

    assert envelope is not None
    assert cli_called["n"] == 0
    assert len(spec.create_calls) == 1

    call = spec.create_calls[0]
    assert call["model"] == "claude-sonnet-4-6"   # mapped from "sonnet"

    blocks = call["messages"][0]["content"]
    assert len(blocks) == 2
    # Cache breakpoint ON the stable rubric+profile prefix, ABSENT on jobs.
    assert blocks[0].get("cache_control") == {"type": "ephemeral"}
    assert "cache_control" not in blocks[1]
    assert blocks[0]["text"] == prefix
    assert blocks[1]["text"] == suffix
    # The cached block is the rubric+profile; the uncached block is the jobs.
    assert "CANDIDATE RESUME" in blocks[0]["text"]
    assert "=== JOBS (JSON array) ===" in blocks[0]["text"]
    assert jobs[0].external_id in blocks[1]["text"]
    assert jobs[0].external_id not in blocks[0]["text"]

    # Verdict shape parity: parse the SDK envelope exactly as job_enrich does.
    body = job_enrich.extract_assistant_text(envelope)
    data = job_enrich.parse_json_block(body)
    assert isinstance(data, dict) and isinstance(data.get("results"), list)
    assert len(data["results"]) == 3
    r0 = data["results"][0]
    assert set(r0.keys()) >= {
        "id", "match_score", "why_match", "why_mismatch", "key_details",
    }


# ---------------------------------------------------------------------------
# 5. SDK error -> graceful CLI fallback
# ---------------------------------------------------------------------------

def test_sdk_error_falls_back_to_cli(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    spec = _MockSpec(all_jobs=_make_jobs(2),
                     raise_exc=RuntimeError("simulated 429 storm"))
    _install_fake(spec, monkeypatch)

    import sdk_scoring
    import job_enrich

    # Direct: score_batch swallows the error and returns None.
    assert sdk_scoring.score_batch(
        None, "job_enrich:haiku", "PREFIX", "SUFFIX",
        model="haiku", timeout_s=30,
    ) is None

    # Through _run_scoring_batch: CLI fallback fires.
    cli_calls: list[str] = []

    def fake_wrapped_run_p(store, caller, prompt, *, timeout_s=None, **kw):
        cli_calls.append(prompt)
        return json.dumps({"result": json.dumps({"results": []})})

    monkeypatch.setattr(job_enrich, "wrapped_run_p", fake_wrapped_run_p)

    out = job_enrich._run_scoring_batch(
        "job_enrich:haiku", "PREFIX", "SUFFIX", timeout_s=30, model="haiku",
    )
    assert out is not None
    assert cli_calls == ["PREFIXSUFFIX"]


# ---------------------------------------------------------------------------
# 6. End-to-end through enrich_jobs_ai with the SDK mocked
# ---------------------------------------------------------------------------

def test_enrich_jobs_ai_end_to_end_via_sdk(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))

    jobs = _make_jobs(4)
    spec = _MockSpec(all_jobs=jobs)
    _install_fake(spec, monkeypatch)

    # forensic reads STATE_DIR at import — reload so it picks up tmp_path.
    sys.modules.pop("forensic", None)
    import job_enrich

    # Make the CLI path explode if ever taken — proves the SDK served
    # every batch end-to-end.
    def boom(*a, **k):
        raise AssertionError("CLI path taken though SDK was available")

    monkeypatch.setattr(job_enrich, "wrapped_run_p", boom)

    verdicts = job_enrich.enrich_jobs_ai(
        jobs=jobs,
        resume_text="Mid frontend React/TS.",
        prefs_text="Remote EU.",
        timeout_s=60,
        max_jobs_per_call=10,
        two_pass=False,   # single-pass Sonnet (pure-text scoring)
        workers=1,
    )
    assert len(verdicts) == 4
    for j in jobs:
        v = verdicts.get(j.external_id) or {}
        assert v.get("match_score") == 4
        # Exactly the CLI-path verdict contract.
        assert set(v.keys()) == {
            "match_score", "why_match", "why_mismatch", "key_details",
        }
        assert isinstance(v["key_details"], dict)
        assert "remote_policy" in v["key_details"]
    assert len(spec.create_calls) >= 1


# ---------------------------------------------------------------------------
# 7. Telemetry: cache tokens + token-derived cost recorded
# ---------------------------------------------------------------------------

def test_telemetry_records_cache_tokens_and_cost(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))

    jobs = _make_jobs(2)
    spec = _MockSpec(all_jobs=jobs)
    _install_fake(spec, monkeypatch)

    sys.modules.pop("forensic", None)
    import sdk_scoring
    from db import DB
    from telemetry import MonitorStore

    store = MonitorStore(DB(tmp_path / "jobs.db"))

    prefix, suffix = ("PREFIX " * 100), json.dumps([_brief(j) for j in jobs])
    envelope = sdk_scoring.score_batch(
        store, "job_enrich:sonnet", prefix, suffix,
        model="sonnet", timeout_s=60,
    )
    assert envelope is not None

    with store._db._conn() as c:
        row = c.execute(
            "SELECT * FROM claude_calls WHERE caller='job_enrich:sonnet'"
        ).fetchone()
    assert row is not None
    assert int(row["cache_creation_tokens"]) == 9000
    assert int(row["cache_read_tokens"]) == 0
    assert int(row["input_tokens"]) == 120
    assert int(row["output_tokens"]) == 80
    assert row["status"] == "ok"
    # Cost computed from REAL tokens incl. the cache write tier.
    # sonnet in-rate $3/Mtok, out $15/Mtok:
    #   120 uncached*3 + 9000 write*3*1.25 + 0 read + 80 out*15
    #   = 360 + 33750 + 0 + 1200 = 35310 micro-USD.
    assert int(row["cost_actual_us"]) == 35310


def test_telemetry_degrades_when_record_signature_old(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    spec = _MockSpec(all_jobs=_make_jobs(1))
    _install_fake(spec, monkeypatch)

    import sdk_scoring

    # A store whose record_claude_call ONLY accepts the core args
    # (simulating a pre-Tier-3 signature). score_batch must still return
    # the envelope and the core record call must succeed.
    recorded = {"core": 0, "full_attempts": 0}
    usage_keys = {"cost_actual_us", "input_tokens", "output_tokens",
                  "cache_read_tokens", "cache_creation_tokens", "num_turns"}

    class _OldStore:
        def record_claude_call(self, **kwargs):
            if usage_keys & set(kwargs):
                recorded["full_attempts"] += 1
                raise TypeError("unexpected keyword argument (old signature)")
            recorded["core"] += 1
            return 1

    envelope = sdk_scoring.score_batch(
        _OldStore(), "job_enrich:haiku", "PREFIX", "SUFFIX",
        model="haiku", timeout_s=30,
    )
    assert envelope is not None
    assert recorded["full_attempts"] == 1   # tried full signature first
    assert recorded["core"] == 1            # fell back to core signature
