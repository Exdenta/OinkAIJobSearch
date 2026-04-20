#!/usr/bin/env python3
"""Offline smoke test for market_research.py.

Covers every piece that doesn't need the real `claude` CLI:

  1. _validate_manager_report — good fixture passes
  2. _validate_manager_report — 6 mutations surface specific error substrings
  3. per-worker validators — good fixture for each of 10 topics passes,
     a deliberately broken fixture for each topic fails with a specific error
  4. market_research_sync happy path — stubbed run_p_with_tools → status=ok
     and manager_report present
  5. market_research_sync partial — 3 workers return None → status=partial
  6. market_research_sync catastrophic — 6 workers fail → status=failed
  7. run_worker retry-on-invalid — stub yields bad JSON first then valid,
     final status=ok, call count=2
  8. run_p_with_tools CLI flag fallback — subprocess.run raises
     "unrecognized arguments" on the first call, succeeds on the second
     without the tool flags
  9. run_all overall timeout — slow worker + short deadline → failed entries
 10. source dedup — 5 URLs all identical fails validator's "distinct URLs" check

No Telegram, no network, no real Claude CLI. Exits non-zero on any failure.
"""
from __future__ import annotations

import copy
import json
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import market_research as mr  # noqa: E402
import claude_cli  # noqa: E402
from market_research import (  # noqa: E402
    ResearchRun,
    WORKERS,
    WorkerSpec,
    _validate_manager_report,
    _validate_demand,
    _validate_history,
    _validate_current_trends,
    _validate_skills_match,
    _validate_projections,
    _validate_salary_home,
    _validate_salary_neighbors,
    _validate_companies,
    _validate_hiring_bar,
    _validate_upskilling,
    market_research_sync,
    run_all,
    run_worker,
    synthesize_with_manager,
)


failures: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  OK  {label}")
    else:
        print(f"  FAIL {label}")
        failures.append(label)


# ---------------------------------------------------------------------------
# Fixture factories
# ---------------------------------------------------------------------------

def _sources(n: int = 5, base: str = "https://example.com/a") -> list[dict]:
    return [
        {
            "title": f"Source {i}",
            "url": f"{base}-{i}",
            "date": "2026-03-01",
            "snippet": f"snippet {i}",
        }
        for i in range(n)
    ]


def _demand() -> dict:
    return {
        "topic": "demand",
        "role_family": "frontend engineer",
        "total_open_postings_estimate": 1200,
        "postings_by_level": {"junior": 100, "mid": 500, "senior": 500, "lead": 100},
        "top_employers": [{"name": "Foo", "count": 30}],
        "headline_summary": "Healthy demand.",
        "confidence": "medium",
        "sources": _sources(5, "https://linkedin.com/jobs"),
    }


def _history() -> dict:
    return {
        "topic": "history",
        "timeline": [
            {"quarter": f"2024-Q{q}", "demand_index": 50 + q, "notable_event": f"e{q}"}
            for q in range(1, 9)
        ],
        "layoff_events": [{"company": "X", "date": "2024-01", "count": 50, "source_idx": 0}],
        "hiring_freezes": ["some freeze"],
        "narrative": "A downturn and recovery.",
        "confidence": "medium",
        "sources": _sources(5, "https://layoffs.fyi/p"),
    }


def _current_trends() -> dict:
    return {
        "topic": "current_trends",
        "hot_topics": [
            {"topic": "rsc", "why": "frameworks converging on rsc", "source_idx": 0},
            {"topic": "llm evals", "why": "hiring managers want eval rigour", "source_idx": 1},
        ],
        "fading_topics": [{"topic": "spa-only", "why": "seo pressure"}],
        "buzzwords_in_jds": ["RSC", "tRPC"],
        "narrative": "RSC and evals are hot.",
        "confidence": "medium",
        "sources": _sources(5, "https://vercel.com/blog"),
    }


