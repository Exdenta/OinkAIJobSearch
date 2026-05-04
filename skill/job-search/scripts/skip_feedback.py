"""Surgical profile updates from a single user-reported skip reason.

When a user rejects a posting and explains why ("not interested in fintech",
"hate working with WordPress", "had a bad time at Acme"), we feed that text
plus the rejected job into Haiku and ask for a SMALL, CONSERVATIVE delta of
exclusion tokens to merge into their existing profile. The bot.py layer
already runs `safety_check` against the user's text before it reaches us,
so this module trusts its inputs to be non-injection but still wraps them
as opaque data inside the prompt for defense in depth.

Why a separate module instead of triggering profile_builder?
------------------------------------------------------------
profile_builder.py rebuilds the WHOLE profile via Opus from resume + free-
text. That's a 10-30s call, costs Opus tokens, and runs the risk of the
model "moving" other fields the user didn't ask to change. A skip-reason
feedback signal is narrow ("filter out X going forward") — Haiku is cheap
and fast, and we restrict its surgery to the four exclude lists. Other
fields stay frozen.

Public API
----------
    apply_skip_feedback(db, chat_id, job_context, user_reason) -> dict

Returns a dict with the four `added_*` lists (what we actually merged in,
post-dedupe) plus a human-readable `summary`. Always returns; never raises.
On any failure (CLI missing, parse error, prompt template missing) returns
empty lists and a generic summary so the UX layer always has something to
render.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from claude_cli import (
    SMALLEST_MODEL,
    extract_assistant_text,
    parse_json_block,
)
from instrumentation.wrappers import wrapped_run_p
import forensic
import user_profile as up


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = SMALLEST_MODEL
DEFAULT_TIMEOUT_S = 60

# Token-shape caps. These mirror the prompt's stated rules and are enforced
# server-side too — we never trust the model to honor its own rule list.
_MAX_TITLE_TOKEN = 40
_MAX_KW_TOKEN = 40
_MAX_COMPANY_TOKEN = 80
_MAX_ANTIPATTERN_TOKEN = 40

_MAX_TITLE_ADDITIONS = 3
_MAX_KW_ADDITIONS = 5
_MAX_COMPANY_ADDITIONS = 2
_MAX_ANTIPATTERN_ADDITIONS = 3

# Hard cap on each exclude list AFTER merge. Profiles with thousands of
# excludes balloon the prompt context and slow every match. FIFO eviction
# (oldest items go first) — fresher feedback is more relevant than stale
# tokens from months ago. This is a deterministic policy; tests rely on it.
_MAX_LIST_LEN = 50

# Snippet head we feed into the prompt — enough for the model to reason
# about role/domain, not so much that we balloon Haiku tokens.
_MAX_SNIPPET_CHARS = 1500
_MAX_REASON_CHARS = 800

# Generic words the model should never propose (a second line of defense
# beyond the prompt's "don't add these"). If a candidate token matches one
# of these (case-insensitive), we drop it.
_GENERIC_TOKENS = frozenset({
    "company", "team", "role", "position", "work", "job",
    "engineer", "developer", "programmer", "software",
    "salary", "remote", "hybrid", "onsite", "office",
    "scam", "scammy", "bad", "weird", "boring",
    "yes", "no", "skip", "not", "maybe",
})

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "skip_feedback.txt"


# Generic-summary fallbacks. Two flavors so the UX layer can distinguish
# "we processed your reason but couldn't extract anything actionable" from
# "we processed nothing because the model call failed".
_FALLBACK_SUMMARY_NO_EXTRACT = (
    "Got it - feedback noted but no specific filter to add."
)
_FALLBACK_SUMMARY_PARSE_ERROR = (
    "Got it - feedback noted but couldn't auto-update your profile this time."
)

# Canned hints for structured-preference feedback that the skip-reason
# flow intentionally cannot apply. Skip-reason only writes EXCLUSION lists
# (title_exclude, exclude_keywords, exclude_companies, stack_antipatterns).
# Location / remote-policy / salary / seniority live in their own
# structured profile fields and bleeding them into body-text excludes
# creates false negatives (e.g. "office" matches lots of remote postings).
# When we detect that intent, we surface a clear pointer to /prefs.
_STRUCTURED_INTENT_HINTS: dict[str, str] = {
    "location": (
        "Looks like location feedback. Skip-reason only adjusts exclusion "
        "lists. To change where you want jobs, tap /prefs and update "
        "your locations directly."
    ),
    "remote": (
        "Looks like remote-policy feedback. Skip-reason only adjusts "
        "exclusion lists. Tap /prefs to set remote / hybrid / onsite "
        "preference directly."
    ),
    "salary": (
        "Looks like salary feedback. Skip-reason only adjusts exclusion "
        "lists. Tap /prefs to set your minimum salary directly."
    ),
    "seniority": (
        "Looks like seniority feedback. Skip-reason only adjusts "
        "exclusion lists. Tap /prefs to set target levels directly."
    ),
}

# Word lists for structured-intent detection. Order matters: first hit
# wins, so put the more specific signal (salary > seniority > remote >
# location) first when ambiguous. Each token is matched as a whole word
# (\b boundary) against the lowercased reason.
_INTENT_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("salary",    ("salary", "pay", "compensation", "comp", "wage", "wages",
                   "underpaid", "low pay", "too low", "money", "$", "€", "£")),
    ("remote",    ("remote", "office", "in office", "in-office", "on-site",
                   "onsite", "hybrid", "wfh", "work from home", "rto",
                   "return to office", "office job", "office jobs",
                   "office only", "office-only", "office work")),
    ("seniority", ("junior", "intern", "entry level", "entry-level", "senior",
                   "staff", "principal", "lead", "head of", "director", "vp",
                   "manager", "too senior", "too junior")),
    # Location keywords + common geographic acronyms / country names that
    # users typically type lowercase ("in usa", "from uk", "spain only").
    # The case-strict `_LOCATION_GEO_RE` below only catches Capitalized
    # place names; this list backstops the common lowercase variants.
    ("location",  ("location", "city", "country", "commute", "far", "far away",
                   "too far", "wrong city", "wrong country", "relocate",
                   "relocation", "abroad", "timezone", "time zone",
                   # acronyms (whole-word matched)
                   "usa", "us", "uk", "eu", "eea", "emea", "apac", "latam",
                   "asia", "europe", "africa", "america", "americas",
                   "oceania", "middle east",
                   # frequent country mentions (lowercase typed)
                   "spain", "france", "germany", "italy", "netherlands",
                   "portugal", "ireland", "denmark", "sweden", "norway",
                   "finland", "poland", "ukraine", "russia", "japan",
                   "china", "india", "australia", "canada", "mexico",
                   "brazil", "argentina", "england", "scotland")),
]


import re as _re

# "in Berlin", "from Madrid", "near London" → strong location signal even
# when the user didn't say "location" / "city" / "country" outright.
# Inline `(?i:...)` makes the leading preposition case-insensitive while
# keeping the place-name bracket strict ([A-Z]). That way "in Berlin" and
# "From Madrid" both fire, but "in java" / "from anywhere" do not.
_LOCATION_GEO_RE = _re.compile(
    r"\b(?i:in|from|near|outside|around|based\s+in|located\s+in)\s+[A-Z][\w-]+",
)


def _detect_structured_intent(reason: str) -> str | None:
    """Return 'location' / 'remote' / 'salary' / 'seniority' if the reason
    looks like structured-preference feedback, else None.

    Whole-word matched, case-insensitive. Conservative — only fires on
    explicit keyword hits so a reason like "I'm not into fintech" doesn't
    falsely trip "location" via the word "into".

    A separate regex (`_LOCATION_GEO_RE`) catches geography phrases like
    "in Berlin" / "from Madrid" that wouldn't otherwise match the
    keyword list. Case matters here on purpose — capitalized noun after
    "in" is usually a place name, lowercased "in java" is usually tech.
    """
    if not reason:
        return None

    # Geography phrase scan first — runs against the ORIGINAL casing so
    # capitalization can disambiguate "in Berlin" from "in java".
    if _LOCATION_GEO_RE.search(reason):
        return "location"

    text = " " + reason.lower() + " "
    for label, tokens in _INTENT_PATTERNS:
        for tok in tokens:
            t = tok.lower()
            if any(ch.isalpha() for ch in t):
                # Word-boundary check: token must be flanked by punctuation
                # or whitespace so "lead" doesn't match "leader".
                for sep in (" ", ".", ",", ";", ":", "!", "?", "(", ")", "/", "\n", "\t"):
                    if (sep + t + sep) in text or (sep + t + sep) in text.replace("\n", " ").replace("\t", " "):
                        return label
                if (" " + t + " ") in text:
                    return label
            else:
                if t in text:
                    return label
    return None


# ---------------------------------------------------------------------------
# Empty-result helpers
# ---------------------------------------------------------------------------

def _empty_result(summary: str) -> dict[str, Any]:
    return {
        "added_title_excludes":     [],
        "added_exclude_keywords":   [],
        "added_exclude_companies":  [],
        "added_stack_antipatterns": [],
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------

def _load_prompt_template() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError as e:
        log.error("skip_feedback: can't read prompt at %s: %s", _PROMPT_PATH, e)
        return ""


def _render_prompt(
    job_context: dict[str, Any],
    user_reason: str,
    current_excludes: dict[str, list[str]],
) -> str:
    """Substitute the rendered fields into the template.

    We don't use str.format() because the template contains literal `{` /
    `}` from the JSON schema documentation. Plain .replace() is enough.
    """
    tmpl = _load_prompt_template()
    if not tmpl:
        return ""
    title = str(job_context.get("title") or "").strip()[:200]
    company = str(job_context.get("company") or "").strip()[:120]
    source = str(job_context.get("source") or "").strip()[:60]
    url = str(job_context.get("url") or "").strip()[:300]
    snippet = str(job_context.get("snippet") or "").strip()[:_MAX_SNIPPET_CHARS]
    reason = str(user_reason or "").strip()[:_MAX_REASON_CHARS]

    def _fmt_list(xs: list[str]) -> str:
        # Render as comma-separated; the model sees "(none)" when empty so
        # it knows the slot is intentionally empty rather than missing.
        if not xs:
            return "(none)"
        # Cap to 30 to keep the prompt small even for users with maxed-out
        # exclude lists. The model's job is to dedupe, not to enumerate.
        return ", ".join(str(x) for x in xs[:30])

    return (
        tmpl
        .replace("{title}",   title)
        .replace("{company}", company)
        .replace("{source}",  source)
        .replace("{url}",     url)
        .replace("{snippet}", snippet)
        .replace("{reason}",  reason)
        .replace("{title_exclude}",      _fmt_list(current_excludes.get("title_exclude") or []))
        .replace("{exclude_keywords}",   _fmt_list(current_excludes.get("exclude_keywords") or []))
        .replace("{exclude_companies}",  _fmt_list(current_excludes.get("exclude_companies") or []))
        .replace("{stack_antipatterns}", _fmt_list(current_excludes.get("stack_antipatterns") or []))
    )


# ---------------------------------------------------------------------------
# Validation / sanitization of model output
# ---------------------------------------------------------------------------

def _sanitize_token(s: Any, *, lowercase: bool, max_len: int) -> str | None:
    """Clean a single proposed token. Returns None if the result is unusable."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    if lowercase:
        s = s.lower()
    if len(s) > max_len:
        return None
    if lowercase and s in _GENERIC_TOKENS:
        return None
    return s


