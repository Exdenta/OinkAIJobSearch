"""Deep market-research orchestrator for the `/marketresearch` Telegram bot command.

Spawns up to 10 parallel Claude-Opus sub-agents — each restricted to
WebSearch + WebFetch — that each own a narrow research topic (demand,
history, salary, company landscape, etc.). Once the workers settle (or the
overall deadline trips), a manager/synthesizer agent with NO tools receives
the collated worker JSON and merges it into one cohesive, schema-validated
report. That report is persisted and later rendered to DOCX by
`market_research_render.py`.

Design mirrors `profile_builder.py`:

  • **Soft-fail.** Every public entry point returns a `ResearchRun` describing
    the outcome — never raises. A CLI-missing or a per-worker timeout drops
    into `status="partial"` / `"failed"` rather than crashing the bot.

  • **Bounded, validated I/O.** Each worker's output is validated against
    the prompt's schema with topic-specific shape checks + a shared
    `_basic_worker_shape` helper. Manager output is validated against
    `market_research_manager.txt`, including global citation integrity.

  • **No code-exec surface.** All sub-agents are forbidden from using
    Bash/Edit/Write/Read; the manager additionally has WebSearch/WebFetch
    disabled. Everything returned is pure JSON consumed by typed Python.

  • **Test-friendly injection.** Both `run_worker` and
    `synthesize_with_manager` accept a `_run_p_with_tools=` parameter, and
    `run_all` accepts `_run_worker=`. Offline smoke tests inject stubs to
    avoid touching the real `claude` CLI.
"""
from __future__ import annotations