def _skills_match() -> dict:
    return {
        "topic": "skills_match",
        "skill_grades": [
            {"skill": "vue", "relevance": "strong", "market_demand_score": 6,
             "notes": "Vue solid (src: 0)."},
        ],
        "gap_skills": ["playwright", "graphql"],
        "overrepresented_skills": ["jquery"],
        "headline": "Vue/TS core is solid.",
        "confidence": "medium",
        "sources": _sources(5, "https://survey.stackoverflow.co/a"),
    }


def _projections() -> dict:
    return {
        "topic": "projections",
        "demand_trajectory": "steady",
        "salary_trajectory": "+2% YoY",
        "emerging_roles": [
            {"title": "AI PE", "description": "Own LLM feats.", "fit_score": 4,
             "speculative": False},
        ],
        "risks": ["Code-gen commoditising CRUD"],
        "ai_automation_exposure": {
            "likelihood": "medium",
            "tasks_at_risk": ["boilerplate"],
            "defensible_activities": ["perf tuning", "a11y"],
            "speculative": True,
        },
        "narrative": "Demand steady. Salaries flat-ish. Move up-stack.",
        "speculative_claims": ["AI exposure medium."],
        "confidence": "low",
        "sources": _sources(5, "https://bls.gov/report"),
    }


def _salary_home() -> dict:
    return {
        "topic": "salary_home",
        "currency_local": "EUR",
        "bands": [
            {"level": "mid", "p25_local": 60000, "p50_local": 72000, "p75_local": 84000,
             "p25_usd": 64000, "p50_usd": 77000, "p75_usd": 90000},
        ],
        "total_comp_notes": "Bonus 5-15% STI; RSU US-HQ only. FX 1 EUR = 1.07 USD.",
        "narrative": "Mid 5y sits near p50.",
        "confidence": "medium",
        "sources": _sources(5, "https://levels.fyi/x"),
    }


def _salary_neighbors() -> dict:
    return {
        "topic": "salary_neighbors",
        "neighbors": [
            {
                "market_name": "Amsterdam",
                "currency": "EUR",
                "bands": [
                    {"level": "mid", "p25_local": 62000, "p50_local": 75000, "p75_local": 90000,
                     "p25_usd": 66000, "p50_usd": 80000, "p75_usd": 96000},
                ],
                "cost_of_living_index_vs_home": 1.18,
                "commute_feasibility": "onsite",
                "why_comparable": "Common EU migration path.",
            },
        ],
        "confidence": "medium",
        "sources": _sources(5, "https://levels.fyi/y"),
    }


def _companies() -> dict:
    return {
        "topic": "companies",
        "top_employers": [
            {"name": "N26", "hq": "Berlin, DE", "headcount_band": "501-5k",
             "stack_overlap_pct": 70, "remote_policy": "hybrid",
             "notable_signals": "React + TS stack (src: 0)."},
        ],
        "rising_startups": [
            {"name": "ExAI", "stage": "series-a", "what_they_do": "LLM analytics.",
             "why_notable": "Hiring aggressively (src: 1)."},
        ],
        "companies_to_avoid": [],
        "narrative": "Concentrated fintech scene.",
        "confidence": "medium",
        "sources": _sources(5, "https://crunchbase.com/x"),
    }


def _hiring_bar() -> dict:
    return {
        "topic": "hiring_bar",
        "common_stages": [
            {"stage": "recruiter screen", "typical_duration_mins": 30, "format": "phone"},
            {"stage": "coding screen", "typical_duration_mins": 60, "format": "video"},
        ],
        "coding_topics_seen": ["array", "graph"],
        "system_design_topics": ["component architecture"],
        "behavioral_themes": ["ownership"],
        "leetcode_tag_frequencies": [{"tag": "array", "frequency": "high"}],
        "average_loop_length_days": 28,
        "narrative": "Mid-level frontend loop ~4 weeks.",
        "confidence": "medium",
        "sources": _sources(5, "https://glassdoor.com/q"),
    }


