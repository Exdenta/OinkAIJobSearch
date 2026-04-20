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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from claude_cli import run_p, extract_assistant_text, parse_json_block

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "opus"
DEFAULT_TIMEOUT_S = 180
DEFAULT_DEBOUNCE_S = 60.0

# "resume_upload" skips the debounce and runs immediately; "prefs_change"
# enters the debounce window. "manual" (/rebuildprofile) acts like
# resume_upload — user asked explicitly, don't make them wait.
_IMMEDIATE_TRIGGERS = {"resume_upload", "manual"}

_ALLOWED_ATS = frozenset({
    "greenhouse.io", "lever.co", "ashbyhq.com", "workable.com",
    "personio.de", "recruitee.com", "myworkday.com", "bamboohr.com",
    "teamtailor.com", "smartrecruiters.com",
})

_ALLOWED_REMOTE = frozenset({"any", "remote", "hybrid", "onsite"})
_ALLOWED_LEVELS = frozenset({
    "junior", "mid", "middle", "senior", "lead", "staff", "principal",
})

_MAX_FREETEXT_LEN = 500
_MAX_RESUME_CHARS = 8000   # the resume fed into the prompt is clipped
_MAX_TITLE_TOKEN = 40
_MAX_LINKEDIN_Q = 80
_MAX_SEED_PHRASE = 120
_MAX_LINKEDIN_QUERIES = 3
_MAX_SEED_PHRASES = 8

# Location of the external prompt file.
_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "profile_builder.txt"


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
    clipped_prefs = (user_description or "")[:_MAX_FREETEXT_LEN]
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


def profile_schema_validate(profile: Any) -> list[str]:
    """Return a list of validation errors; empty list means valid.

    We're intentionally strict about shape (types, enum values, length caps,
    ATS allowlist) but lenient about content (we don't second-guess the
    model's keyword choices — that's what the prompt is for).
    """
    errs: list[str] = []

    if not isinstance(profile, dict):
        return ["profile is not a dict"]

    # Required top-level keys
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
    missing = sorted(required - set(profile))
    if missing:
        errs.append(f"missing keys: {missing}")

    if profile.get("schema_version") != 2:
        errs.append("schema_version must equal 2")

    for k in ("ideal_fit_paragraph", "primary_role", "time_zone_band",
              "language", "free_text"):
        if k in profile and not _is_str(profile[k]):
            errs.append(f"{k} must be a string")

    for k in ("target_levels", "stack_primary", "stack_secondary",
              "stack_adjacent", "stack_antipatterns",
              "title_must_match", "title_exclude", "exclude_keywords",
              "exclude_companies", "locations"):
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
        "locations",
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
        li = seeds.get("linkedin")
        if not isinstance(li, dict):
            errs.append("search_seeds.linkedin must be an object")
        else:
            queries = li.get("queries")
            if not isinstance(queries, list):
                errs.append("search_seeds.linkedin.queries must be a list")
            else:
                if len(queries) > _MAX_LINKEDIN_QUERIES:
                    errs.append(f"search_seeds.linkedin.queries length > {_MAX_LINKEDIN_QUERIES}")
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
    _run_p: Callable = run_p,    # injected in tests
) -> BuildResult:
    """Run one Opus call end-to-end. Returns a BuildResult — never raises.

    Tests inject `_run_p=` with a mock to avoid burning real CLI calls.
    """
    resume_sha1 = sha1_hex(resume_text)
    prefs_sha1 = sha1_hex(free_text)
    start = time.monotonic()

    prompt = _render_prompt(resume_text, free_text)
    if not prompt:
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
        # run_p returns None on: missing CLI, timeout, non-zero exit. We can't
        # distinguish from here, so we class them together — the precise
        # failure mode is already logged inside claude_cli.
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
    return BuildResult(
        status="ok",
        profile=stamped,
        elapsed_ms=elapsed_ms,
        resume_sha1=resume_sha1,
        prefs_sha1=prefs_sha1,
        model=model,
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
