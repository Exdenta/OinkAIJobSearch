"""Per-user job-search profile builder (Opus subagent).

This module runs on every resume upload and every /prefs change, invokes the
`claude` CLI with `--model opus` and the prompt in
`prompts/profile_builder.txt`, validates the returned JSON against our
schema, and persists it at `users.user_profile` plus a row in
`profile_builds`.

Design choices, recorded so the next editor doesn't have to reverse-engineer:

  • **Debounce.** `/prefs` edits are coalesced with a 60-second timer per
    user: rapid tweaks → one Opus call at the end of the window. Resume
    uploads cancel the timer and run immediately (a new CV is a bigger
    signal than a sentence change).

  • **Single in-flight per user.** If a build is running, a new trigger
    marks `_pending[chat_id] = latest_inputs` and the in-flight worker
    re-runs itself on completion. Guarantees freshness without
    parallel double-spend on Opus tokens.

  • **Fail soft.** On parse_error / validation_error / timeout / CLI-missing,
    the LIVE profile is left untouched. The user sees a soft-fail Telegram
    message ("couldn't rebuild — your previous profile is still active")
    iff they're online; otherwise silent. The next trigger tries again.

  • **No code-exec surface.** Everything Opus returns is pure JSON,
    consumed by typed Python. `profile_schema_validate` enforces the
    allowlist of ATS domains, length caps, enum values, and the
    resume/prefs sha1 match (so a model that fabricates identity fields
    gets rejected before we persist).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from claude_cli import run_p, extract_assistant_text, parse_json_block
from instrumentation.wrappers import wrapped_run_p
import forensic


def _instrumented_run_p(prompt, **kwargs):
    """Default `_run_p` for `build_profile_sync` — records every call to the
    `claude_calls` telemetry table under caller='profile_builder'. Tests can
    still inject a stub via `_run_p=` to avoid burning real CLI calls.
    """
    return wrapped_run_p(None, "profile_builder", prompt, **kwargs)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "opus"
# Opus profile-build latency has crept up over time (measured `claude_calls`
# for caller='profile_builder': ~10-25s in May → 130-290s by late June; a
# 2026-06-24 resume_upload rebuild for chat 433775883 took 287.8s). The old
# 180s ceiling timed out outright, and even a 300s cap cleared that build by
# only ~12s. Set to 3000s (operator request 2026-06-24) for generous headroom
# against the slowest Opus builds — the build runs async on its own daemon
# thread and a slow success is harmless, so a wide ceiling only matters as a
# backstop against a genuinely hung CLI. Override via PROFILE_BUILD_TIMEOUT_S.
DEFAULT_TIMEOUT_S = int(os.environ.get("PROFILE_BUILD_TIMEOUT_S", "3000"))
DEFAULT_DEBOUNCE_S = 60.0

# "resume_upload" skips the debounce and runs immediately; "prefs_change"
# enters the debounce window. "manual" (/rebuildprofile) acts like
# resume_upload — user asked explicitly, don't make them wait. "onboarding"
# is the wizard's final-step build (the wizard defers the resume_upload
# build to finalize, so this is the user's FIRST real profile) — must run
# immediately, never debounced.
_IMMEDIATE_TRIGGERS = {"resume_upload", "manual", "onboarding"}

_ALLOWED_ATS = frozenset({
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
    "personio.de", "recruitee.com", "myworkday.com", "bamboohr.com",
    "teamtailor.com", "smartrecruiters.com",
})

_ALLOWED_REMOTE = frozenset({"any", "remote", "hybrid", "onsite"})
_ALLOWED_LEVELS = frozenset({
    "junior", "mid", "middle", "senior", "lead", "staff", "principal",
})

# Output cap: the `free_text` field the profile JSON echoes back stays
# tight (500 chars). The INPUT we feed the prompt — `user_description` —
# accumulates the user's /prefs verbatim PLUS rolling skip-feedback
# comments, so we clip it at a higher ceiling to preserve those signals.
_MAX_FREETEXT_LEN = 500
_MAX_USER_DESC_INPUT_CHARS = 3000
_MAX_RESUME_CHARS = 8000   # the resume fed into the prompt is clipped
_MAX_TITLE_TOKEN = 40
_MAX_LINKEDIN_Q = 80
_MAX_SEED_PHRASE = 120
# Lifted from 5 → 10 (P6-T2 2026-05-23): on a single 1h run the
# continuous searcher only delivers ~0.3% of LinkedIn candidates above
# the score-≥4 floor, partly because 5 queries × N geos was too narrow
# a candidate pool. Opus now picks 8-12 queries (primary-stack variants
# + secondary-stack + adjacent-role titles + one remote-only variant),
# and the cross-product still trims to MAX_LINKEDIN_DISPATCHES in
# `sources/linkedin.py`. Existing 5-query user profiles stay valid —
# back-compat: old shapes pass `_validate_linkedin_seeds` unchanged.
_MAX_LINKEDIN_QUERIES = 10
_MAX_SEED_PHRASES = 12

# Location of the external prompt files.
#   profile_builder.txt — v3 schema (full structured profile). THE LIVE
#       PROMPT: rendered by build_profile_sync, which is the production
#       default for ProfileBuilderQueue and rebuild_profile. Carries the
#       language-awareness rules (18a / 21 / 22) for native-language queries.
#   profile_seeds.txt   — v4 schema (search seeds only — algorithm v2).
#       NOT PRESENT ON DISK. build_search_seeds_sync renders it; selecting
#       that builder without first creating the template short-circuits every
#       build to status='exception'. Kept for a future seeds-only rollout.
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "profile_builder.txt"
_SEEDS_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "profile_seeds.txt"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def sha1_hex(s: str | None) -> str:
    return hashlib.sha1((s or "").encode("utf-8", "ignore")).hexdigest()


def _load_prompt_template() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log.error("profile_builder: can't read prompt at %s: %s", _PROMPT_PATH, e)
        return ""


def _render_prompt(resume_text: str, user_description: str) -> str:
    """Substitute {resume_text} and {user_description}. We don't use .format()
    because the template contains literal braces (it documents a JSON schema).
    """
    tmpl = _load_prompt_template()
    if not tmpl:
        return ""
    clipped_resume = (resume_text or "")[:_MAX_RESUME_CHARS]
    clipped_prefs = (user_description or "")[:_MAX_USER_DESC_INPUT_CHARS]
    return (
        tmpl
        .replace("{resume_text}", clipped_resume)
        .replace("{user_description}", clipped_prefs)
    )


def _load_seeds_template() -> str:
    """Read the algorithm-v2 seeds-only prompt. Returns "" on failure."""
    try:
        return _SEEDS_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log.error("profile_builder: can't read seeds prompt at %s: %s",
                  _SEEDS_PROMPT_PATH, e)
        return ""


def _render_seeds_prompt(resume_text: str, user_description: str) -> str:
    tmpl = _load_seeds_template()
    if not tmpl:
        return ""
    clipped_resume = (resume_text or "")[:_MAX_RESUME_CHARS]
    clipped_prefs = (user_description or "")[:_MAX_USER_DESC_INPUT_CHARS]
    return (
        tmpl
        .replace("{resume_text}", clipped_resume)
        .replace("{user_description}", clipped_prefs)
    )


# ---------------------------------------------------------------------------
# Schema validator
# ---------------------------------------------------------------------------

def _is_str(x: Any) -> bool:
    return isinstance(x, str)


def _is_str_list(x: Any) -> bool:
    return isinstance(x, list) and all(isinstance(s, str) for s in x)


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _all_lowercase(xs: list[str]) -> bool:
    return all(s == s.lower() for s in xs)


def _len_le(xs: list[str], n: int) -> bool:
    return all(len(s) <= n for s in xs)


def _validate_linkedin_seeds(li: Any) -> list[str]:
    """Validate the search_seeds.linkedin block. Accepts BOTH shapes:

      SEPARATED (v2.3 — preferred):
        {"queries": [<str>, ...], "geos": [<str>, ...], "f_TPR": "r86400"}

      PAIRED (v2.0 legacy, still accepted):
        {"queries": [{"q":..., "geo":..., "f_TPR":...}, ...]}

    Returns the list of validation error strings (empty when shape OK).
    """
    errs: list[str] = []
    if not isinstance(li, dict):
        return ["search_seeds.linkedin must be an object"]
    queries = li.get("queries")
    if not isinstance(queries, list):
        return ["search_seeds.linkedin.queries must be a list"]
    if not queries:
        return errs  # empty list is fine — adapter returns []
    if len(queries) > _MAX_LINKEDIN_QUERIES:
        errs.append(
            f"search_seeds.linkedin.queries length > {_MAX_LINKEDIN_QUERIES}"
        )

    # Distinguish shapes by the first entry's type.
    first = queries[0]
    if isinstance(first, str):
        # SEPARATED shape: queries = [<str>], geos = [<str>], f_TPR = <str>
        for i, q in enumerate(queries):
            if not isinstance(q, str):
                errs.append(f"linkedin.queries[{i}] must be a string")
                continue
            if len(q) > _MAX_LINKEDIN_Q:
                errs.append(f"linkedin.queries[{i}] > {_MAX_LINKEDIN_Q} chars")
        geos = li.get("geos")
        if geos is not None:
            if not isinstance(geos, list) or not all(isinstance(g, str) for g in geos):
                errs.append("linkedin.geos must be a list of strings")
        f_tpr = li.get("f_TPR")
        if f_tpr is not None and not isinstance(f_tpr, str):
            errs.append("linkedin.f_TPR must be a string")
    elif isinstance(first, dict):
        # PAIRED legacy shape.
        for i, q in enumerate(queries):
            if not isinstance(q, dict):
                errs.append(f"linkedin.queries[{i}] must be an object")
                continue
            qs = q.get("q")
            if not isinstance(qs, str):
                errs.append(f"linkedin.queries[{i}].q must be a string")
            elif len(qs) > _MAX_LINKEDIN_Q:
                errs.append(f"linkedin.queries[{i}].q > {_MAX_LINKEDIN_Q} chars")
            if not isinstance(q.get("geo"), str):
                errs.append(f"linkedin.queries[{i}].geo must be a string")
            if not isinstance(q.get("f_TPR"), str):
                errs.append(f"linkedin.queries[{i}].f_TPR must be a string")
    else:
        errs.append(
            "linkedin.queries entries must be strings (v2.3 separated) "
            "or objects (v2.0 paired)"
        )
    return errs


def seeds_schema_validate(profile: Any) -> list[str]:
    """Validate the algorithm-v2 seeds-only schema (schema_version=4).

    Looser than `profile_schema_validate` — most legacy fields are absent
    by design. Required: schema_version=4, primary_role string, and a
    well-formed `search_seeds` block. Bookkeeping keys (`built_at`,
    `built_from`) are accepted but not required.
    """
    errs: list[str] = []
    if not isinstance(profile, dict):
        return ["profile is not a dict"]

    required = {"schema_version", "primary_role", "search_seeds"}
    missing = sorted(required - set(profile))
    if missing:
        errs.append(f"missing keys: {missing}")

    if profile.get("schema_version") != 4:
        errs.append("schema_version must equal 4")

    if "primary_role" in profile and not _is_str(profile["primary_role"]):
        errs.append("primary_role must be a string")

    # `enabled_sources` is OPTIONAL — older v4 profiles built before
    # 2026-05-12 don't carry it. When present, must be a list of strings.
    if "enabled_sources" in profile:
        es = profile["enabled_sources"]
        if not (isinstance(es, list) and all(isinstance(s, str) for s in es)):
            errs.append("enabled_sources must be a list of strings")

    seeds = profile.get("search_seeds")
    if not isinstance(seeds, dict):
        errs.append("search_seeds must be an object")
    else:
        errs.extend(_validate_linkedin_seeds(seeds.get("linkedin")))
        ws = seeds.get("web_search")
        if not isinstance(ws, dict):
            errs.append("search_seeds.web_search must be an object")
        else:
            phrases = ws.get("seed_phrases") or []
            if not _is_str_list(phrases):
                errs.append("search_seeds.web_search.seed_phrases must be a list of strings")
            elif phrases and (len(phrases) > _MAX_SEED_PHRASES
                              or not _len_le(phrases, _MAX_SEED_PHRASE)):
                errs.append(
                    f"seed_phrases length > {_MAX_SEED_PHRASES} or items > {_MAX_SEED_PHRASE} chars"
                )
            ats = ws.get("ats_domains") or []
            if not _is_str_list(ats):
                errs.append("search_seeds.web_search.ats_domains must be a list of strings")
            else:
                bad = [d for d in ats if d not in _ALLOWED_ATS]
                if bad:
                    errs.append(f"ats_domains contains disallowed entries: {bad}")
    return errs


def profile_schema_validate(profile: Any) -> list[str]:
    """Return a list of validation errors; empty list means valid.

    We're intentionally strict about shape (types, enum values, length caps,
    ATS allowlist) but lenient about content (we don't second-guess the
    model's keyword choices — that's what the prompt is for).
    """
    errs: list[str] = []

    if not isinstance(profile, dict):
        return ["profile is not a dict"]

    # Required top-level keys. v3 adds `onsite_locations` + `remote_regions`
    # alongside the legacy `locations` field (kept for back-compat readers).
    required = {
        "schema_version", "ideal_fit_paragraph", "primary_role",
        "target_levels", "years_experience",
        "stack_primary", "stack_secondary", "stack_adjacent", "stack_antipatterns",
        "title_must_match", "title_exclude", "exclude_keywords", "exclude_companies",
        "locations", "remote", "time_zone_band",
        "salary_min_usd", "drop_if_salary_unknown", "language",
        "max_age_hours", "min_match_score",
        "search_seeds", "free_text",
    }
    sv = profile.get("schema_version")
    if sv == 4:
        # Algorithm-v2 seeds-only profile. Loosens the required set
        # drastically — see `seeds_schema_validate` for the strict
        # checker. Falling through to legacy validation would reject
        # every v4 profile because most fields are intentionally absent.
        return seeds_schema_validate(profile)
    if sv == 3:
        required = required | {"onsite_locations", "remote_regions"}
    missing = sorted(required - set(profile))
    if missing:
        errs.append(f"missing keys: {missing}")

    if sv not in (2, 3):
        errs.append("schema_version must equal 2, 3, or 4")

    for k in ("ideal_fit_paragraph", "primary_role", "time_zone_band",
              "language", "free_text"):
        if k in profile and not _is_str(profile[k]):
            errs.append(f"{k} must be a string")

    for k in ("target_levels", "stack_primary", "stack_secondary",
              "stack_adjacent", "stack_antipatterns",
              "title_must_match", "title_exclude", "exclude_keywords",
              "exclude_companies", "locations",
              "onsite_locations", "remote_regions"):
        if k in profile and not _is_str_list(profile[k]):
            errs.append(f"{k} must be a list of strings")

    # Numeric + boolean fields
    for k in ("years_experience", "salary_min_usd", "max_age_hours",
              "min_match_score"):
        if k in profile and not _is_int(profile[k]):
            errs.append(f"{k} must be an integer")

    if "drop_if_salary_unknown" in profile and not isinstance(
        profile["drop_if_salary_unknown"], bool
    ):
        errs.append("drop_if_salary_unknown must be a bool")

    remote = profile.get("remote")
    if isinstance(remote, str) and remote not in _ALLOWED_REMOTE:
        errs.append(f"remote must be one of {sorted(_ALLOWED_REMOTE)}")

    # min_match_score clamp
    ms = profile.get("min_match_score")
    if _is_int(ms) and not (0 <= ms <= 5):
        errs.append("min_match_score must be in [0, 5]")

    # target_levels: enum-ish
    for lvl in (profile.get("target_levels") or []):
        if isinstance(lvl, str) and lvl and lvl not in _ALLOWED_LEVELS:
            # Permissive: warn-style, don't reject
            pass

    # Lowercase enforcement for list fields (except exclude_companies —
    # company names keep their case).
    lower_fields = [
        "target_levels", "stack_primary", "stack_secondary",
        "stack_adjacent", "stack_antipatterns",
        "title_must_match", "title_exclude", "exclude_keywords",
        "locations", "onsite_locations", "remote_regions",
    ]
    for k in lower_fields:
        xs = profile.get(k)
        if _is_str_list(xs) and not _all_lowercase(xs):
            errs.append(f"{k} must be all-lowercase")

    # Length caps on title gates.
    for k in ("title_must_match", "title_exclude"):
        xs = profile.get(k)
        if _is_str_list(xs) and not _len_le(xs, _MAX_TITLE_TOKEN):
            errs.append(f"{k} items must be ≤ {_MAX_TITLE_TOKEN} chars")

    # Salary bounds.
    if _is_int(profile.get("salary_min_usd")):
        if not (0 <= profile["salary_min_usd"] <= 10_000_000):
            errs.append("salary_min_usd out of sane range")

    # search_seeds shape.
    seeds = profile.get("search_seeds")
    if not isinstance(seeds, dict):
        errs.append("search_seeds must be an object")
    else:
        errs.extend(_validate_linkedin_seeds(seeds.get("linkedin")))
        ws = seeds.get("web_search")
        if not isinstance(ws, dict):
            errs.append("search_seeds.web_search must be an object")
        else:
            phrases = ws.get("seed_phrases")
            if not _is_str_list(phrases or []):
                errs.append("search_seeds.web_search.seed_phrases must be a list of strings")
            elif phrases and (len(phrases) > _MAX_SEED_PHRASES
                              or not _len_le(phrases, _MAX_SEED_PHRASE)):
                errs.append(
                    f"seed_phrases length > {_MAX_SEED_PHRASES} or items > {_MAX_SEED_PHRASE} chars"
                )
            ats = ws.get("ats_domains") or []
            if not _is_str_list(ats):
                errs.append("search_seeds.web_search.ats_domains must be a list of strings")
            else:
                bad = [d for d in ats if d not in _ALLOWED_ATS]
                if bad:
                    errs.append(f"ats_domains contains disallowed entries: {bad}")
            fn = ws.get("focus_notes")
            if fn is not None and not isinstance(fn, str):
                errs.append("focus_notes must be a string")

    return errs


# ---------------------------------------------------------------------------
# Post-processing (normalize + stamp identity fields)
# ---------------------------------------------------------------------------

def _stamp_metadata(
    profile: dict[str, Any],
    *,
    resume_sha1: str,
    prefs_sha1: str,
    model: str,
    elapsed_ms: int,
) -> dict[str, Any]:
    """Add `built_at` / `built_from` blocks we control, so downstream can
    trust that these fields reflect OUR submission — not whatever the model
    hallucinated."""
    out = dict(profile)
    out["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out["built_from"] = {
        "resume_sha1": resume_sha1,
        "prefs_sha1":  prefs_sha1,
        "model":       model,
        "elapsed_ms":  int(elapsed_ms),
    }
    return out


def _clip_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Enforce caps that are easy to trim rather than reject on."""
    p = dict(profile)
    # free_text cap
    ft = p.get("free_text") or ""
    if isinstance(ft, str) and len(ft) > _MAX_FREETEXT_LEN:
        p["free_text"] = ft[:_MAX_FREETEXT_LEN]
    # seed_phrases cap
    seeds = p.get("search_seeds")
    if isinstance(seeds, dict):
        ws = seeds.get("web_search") or {}
        phrases = ws.get("seed_phrases")
        if isinstance(phrases, list) and len(phrases) > _MAX_SEED_PHRASES:
            ws["seed_phrases"] = phrases[:_MAX_SEED_PHRASES]
            seeds["web_search"] = ws
            p["search_seeds"] = seeds
        # Drop disallowed ATS (don't reject the whole build — just sanitize)
        ats = (ws or {}).get("ats_domains") or []
        if isinstance(ats, list):
            clean = [d for d in ats if isinstance(d, str) and d in _ALLOWED_ATS]
            if clean != ats:
                ws["ats_domains"] = clean
                seeds["web_search"] = ws
                p["search_seeds"] = seeds
        li = seeds.get("linkedin") or {}
        queries = li.get("queries")
        if isinstance(queries, list) and len(queries) > _MAX_LINKEDIN_QUERIES:
            li["queries"] = queries[:_MAX_LINKEDIN_QUERIES]
            seeds["linkedin"] = li
            p["search_seeds"] = seeds
    return p