def _upskilling() -> dict:
    return {
        "topic": "upskilling",
        "recommendations": [
            {
                "skill": "playwright",
                "priority": "must",
                "time_to_proficiency_hours": 25,
                "suggested_resources": [
                    {"title": "Playwright docs", "url": "https://playwright.dev/docs/intro",
                     "format": "doc"},
                ],
                "why_it_matters": "Most frontend JDs require it.",
            },
        ],
        "learning_plan_weeks": 10,
        "narrative": "Playwright first; then GraphQL.",
        "confidence": "medium",
        "sources": _sources(5, "https://playwright.dev/p"),
    }


_TOPIC_FIXTURES: dict[str, callable] = {
    "demand":           _demand,
    "history":          _history,
    "current_trends":   _current_trends,
    "skills_match":     _skills_match,
    "projections":      _projections,
    "salary_home":      _salary_home,
    "salary_neighbors": _salary_neighbors,
    "companies":        _companies,
    "hiring_bar":       _hiring_bar,
    "upskilling":       _upskilling,
}

_VALIDATORS = {
    "demand":           _validate_demand,
    "history":          _validate_history,
    "current_trends":   _validate_current_trends,
    "skills_match":     _validate_skills_match,
    "projections":      _validate_projections,
    "salary_home":      _validate_salary_home,
    "salary_neighbors": _validate_salary_neighbors,
    "companies":        _validate_companies,
    "hiring_bar":       _validate_hiring_bar,
    "upskilling":       _validate_upskilling,
}


def _good_manager_report() -> dict:
    return {
        "cover": {
            "title": "Market Research — frontend engineer in Berlin",
            "subtitle": "12-month forward view",
            "prepared_for": "Candidate",
            "prepared_on": "2026-04-20",
            "word_count_estimate": 2500,
        },
        "executive_summary": [
            "Demand is steady.",
            "Salaries flat.",
            "Playwright is the biggest skill gap.",
        ],
        "key_findings": [
            "Finding 1", "Finding 2", "Finding 3", "Finding 4", "Finding 5",
        ],
        "sections": [
            {
                "id": "demand",
                "heading": "Market demand & volume",
                "paragraphs": ["p1"],
                "bullets": ["b1"],
                "citations": [1],
            },
        ],
        "recommendations": [
            {
                "priority": "must",
                "text": "Learn Playwright.",
                "rationale": "Most JDs want it.",
                "citations": [2],
            },
        ],
        "risks": ["risk1"],
        "opportunities": ["opp1"],
        "sources": [
            {"n": 1, "title": "LinkedIn Jobs", "url": "https://linkedin.com/jobs",
             "date": "2026-03-01", "snippet": "1240 results"},
            {"n": 2, "title": "Playwright docs", "url": "https://playwright.dev/docs/intro",
             "date": "", "snippet": "Getting started."},
        ],
        "manager_confidence": "medium",
        "gaps_acknowledged": [],
    }


# ---------------------------------------------------------------------------
# 1. _validate_manager_report — good fixture passes
# ---------------------------------------------------------------------------
print("1. _validate_manager_report — good fixture passes")
errs = _validate_manager_report(_good_manager_report())
check(errs == [], f"good manager report is valid (got {errs})")


# ---------------------------------------------------------------------------
# 2. _validate_manager_report — 6 mutations
# ---------------------------------------------------------------------------
print("\n2. _validate_manager_report — 6 bad mutations")


def _mut(fn) -> dict:
    r = copy.deepcopy(_good_manager_report())
    fn(r)
    return r


manager_bad_cases = [
    (
        "missing cover",
        lambda r: r.pop("cover"),
        "missing keys",
    ),
    (
        "unreferenced citation in section",
        lambda r: r["sections"][0].__setitem__("citations", [99]),
        "citation 99",
    ),
    (
        "non-int n in sources",
        lambda r: r["sources"][0].__setitem__("n", "one"),
        "sources[0].n must be an int",
    ),
    (
        "bad manager_confidence",
        lambda r: r.__setitem__("manager_confidence", "maybe"),
        "manager_confidence must be one of",
    ),
    (
        "sections not a list",
        lambda r: r.__setitem__("sections", {}),
        "sections must be a list",
    ),
    (
        "recommendations with unreferenced citation",
        lambda r: r["recommendations"][0].__setitem__("citations", [77]),
        "citation 77",
    ),
]

