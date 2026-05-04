"""Per-user profile: Opus-built structured JSON + filter helpers.

The profile is produced by `profile_builder.py` — a subagent that reads the
user's resume and their free-text /prefs input and emits a strict-shaped
JSON object. This module provides the consumer-side helpers:

    * `profile_from_json(raw)`          load stored JSON → dict (None on fail)
    * `profile_to_json(profile)`        dict → JSON string
    * `effective_filters(g, profile)`   per-user filter dict layered on globals
    * `apply_profile(jobs, profile)`    post-filter jobs by every stated gate
    * `format_profile_summary_mdv2`     human-readable MarkdownV2 rendering
    * `set_min_match_score(p, score)`   surgical ⭐ edit — returns updated dict

Profile shape (see `profile_builder.py` for the authoritative schema):

    {
      "schema_version": 2,
      "ideal_fit_paragraph": "...",
      "primary_role": "...",
      "target_levels": ["senior"],
      "years_experience": 7,
      "stack_primary": ["react", "typescript"],
      "stack_secondary": [...],
      "stack_adjacent": [...],
      "stack_antipatterns": [...],
      "title_must_match": [...],
      "title_exclude": [...],
      "exclude_keywords": [...],
      "exclude_companies": [...],
      "locations": [...],
      "remote": "remote"|"hybrid"|"onsite"|"any",
      "time_zone_band": "...",
      "salary_min_usd": 80000,
      "drop_if_salary_unknown": false,
      "language": "english",
      "max_age_hours": 168,
      "min_match_score": 3,
      "search_seeds": {
          "linkedin": {"queries": [{"q","geo","f_TPR"}, ...]},
          "web_search": {
              "seed_phrases": [...],
              "ats_domains":  [...],
              "focus_notes":  "..."
          }
      },
      "free_text": "<raw user wording, echoed>",
      "built_at":   "<iso>",              # stamped post-build
      "built_from": {resume_sha1, prefs_sha1, model, elapsed_ms}
    }

Fields the user didn't state stay at their sentinel (`[]` / `"any"` / `0` /
`""`). `effective_filters` treats sentinel values as "inherit from global".
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from dedupe import Job


_VALID_REMOTE = {"any", "remote", "hybrid", "onsite"}
_VALID_SENIORITY = {"any", "junior", "mid", "senior"}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def profile_to_json(profile: dict[str, Any]) -> str:
    return json.dumps(profile, ensure_ascii=False)


def profile_from_json(raw: str | None) -> dict[str, Any] | None:
    """Load a stored profile dict. Returns None on any decode/shape failure.

    We do NOT re-run the strict schema validator here — the builder already
    did that before persisting. This is defense in depth: if the stored JSON
    is malformed (shouldn't happen), callers fall back to "no profile" which
    degrades gracefully to global-default behavior.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def is_empty_profile(profile: dict[str, Any] | None) -> bool:
    """True if the profile has no actionable signal. A profile with ONLY
    `min_match_score` set counts as NON-empty so the score gate still fires."""
    if not profile:
        return True
    for key in ("stack_primary", "stack_secondary", "title_must_match",
                "title_exclude", "exclude_keywords", "exclude_companies",
                "locations"):
        if profile.get(key):
            return False
    if (profile.get("remote") or "any") != "any":
        return False
    if (profile.get("target_levels") or []):
        return False
    if int(profile.get("salary_min_usd") or 0) > 0:
        return False
    if profile.get("drop_if_salary_unknown"):
        return False
    if (profile.get("language") or "").strip():
        return False
    if int(profile.get("max_age_hours") or 0) > 0:
        return False
    if int(profile.get("min_match_score") or 0) > 0:
        return False
    return True


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _str_list(v: Any) -> list[str]:
    """Lowercase, de-duplicate, drop empties while preserving first-seen order."""
    if not isinstance(v, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in v:
        if not isinstance(item, str):
            continue
        s = item.strip().lower()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _digits(s: str) -> list[int]:
    """Runs of digits of length ≥ 4 as ints (salary-detector helper)."""
    out: list[int] = []
    buf: list[str] = []
    for ch in s or "":
        if ch.isdigit():
            buf.append(ch)
        else:
            if len(buf) >= 4:
                try:
                    out.append(int("".join(buf)))
                except ValueError:
                    pass
            buf = []
    if len(buf) >= 4:
        try:
            out.append(int("".join(buf)))
        except ValueError:
            pass
    return out


def _job_haystack(j: Job) -> str:
    return " ".join([
        (j.title or ""), (j.company or ""), (j.location or ""),
        (j.snippet or ""), (j.salary or ""),
    ]).lower()


# ---------------------------------------------------------------------------
# Projection onto flat filter fields
# ---------------------------------------------------------------------------
# The profile carries a rich structure (keyword clusters, per-source search
# seeds, ideal-fit paragraph, …) but for filter-evaluation purposes we only
# care about the projection onto a flat slot dict (the legacy filters.yaml
# field names) consumed by the post-filter gate. This projection is conservative:
# we never invent a constraint the user didn't state.

def project_to_prefs(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Public wrapper around `_project`. Callers that need a flat dict of
    the user's stated preferences (e.g. the AI-enrichment prompt) go through
    this. Returns an empty dict for a None profile so readers can safely
    `.get("keywords") or []` etc.
    """
    return _project(profile or {})


def _project(profile: dict[str, Any]) -> dict[str, Any]:
    """Project a profile onto flat filter-slot names.

    Mapping:
      * `keywords`         = stack_primary ∪ stack_secondary
                             (antipatterns go to exclude_keywords, not here —
                             ORing a veto into an OR-match gate would invert
                             its meaning.)
      * `exclude_keywords` = exclude_keywords ∪ stack_antipatterns
      * `seniority`        = target_levels collapsed to a single enum if it
                             contains exactly one distinct level, else "any".
      * title gates, locations, remote, companies, salary, language,
        max_age_hours, min_match_score, free_text pass through.
    """
    p = profile or {}
    primary   = _str_list(p.get("stack_primary"))
    secondary = _str_list(p.get("stack_secondary"))
    anti      = _str_list(p.get("stack_antipatterns"))

    # OR-match keyword pool.
    kw_seen: set[str] = set()
    keywords: list[str] = []
    for s in primary + secondary:
        if s and s not in kw_seen:
            kw_seen.add(s)
            keywords.append(s)

    # Body-level veto: stated excludes + antipatterns.
    excl_seen: set[str] = set()
    exclude_keywords: list[str] = []
    for s in _str_list(p.get("exclude_keywords")) + anti:
        if s and s not in excl_seen:
            excl_seen.add(s)
            exclude_keywords.append(s)

    # exclude_companies keeps case (company names).
    co = p.get("exclude_companies") or []
    if not isinstance(co, list):
        co = []
    exclude_companies = [str(c).strip() for c in co if isinstance(c, str) and c.strip()]

    # Seniority: "any" unless there's exactly one level named.
    target_levels = _str_list(p.get("target_levels"))
    collapsed = {"mid" if t in ("middle", "mid") else t for t in target_levels}
    if len(collapsed) == 1:
        sen = next(iter(collapsed))
        seniority = sen if sen in _VALID_SENIORITY else "any"
    else:
        seniority = "any"

    remote = str(p.get("remote") or "any").strip().lower()
    if remote not in _VALID_REMOTE:
        remote = "any"

    return {
        "keywords":               keywords,
        "title_must_match":       _str_list(p.get("title_must_match")),
        "title_exclude":          _str_list(p.get("title_exclude")),
        "exclude_keywords":       exclude_keywords,
        "exclude_companies":      exclude_companies,
        "seniority":              seniority,
        "locations":              _str_list(p.get("locations")),
        "remote":                 remote,
        "salary_min_usd":         int(p.get("salary_min_usd") or 0),
        "drop_if_salary_unknown": bool(p.get("drop_if_salary_unknown")),
        "language":               str(p.get("language") or "").strip().lower(),
        "max_age_hours":          int(p.get("max_age_hours") or 0),
        "min_match_score":        max(0, min(5, int(p.get("min_match_score") or 0))),
        "free_text":              str(p.get("free_text") or "").strip()[:500],
    }


# The v1 filter merge used this field map; preserved here so the merge
# semantics match exactly.
_LIST_FIELDS = {
    "keywords":          "keywords",
    "title_must_match":  "title_must_match",
    "title_exclude":     "title_exclude",
    "exclude_keywords":  "exclude_keywords",
    "exclude_companies": "exclude_companies",
    "locations":         "locations",
}


def effective_filters(
    global_filters: dict[str, Any],
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """Produce the EFFECTIVE filters dict for one user.

    Semantics: full override per user. For every field the user SPECIFIED
    (list non-empty, remote/seniority != "any", numeric > 0), the user's
    value wins. Fields left at the sentinel inherit from `global_filters`,
    so universal guardrails (exclude_companies, title_exclude) still apply
    unless the user consciously overrode them.

    The `sources` block and `message` block come from `global_filters`
    unchanged — users don't control which adapters run or how the digest
    is formatted.
    """
    out = dict(global_filters or {})
    if not profile:
        return out
    projected = _project(profile)

    # List fields: user's list replaces global's if user supplied any.
    for prof_key, glob_key in _LIST_FIELDS.items():
        v = projected.get(prof_key) or []
        if v:
            out[glob_key] = list(v)

    sen = projected.get("seniority") or "any"
    if sen != "any":
        out["seniority"] = sen

    remote = projected.get("remote") or "any"
    if remote != "any":
        out["remote"] = remote

    min_usd  = int(projected.get("salary_min_usd") or 0)
    drop_unk = bool(projected.get("drop_if_salary_unknown"))
    if min_usd or drop_unk:
        salary = dict(out.get("salary") or {})
        if min_usd:
            salary["min_usd"] = min_usd
        if drop_unk:
            salary["drop_if_unknown"] = True
        out["salary"] = salary

    max_age = int(projected.get("max_age_hours") or 0)
    if max_age > 0:
        out["max_age_hours"] = max_age

    return out


# ---------------------------------------------------------------------------
# Post-filter (applies the projection to fetched jobs)
# ---------------------------------------------------------------------------

def apply_profile(jobs: Iterable[Job], profile: dict[str, Any] | None) -> list[Job]:
    """Filter `jobs` by the profile's flat-projected constraints.

    In production the pipeline's AI enrichment pass is the sole matching gate
    (see `search_jobs.post_filter`), so this function is unused by the digest.
    It's kept because:
      * tests assert constraint-by-constraint behavior against it,
      * reviving a cheap pre-filter in the future is a one-line change.

    Every field left at its sentinel is a pass — "no opinion" means "any job
    can still satisfy this slot".
    """
    if is_empty_profile(profile):
        return list(jobs)

    p = _project(profile or {})
    kw           = p.get("keywords") or []
    title_must   = p.get("title_must_match") or []
    title_excl   = p.get("title_exclude") or []
    body_excl    = p.get("exclude_keywords") or []
    co_excl      = {c.lower() for c in (p.get("exclude_companies") or []) if c}
    seniority    = (p.get("seniority") or "any").lower()
    locations    = p.get("locations") or []
    remote       = (p.get("remote") or "any").lower()
    min_usd      = int(p.get("salary_min_usd") or 0)
    drop_unk     = bool(p.get("drop_if_salary_unknown"))
    language     = (p.get("language") or "").strip().lower()

    out: list[Job] = []
    for j in jobs:
        hay = _job_haystack(j)
        title_lc = (j.title or "").lower()
        # Tighter haystack for geography to avoid body-text bleed (e.g.
        # "Canada-friendly candidates welcome" in an EU posting).
        geo_hay = " ".join([(j.title or ""), (j.location or "")]).lower()

        if title_must and not any(t in title_lc for t in title_must):
            continue
        if title_excl and any(t in title_lc for t in title_excl):
            continue
        if j.company and j.company.lower() in co_excl:
            continue
        if kw and not any(k in hay for k in kw):
            continue
        if seniority != "any" and seniority not in hay:
            continue
        if locations and not any(loc in geo_hay for loc in locations):
            continue
        if remote != "any":
            if remote == "remote":
                if "remote" not in hay and "anywhere" not in hay:
                    continue
            elif remote == "hybrid":
                if "hybrid" not in hay:
                    continue
            elif remote == "onsite":
                # Drop obvious-remote postings; keep ambiguous ones.
                if ("remote" in hay and "hybrid" not in hay
                        and "on-site" not in hay and "onsite" not in hay):
                    continue
        if body_excl and any(k in hay for k in body_excl):
            continue
        if min_usd or drop_unk:
            if j.salary:
                nums = _digits(j.salary)
                if nums and max(nums) < min_usd:
                    continue
            else:
                if drop_unk:
                    continue
                # else: unknown salary → keep
        if language and language not in hay:
            continue

        out.append(j)
    return out


# ---------------------------------------------------------------------------
# ⭐ Min-match-score surgical edit
# ---------------------------------------------------------------------------

def set_min_match_score(
    profile: dict[str, Any] | None,
    score: int,
) -> dict[str, Any]:
    """Return a new profile dict with `min_match_score` set to `score`.

    Preserves every other field if `profile` is non-None. If `profile` is
    None (user hasn't had a profile built yet), returns a minimal stub
    containing only the score — search_jobs.py reads it via
    `profile.get("min_match_score")` regardless of shape, so a stub works.

    The next successful profile rebuild (Opus) WILL overwrite the stub. If
    the user has tweaked the score manually and wants it preserved across
    rebuilds, that's a feature we'd have to bolt onto the builder queue;
    for now, re-tapping ⭐ after a rebuild is the documented workflow.
    """
    score = max(0, min(5, int(score or 0)))
    out = dict(profile) if isinstance(profile, dict) else {}
    out["min_match_score"] = score
    return out


def get_free_text(profile: dict[str, Any] | None) -> str:
    """Small helper: Opus echoes back the user's free-text in the profile;
    readers that want the raw input for downstream subagents use this."""
    if not profile:
        return ""
    ft = profile.get("free_text")
    return str(ft).strip() if isinstance(ft, str) else ""


# ---------------------------------------------------------------------------
# MarkdownV2 summary for /myprofile
# ---------------------------------------------------------------------------

def format_profile_summary_mdv2(
    profile: dict[str, Any] | None,
    mdv2_escape,
) -> str:
    """Render a profile as a human-readable MarkdownV2 block. Surfaces the
    ideal_fit_paragraph and per-source search seeds so the user can
    sanity-check what Opus inferred from their resume + prefs.
    """
    if not profile:
        return mdv2_escape(
            "No profile yet — upload a resume and run /prefs to describe "
            "what you're looking for."
        )
    p = profile

    def row(emoji: str, label: str, value: str) -> str:
        return f"{emoji} *{mdv2_escape(label)}:* {mdv2_escape(value)}"

    lines: list[str] = ["🧠 *Your profile*", ""]

    ideal = (p.get("ideal_fit_paragraph") or "").strip()
    if ideal:
        lines.append("_" + mdv2_escape(ideal) + "_")
        lines.append("")

    role = (p.get("primary_role") or "").strip()
    if role:
        lines.append(row("🎯", "Role", role))
    levels = p.get("target_levels") or []
    if levels:
        lines.append(row("🎚", "Levels", ", ".join(levels)))
    yrs = p.get("years_experience")
    if yrs:
        lines.append(row("📅", "Years", str(yrs)))

    for label, key, emoji in [
        ("Primary stack",     "stack_primary",      "🧰"),
        ("Secondary stack",   "stack_secondary",    "🔧"),
        ("Antipatterns",      "stack_antipatterns", "🚫"),
        ("Title must match",  "title_must_match",   "📎"),
        ("Title exclude",     "title_exclude",      "🚫"),
        ("Body exclude",      "exclude_keywords",   "🚫"),
        ("Locations",         "locations",          "📍"),
        ("Excluded companies","exclude_companies",  "🏢"),
    ]:
        vals = p.get(key) or []
        if vals:
            lines.append(row(emoji, label, ", ".join(vals[:12])))

    remote = p.get("remote") or "any"
    if remote != "any":
        lines.append(row("🏠", "Remote policy", remote))
    tz = (p.get("time_zone_band") or "").strip()
    if tz:
        lines.append(row("⏰", "Timezone band", tz))
    min_usd = int(p.get("salary_min_usd") or 0)
    if min_usd:
        lines.append(row("💶", "Min salary", f"{min_usd:,} USD"))
    if p.get("drop_if_salary_unknown"):
        lines.append("💶 " + mdv2_escape("Drops postings without listed salary."))
    lang = (p.get("language") or "").strip()
    if lang:
        lines.append(row("🗣", "Language", lang))
    max_age = int(p.get("max_age_hours") or 0)
    if max_age:
        lines.append(row("⏱", "Max posting age", f"{max_age}h"))
    min_score = int(p.get("min_match_score") or 0)
    if min_score:
        stars = "⭐" * min_score + "☆" * (5 - min_score)
        lines.append(
            f"📈 *{mdv2_escape('Min match score')}:* "
            f"{stars} {mdv2_escape(f'{min_score}/5')}"
        )

    seeds = (p.get("search_seeds") or {})
    li = (seeds.get("linkedin") or {}).get("queries") or []
    if li:
        lines.append("")
        lines.append("🔎 *" + mdv2_escape("LinkedIn queries") + "*")
        for q in li[:3]:
            if isinstance(q, dict):
                lines.append(
                    "• " + mdv2_escape(f"{q.get('q','')}  [{q.get('geo','')}]")
                )
    phrases = (seeds.get("web_search") or {}).get("seed_phrases") or []
    if phrases:
        lines.append("")
        lines.append("🌐 *" + mdv2_escape("Web-search seeds") + "*")
        for s in phrases[:6]:
            if isinstance(s, str):
                lines.append("• " + mdv2_escape(s))
    notes = (seeds.get("web_search") or {}).get("focus_notes") or ""
    if notes:
        lines.append("")
        lines.append("_" + mdv2_escape(notes) + "_")

    built = (p.get("built_from") or {})
    model = built.get("model")
    elapsed = built.get("elapsed_ms")
    if model:
        lines.append("")
        footer = f"built with {model}"
        if elapsed:
            footer += f" · {int(elapsed)}ms"
        lines.append("_" + mdv2_escape(footer) + "_")

    return "\n".join(lines)