import concurrent.futures
import functools
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from claude_cli import (
    run_p_with_tools,
    run_p,
    extract_assistant_text,
    parse_json_block,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_MODEL           = os.environ.get("MARKET_RESEARCH_MODEL", "opus")
DEFAULT_CONCURRENCY     = int(os.environ.get("MARKET_RESEARCH_CONCURRENCY", "8"))
DEFAULT_WORKER_TIMEOUT  = int(os.environ.get("MARKET_RESEARCH_WORKER_TIMEOUT_S", "900"))
DEFAULT_OVERALL_TIMEOUT = int(os.environ.get("MARKET_RESEARCH_OVERALL_TIMEOUT_S", "2100"))
DEFAULT_MANAGER_TIMEOUT = int(os.environ.get("MARKET_RESEARCH_MANAGER_TIMEOUT_S", "1500"))

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_MAX_RESUME_CHARS = 2000       # clipped into resume_summary for workers (keep prompt compact)
_MAX_FREETEXT_LEN = 500
_MAX_LOCATION_LEN = 64

_HISTORICAL_WINDOW_MONTHS = "24"
_PROJECTION_WINDOW_MONTHS = "12-18"

_WORKER_ALLOWED_TOOLS    = "WebSearch,WebFetch"
_WORKER_DISALLOWED_TOOLS = "Bash,Edit,Write,Read"
_MANAGER_ALLOWED_TOOLS    = ""
_MANAGER_DISALLOWED_TOOLS = "WebSearch,WebFetch,Bash,Edit,Write,Read"

_VALID_CONFIDENCE = frozenset({"high", "medium", "low"})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def sha1_hex(s: str | None) -> str:
    return hashlib.sha1((s or "").encode("utf-8", "ignore")).hexdigest()


def _is_str(x: Any) -> bool:
    return isinstance(x, str)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_str_list(x: Any) -> bool:
    return isinstance(x, list) and all(isinstance(s, str) for s in x)


def _is_dict(x: Any) -> bool:
    return isinstance(x, dict)


def _is_list(x: Any) -> bool:
    return isinstance(x, list)


def _read_prompt(filename: str) -> str:
    try:
        return (_PROMPTS_DIR / filename).read_text(encoding="utf-8")
    except OSError as e:
        log.error("market_research: can't read prompt %s: %s", filename, e)
        return ""


def _render_prompt(tmpl: str, ctx: dict[str, str]) -> str:
    """Manual `{key}` substitution — prompts contain literal JSON braces so
    `str.format` is unusable."""
    out = tmpl
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", v)
    return out


def _summarize_resume(resume_text: str, limit: int = _MAX_RESUME_CHARS) -> str:
    return (resume_text or "").strip()[:limit]


def _build_ctx(
    resume_text: str,
    profile: dict,
    location: str,
    today_iso: str,
) -> dict[str, str]:
    profile = profile or {}
    return {
        "resume_summary":             _summarize_resume(resume_text),
        "primary_role":               str(profile.get("primary_role") or "").strip(),
        "target_levels":              ", ".join(profile.get("target_levels") or []),
        "years_experience":           str(profile.get("years_experience") or 0),
        "location":                   (location or "").strip()[:_MAX_LOCATION_LEN],
        "stack_primary":              ", ".join(profile.get("stack_primary") or []),
        "language":                   str(profile.get("language") or ""),
        "free_text":                  (profile.get("free_text") or "").strip()[:_MAX_FREETEXT_LEN],
        "today_iso":                  today_iso,
        "historical_window_months":   _HISTORICAL_WINDOW_MONTHS,
        "projection_window_months":   _PROJECTION_WINDOW_MONTHS,
    }


# ---------------------------------------------------------------------------
# Shared worker-shape validator
# ---------------------------------------------------------------------------

def _basic_worker_shape(
    data: Any,
    expected_topic: str,
    min_sources: int,
) -> list[str]:
    """Common shape checks shared by every worker's validator:
      - object
      - `topic` == expected
      - `confidence` in enum
      - `sources`: list of dicts, ≥ min_sources DISTINCT http/https URLs, each
        with string `title`, `url`, `date`, `snippet`.
    Returns the accumulated error list (empty = OK)."""
    errs: list[str] = []
    if not isinstance(data, dict):
        return [f"{expected_topic}: response is not a dict"]

    topic = data.get("topic")
    if topic != expected_topic:
        errs.append(f"{expected_topic}: topic must equal {expected_topic!r} (got {topic!r})")

    conf = data.get("confidence")
    if conf not in _VALID_CONFIDENCE:
        errs.append(f"{expected_topic}: confidence must be one of {sorted(_VALID_CONFIDENCE)}")

    sources = data.get("sources")
    if not isinstance(sources, list):
        errs.append(f"{expected_topic}: sources must be a list")
        return errs

    seen_urls: set[str] = set()
    for i, src in enumerate(sources):
        if not isinstance(src, dict):
            errs.append(f"{expected_topic}: sources[{i}] must be an object")
            continue
        t = src.get("title")
        u = src.get("url")
        d = src.get("date")
        sn = src.get("snippet")
        if not isinstance(t, str):
            errs.append(f"{expected_topic}: sources[{i}].title must be a string")
        if not isinstance(u, str) or not (u.startswith("http://") or u.startswith("https://")):
            errs.append(f"{expected_topic}: sources[{i}].url must start with http(s)://")
        else:
            seen_urls.add(u.strip().lower())
        if not isinstance(d, str):
            errs.append(f"{expected_topic}: sources[{i}].date must be a string")
        if not isinstance(sn, str):
            errs.append(f"{expected_topic}: sources[{i}].snippet must be a string")

    if len(seen_urls) < min_sources:
        errs.append(
            f"{expected_topic}: need at least {min_sources} distinct http(s) sources "
            f"(got {len(seen_urls)})"
        )
    return errs


# ---------------------------------------------------------------------------
# Per-topic validators — presence + type, lenient on content
# ---------------------------------------------------------------------------

def _validate_demand(data: Any) -> list[str]:
    errs = _basic_worker_shape(data, "demand", 5)
    if not isinstance(data, dict):
        return errs
    if not _is_str(data.get("role_family")):
        errs.append("demand: role_family must be a string")
    if not _is_int(data.get("total_open_postings_estimate")):
        errs.append("demand: total_open_postings_estimate must be an int")
    pbl = data.get("postings_by_level")
    if not isinstance(pbl, dict):
        errs.append("demand: postings_by_level must be an object")
    else:
        for k in ("junior", "mid", "senior", "lead"):
            if not _is_int(pbl.get(k)):
                errs.append(f"demand: postings_by_level.{k} must be an int")
    top = data.get("top_employers")
    if not isinstance(top, list):
        errs.append("demand: top_employers must be a list")
    else:
        for i, e in enumerate(top):
            if not isinstance(e, dict) or not _is_str(e.get("name")) or not _is_int(e.get("count")):
                errs.append(f"demand: top_employers[{i}] must be {{name:str, count:int}}")
    if not _is_str(data.get("headline_summary")):
        errs.append("demand: headline_summary must be a string")
    return errs


def _validate_history(data: Any) -> list[str]:
    errs = _basic_worker_shape(data, "history", 5)
    if not isinstance(data, dict):
        return errs
    tl = data.get("timeline")
    if not isinstance(tl, list):
        errs.append("history: timeline must be a list")
    else:
        for i, row in enumerate(tl):
            if not isinstance(row, dict):
                errs.append(f"history: timeline[{i}] must be an object")
                continue
            if not _is_str(row.get("quarter")):
                errs.append(f"history: timeline[{i}].quarter must be a string")
            if not _is_int(row.get("demand_index")):
                errs.append(f"history: timeline[{i}].demand_index must be an int")
            if not _is_str(row.get("notable_event")):
                errs.append(f"history: timeline[{i}].notable_event must be a string")
    le = data.get("layoff_events")
    if not isinstance(le, list):
        errs.append("history: layoff_events must be a list")
    hf = data.get("hiring_freezes")
    if not _is_str_list(hf or []):
        errs.append("history: hiring_freezes must be a list of strings")
    if not _is_str(data.get("narrative")):
        errs.append("history: narrative must be a string")
    return errs


def _validate_current_trends(data: Any) -> list[str]:
    errs = _basic_worker_shape(data, "current_trends", 5)
    if not isinstance(data, dict):
        return errs
    ht = data.get("hot_topics")
    if not isinstance(ht, list):
        errs.append("current_trends: hot_topics must be a list")
    else:
        for i, h in enumerate(ht):
            if not isinstance(h, dict) or not _is_str(h.get("topic")) or not _is_str(h.get("why")):
                errs.append(f"current_trends: hot_topics[{i}] must be {{topic, why, source_idx}}")
    ft = data.get("fading_topics")
    if not isinstance(ft, list):
        errs.append("current_trends: fading_topics must be a list")
    bw = data.get("buzzwords_in_jds")
    if not _is_str_list(bw or []):
        errs.append("current_trends: buzzwords_in_jds must be a list of strings")
    if not _is_str(data.get("narrative")):
        errs.append("current_trends: narrative must be a string")
    return errs


def _validate_skills_match(data: Any) -> list[str]:
    errs = _basic_worker_shape(data, "skills_match", 5)
    if not isinstance(data, dict):
        return errs
    sg = data.get("skill_grades")
    if not isinstance(sg, list):
        errs.append("skills_match: skill_grades must be a list")
    else:
        for i, row in enumerate(sg):
            if not isinstance(row, dict):
                errs.append(f"skills_match: skill_grades[{i}] must be an object")
                continue
            if not _is_str(row.get("skill")):
                errs.append(f"skills_match: skill_grades[{i}].skill must be a string")
            if not _is_str(row.get("relevance")):
                errs.append(f"skills_match: skill_grades[{i}].relevance must be a string")
            if not _is_int(row.get("market_demand_score")):
                errs.append(f"skills_match: skill_grades[{i}].market_demand_score must be an int")
            if not _is_str(row.get("notes")):
                errs.append(f"skills_match: skill_grades[{i}].notes must be a string")
    if not _is_str_list(data.get("gap_skills") or []):
        errs.append("skills_match: gap_skills must be a list of strings")
    if not _is_str_list(data.get("overrepresented_skills") or []):
        errs.append("skills_match: overrepresented_skills must be a list of strings")
    if not _is_str(data.get("headline")):
        errs.append("skills_match: headline must be a string")
    return errs


def _validate_projections(data: Any) -> list[str]:
    errs = _basic_worker_shape(data, "projections", 5)
    if not isinstance(data, dict):
        return errs
    if not _is_str(data.get("demand_trajectory")):
        errs.append("projections: demand_trajectory must be a string")
    if not _is_str(data.get("salary_trajectory")):
        errs.append("projections: salary_trajectory must be a string")
    er = data.get("emerging_roles")
    if not isinstance(er, list):
        errs.append("projections: emerging_roles must be a list")
    else:
        for i, row in enumerate(er):
            if not isinstance(row, dict):
                errs.append(f"projections: emerging_roles[{i}] must be an object")
                continue
            if not _is_str(row.get("title")) or not _is_str(row.get("description")):
                errs.append(f"projections: emerging_roles[{i}] must have string title/description")
            if not _is_int(row.get("fit_score")):
                errs.append(f"projections: emerging_roles[{i}].fit_score must be an int")
            if not isinstance(row.get("speculative"), bool):
                errs.append(f"projections: emerging_roles[{i}].speculative must be a bool")
    if not _is_str_list(data.get("risks") or []):
        errs.append("projections: risks must be a list of strings")
    aie = data.get("ai_automation_exposure")
    if not isinstance(aie, dict):
        errs.append("projections: ai_automation_exposure must be an object")
    else:
        if not _is_str(aie.get("likelihood")):
            errs.append("projections: ai_automation_exposure.likelihood must be a string")
        if not _is_str_list(aie.get("tasks_at_risk") or []):
            errs.append("projections: ai_automation_exposure.tasks_at_risk must be list of strings")
        if not _is_str_list(aie.get("defensible_activities") or []):
            errs.append("projections: ai_automation_exposure.defensible_activities must be list")
        if not isinstance(aie.get("speculative"), bool):
            errs.append("projections: ai_automation_exposure.speculative must be a bool")
    if not _is_str(data.get("narrative")):
        errs.append("projections: narrative must be a string")
    return errs


def _validate_salary_home(data: Any) -> list[str]:
    errs = _basic_worker_shape(data, "salary_home", 5)
    if not isinstance(data, dict):
        return errs
    if not _is_str(data.get("currency_local")):
        errs.append("salary_home: currency_local must be a string")
    bands = data.get("bands")
    if not isinstance(bands, list) or not bands:
        errs.append("salary_home: bands must be a non-empty list")
    else:
        for i, b in enumerate(bands):
            if not isinstance(b, dict):
                errs.append(f"salary_home: bands[{i}] must be an object")
                continue
            if not _is_str(b.get("level")):
                errs.append(f"salary_home: bands[{i}].level must be a string")
            for k in ("p25_local", "p50_local", "p75_local",
                      "p25_usd", "p50_usd", "p75_usd"):
                if not _is_int(b.get(k)):
                    errs.append(f"salary_home: bands[{i}].{k} must be an int")
    if not _is_str(data.get("total_comp_notes")):
        errs.append("salary_home: total_comp_notes must be a string")
    if not _is_str(data.get("narrative")):
        errs.append("salary_home: narrative must be a string")
    return errs


def _validate_salary_neighbors(data: Any) -> list[str]:
    errs = _basic_worker_shape(data, "salary_neighbors", 5)
    if not isinstance(data, dict):
        return errs
    nbrs = data.get("neighbors")
    if not isinstance(nbrs, list) or not nbrs:
        errs.append("salary_neighbors: neighbors must be a non-empty list")
        return errs
    for i, n in enumerate(nbrs):
        if not isinstance(n, dict):
            errs.append(f"salary_neighbors: neighbors[{i}] must be an object")
            continue
        if not _is_str(n.get("market_name")):
            errs.append(f"salary_neighbors: neighbors[{i}].market_name must be a string")
        if not _is_str(n.get("currency")):
            errs.append(f"salary_neighbors: neighbors[{i}].currency must be a string")
        if not _is_str(n.get("commute_feasibility")):
            errs.append(f"salary_neighbors: neighbors[{i}].commute_feasibility must be a string")
        if not _is_str(n.get("why_comparable")):
            errs.append(f"salary_neighbors: neighbors[{i}].why_comparable must be a string")
        col = n.get("cost_of_living_index_vs_home")
        if not isinstance(col, (int, float)) or isinstance(col, bool):
            errs.append(
                f"salary_neighbors: neighbors[{i}].cost_of_living_index_vs_home must be a number"
            )
        bands = n.get("bands")
        if not isinstance(bands, list) or not bands:
            errs.append(f"salary_neighbors: neighbors[{i}].bands must be a non-empty list")
            continue
        for j, b in enumerate(bands):
            if not isinstance(b, dict):
                errs.append(f"salary_neighbors: neighbors[{i}].bands[{j}] must be an object")
                continue
            if not _is_str(b.get("level")):
                errs.append(f"salary_neighbors: neighbors[{i}].bands[{j}].level must be a string")
            for k in ("p25_local", "p50_local", "p75_local",
                      "p25_usd", "p50_usd", "p75_usd"):
                if not _is_int(b.get(k)):
                    errs.append(f"salary_neighbors: neighbors[{i}].bands[{j}].{k} must be an int")
    return errs


def _validate_companies(data: Any) -> list[str]:
    errs = _basic_worker_shape(data, "companies", 5)
    if not isinstance(data, dict):
        return errs
    te = data.get("top_employers")
    if not isinstance(te, list):
        errs.append("companies: top_employers must be a list")
    else:
        for i, e in enumerate(te):
            if not isinstance(e, dict):
                errs.append(f"companies: top_employers[{i}] must be an object")
                continue
            for k in ("name", "hq", "headcount_band", "remote_policy", "notable_signals"):
                if not _is_str(e.get(k)):
                    errs.append(f"companies: top_employers[{i}].{k} must be a string")
            if not _is_int(e.get("stack_overlap_pct")):
                errs.append(f"companies: top_employers[{i}].stack_overlap_pct must be an int")
    rs = data.get("rising_startups")
    if not isinstance(rs, list):
        errs.append("companies: rising_startups must be a list")
    else:
        for i, s in enumerate(rs):
            if not isinstance(s, dict):
                errs.append(f"companies: rising_startups[{i}] must be an object")
                continue
            for k in ("name", "stage", "what_they_do", "why_notable"):
                if not _is_str(s.get(k)):
                    errs.append(f"companies: rising_startups[{i}].{k} must be a string")
    ca = data.get("companies_to_avoid")
    if not isinstance(ca, list):
        errs.append("companies: companies_to_avoid must be a list")
    else:
        for i, a in enumerate(ca):
            if not isinstance(a, dict):
                errs.append(f"companies: companies_to_avoid[{i}] must be an object")
                continue
            if not _is_str(a.get("name")) or not _is_str(a.get("reason")):
                errs.append(f"companies: companies_to_avoid[{i}] must have name+reason strings")
    if not _is_str(data.get("narrative")):
        errs.append("companies: narrative must be a string")
    return errs


def _validate_hiring_bar(data: Any) -> list[str]:
    errs = _basic_worker_shape(data, "hiring_bar", 5)
    if not isinstance(data, dict):
        return errs
    cs = data.get("common_stages")
    if not isinstance(cs, list):
        errs.append("hiring_bar: common_stages must be a list")
    else:
        for i, s in enumerate(cs):
            if not isinstance(s, dict):
                errs.append(f"hiring_bar: common_stages[{i}] must be an object")
                continue
            if not _is_str(s.get("stage")):
                errs.append(f"hiring_bar: common_stages[{i}].stage must be a string")
            if not _is_int(s.get("typical_duration_mins")):
                errs.append(f"hiring_bar: common_stages[{i}].typical_duration_mins must be an int")
            if not _is_str(s.get("format")):
                errs.append(f"hiring_bar: common_stages[{i}].format must be a string")
    for k in ("coding_topics_seen", "system_design_topics", "behavioral_themes"):
        if not _is_str_list(data.get(k) or []):
            errs.append(f"hiring_bar: {k} must be a list of strings")
    lt = data.get("leetcode_tag_frequencies")
    if not isinstance(lt, list):
        errs.append("hiring_bar: leetcode_tag_frequencies must be a list")
    else:
        for i, row in enumerate(lt):
            if not isinstance(row, dict) or not _is_str(row.get("tag")) or not _is_str(row.get("frequency")):
                errs.append(f"hiring_bar: leetcode_tag_frequencies[{i}] must be {{tag, frequency}}")
    if not _is_int(data.get("average_loop_length_days")):
        errs.append("hiring_bar: average_loop_length_days must be an int")
    if not _is_str(data.get("narrative")):
        errs.append("hiring_bar: narrative must be a string")
    return errs


def _validate_upskilling(data: Any) -> list[str]:
    errs = _basic_worker_shape(data, "upskilling", 5)
    if not isinstance(data, dict):
        return errs
    recs = data.get("recommendations")
    if not isinstance(recs, list) or not recs:
        errs.append("upskilling: recommendations must be a non-empty list")
    else:
        for i, r in enumerate(recs):
            if not isinstance(r, dict):
                errs.append(f"upskilling: recommendations[{i}] must be an object")
                continue
            if not _is_str(r.get("skill")):
                errs.append(f"upskilling: recommendations[{i}].skill must be a string")
            if not _is_str(r.get("priority")):
                errs.append(f"upskilling: recommendations[{i}].priority must be a string")
            if not _is_int(r.get("time_to_proficiency_hours")):
                errs.append(f"upskilling: recommendations[{i}].time_to_proficiency_hours must be int")
            if not _is_str(r.get("why_it_matters")):
                errs.append(f"upskilling: recommendations[{i}].why_it_matters must be a string")
            sr = r.get("suggested_resources")
            if not isinstance(sr, list) or not sr:
                errs.append(f"upskilling: recommendations[{i}].suggested_resources must be non-empty list")
            else:
                for j, res in enumerate(sr):
                    if not isinstance(res, dict):
                        errs.append(f"upskilling: recommendations[{i}].suggested_resources[{j}] must be an object")
                        continue
                    if not _is_str(res.get("title")):
                        errs.append(f"upskilling: recommendations[{i}].suggested_resources[{j}].title must be a string")
                    u = res.get("url")
                    if not isinstance(u, str) or not (u.startswith("http://") or u.startswith("https://")):
                        errs.append(f"upskilling: recommendations[{i}].suggested_resources[{j}].url must be http(s)://")
                    if not _is_str(res.get("format")):
                        errs.append(f"upskilling: recommendations[{i}].suggested_resources[{j}].format must be a string")
    if not _is_int(data.get("learning_plan_weeks")):
        errs.append("upskilling: learning_plan_weeks must be an int")
    if not _is_str(data.get("narrative")):
        errs.append("upskilling: narrative must be a string")
    return errs


# ---------------------------------------------------------------------------
# Worker roster
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkerSpec:
    topic: str
    prompt_filename: str
    validator: Callable[[Any], list[str]]
    required_source_count: int = 5
    timeout_s: int = DEFAULT_WORKER_TIMEOUT


WORKERS: list[WorkerSpec] = [
    WorkerSpec("demand",           "market_research_demand.txt",           _validate_demand),
    WorkerSpec("history",          "market_research_history.txt",          _validate_history),
    WorkerSpec("current_trends",   "market_research_current_trends.txt",   _validate_current_trends),
    WorkerSpec("skills_match",     "market_research_skills_match.txt",     _validate_skills_match),
    WorkerSpec("projections",      "market_research_projections.txt",      _validate_projections),
    WorkerSpec("salary_home",      "market_research_salary_home.txt",      _validate_salary_home),
    WorkerSpec("salary_neighbors", "market_research_salary_neighbors.txt", _validate_salary_neighbors),
    WorkerSpec("companies",        "market_research_companies.txt",        _validate_companies),
    WorkerSpec("hiring_bar",       "market_research_hiring_bar.txt",       _validate_hiring_bar),
    WorkerSpec("upskilling",       "market_research_upskilling.txt",       _validate_upskilling),
]


# ---------------------------------------------------------------------------
# Core dataclass
# ---------------------------------------------------------------------------

@dataclass
class ResearchRun:
    status: str                    # ok | partial | failed | cli_missing | exception
    workers_ok: list[str] = field(default_factory=list)
    workers_failed: list[dict] = field(default_factory=list)   # [{topic, status, error_head}]
    worker_results: dict[str, dict] = field(default_factory=dict)
    manager_report: dict | None = None
    elapsed_ms: int = 0
    started_at_iso: str = ""
    finished_at_iso: str = ""
    location_used: str = ""
    resume_sha1: str = ""
    prefs_sha1: str = ""
    model: str = ""
    error: str | None = None


# ---------------------------------------------------------------------------
# Worker runner
# ---------------------------------------------------------------------------

def _parse_worker_response(stdout: str | None) -> tuple[dict | None, str | None]:
    """Return (parsed_dict, error_head) from a run_p_with_tools stdout."""
    if stdout is None:
        return None, "cli_missing_or_timeout"
    body = extract_assistant_text(stdout)
    parsed = parse_json_block(body)
    if parsed is None:
        return None, f"parse_error: {body[:120]!r}"
    if not isinstance(parsed, dict):
        return None, f"parse_error: top-level not an object ({body[:120]!r})"
    return parsed, None


def run_worker(
    spec: WorkerSpec,
    ctx: dict[str, str],
    *,
    model: str = DEFAULT_MODEL,
    _run_p_with_tools: Callable = run_p_with_tools,
) -> tuple[str, dict | None, str | None]:
    """Run ONE worker end-to-end. Returns (topic, parsed_or_None, error_or_None).

    Never raises — exceptions are caught and surfaced as (topic, None, "exception: ...").
    """
    topic = spec.topic
    t0 = time.monotonic()
    try:
        tmpl = _read_prompt(spec.prompt_filename)
        if not tmpl:
            return topic, None, "prompt_missing"
        prompt = _render_prompt(tmpl, ctx)

        stdout = _run_p_with_tools(
            prompt,
            allowed_tools=_WORKER_ALLOWED_TOOLS,
            disallowed_tools=_WORKER_DISALLOWED_TOOLS,
            model=model,
            timeout_s=spec.timeout_s,
            output_format="json",
        )
        parsed, err = _parse_worker_response(stdout)
        if parsed is None:
            log.info(
                "market_research: worker=%s failed first call err=%s elapsed=%dms",
                topic, (err or "")[:120], int((time.monotonic() - t0) * 1000),
            )
            # Retry path only makes sense when CLI responded with something
            # unparseable — not when the CLI itself is missing/timed out.
            if err and err.startswith("cli_missing_or_timeout"):
                return topic, None, err
            # Fall through to one retry with feedback.
            retry_prompt = (
                prompt
                + "\n\nYour previous response was invalid. Re-emit strict JSON "
                + f"satisfying the schema. Error: {err}."
            )
            stdout2 = _run_p_with_tools(
                retry_prompt,
                allowed_tools=_WORKER_ALLOWED_TOOLS,
                disallowed_tools=_WORKER_DISALLOWED_TOOLS,
                model=model,
                timeout_s=spec.timeout_s,
                output_format="json",
            )
            parsed, err2 = _parse_worker_response(stdout2)
            if parsed is None:
                log.info(
                    "market_research: worker=%s retry failed err=%s elapsed=%dms",
                    topic, (err2 or "")[:120], int((time.monotonic() - t0) * 1000),
                )
                return topic, None, err2 or err

        v_errs = spec.validator(parsed)
        if v_errs:
            first = v_errs[0]
            log.info(
                "market_research: worker=%s validation failed first attempt err=%s",
                topic, first[:200],
            )
            retry_prompt = (
                prompt
                + "\n\nYour previous response was invalid. Re-emit strict JSON "
                + f"satisfying the schema. Error: {first}."
            )
            stdout3 = _run_p_with_tools(
                retry_prompt,
                allowed_tools=_WORKER_ALLOWED_TOOLS,
                disallowed_tools=_WORKER_DISALLOWED_TOOLS,
                model=model,
                timeout_s=spec.timeout_s,
                output_format="json",
            )
            parsed2, err3 = _parse_worker_response(stdout3)
            if parsed2 is None:
                return topic, None, f"validation_error: {first}"
            v_errs2 = spec.validator(parsed2)
            if v_errs2:
                return topic, None, f"validation_error: {v_errs2[0]}"
            parsed = parsed2

        log.info(
            "market_research: worker=%s OK elapsed=%dms sources=%d",
            topic,
            int((time.monotonic() - t0) * 1000),
            len((parsed or {}).get("sources") or []),
        )
        return topic, parsed, None
    except Exception as e:
        log.exception("market_research: worker=%s crashed", topic)
        return topic, None, f"exception: {e!r}"


# ---------------------------------------------------------------------------
# Parallel orchestration
# ---------------------------------------------------------------------------

def run_all(
    ctx: dict[str, str],
    *,
    workers: list[WorkerSpec] = WORKERS,
    concurrency: int = DEFAULT_CONCURRENCY,
    overall_timeout_s: int = DEFAULT_OVERALL_TIMEOUT,
    model: str = DEFAULT_MODEL,
    progress: Callable[[int, int], None] | None = None,
    _run_worker: Callable = run_worker,
) -> tuple[dict[str, dict], list[dict]]:
    """Submit every worker, gather results, enforce `overall_timeout_s`.

    Returns (ok_results, failed_list). Never raises — per-future exceptions
    are recorded as failures.
    """
    ok_results: dict[str, dict] = {}
    failed: list[dict] = []
    total = len(workers)
    done_count = 0

    ex = concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, int(concurrency)),
        thread_name_prefix="market-research",
    )
    try:
        future_to_spec: dict[concurrent.futures.Future, WorkerSpec] = {}
        for spec in workers:
            fut = ex.submit(_run_worker, spec, ctx, model=model)
            future_to_spec[fut] = spec

        deadline = time.monotonic() + max(0.0, float(overall_timeout_s))

        for fut in concurrent.futures.as_completed(
            list(future_to_spec.keys()),
            timeout=None,
        ):
            now = time.monotonic()
            if now > deadline:
                break
            spec = future_to_spec[fut]
            try:
                # Already completed at this point; short remaining timeout.
                topic, parsed, err = fut.result(timeout=max(0.01, deadline - now))
            except concurrent.futures.TimeoutError:
                failed.append({
                    "topic": spec.topic,
                    "status": "overall_timeout",
                    "error_head": "deadline exceeded",
                })
                done_count += 1
                _safe_progress(progress, done_count, total)
                continue
            except Exception as e:
                failed.append({
                    "topic": spec.topic,
                    "status": "exception",
                    "error_head": repr(e)[:200],
                })
                done_count += 1
                _safe_progress(progress, done_count, total)
                continue

            if parsed is not None and err is None:
                ok_results[topic] = parsed
            else:
                failed.append({
                    "topic": topic,
                    "status": "failed",
                    "error_head": (err or "unknown")[:240],
                })
            done_count += 1
            _safe_progress(progress, done_count, total)

        # Anything still pending past the deadline → overall_timeout.
        for fut, spec in future_to_spec.items():
            if spec.topic in ok_results:
                continue
            if any(f["topic"] == spec.topic for f in failed):
                continue
            if not fut.done():
                fut.cancel()
            failed.append({
                "topic": spec.topic,
                "status": "overall_timeout",
                "error_head": "deadline exceeded",
            })
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    return ok_results, failed