# ---------------------------------------------------------------------------
# Synchronous build
# ---------------------------------------------------------------------------

@dataclass
class BuildResult:
    status: str                      # ok | cli_missing_or_timeout | parse_error | validation_error | exception
    profile: dict[str, Any] | None = None
    error: str | None = None
    elapsed_ms: int = 0
    resume_sha1: str = ""
    prefs_sha1: str = ""
    model: str = ""


def build_profile_sync(
    resume_text: str,
    free_text: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    model: str = DEFAULT_MODEL,
    _run_p: Callable = _instrumented_run_p,    # injected in tests
) -> BuildResult:
    """Run one Opus call end-to-end. Returns a BuildResult — never raises.

    Tests inject `_run_p=` with a mock to avoid burning real CLI calls.
    """
    resume_sha1 = sha1_hex(resume_text)
    prefs_sha1 = sha1_hex(free_text)
    start = time.monotonic()

    with forensic.step(
        "profile_builder.build_profile_sync",
        input={
            "resume_chars": len(resume_text or ""),
            "free_text_chars": len(free_text or ""),
            "free_text_head": (free_text or "")[:300],
            "resume_sha1": resume_sha1,
            "prefs_sha1": prefs_sha1,
            "model": model,
            "timeout_s": timeout_s,
        },
    ) as fctx:
        prompt = _render_prompt(resume_text, free_text)
        if not prompt:
            fctx.set_output({"status": "exception", "reason": "prompt template missing/empty"})
            return BuildResult(
                status="exception",
                error="prompt template missing/empty",
                resume_sha1=resume_sha1,
                prefs_sha1=prefs_sha1,
                model=model,
            )

        try:
            stdout = _run_p(prompt, timeout_s=timeout_s, model=model)
        except Exception as e:
            fctx.set_output({"status": "exception", "reason": f"run_p raised: {e!r}"})
            return BuildResult(
                status="exception",
                error=f"run_p raised: {e!r}",
                elapsed_ms=int((time.monotonic() - start) * 1000),
                resume_sha1=resume_sha1,
                prefs_sha1=prefs_sha1,
                model=model,
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        if stdout is None:
            fctx.set_output({"status": "cli_missing_or_timeout", "elapsed_ms": elapsed_ms})
            return BuildResult(
                status="cli_missing_or_timeout",
                error="run_p returned None",
                elapsed_ms=elapsed_ms,
                resume_sha1=resume_sha1,
                prefs_sha1=prefs_sha1,
                model=model,
            )

        body = extract_assistant_text(stdout)
        parsed = parse_json_block(body)
        if parsed is None:
            fctx.set_output({
                "status": "parse_error",
                "elapsed_ms": elapsed_ms,
                "body_head": (body or "")[:300],
            })
            return BuildResult(
                status="parse_error",
                error=f"unparseable response (head={body[:200]!r})",
                elapsed_ms=elapsed_ms,
                resume_sha1=resume_sha1,
                prefs_sha1=prefs_sha1,
                model=model,
            )

        errs = profile_schema_validate(parsed)
        if errs:
            fctx.set_output({
                "status": "validation_error",
                "elapsed_ms": elapsed_ms,
                "errors": errs[:8],
            })
            return BuildResult(
                status="validation_error",
                error="; ".join(errs)[:500],
                elapsed_ms=elapsed_ms,
                resume_sha1=resume_sha1,
                prefs_sha1=prefs_sha1,
                model=model,
            )

        stamped = _stamp_metadata(
            _clip_profile(parsed),
            resume_sha1=resume_sha1,
            prefs_sha1=prefs_sha1,
            model=model,
            elapsed_ms=elapsed_ms,
        )
        fctx.set_output({
            "status": "ok",
            "elapsed_ms": elapsed_ms,
            "primary_role": stamped.get("primary_role"),
            "stack_primary": stamped.get("stack_primary"),
            "min_match_score": stamped.get("min_match_score"),
            "search_seed_keys": list((stamped.get("search_seeds") or {}).keys()),
        })
        return BuildResult(
            status="ok",
            profile=stamped,
            elapsed_ms=elapsed_ms,
            resume_sha1=resume_sha1,
            prefs_sha1=prefs_sha1,
            model=model,
        )


def build_search_seeds_sync(
    resume_text: str,
    free_text: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    model: str = DEFAULT_MODEL,
    _run_p: Callable = _instrumented_run_p,
) -> BuildResult:
    """Algorithm-v2 builder: emit a search_seeds-only profile (schema v4).

    Same control flow as `build_profile_sync`, but renders the
    seeds-only prompt and validates against `seeds_schema_validate`. The
    output is a small profile dict carrying just `search_seeds` plus
    bookkeeping (`built_at`, `built_from`). Used by the queue's
    `_run_one` whenever the operator has flipped the profile to v2.
    """
    resume_sha1 = sha1_hex(resume_text)
    prefs_sha1 = sha1_hex(free_text)
    start = time.monotonic()

    with forensic.step(
        "profile_builder.build_search_seeds_sync",
        input={
            "resume_chars": len(resume_text or ""),
            "free_text_chars": len(free_text or ""),
            "free_text_head": (free_text or "")[:300],
            "resume_sha1": resume_sha1,
            "prefs_sha1": prefs_sha1,
            "model": model,
            "timeout_s": timeout_s,
        },
    ) as fctx:
        prompt = _render_seeds_prompt(resume_text, free_text)
        if not prompt:
            fctx.set_output({"status": "exception", "reason": "seeds prompt template missing"})
            return BuildResult(
                status="exception",
                error="seeds prompt template missing",
                resume_sha1=resume_sha1, prefs_sha1=prefs_sha1, model=model,
            )

        try:
            stdout = _run_p(prompt, timeout_s=timeout_s, model=model)
        except Exception as e:
            fctx.set_output({"status": "exception", "reason": f"run_p raised: {e!r}"})
            return BuildResult(
                status="exception",
                error=f"run_p raised: {e!r}",
                elapsed_ms=int((time.monotonic() - start) * 1000),
                resume_sha1=resume_sha1, prefs_sha1=prefs_sha1, model=model,
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        if stdout is None:
            fctx.set_output({"status": "cli_missing_or_timeout", "elapsed_ms": elapsed_ms})
            return BuildResult(
                status="cli_missing_or_timeout",
                error="run_p returned None",
                elapsed_ms=elapsed_ms,
                resume_sha1=resume_sha1, prefs_sha1=prefs_sha1, model=model,
            )

        body = extract_assistant_text(stdout)
        parsed = parse_json_block(body)
        if parsed is None:
            fctx.set_output({
                "status": "parse_error", "elapsed_ms": elapsed_ms,
                "body_head": (body or "")[:300],
            })
            return BuildResult(
                status="parse_error",
                error=f"unparseable response (head={body[:200]!r})",
                elapsed_ms=elapsed_ms,
                resume_sha1=resume_sha1, prefs_sha1=prefs_sha1, model=model,
            )

        errs = seeds_schema_validate(parsed)
        if errs:
            fctx.set_output({
                "status": "validation_error", "elapsed_ms": elapsed_ms,
                "errors": errs[:8],
            })
            return BuildResult(
                status="validation_error",
                error="; ".join(errs)[:500],
                elapsed_ms=elapsed_ms,
                resume_sha1=resume_sha1, prefs_sha1=prefs_sha1, model=model,
            )

        # Reuse `_clip_profile` to enforce caps; then stamp bookkeeping.
        stamped = _stamp_metadata(
            _clip_profile(parsed),
            resume_sha1=resume_sha1, prefs_sha1=prefs_sha1,
            model=model, elapsed_ms=elapsed_ms,
        )
        fctx.set_output({
            "status": "ok",
            "elapsed_ms": elapsed_ms,
            "primary_role": stamped.get("primary_role"),
            "search_seed_keys": list((stamped.get("search_seeds") or {}).keys()),
        })
        return BuildResult(
            status="ok",
            profile=stamped,
            elapsed_ms=elapsed_ms,
            resume_sha1=resume_sha1, prefs_sha1=prefs_sha1, model=model,
        )


# ---------------------------------------------------------------------------
# Async debounced queue (per-user)
# ---------------------------------------------------------------------------

@dataclass
class _QueueEntry:
    timer: threading.Timer | None = None     # debounce timer; None = no debounce pending
    pending: dict[str, Any] | None = field(default=None)  # latest inputs if a build is in-flight
    inflight: bool = False


class ProfileBuilderQueue:
    """Per-chat debounce + single-in-flight coordinator.

    One instance per bot process. `bot.py` constructs a module-level singleton
    with `make_default_queue(db, tg)` and calls `queue.enqueue(...)` from the
    HTTP handlers.
    """

    def __init__(
        self,
        db: Any,                  # the DB instance from db.py
        tg: Any | None = None,    # optional TelegramClient for progress messages
        *,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        timeout_s: int = DEFAULT_TIMEOUT_S,
        model: str = DEFAULT_MODEL,
        # Production default = `build_profile_sync`, which renders
        # `prompts/profile_builder.txt` (the v3 structured-profile prompt that
        # carries the language-awareness rules 18a/21/22). The seeds-only
        # `build_search_seeds_sync` renders `prompts/profile_seeds.txt`, which
        # has never existed on disk — selecting it short-circuits every rebuild
        # to status='exception' (seeds prompt template missing) and the
        # language-aware prompt never runs. Operators can still opt into the
        # seeds-only builder explicitly via `sync_builder=build_search_seeds_sync`
        # once a profile_seeds.txt template exists. Tests inject mocks here.
        sync_builder: Callable = build_profile_sync,
        on_done: Callable[[int, BuildResult], None] | None = None,
    ) -> None:
        self.db = db
        self.tg = tg
        self.debounce_s = float(debounce_s)
        self.timeout_s = int(timeout_s)
        self.model = model
        self._build = sync_builder
        self._on_done = on_done
        self._entries: dict[int, _QueueEntry] = {}
        self._lock = threading.Lock()

    def enqueue(
        self,
        chat_id: int,
        resume_text: str,
        free_text: str,
        *,
        trigger: str,
    ) -> None:
        """Queue a build. Immediate triggers run right away; prefs_change
        debounces for `self.debounce_s` seconds."""
        inputs = {
            "resume_text": resume_text or "",
            "free_text":   free_text or "",
            "trigger":     trigger,
        }
        with self._lock:
            entry = self._entries.setdefault(chat_id, _QueueEntry())

            if entry.inflight:
                # Coalesce: remember the latest inputs; worker will re-run on
                # completion with whatever is latest at that time.
                entry.pending = inputs
                log.debug("profile_builder: chat=%s in-flight, coalescing %s",
                          chat_id, trigger)
                return

            # Cancel any pending debounce timer — we'll either fire now
            # (immediate trigger) or restart the window (prefs_change).
            if entry.timer is not None:
                entry.timer.cancel()
                entry.timer = None

            if trigger in _IMMEDIATE_TRIGGERS or self.debounce_s <= 0:
                entry.inflight = True
                # Release lock before spawning the worker; worker acquires
                # its own lock when flipping state back.
                threading.Thread(
                    target=self._worker,
                    args=(chat_id, inputs),
                    daemon=True,
                    name=f"profile-builder-{chat_id}",
                ).start()
                return

            # Debounced path: schedule a Timer that triggers the worker.
            def _fire() -> None:
                with self._lock:
                    entry2 = self._entries.get(chat_id)
                    if entry2 is None or entry2.inflight:
                        return
                    entry2.timer = None
                    entry2.inflight = True
                threading.Thread(
                    target=self._worker,
                    args=(chat_id, inputs),
                    daemon=True,
                    name=f"profile-builder-{chat_id}",
                ).start()

            t = threading.Timer(self.debounce_s, _fire)
            t.daemon = True
            entry.timer = t
            t.start()
            log.debug("profile_builder: chat=%s debounced %.1fs (%s)",
                      chat_id, self.debounce_s, trigger)

    def _worker(self, chat_id: int, inputs: dict[str, Any]) -> None:
        """Run builds in a loop until no pending follow-ups remain.

        inflight is already True when we enter; we flip it False only once
        we drain all coalesced pending inputs. This keeps the single-in-
        flight invariant intact even if enqueue() is called multiple times
        during a long-running build.
        """
        current: dict[str, Any] | None = inputs
        while current is not None:
            try:
                self._run_one(chat_id, current)
            except Exception:
                log.exception("profile_builder: worker crashed for chat=%s", chat_id)
            with self._lock:
                entry = self._entries.get(chat_id)
                if entry is None:
                    return
                if entry.pending is not None:
                    current = entry.pending
                    entry.pending = None
                    # keep inflight = True — we're about to run again
                else:
                    entry.inflight = False
                    current = None

    def _run_one(self, chat_id: int, inputs: dict[str, Any]) -> None:
        trigger = inputs.get("trigger") or "manual"
        resume_text = inputs.get("resume_text") or ""
        free_text = inputs.get("free_text") or ""

        log.info("profile_builder: START chat=%s trigger=%s", chat_id, trigger)
        result = self._build(
            resume_text,
            free_text,
            timeout_s=self.timeout_s,
            model=self.model,
        )

        profile_json: str | None = None
        if result.status == "ok" and result.profile is not None:
            profile_json = json.dumps(result.profile, ensure_ascii=False)

        # Audit log — always, success or failure.
        try:
            self.db.log_profile_build(
                chat_id=chat_id,
                trigger=trigger,
                status=result.status,
                elapsed_ms=result.elapsed_ms,
                resume_sha1=result.resume_sha1,
                prefs_sha1=result.prefs_sha1,
                model=result.model,
                error_head=result.error,
                profile_json=profile_json,
            )
        except Exception:
            log.exception("profile_builder: audit-log insert failed for chat=%s", chat_id)

        # Persist live profile on success. Preserve the user's current
        # min_match_score if Opus didn't set one of its own — the ⭐ button
        # writes straight to the profile JSON, and we don't want a rebuild
        # to silently clear a manual user setting.
        if result.status == "ok" and result.profile is not None:
            try:
                if int(result.profile.get("min_match_score") or 0) == 0:
                    try:
                        prior_raw = self.db.get_user_profile(chat_id)
                        if prior_raw:
                            prior = json.loads(prior_raw) or {}
                            prior_score = int((prior or {}).get("min_match_score") or 0)
                            if prior_score > 0:
                                result.profile["min_match_score"] = prior_score
                    except Exception:
                        log.exception(
                            "profile_builder: could not carry min_match_score "
                            "forward for chat=%s", chat_id,
                        )
                profile_json = json.dumps(result.profile, ensure_ascii=False)
                rev = self.db.set_user_profile(chat_id, profile_json)
                # Mirror Opus-picked enabled_sources into the dedicated
                # DB column so search_jobs can read it without parsing
                # the profile JSON every run. Bot /sources UI also reads
                # from the column. Best-effort: a failure here leaves
                # the user on operator-default sources.
                es = result.profile.get("enabled_sources")
                if isinstance(es, list) and es:
                    try:
                        self.db.set_enabled_sources(chat_id, es)
                    except Exception:
                        log.exception(
                            "profile_builder: set_enabled_sources failed "
                            "for chat=%s", chat_id,
                        )
                log.info(
                    "profile_builder: DONE chat=%s status=ok rev=%s elapsed=%dms "
                    "keywords=%d title_must=%d title_excl=%d ln_queries=%d seeds=%d",
                    chat_id,
                    rev,
                    result.elapsed_ms,
                    len((result.profile or {}).get("stack_primary") or []),
                    len((result.profile or {}).get("title_must_match") or []),
                    len((result.profile or {}).get("title_exclude") or []),
                    len(((result.profile or {}).get("search_seeds") or {})
                        .get("linkedin", {}).get("queries") or []),
                    len(((result.profile or {}).get("search_seeds") or {})
                        .get("web_search", {}).get("seed_phrases") or []),
                )
            except Exception:
                log.exception("profile_builder: set_user_profile failed for chat=%s", chat_id)
        else:
            log.warning(
                "profile_builder: DONE chat=%s status=%s elapsed=%dms err=%r",
                chat_id, result.status, result.elapsed_ms, (result.error or "")[:200],
            )

        # Telegram progress messages (best-effort, never raise).
        self._notify_user(chat_id, result)

        # Test / admin hook.
        if self._on_done is not None:
            try:
                self._on_done(chat_id, result)
            except Exception:
                log.exception("profile_builder: on_done hook raised")

    # -----------------------------------------------------------------------
    # User-facing messages
    # -----------------------------------------------------------------------

    def _notify_user(self, chat_id: int, result: BuildResult) -> None:
        if self.tg is None:
            return
        try:
            if result.status == "ok":
                secs = result.elapsed_ms // 1000
                self.tg.send_plain(
                    chat_id,
                    "✅ Profile rebuilt — new filter rules are live "
                    f"({secs}s). Try /myprofile to inspect.",
                )
            elif result.status == "cli_missing_or_timeout":
                self.tg.send_plain(
                    chat_id,
                    "⚠️ Couldn't rebuild your profile (CLI unavailable or timeout). "
                    "Your previous profile is still active.",
                )
            elif result.status in ("validation_error", "parse_error"):
                self.tg.send_plain(
                    chat_id,
                    "⚠️ Profile rebuild failed schema check — your previous "
                    "profile is still active. (Details logged for the admin.)",
                )
            else:
                self.tg.send_plain(
                    chat_id,
                    "⚠️ Profile rebuild hit an error — your previous profile "
                    "is still active.",
                )
        except Exception:
            log.exception("profile_builder: notify_user failed for chat=%s", chat_id)

    # -----------------------------------------------------------------------
    # Test helpers
    # -----------------------------------------------------------------------

    def wait_idle(self, timeout_s: float = 30.0) -> bool:
        """Block until no entries are in-flight or have a pending timer.
        Returns False on timeout. Tests use this to deterministically wait
        for debounced work."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                busy = any(
                    e.inflight or e.timer is not None
                    for e in self._entries.values()
                )
            if not busy:
                return True
            time.sleep(0.05)
        return False


# ---------------------------------------------------------------------------
# Standalone rebuild trigger (P4 auto-rebuild path)
# ---------------------------------------------------------------------------

def rebuild_profile(
    db: Any,
    chat_id: int,
    *,
    # Default = `build_profile_sync` (renders the language-aware
    # prompts/profile_builder.txt). `build_search_seeds_sync` renders the
    # absent prompts/profile_seeds.txt and would short-circuit every
    # auto-rebuild to an exception — see ProfileBuilderQueue.__init__ note.
    sync_builder: Callable = build_profile_sync,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    model: str = DEFAULT_MODEL,
) -> BuildResult:
    """Run one profile build for `chat_id` synchronously.

    Resolves resume + free-text + accumulated skip-reason notes from the
    DB exactly the way the bot's `_enqueue_profile_rebuild` does, then
    calls the chosen sync builder. Returns the `BuildResult` so the
    caller can decide whether to reset its own bookkeeping (e.g. the P4
    `skip_events_since_rebuild` counter only resets on `status == "ok"`).

    Raises iff the builder itself raises an uncaught exception — every
    schema/parse/CLI failure is surfaced as a non-ok BuildResult so
    `search_jobs` callers can decide ("retry next iteration" vs "reset
    counter") without try/except gymnastics. Persists the new profile
    on success.
    """
    row = db.get_user(chat_id)
    if row is None:
        return BuildResult(
            status="exception", error="no_user_row", model=model,
        )

    resume_text = ""
    try:
        resume_text = row["resume_text"] or ""
    except (IndexError, KeyError):
        resume_text = ""
    prefs = (db.get_prefs_free_text(chat_id) or "").strip()
    skip_notes = (db.get_skip_notes_text(chat_id) or "").strip()
    if skip_notes:
        sep = "\n\n[Recent 'not a fit' comments]\n"
        free_text = (prefs + sep + skip_notes) if prefs else (
            "[Recent 'not a fit' comments]\n" + skip_notes
        )
    else:
        free_text = prefs

    result = sync_builder(
        resume_text, free_text, timeout_s=timeout_s, model=model,
    )

    profile_json: str | None = None
    if result.status == "ok" and result.profile is not None:
        profile_json = json.dumps(result.profile, ensure_ascii=False)
    try:
        db.log_profile_build(
            chat_id=chat_id,
            trigger="auto_skip_threshold",
            status=result.status,
            elapsed_ms=result.elapsed_ms,
            resume_sha1=result.resume_sha1,
            prefs_sha1=result.prefs_sha1,
            model=result.model,
            error_head=result.error,
            profile_json=profile_json,
        )
    except Exception:
        log.exception(
            "rebuild_profile: audit-log insert failed for chat=%s",
            chat_id,
        )

    if result.status == "ok" and result.profile is not None:
        try:
            db.set_user_profile(chat_id, profile_json)
            es = result.profile.get("enabled_sources")
            if isinstance(es, list) and es:
                try:
                    db.set_enabled_sources(chat_id, es)
                except Exception:
                    log.exception(
                        "rebuild_profile: set_enabled_sources failed "
                        "for chat=%s", chat_id,
                    )
        except Exception:
            log.exception(
                "rebuild_profile: set_user_profile failed for chat=%s",
                chat_id,
            )
    return result