for label, mutate, sub in manager_bad_cases:
    errs = _validate_manager_report(_mut(mutate))
    check(
        any(sub in e for e in errs),
        f"{label} → error contains {sub!r} (got {errs[:3]})",
    )


# ---------------------------------------------------------------------------
# 3. Worker validators — good passes, one break fails with expected substring
# ---------------------------------------------------------------------------
print("\n3. Worker validators — good passes; one targeted break fails")

for topic in _TOPIC_FIXTURES:
    good = _TOPIC_FIXTURES[topic]()
    errs = _VALIDATORS[topic](good)
    check(errs == [], f"{topic}: good fixture passes (got {errs[:2]})")


# Each topic: 4 sources instead of 5 should trip the "distinct http(s) sources" error.
print("   --- bad mutations: only 4 distinct sources")
for topic in _TOPIC_FIXTURES:
    bad = _TOPIC_FIXTURES[topic]()
    bad["sources"] = bad["sources"][:4]
    errs = _VALIDATORS[topic](bad)
    check(
        any("need at least 5 distinct" in e for e in errs),
        f"{topic}: few-sources mutation flagged (got {errs[:2]})",
    )

# Wrong topic name
print("   --- bad mutations: wrong topic string")
for topic in _TOPIC_FIXTURES:
    bad = _TOPIC_FIXTURES[topic]()
    bad["topic"] = "nope"
    errs = _VALIDATORS[topic](bad)
    check(
        any(f"topic must equal {topic!r}" in e for e in errs),
        f"{topic}: wrong-topic mutation flagged",
    )

# Bad confidence enum
print("   --- bad mutations: bad confidence enum")
for topic in _TOPIC_FIXTURES:
    bad = _TOPIC_FIXTURES[topic]()
    bad["confidence"] = "maybe"
    errs = _VALIDATORS[topic](bad)
    check(
        any("confidence must be one of" in e for e in errs),
        f"{topic}: bad-confidence mutation flagged",
    )


# ---------------------------------------------------------------------------
# 4. market_research_sync happy path
# ---------------------------------------------------------------------------
print("\n4. market_research_sync — happy path (all 10 workers + manager succeed)")


def _canned_manager_stdout() -> str:
    body = json.dumps(_good_manager_report(), ensure_ascii=False)
    return json.dumps({"result": body})


def _make_stub(fail_topics: set[str] | None = None,
               invalid_first_call: bool = False,
               delay_s: float = 0.0):
    """Build a run_p_with_tools stub that inspects the prompt, detects which
    worker or the manager is being called, and returns canned JSON keyed by
    topic. Uses a per-call counter so we can implement "invalid first, valid
    second" behavior.
    """
    fail_topics = fail_topics or set()
    call_counts: dict[str, int] = {}

    def _detect_topic(prompt: str) -> str | None:
        # Manager prompt contains the exact role marker.
        if "You are the manager agent" in prompt:
            return "__manager__"
        # Workers contain "You are one of ten parallel market-research sub-agents"
        # + a distinctive topic marker. Match the prompt filename-scoped topic by
        # scanning for the explicit JSON schema `"topic": "<name>"` literal —
        # each worker prompt has exactly one such literal.
        for topic in _TOPIC_FIXTURES:
            if f'"topic": "{topic}"' in prompt:
                return topic
        return None

    def _stub(prompt: str, *, allowed_tools: str = "", disallowed_tools: str = "",
              model=None, timeout_s: int = 300, output_format: str = "json",
              cwd=None, extra_args=None) -> str | None:
        if delay_s:
            time.sleep(delay_s)
        topic = _detect_topic(prompt)
        call_counts[topic or "?"] = call_counts.get(topic or "?", 0) + 1
        if topic is None:
            return None
        if topic == "__manager__":
            if topic in fail_topics:
                return None
            return _canned_manager_stdout()
        # Worker:
        if topic in fail_topics:
            return None
        n_call = call_counts[topic]
        if invalid_first_call and n_call == 1:
            # Return unparseable garbage first.
            return json.dumps({"result": "<html>not json</html>"})
        fixture = _TOPIC_FIXTURES[topic]()
        return json.dumps({"result": json.dumps(fixture, ensure_ascii=False)})

    _stub.call_counts = call_counts  # type: ignore[attr-defined]
    return _stub


