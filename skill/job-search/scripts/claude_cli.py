"""Helpers for invoking the `claude` CLI in non-interactive mode.

Centralized here so every AI-backed adapter (curated_boards, resume_tailor,
job_enrich) uses the same subprocess invocation, the same envelope-unwrap, and
the same defensive JSON parser.

Typical usage:

    from claude_cli import run_p, extract_assistant_text, parse_json_block

    stdout = run_p("Your prompt here", timeout_s=120)
    if stdout is None:
        # CLI missing or failed — caller should fall back gracefully.
        return None
    body = extract_assistant_text(stdout)
    data = parse_json_block(body)
    if data is None:
        # Output wasn't parseable JSON.
        return None
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured failure channel.
#
# `run_p` / `run_p_with_tools` return a bare `str | None` — `None` on ANY
# failure. That collapses five distinct causes (binary absent, timeout,
# subprocess start error, non-zero exit with an empty envelope, non-zero exit
# whose stdout envelope actually carried an API error like a rate limit) into
# one indistinguishable value. Downstream, `instrumentation.wrappers` then
# labelled every `None` as `cli_missing`, so the DB read "CLI missing" even
# when the binary was present and the real cause was an API error mid-run.
#
# We keep the `str | None` return contract (raw callers are unchanged) and
# publish the *reason* for the most recent failure out-of-band via a
# thread-local. The instrumented wrapper pops it right after a `None` return to
# record an honest status + exit_code. Thread-local (not a module global) so
# parallel liveness verifiers don't clobber each other's reason.
_last_failure = threading.local()

# Reason tags (also the DB `status` values the wrapper maps them to):
#   cli_absent   — `claude` not on PATH (the ONLY true "missing CLI" case)
#   timeout      — subprocess.TimeoutExpired
#   start_error  — subprocess failed to start (OSError etc.)
#   nonzero_exit — exit != 0 with no parseable error envelope on stdout
#   api_error    — exit != 0 but stdout envelope carried is_error/api_error_status
#                  (rate limit, overloaded, usage limit — the real culprit)


def _set_failure(
    reason: str,
    *,
    exit_code: int | None = None,
    stderr_head: str = "",
    stdout_head: str = "",
    api_error_status: str | None = None,
) -> None:
    _last_failure.detail = {
        "reason": reason,
        "exit_code": exit_code,
        "stderr_head": stderr_head,
        "stdout_head": stdout_head,
        "api_error_status": api_error_status,
    }


def pop_last_failure() -> dict[str, Any] | None:
    """Return + clear the structured detail of the most recent CLI failure on
    this thread, or None if the last call succeeded / nothing ran. Consumed by
    `instrumentation.wrappers` to label the `claude_calls` row honestly."""
    detail = getattr(_last_failure, "detail", None)
    _last_failure.detail = None
    return detail


def _classify_nonzero(exit_code: int, stdout: str, stderr: str) -> None:
    """Record a non-zero-exit failure, mining the (otherwise discarded) stdout
    JSON envelope for the real cause. `claude -p --output-format json` prints
    its result/error envelope to stdout and often leaves stderr EMPTY, so
    logging only stderr produced the useless `err=` blank lines in prod."""
    stdout_head = (stdout or "")[:600]
    api_error_status = None
    is_error = False
    result_head = ""
    try:
        env = json.loads(stdout) if stdout.strip() else {}
        if isinstance(env, dict):
            api_error_status = env.get("api_error_status")
            is_error = bool(env.get("is_error"))
            result_head = str(env.get("result") or "")[:200]
    except (json.JSONDecodeError, ValueError):
        pass
    reason = "api_error" if (api_error_status or is_error) else "nonzero_exit"
    _set_failure(
        reason,
        exit_code=exit_code,
        stderr_head=(stderr or "")[:200],
        stdout_head=stdout_head,
        api_error_status=api_error_status,
    )
    # Surface the REAL diagnostic: envelope api_error_status + result text, not
    # the (usually empty) stderr that made prod logs read `err=`.
    log.warning(
        "claude_cli: exit=%s api_error_status=%s is_error=%s result=%r stderr=%r",
        exit_code, api_error_status, is_error, result_head, (stderr or "")[:200],
    )


# The smallest / cheapest Claude model we use for high-volume matching calls
# (e.g. scoring every job posting against every user on every run). The
# `claude` CLI accepts short aliases, so passing "haiku" lets the CLI map it
# to whichever Haiku generation is current at runtime — avoids hardcoding a
# version string that ages out.
#
# Override with env var CLAUDE_SMALLEST_MODEL if you ever need to point at a
# specific Haiku build or promote to Sonnet for an A/B.
SMALLEST_MODEL = os.environ.get("CLAUDE_SMALLEST_MODEL", "haiku")

# Mid-tier model used for the second pass of the two-pass enrichment flow:
# Haiku triages every posting cheaply, then Sonnet re-scores survivors with
# the same prompt. Sonnet is better at implicit constraints (language,
# location nuance, years-experience math) where Haiku is noisy.
MID_MODEL = os.environ.get("CLAUDE_MID_MODEL", "sonnet")


# ---------------------------------------------------------------------------
# Canonical tool-grant strings for `run_p_with_tools(allowed_tools=...,
# disallowed_tools=...)`.
#
# Format: comma-separated tool names. The `claude` CLI's `--allowed-tools`
# and `--disallowed-tools` flags accept BOTH comma- and space-separated
# input (the flag is declared `<tools...>`), so either works, but we
# standardize on commas project-wide so a future grep / audit only has to
# match one form. The two outliers that previously used space-separated
# strings (`sources/ub_doctoral.py`, `telegram_client._web_search_listing_
# still_open`) have been migrated to use these constants.
#
# WHY DENY `Bash,Edit,Write,Read`:
#   Every AI sub-agent we spawn with WebFetch/WebSearch is, by definition,
#   pulling untrusted third-party text (job postings, recruiter blog
#   posts, ATS pages) into its context window. A malicious page can
#   include prompt-injection payloads that try to coerce the agent into
#   side effects — exfiltrating files via `Read`, writing payloads with
#   `Edit`/`Write`, or executing commands with `Bash`. We deny those four
#   capabilities even when the sub-agent has no legitimate need for them,
#   as a belt-and-suspenders defense: the agent could only ever invoke
#   them via injection, so blocking them is pure upside.
#
#   The four-tool deny list (`Bash,Edit,Write,Read`) is the project-wide
#   convention because those are the keystone capabilities — shell
#   execution + filesystem read/write. Other tools that exist on the CLI
#   (Grep, Glob, NotebookEdit, Task, etc.) either can't directly cause
#   exfiltration/execution (Grep/Glob are search-only) or aren't
#   reachable from the prompts we ship. If new high-risk tools land
#   upstream, extend `TOOLS_DENY_SHELL_FS` here in one place.
#
# Usage example:
#
#     from claude_cli import (
#         run_p_with_tools,
#         TOOLS_WEB_BOTH,
#         TOOLS_DENY_SHELL_FS,
#     )
#     stdout = run_p_with_tools(
#         prompt,
#         allowed_tools=TOOLS_WEB_BOTH,
#         disallowed_tools=TOOLS_DENY_SHELL_FS,
#         ...
#     )

#: Allow both web tools (the WebSearch+WebFetch pair every job-discovery
#: sub-agent needs).
TOOLS_WEB_BOTH = "WebSearch,WebFetch"

#: Allow ONLY WebFetch (no search). Used by callers that load a known URL
#: directly (no discovery step) — currently ``sources/un_careers.py`` and
#: ``sources/curated_boards.py``, both of which point Claude at a specific
#: landing page and parse what it returns. Keeping the allow list minimal
#: narrows the prompt-injection attack surface.
TOOLS_WEB_FETCH_ONLY = "WebFetch"

#: Deny shell + filesystem read/write. Use as the canonical
#: `disallowed_tools` for any caller that grants web tools to an agent
#: processing third-party text (prompt-injection defense).
TOOLS_DENY_SHELL_FS = "Bash,Edit,Write,Read"

#: Deny ALL high-risk capabilities — including the web tools themselves.
#: Use for "synthesizer" / "manager" agents that should reason over
#: already-fetched fixtures and never call out to the network or the
#: filesystem. Equivalent to `TOOLS_WEB_BOTH + "," + TOOLS_DENY_SHELL_FS`
#: but spelled out so the wire format is grep-able.
TOOLS_DENY_WEB_AND_SHELL_FS = "WebSearch,WebFetch,Bash,Edit,Write,Read"


#: Env vars the `claude` CLI honours as an auth source *in preference to* the
#: subscription OAuth token in ~/.claude/.credentials.json. We authenticate as a
#: subscription, so any of these shadow the login and 401 every call. Verified
#: against the CLI on prod 2026-07-09: a bad ANTHROPIC_AUTH_TOKEN yields "401
#: Invalid bearer token", and the CLI itself warns that ANTHROPIC_API_KEY "takes
#: precedence over your claude.ai login". (CLAUDE_CODE_OAUTH_TOKEN is ignored by
#: the CLI, so it is not worth stripping.)
_SHADOWING_AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _subscription_env() -> dict[str, str]:
    """The process env minus any API-key auth source.

    Every `claude` invocation in this codebase must authenticate as the
    subscription, so this is unconditional rather than a per-call flag. It used
    to be an opt-in `subscription_auth_only=True` that only `prewarm_token`
    passed — which meant the health check stripped the key and reported `ok`
    while the enrich/liveness fan-out inherited it and 401'd. Strip once, here,
    where every call site routes through.
    """
    return {k: v for k, v in os.environ.items() if k not in _SHADOWING_AUTH_VARS}


def run_p(
    prompt: str,
    timeout_s: int = 180,
    output_format: str = "json",
    model: str | None = None,
) -> str | None:
    """Invoke `claude -p <prompt> --output-format <fmt>` and return stdout.

    Returns None on any failure (missing CLI, timeout, non-zero exit). The caller
    is expected to degrade gracefully — e.g. return empty results, or fall back
    to a heuristic.

    `model` maps to `--model <alias>`. Pass e.g. "opus" or "sonnet" to force a
    specific model; leave unset to let the CLI pick its default (typically the
    cheap/fast tier). The profile_builder uses "opus" because the per-user
    profile rewrite is a high-leverage, low-volume call.
    """
    if not shutil.which("claude"):
        log.warning("claude_cli: `claude` CLI not found on PATH")
        _set_failure("cli_absent")
        return None
    cmd = ["claude", "-p", prompt, "--output-format", output_format]
    if model:
        cmd += ["--model", model]
    env = _subscription_env()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        log.error("claude_cli: timed out after %ds", timeout_s)
        _set_failure("timeout", stderr_head=f"timeout after {timeout_s}s")
        return None
    except Exception as e:
        log.error("claude_cli: failed to start: %s", e)
        _set_failure("start_error", stderr_head=str(e)[:200])
        return None
    if proc.returncode != 0:
        _classify_nonzero(proc.returncode, proc.stdout or "", proc.stderr or "")
        return None
    _last_failure.detail = None
    return proc.stdout or ""


def run_p_with_tools(
    prompt: str,
    *,
    allowed_tools: str = "",
    disallowed_tools: str = "",
    model: str | None = None,
    timeout_s: int = 300,
    output_format: str = "json",
    cwd: str | None = None,
    extra_args: list[str] | None = None,
) -> str | None:
    """Invoke `claude -p <prompt>` with optional `--allowed-tools` / `--disallowed-tools`
    wiring for subagents that need WebSearch+WebFetch.

    Semantics match `run_p` (returns None on any failure, never raises). The primary
    differences: accepts explicit tool allow/deny strings, exposes a `cwd`, and
    tolerates older `claude` builds that don't know the tool flags — in that case
    it retries ONCE with the flags stripped and logs the fallback.
    """
    if not shutil.which("claude"):
        log.warning("claude_cli: `claude` CLI not found on PATH")
        _set_failure("cli_absent")
        return None

    def _build_cmd(with_tool_flags: bool) -> list[str]:
        cmd = ["claude", "-p", prompt, "--output-format", output_format]
        if model:
            cmd += ["--model", model]
        if with_tool_flags and allowed_tools:
            cmd += ["--allowed-tools", allowed_tools]
        if with_tool_flags and disallowed_tools:
            cmd += ["--disallowed-tools", disallowed_tools]
        if extra_args:
            cmd += list(extra_args)
        return cmd

    def _argv_summary(cmd: list[str]) -> str:
        # Drop the big prompt blob from any logged argv — too noisy.
        safe = []
        skip_next = False
        for i, tok in enumerate(cmd):
            if skip_next:
                safe.append("<prompt>")
                skip_next = False
                continue
            if tok == "-p":
                safe.append(tok)
                skip_next = True
                continue
            safe.append(tok)
        return " ".join(safe)

    def _invoke(cmd: list[str]) -> tuple[int, str, str] | None:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                env=_subscription_env(),
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            log.error("claude_cli: run_p_with_tools timed out after %ds", timeout_s)
            _set_failure("timeout", stderr_head=f"timeout after {timeout_s}s")
            return None
        except Exception as e:
            log.error("claude_cli: run_p_with_tools failed to start: %s", e)
            _set_failure("start_error", stderr_head=str(e)[:200])
            return None
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    cmd = _build_cmd(with_tool_flags=True)
    result = _invoke(cmd)
    if result is None:
        return None  # _invoke already set the failure detail (timeout/start_error)
    rc, stdout, stderr = result
    if rc == 0:
        _last_failure.detail = None
        return stdout

    # Non-zero: decide whether to retry without the tool flags.
    err_head = stderr[:400]
    needs_fallback = (
        ("--allowed-tools" in err_head or "--disallowed-tools" in err_head)
        and "unrecognized" in err_head
        and (allowed_tools or disallowed_tools)
    )
    if not needs_fallback:
        # Mine the stdout envelope for the real cause (api_error_status) — the
        # dominant prod failure is exit=1 with EMPTY stderr and the error
        # hiding in the JSON envelope we used to discard.
        _classify_nonzero(rc, stdout, stderr)
        log.warning(
            "claude_cli: run_p_with_tools exit=%s argv=%s", rc, _argv_summary(cmd),
        )
        return None

    log.warning("run_p_with_tools: CLI rejected tool flags; retried without them")
    cmd2 = _build_cmd(with_tool_flags=False)
    result2 = _invoke(cmd2)
    if result2 is None:
        return None  # _invoke set the failure detail
    rc2, stdout2, stderr2 = result2
    if rc2 == 0:
        _last_failure.detail = None
        return stdout2
    _classify_nonzero(rc2, stdout2, stderr2)
    log.warning(
        "claude_cli: run_p_with_tools fallback exit=%s argv=%s", rc2, _argv_summary(cmd2),
    )
    return None


def prewarm_token(model: str | None = None, timeout_s: int = 60) -> tuple[str, str]:
    """Make ONE serial `claude -p` call to refresh the OAuth access token
    before the parallel enrich/liveness fan-out, and classify the outcome.

    Returns ``(category, detail)`` where category is one of:
      * ``ok``        — call succeeded; the OAuth access token is now warm.
      * ``auth``      — 401/403 / "Invalid authentication" — creds broken.
      * ``quota``     — usage limit / credit balance exhausted (subscription).
      * ``transient`` — timeout / 429 / overloaded / network — do NOT alert.
      * ``cli_absent``— the `claude` binary is not on PATH.

    WHY THIS EXISTS — the refresh-token rotation race:
      The subscription auth is an 8h OAuth *access* token in
      ``~/.claude/.credentials.json``, refreshed via a single-use (rotating)
      *refresh* token. ``job_enrich.enrich_jobs_ai`` fans out ``workers`` (4)
      concurrent ``claude -p`` subprocesses. When the access token has expired,
      all N workers try to redeem the SAME refresh token at once — the auth
      server accepts the first and 401s the rest, and because the redeemed
      token rotated, the losers can't retry cleanly either. The token never
      settles, so every run 401-storms until a lone serial call refreshes it
      (observed in prod 2026-07-05, 07:00–20:00: job_enrich:haiku 76% fail).
      Calling this ONCE, serially, up front performs that single clean refresh
      so the downstream pool inherits a valid token.
    """
    def _attempt() -> tuple[str, str]:
        out = run_p(
            "Reply with: ok",
            timeout_s=timeout_s,
            model=model or SMALLEST_MODEL,
        )
        if out is not None:
            return ("ok", "")
        detail = pop_last_failure() or {}
        reason = str(detail.get("reason") or "error")
        status_code = str(detail.get("api_error_status") or "")
        blob = f"{detail.get('stdout_head', '')} {detail.get('stderr_head', '')}".lower()
        if reason == "cli_absent":
            return ("cli_absent", "claude CLI not on PATH")
        if (
            status_code.startswith("401")
            or status_code.startswith("403")
            or "invalid authentication" in blob
            or "authenticate" in blob
            or "oauth" in blob
        ):
            return ("auth", f"api_error_status={status_code or '?'}")
        if (
            "usage limit" in blob
            or "credit balance" in blob
            or "quota" in blob
            or "insufficient" in blob
        ):
            return ("quota", f"{status_code or '-'} {blob[:100]}".strip())
        # 429 / overloaded / 5xx / timeout / nonzero — transient, not a
        # subscription fault. Surface but do not page the operator.
        return ("transient", f"reason={reason} status={status_code or '-'}")

    # The FIRST cold call of an idle searcher cycle triggers an OAuth
    # access-token refresh against the rotating refresh-token endpoint, which
    # intermittently returns a transient 401. That single 401 is not broken
    # creds — the very next call inherits the settled token and succeeds (in
    # prod the enrich/liveness fan-out right after a prewarm 401 runs at 0%
    # auth failure). Retry once so a refresh-race 401 clears silently; a truly
    # broken/expired subscription 401s on BOTH attempts and still pages.
    cat, detail = _attempt()
    if cat == "auth":
        cat, detail = _attempt()
    return (cat, detail)


def extract_assistant_text(cli_stdout: str) -> str:
    """`claude -p --output-format json` emits {"result": "<text>", ...}.

    Unwrap that envelope and return the inner assistant text. If the envelope
    can't be parsed we return the raw stdout, which covers the case where a
    caller used `--output-format text` or stripped the envelope already.

    Semantics around an empty `result`:
      * Standard Claude-CLI envelopes always carry a `result` key. When that
        key is present we return its literal value — including the empty
        string — instead of falling through to the raw envelope text. The
        old behavior conflated "model produced nothing" with "model
        produced an opaque envelope", which made it impossible for
        telemetry to count silent-empty replies separately. See the
        `_is_empty_result_envelope` helper in job_enrich.py for the
        upstream signal that this change now makes consistent — and the
        `result_chars` column in `claude_calls` for the downstream signal
        operators query.
      * Older / hypothetical envelopes that lack a `result` key still fall
        through to `content` / `text` / `message`, then to the raw stdout
        — preserving the prior fallback chain for non-CLI inputs.
    """
    s = (cli_stdout or "").strip()
    if not s:
        return ""
    try:
        envelope = json.loads(s)
    except json.JSONDecodeError:
        return s
    if isinstance(envelope, dict):
        # The CLI's canonical envelope key. When it is present (even as ""),
        # honor it literally so callers can distinguish "model emitted
        # nothing" from "model emitted something we couldn't parse".
        if "result" in envelope and isinstance(envelope["result"], str):
            return envelope["result"]
        # Legacy / defensive fallback: pre-existing callers relied on these
        # keys when the envelope shape didn't match the current CLI.
        for key in ("content", "text", "message"):
            val = envelope.get(key)
            if isinstance(val, str) and val.strip():
                return val
    return s


def parse_json_block(text: str) -> Any | None:
    """Parse a JSON object (or array) from `text`, tolerant of:
      - leading/trailing whitespace
      - wrapping code fences (```json ... ```)
      - leading narration before the JSON

    Returns the parsed value (dict or list) or None if nothing could be parsed.
    """
    s = (text or "").strip()
    if not s:
        return None
    # Strip fenced markdown if present.
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # Slice from first '{' or '[' to the last '}' or ']' as a last-ditch effort.
    candidates = [
        (s.find("{"), s.rfind("}")),
        (s.find("["), s.rfind("]")),
    ]
    for i, j in candidates:
        if i != -1 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None
