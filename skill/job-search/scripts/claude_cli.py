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
from typing import Any

log = logging.getLogger(__name__)


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
        return None
    cmd = ["claude", "-p", prompt, "--output-format", output_format]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        log.error("claude_cli: timed out after %ds", timeout_s)
        return None
    except Exception as e:
        log.error("claude_cli: failed to start: %s", e)
        return None
    if proc.returncode != 0:
        log.error("claude_cli: exit=%s err=%s", proc.returncode, (proc.stderr or "")[:200])
        return None
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
                env=dict(os.environ),
                cwd=cwd,
            )
        except subprocess.TimeoutExpired:
            log.error("claude_cli: run_p_with_tools timed out after %ds", timeout_s)
            return None
        except Exception as e:
            log.error("claude_cli: run_p_with_tools failed to start: %s", e)
            return None
        return proc.returncode, proc.stdout or "", proc.stderr or ""

    cmd = _build_cmd(with_tool_flags=True)
    result = _invoke(cmd)
    if result is None:
        return None
    rc, stdout, stderr = result
    if rc == 0:
        return stdout

    # Non-zero: decide whether to retry without the tool flags.
    err_head = stderr[:400]
    needs_fallback = (
        ("--allowed-tools" in err_head or "--disallowed-tools" in err_head)
        and "unrecognized" in err_head
        and (allowed_tools or disallowed_tools)
    )
    if not needs_fallback:
        log.warning(
            "claude_cli: run_p_with_tools exit=%s argv=%s err=%s",
            rc, _argv_summary(cmd), err_head[:200],
        )
        return None

    log.warning("run_p_with_tools: CLI rejected tool flags; retried without them")
    cmd2 = _build_cmd(with_tool_flags=False)
    result2 = _invoke(cmd2)
    if result2 is None:
        return None
    rc2, stdout2, stderr2 = result2
    if rc2 == 0:
        return stdout2
    log.warning(
        "claude_cli: run_p_with_tools fallback exit=%s argv=%s err=%s",
        rc2, _argv_summary(cmd2), (stderr2 or "")[:200],
    )
    return None


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