def _sanitize_list(
    raw: Any,
    *,
    lowercase: bool,
    max_len: int,
    max_items: int,
) -> list[str]:
    """Clean a proposed list. Drops None / wrong-type items, dedupes within
    the new batch, applies caps."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        tok = _sanitize_token(item, lowercase=lowercase, max_len=max_len)
        if tok is None:
            continue
        key = tok.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tok)
        if len(out) >= max_items:
            break
    return out


def _parse_model_output(stdout: str | None) -> dict[str, Any] | None:
    """Decode the CLI envelope → JSON object → sanitized dict.
    Returns None on any failure."""
    if stdout is None:
        return None
    body = extract_assistant_text(stdout)
    parsed = parse_json_block(body)
    if not isinstance(parsed, dict):
        return None
    title_excludes = _sanitize_list(
        parsed.get("title_excludes_to_add"),
        lowercase=True, max_len=_MAX_TITLE_TOKEN, max_items=_MAX_TITLE_ADDITIONS,
    )
    keywords = _sanitize_list(
        parsed.get("exclude_keywords_to_add"),
        lowercase=True, max_len=_MAX_KW_TOKEN, max_items=_MAX_KW_ADDITIONS,
    )
    companies = _sanitize_list(
        parsed.get("exclude_companies_to_add"),
        lowercase=False, max_len=_MAX_COMPANY_TOKEN, max_items=_MAX_COMPANY_ADDITIONS,
    )
    antipatterns = _sanitize_list(
        parsed.get("stack_antipatterns_to_add"),
        lowercase=True, max_len=_MAX_ANTIPATTERN_TOKEN, max_items=_MAX_ANTIPATTERN_ADDITIONS,
    )
    summary = parsed.get("summary")
    summary_str = summary.strip()[:200] if isinstance(summary, str) else ""

    return {
        "title_excludes": title_excludes,
        "exclude_keywords": keywords,
        "exclude_companies": companies,
        "stack_antipatterns": antipatterns,
        "summary": summary_str,
    }


# ---------------------------------------------------------------------------
# Profile merge
# ---------------------------------------------------------------------------

def _merge_one(
    existing: list[str],
    additions: list[str],
    *,
    case_insensitive: bool,
) -> tuple[list[str], list[str]]:
    """Merge `additions` into `existing` with dedupe + FIFO cap.

    Returns (new_list, actually_added). `actually_added` only contains tokens
    that weren't already present (case-insensitive when applicable) — those
    are what the UX layer surfaces to the user. FIFO cap policy: when the
    list would exceed `_MAX_LIST_LEN`, we drop the oldest items first.
    Tokens that are present-but-different-case are NOT re-added (the
    existing item stays).
    """
    if not isinstance(existing, list):
        existing = []
    seen_keys: set[str] = set()
    base: list[str] = []
    for s in existing:
        if not isinstance(s, str):
            continue
        key = s.lower() if case_insensitive else s
        if key in seen_keys:
            continue
        seen_keys.add(key)
        base.append(s)

    actually_added: list[str] = []
    for tok in additions:
        key = tok.lower() if case_insensitive else tok
        if key in seen_keys:
            continue
        seen_keys.add(key)
        base.append(tok)
        actually_added.append(tok)

    # FIFO cap — oldest first, so brand-new additions survive even when the
    # list was previously full. Tests rely on this exact policy.
    if len(base) > _MAX_LIST_LEN:
        base = base[len(base) - _MAX_LIST_LEN:]

    return base, actually_added


def _merge_excludes(
    profile: dict[str, Any],
    additions: dict[str, list[str]],
) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """Apply additions to a profile dict in place-style (returns a new copy).
    Returns (updated_profile, actually_added_per_field)."""
    updated = dict(profile)
    out_added: dict[str, list[str]] = {}

    for field, ci in (
        ("title_exclude",      True),
        ("exclude_keywords",   True),
        ("exclude_companies",  True),   # case-insensitive dedupe even though we preserve case
        ("stack_antipatterns", True),
    ):
        # Map model-output key to profile field.
        ck = {
            "title_exclude":      "title_excludes",
            "exclude_keywords":   "exclude_keywords",
            "exclude_companies":  "exclude_companies",
            "stack_antipatterns": "stack_antipatterns",
        }[field]
        existing = updated.get(field) or []
        new_list, added = _merge_one(existing, additions.get(ck) or [], case_insensitive=ci)
        updated[field] = new_list
        out_added[field] = added

    return updated, out_added


def _stub_profile_with_additions(additions: dict[str, list[str]]) -> dict[str, Any]:
    """Build a minimal profile dict carrying only the new excludes — used
    when the user has no profile yet but still gives skip feedback. The
    next full Opus rebuild will overwrite this stub but preserve the
    excludes (the builder doesn't blow away `*_exclude*` fields)."""
    return {
        "title_exclude":      list(additions.get("title_excludes") or []),
        "exclude_keywords":   list(additions.get("exclude_keywords") or []),
        "exclude_companies":  list(additions.get("exclude_companies") or []),
        "stack_antipatterns": list(additions.get("stack_antipatterns") or []),
    }


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------

def _render_summary(added: dict[str, list[str]], model_summary: str) -> str:
    """Prefer a deterministic, factual one-liner over the model's free-form
    summary so the UX is predictable. Falls back to the model's summary
    when nothing was added (the model has more context to say "no useful
    signal extracted, here's a polite line"). Final fallback is a generic
    string."""
    title_excl   = added.get("title_exclude") or []
    keywords     = added.get("exclude_keywords") or []
    companies    = added.get("exclude_companies") or []
    antipatterns = added.get("stack_antipatterns") or []

    nothing_added = not any([title_excl, keywords, companies, antipatterns])
    if nothing_added:
        if model_summary:
            return model_summary[:200]
        return _FALLBACK_SUMMARY_NO_EXTRACT

    parts: list[str] = []
    if title_excl:
        parts.append(
            "title containing "
            + " or ".join(f"'{t}'" for t in title_excl[:3])
        )
    if companies:
        parts.append(
            "company "
            + " or ".join(f"'{c}'" for c in companies[:2])
        )
    if keywords:
        parts.append(
            "mentions of "
            + " or ".join(f"'{k}'" for k in keywords[:3])
        )
    if antipatterns:
        parts.append(
            "stack "
            + " or ".join(f"'{a}'" for a in antipatterns[:3])
        )

    return ("Won't show jobs with " + ", ".join(parts) + ".")[:240]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_skip_feedback(
    db: Any,
    chat_id: int,
    job_context: dict[str, Any],
    user_reason: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    model: str = DEFAULT_MODEL,
    _run_p=None,    # injected in tests
) -> dict[str, Any]:
    """Parse `user_reason` via Haiku, extract exclusion tokens, persist back
    to the user's profile. Returns a dict the UX layer can render directly.

    Always returns. Never raises. On any failure (template missing, CLI
    missing, parse error, model returns nonsense) returns the empty-result
    shape with a generic fallback summary so the caller can always send
    *something* to the user. In failure paths the profile is NOT modified.
    """
    job_context = job_context or {}
    user_reason = (user_reason or "").strip()

    # Snapshot input metadata for forensic — we capture the count of items
    # in each exclude list so post-hoc analysis can answer "how full was
    # this user's profile when they hit the cap?"
    raw_profile = None
    profile: dict[str, Any] | None = None
    try:
        raw_profile = db.get_user_profile(chat_id) if db is not None else None
        profile = up.profile_from_json(raw_profile) if raw_profile else None
    except Exception:
        log.exception("skip_feedback: get_user_profile failed for chat=%s", chat_id)
        profile = None

    current_excludes = {
        "title_exclude":      list((profile or {}).get("title_exclude") or []),
        "exclude_keywords":   list((profile or {}).get("exclude_keywords") or []),
        "exclude_companies":  list((profile or {}).get("exclude_companies") or []),
        "stack_antipatterns": list((profile or {}).get("stack_antipatterns") or []),
    }

    forensic_input = {
        "chat_id": chat_id,
        "job_id": job_context.get("job_id"),
        "title": (job_context.get("title") or "")[:120],
        "company": (job_context.get("company") or "")[:80],
        "source": (job_context.get("source") or "")[:40],
        "reason_chars": len(user_reason),
        "reason_head": user_reason[:200],
        "current_exclude_counts": {
            k: len(v) for k, v in current_excludes.items()
        },
    }

    # Empty reason → don't waste a Haiku call.
    if not user_reason:
        result = _empty_result(_FALLBACK_SUMMARY_NO_EXTRACT)
        forensic.log_step(
            "skip_feedback.applied",
            input=forensic_input,
            output={"additions": {}, "summary": result["summary"], "skipped": "empty_reason"},
            chat_id=chat_id,
        )
        return result

    prompt = _render_prompt(job_context, user_reason, current_excludes)
    if not prompt:
        result = _empty_result(_FALLBACK_SUMMARY_PARSE_ERROR)
        forensic.log_step(
            "skip_feedback.applied",
            input=forensic_input,
            output={"additions": {}, "summary": result["summary"], "skipped": "no_prompt"},
            chat_id=chat_id,
        )
        return result

    runner = _run_p if _run_p is not None else wrapped_run_p
    try:
        if _run_p is not None:
            stdout = runner(prompt, timeout_s=timeout_s, model=model)
        else:
            stdout = runner(
                None, "skip_feedback", prompt,
                timeout_s=timeout_s, model=model, chat_id=chat_id,
            )
    except Exception as e:
        log.exception("skip_feedback: runner raised for chat=%s: %s", chat_id, e)
        result = _empty_result(_FALLBACK_SUMMARY_PARSE_ERROR)
        forensic.log_step(
            "skip_feedback.applied",
            input=forensic_input,
            output={"additions": {}, "summary": result["summary"], "error": f"runner: {e!r}"},
            chat_id=chat_id,
        )
        return result

    parsed = _parse_model_output(stdout)
    if parsed is None:
        result = _empty_result(_FALLBACK_SUMMARY_PARSE_ERROR)
        forensic.log_step(
            "skip_feedback.applied",
            input=forensic_input,
            output={"additions": {}, "summary": result["summary"], "skipped": "parse_error"},
            chat_id=chat_id,
        )
        return result

    additions = {
        "title_excludes":     parsed["title_excludes"],
        "exclude_keywords":   parsed["exclude_keywords"],
        "exclude_companies":  parsed["exclude_companies"],
        "stack_antipatterns": parsed["stack_antipatterns"],
    }
    model_summary = parsed.get("summary") or ""

    # Merge into the profile (or a fresh stub if no profile exists).
    base_profile = profile if profile is not None else _stub_profile_with_additions({})
    updated, actually_added = _merge_excludes(base_profile, additions)

    # Persist iff we actually changed something.
    persisted = False
    any_added = any(actually_added.values())
    if any_added:
        try:
            if db is not None:
                db.set_user_profile(chat_id, json.dumps(updated, ensure_ascii=False))
            persisted = True
        except Exception:
            log.exception("skip_feedback: set_user_profile failed for chat=%s", chat_id)

    summary = _render_summary(actually_added, model_summary)

    # Structured-intent override: if Haiku extracted nothing AND the
    # reason looks like location/remote/salary/seniority feedback, replace
    # the model's (often jargon-y) explanation with a clear pointer to
    # /prefs. The user gets a UI that tells them where to make the change
    # they wanted, not a confusing "this belongs to profile not excludes"
    # paraphrase. When intent isn't pinpointable but extracts are still
    # empty, append a generic /prefs hint so the user always sees an
    # actionable next step.
    if not any_added:
        intent = _detect_structured_intent(user_reason)
        if intent:
            summary = _STRUCTURED_INTENT_HINTS[intent]
        elif user_reason:
            # Drop trailing punctuation so the appended sentence reads
            # cleanly regardless of what the model returned.
            base = (summary or _FALLBACK_SUMMARY_NO_EXTRACT).rstrip(". ").strip()
            summary = (
                base
                + ". Tap /prefs to adjust structured preferences "
                "(location, remote, salary, seniority)."
            )[:240]

    result = {
        "added_title_excludes":     actually_added.get("title_exclude") or [],
        "added_exclude_keywords":   actually_added.get("exclude_keywords") or [],
        "added_exclude_companies":  actually_added.get("exclude_companies") or [],
        "added_stack_antipatterns": actually_added.get("stack_antipatterns") or [],
        "summary": summary,
    }

    forensic.log_step(
        "skip_feedback.applied",
        input=forensic_input,
        output={
            "additions": result,
            "summary": summary,
            "model_summary": model_summary[:200],
            "persisted": persisted,
        },
        chat_id=chat_id,
    )
    return result