def _safe_progress(cb: Callable[[int, int], None] | None, done: int, total: int) -> None:
    if cb is None:
        return
    try:
        cb(done, total)
    except Exception:
        log.exception("market_research: progress callback raised")


# ---------------------------------------------------------------------------
# Manager synthesizer
# ---------------------------------------------------------------------------

_MANAGER_SECTION_IDS = (
    "demand", "history", "current_trends", "skills_match", "projections",
    "salary", "companies", "hiring_bar", "upskilling",
)
_ALLOWED_PRIORITY = frozenset({"must", "should", "nice"})


def _validate_manager_report(report: Any) -> list[str]:
    """Validate the manager's final report against the schema in
    `market_research_manager.txt`. Return [] on valid.

    Enforces:
      - required top-level keys present
      - cover sub-shape
      - executive_summary 1-10 strings (lenient vs prompt's 3-6)
      - sections is a list; each element has id, heading, citations, and at
        least one of the known content fields
      - sources is a list of 1-200 {n:int, title:str, url:http(s), date?, snippet?}
      - every citation int (across all sections + recommendations) maps to a
        real `n` in sources
      - manager_confidence enum, gaps_acknowledged list of strings
    """
    errs: list[str] = []
    if not isinstance(report, dict):
        return ["manager: report is not a dict"]

    required = {
        "cover", "executive_summary", "key_findings", "sections",
        "recommendations", "risks", "opportunities", "sources",
        "manager_confidence", "gaps_acknowledged",
    }
    missing = sorted(required - set(report))
    if missing:
        errs.append(f"manager: missing keys: {missing}")

    cover = report.get("cover")
    if not isinstance(cover, dict):
        errs.append("manager: cover must be an object")
    else:
        for k in ("title", "subtitle", "prepared_for", "prepared_on"):
            if not _is_str(cover.get(k)):
                errs.append(f"manager: cover.{k} must be a string")
        if not _is_int(cover.get("word_count_estimate")):
            errs.append("manager: cover.word_count_estimate must be an int")

    es = report.get("executive_summary")
    if not _is_str_list(es or []):
        errs.append("manager: executive_summary must be a list of strings")
    elif not (1 <= len(es) <= 10):
        errs.append("manager: executive_summary must have 1-10 entries")

    kf = report.get("key_findings")
    if not _is_str_list(kf or []):
        errs.append("manager: key_findings must be a list of strings")

    if not _is_str_list(report.get("risks") or []):
        errs.append("manager: risks must be a list of strings")
    if not _is_str_list(report.get("opportunities") or []):
        errs.append("manager: opportunities must be a list of strings")
    if not _is_str_list(report.get("gaps_acknowledged") or []):
        errs.append("manager: gaps_acknowledged must be a list of strings")

    conf = report.get("manager_confidence")
    if conf not in _VALID_CONFIDENCE:
        errs.append(f"manager: manager_confidence must be one of {sorted(_VALID_CONFIDENCE)}")

    # Sources
    sources = report.get("sources")
    source_ns: set[int] = set()
    if not isinstance(sources, list):
        errs.append("manager: sources must be a list")
        sources = []
    else:
        if not (1 <= len(sources) <= 200):
            errs.append("manager: sources length must be in [1,200]")
        for i, s in enumerate(sources):
            if not isinstance(s, dict):
                errs.append(f"manager: sources[{i}] must be an object")
                continue
            n = s.get("n")
            if not _is_int(n):
                errs.append(f"manager: sources[{i}].n must be an int")
            else:
                if n in source_ns:
                    errs.append(f"manager: sources[{i}].n duplicate ({n})")
                source_ns.add(n)
            t = s.get("title")
            u = s.get("url")
            if not _is_str(t):
                errs.append(f"manager: sources[{i}].title must be a string")
            if not isinstance(u, str) or not (u.startswith("http://") or u.startswith("https://")):
                errs.append(f"manager: sources[{i}].url must start with http(s)://")
            # date / snippet optional but if present must be strings
            if "date" in s and not _is_str(s.get("date")):
                errs.append(f"manager: sources[{i}].date must be a string")
            if "snippet" in s and not _is_str(s.get("snippet")):
                errs.append(f"manager: sources[{i}].snippet must be a string")

    # Sections
    sections = report.get("sections")
    all_citations: list[int] = []
    if not isinstance(sections, list):
        errs.append("manager: sections must be a list")
    else:
        content_keys = {
            "paragraphs", "bullets", "skill_table", "plan_bullets",
            "salary_home_table", "salary_neighbors_table",
            "gap_skills", "overrepresented_skills",
        }
        for i, sec in enumerate(sections):
            if not isinstance(sec, dict):
                errs.append(f"manager: sections[{i}] must be an object")
                continue
            if not _is_str(sec.get("id")):
                errs.append(f"manager: sections[{i}].id must be a string")
            if not _is_str(sec.get("heading")):
                errs.append(f"manager: sections[{i}].heading must be a string")
            if not any(k in sec for k in content_keys):
                errs.append(
                    f"manager: sections[{i}] must have at least one content field "
                    f"(paragraphs/bullets/skill_table/plan_bullets/salary_*)"
                )
            cits = sec.get("citations")
            if not isinstance(cits, list):
                errs.append(f"manager: sections[{i}].citations must be a list")
            else:
                for c in cits:
                    if not _is_int(c):
                        errs.append(f"manager: sections[{i}].citations entry must be int")
                    else:
                        all_citations.append(c)

    # Recommendations
    recs = report.get("recommendations")
    if not isinstance(recs, list):
        errs.append("manager: recommendations must be a list")
    else:
        for i, r in enumerate(recs):
            if not isinstance(r, dict):
                errs.append(f"manager: recommendations[{i}] must be an object")
                continue
            if r.get("priority") not in _ALLOWED_PRIORITY:
                errs.append(f"manager: recommendations[{i}].priority must be must/should/nice")
            if not _is_str(r.get("text")):
                errs.append(f"manager: recommendations[{i}].text must be a string")
            if not _is_str(r.get("rationale")):
                errs.append(f"manager: recommendations[{i}].rationale must be a string")
            cits = r.get("citations")
            if not isinstance(cits, list):
                errs.append(f"manager: recommendations[{i}].citations must be a list")
            else:
                for c in cits:
                    if not _is_int(c):
                        errs.append(f"manager: recommendations[{i}].citations entry must be int")
                    else:
                        all_citations.append(c)

    # Global citation integrity
    for c in all_citations:
        if c not in source_ns:
            errs.append(f"manager: citation {c} does not match any sources.n")
            break  # one is enough; surfacing all would be spammy

    return errs