def _profile() -> dict:
    return {
        "primary_role": "frontend engineer",
        "target_levels": ["mid"],
        "years_experience": 5,
        "stack_primary": ["vue", "typescript"],
        "language": "english",
        "free_text": "remote EU",
    }


stub_ok = _make_stub()
res = market_research_sync(
    chat_id=42,
    resume_text="Candidate — 5y frontend.",
    profile=_profile(),
    location="Berlin",
    concurrency=4,
    overall_timeout_s=30,
    worker_timeout_s=10,
    manager_timeout_s=10,
    _run_p_with_tools=stub_ok,
)
check(res.status == "ok", f"status=ok (got {res.status!r} err={res.error!r})")
check(sorted(res.workers_ok) == sorted(_TOPIC_FIXTURES.keys()),
      f"all 10 topics in workers_ok (got {res.workers_ok})")
check(res.manager_report is not None, "manager report present")
check(res.elapsed_ms > 0, f"elapsed_ms > 0 (got {res.elapsed_ms})")
check(len(res.workers_failed) == 0, f"no failures (got {res.workers_failed})")


# ---------------------------------------------------------------------------
# 5. partial failure — 3 workers fail
# ---------------------------------------------------------------------------
print("\n5. market_research_sync — partial failure (3 of 10 workers fail)")
fail3 = {"history", "hiring_bar", "upskilling"}
stub_partial = _make_stub(fail_topics=fail3)
# Wrap the stub so we can inspect what the manager receives.
manager_prompts_seen: list[str] = []


def _wrap_with_capture(inner):
    def _wrapped(prompt: str, **kw):
        if "You are the manager agent" in prompt:
            manager_prompts_seen.append(prompt)
        return inner(prompt, **kw)
    return _wrapped


res = market_research_sync(
    chat_id=42,
    resume_text="resume",
    profile=_profile(),
    location="Berlin",
    concurrency=4,
    overall_timeout_s=30,
    worker_timeout_s=10,
    manager_timeout_s=10,
    _run_p_with_tools=_wrap_with_capture(stub_partial),
)
check(res.status == "partial", f"status=partial (got {res.status!r})")
check(len(res.workers_failed) == 3,
      f"3 failed workers (got {len(res.workers_failed)}: {res.workers_failed})")
check(res.manager_report is not None, "manager ran even with partial failures")
check(len(manager_prompts_seen) >= 1, "manager prompt was captured")
if manager_prompts_seen:
    mp = manager_prompts_seen[-1]
    for t in fail3:
        check(t in mp, f"manager prompt mentions missing topic {t!r}")


# ---------------------------------------------------------------------------
# 6. catastrophic failure — 6 workers fail
# ---------------------------------------------------------------------------
print("\n6. market_research_sync — catastrophic (6 of 10 fail)")
fail6 = {"demand", "history", "current_trends", "skills_match", "projections", "salary_home"}
stub_cat = _make_stub(fail_topics=fail6)
res = market_research_sync(
    chat_id=42,
    resume_text="resume",
    profile=_profile(),
    location="Berlin",
    concurrency=4,
    overall_timeout_s=30,
    worker_timeout_s=10,
    manager_timeout_s=10,
    _run_p_with_tools=stub_cat,
)
check(res.status == "failed", f"status=failed (got {res.status!r})")
check(res.manager_report is None, "manager skipped on catastrophic failure")