def synthesize_with_manager(
    ok_results: dict[str, dict],
    failed: list[dict],
    ctx: dict[str, str],
    *,
    model: str = DEFAULT_MODEL,
    timeout_s: int = DEFAULT_MANAGER_TIMEOUT,
    _run_p_with_tools: Callable = run_p_with_tools,
) -> tuple[dict | None, str | None]:
    """Run the manager/synthesizer Opus call. Returns (report, error)."""
    tmpl = _read_prompt("market_research_manager.txt")
    if not tmpl:
        return None, "prompt_missing"

    missing_topics = ", ".join(sorted({f.get("topic", "") for f in (failed or [])}))
    worker_results_json = json.dumps(ok_results, ensure_ascii=False, separators=(",", ":"))

    # Use a merged context so both worker-style and manager-only placeholders resolve.
    mgr_ctx = dict(ctx)
    mgr_ctx["worker_results_json"] = worker_results_json
    mgr_ctx["missing_topics"] = missing_topics

    prompt = _render_prompt(tmpl, mgr_ctx)

    def _call(p: str) -> tuple[dict | None, str | None]:
        stdout = _run_p_with_tools(
            p,
            allowed_tools=_MANAGER_ALLOWED_TOOLS,
            disallowed_tools=_MANAGER_DISALLOWED_TOOLS,
            model=model,
            timeout_s=timeout_s,
            output_format="json",
        )
        return _parse_worker_response(stdout)

    parsed, err = _call(prompt)
    if parsed is None:
        return None, err

    v_errs = _validate_manager_report(parsed)
    if v_errs:
        first = v_errs[0]
        retry_prompt = (
            prompt
            + "\n\nYour previous response was invalid. Re-emit strict JSON "
            + f"satisfying the schema. Error: {first}."
        )
        parsed2, err2 = _call(retry_prompt)
        if parsed2 is None:
            return None, f"validation_error: {first}"
        v_errs2 = _validate_manager_report(parsed2)
        if v_errs2:
            return None, f"validation_error: {v_errs2[0]}"
        parsed = parsed2

    return parsed, None


# ---------------------------------------------------------------------------
# Top-level entrypoint
# ---------------------------------------------------------------------------

def market_research_sync(
    chat_id: int,
    resume_text: str,
    profile: dict,
    location: str,
    *,
    model: str = DEFAULT_MODEL,
    concurrency: int = DEFAULT_CONCURRENCY,
    worker_timeout_s: int = DEFAULT_WORKER_TIMEOUT,
    overall_timeout_s: int = DEFAULT_OVERALL_TIMEOUT,
    manager_timeout_s: int = DEFAULT_MANAGER_TIMEOUT,
    progress: Callable[[int, int], None] | None = None,
    _run_p_with_tools: Callable = run_p_with_tools,
) -> ResearchRun:
    """Full run: fan out to 10 workers, then synthesize. Never raises."""
    today_iso = time.strftime("%Y-%m-%d", time.gmtime())
    started_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ctx = _build_ctx(resume_text, profile or {}, location, today_iso)

    resume_sha1 = sha1_hex(resume_text)
    prefs_sha1 = sha1_hex(ctx.get("free_text") or "")
    start = time.monotonic()

    log.info(
        "market_research: START chat=%s role=%s location=%s concurrency=%d",
        chat_id,
        ctx.get("primary_role") or "?",
        ctx.get("location") or "?",
        concurrency,
    )

    # Inject per-run timeout + run_p_with_tools into the worker callable.
    _worker_bound = functools.partial(
        run_worker,
        _run_p_with_tools=_run_p_with_tools,
    )

    # If the caller passed a custom worker_timeout_s we use it by cloning the
    # WORKERS list with the adjusted timeout — avoids mutating the module-level.
    if worker_timeout_s != DEFAULT_WORKER_TIMEOUT:
        workers_local = [
            WorkerSpec(
                topic=w.topic,
                prompt_filename=w.prompt_filename,
                validator=w.validator,
                required_source_count=w.required_source_count,
                timeout_s=worker_timeout_s,
            )
            for w in WORKERS
        ]
    else:
        workers_local = WORKERS

    ok_results: dict[str, dict] = {}
    failed: list[dict] = []
    manager_report: dict | None = None
    error: str | None = None

    try:
        ok_results, failed = run_all(
            ctx,
            workers=workers_local,
            concurrency=concurrency,
            overall_timeout_s=overall_timeout_s,
            model=model,
            progress=progress,
            _run_worker=_worker_bound,
        )
    except Exception as e:
        log.exception("market_research: run_all crashed")
        error = f"run_all_exception: {e!r}"

    # Decide top-level status.
    total = len(workers_local)
    n_ok = len(ok_results)
    n_failed = len(failed)
    if error is not None and n_ok == 0:
        status = "exception"
    elif n_ok == 0:
        status = "failed"
    elif n_failed >= 5:
        status = "failed"
    elif n_failed == 0:
        status = "ok"
    else:
        status = "partial"

    # Run manager only when we have something to synthesize.
    if status in ("ok", "partial"):
        try:
            manager_report, mgr_err = synthesize_with_manager(
                ok_results,
                failed,
                ctx,
                model=model,
                timeout_s=manager_timeout_s,
                _run_p_with_tools=_run_p_with_tools,
            )
            if manager_report is None:
                # Manager failure demotes OK → partial; partial stays partial.
                if status == "ok":
                    status = "partial"
                if error is None:
                    error = f"manager: {mgr_err}"
        except Exception as e:
            log.exception("market_research: synthesize_with_manager crashed")
            if error is None:
                error = f"manager_exception: {e!r}"
            if status == "ok":
                status = "partial"

    elapsed_ms = int((time.monotonic() - start) * 1000)
    finished_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    log.info(
        "market_research: END chat=%s status=%s elapsed=%dms ok=%d failed=%d manager=%s",
        chat_id, status, elapsed_ms, n_ok, n_failed,
        "yes" if manager_report is not None else "no",
    )

    return ResearchRun(
        status=status,
        workers_ok=sorted(ok_results.keys()),
        workers_failed=failed,
        worker_results=ok_results,
        manager_report=manager_report,
        elapsed_ms=elapsed_ms,
        started_at_iso=started_at_iso,
        finished_at_iso=finished_at_iso,
        location_used=ctx.get("location") or "",
        resume_sha1=resume_sha1,
        prefs_sha1=prefs_sha1,
        model=model,
        error=error,
    )