# ---------------------------------------------------------------------------
# 7. retry-on-invalid — malformed first, valid second per worker
# ---------------------------------------------------------------------------
print("\n7. market_research_sync — retry on malformed first response")
stub_retry = _make_stub(invalid_first_call=True)
res = market_research_sync(
    chat_id=42,
    resume_text="resume",
    profile=_profile(),
    location="Berlin",
    concurrency=4,
    overall_timeout_s=30,
    worker_timeout_s=10,
    manager_timeout_s=10,
    _run_p_with_tools=stub_retry,
)
check(res.status == "ok", f"status=ok after retries (got {res.status!r}, err={res.error!r})")
# Each worker should have been called exactly twice; manager once.
worker_call_counts = [stub_retry.call_counts.get(t, 0) for t in _TOPIC_FIXTURES]  # type: ignore[attr-defined]
check(all(c == 2 for c in worker_call_counts),
      f"each worker called exactly twice (got {worker_call_counts})")


# ---------------------------------------------------------------------------
# 8. run_p_with_tools CLI flag fallback
# ---------------------------------------------------------------------------
print("\n8. run_p_with_tools — falls back when CLI rejects tool flags")


class _FakeProc:
    def __init__(self, rc: int, stdout: str, stderr: str) -> None:
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


_seen_argvs: list[list[str]] = []


def _fake_subprocess_run(cmd, **kwargs):
    _seen_argvs.append(list(cmd))
    if len(_seen_argvs) == 1:
        return _FakeProc(2, "", "error: unrecognized arguments: --allowed-tools")
    return _FakeProc(0, '{"result": "ok-body"}', "")


# Stub shutil.which + subprocess.run on claude_cli.
_orig_which = claude_cli.shutil.which
_orig_run = claude_cli.subprocess.run
claude_cli.shutil.which = lambda name: "/usr/bin/fake-claude"
claude_cli.subprocess.run = _fake_subprocess_run
try:
    out = claude_cli.run_p_with_tools(
        "prompt",
        allowed_tools="WebSearch,WebFetch",
        disallowed_tools="Bash,Edit",
        model="opus",
        timeout_s=5,
    )
finally:
    claude_cli.shutil.which = _orig_which
    claude_cli.subprocess.run = _orig_run

check(out == '{"result": "ok-body"}', f"got fallback stdout (got {out!r})")
check(len(_seen_argvs) == 2, f"two CLI invocations (got {len(_seen_argvs)})")
if len(_seen_argvs) == 2:
    second = _seen_argvs[1]
    check("--allowed-tools" not in second,
          f"second argv omits --allowed-tools (got {second})")
    check("--disallowed-tools" not in second,
          f"second argv omits --disallowed-tools (got {second})")


# ---------------------------------------------------------------------------
# 9. run_all overall timeout
# ---------------------------------------------------------------------------
print("\n9. run_all — overall timeout converts pending workers to failures")


def _slow_worker(spec, ctx, *, model=mr.DEFAULT_MODEL, _run_p_with_tools=None):
    time.sleep(2.0)
    fixture = _TOPIC_FIXTURES[spec.topic]()
    return spec.topic, fixture, None


ok_results, failed = run_all(
    {"primary_role": "x"},
    workers=WORKERS,
    concurrency=10,
    overall_timeout_s=0.2,
    model="opus",
    _run_worker=_slow_worker,
)
check(len(ok_results) == 0, f"no successes within 0.2s (got {list(ok_results)})")
check(len(failed) >= 1, f"failed entries recorded (got {len(failed)}: {failed[:2]})")
if failed:
    stats = {f["status"] for f in failed}
    check(
        "overall_timeout" in stats or "exception" in stats,
        f"failed statuses include overall_timeout/exception (got {stats})",
    )


# ---------------------------------------------------------------------------
# 10. distinct-URL dedup check
# ---------------------------------------------------------------------------
print("\n10. validator — duplicate source URLs fail distinct-count")
dup_demand = _demand()
# 5 entries, all same URL
dup_url = "https://linkedin.com/jobs/dup"
dup_demand["sources"] = [
    {"title": f"t{i}", "url": dup_url, "date": "", "snippet": "s"}
    for i in range(5)
]
errs = _validate_demand(dup_demand)
check(
    any("need at least 5 distinct" in e for e in errs),
    f"duplicate URLs flagged (got {errs[:2]})",
)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
if failures:
    print(f"FAIL {len(failures)} check(s):")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("OK  All market_research smoke checks passed.")
