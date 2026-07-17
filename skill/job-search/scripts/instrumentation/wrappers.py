"""Drop-in wrappers around `claude_cli.run_p` / `run_p_with_tools`.

These do NOT modify `claude_cli` — they wrap it. Each wrapper times the
underlying call, infers a status from the return value, and writes a
`claude_calls` row via `MonitorStore.record_claude_call`.

Status inference (the wrapped functions never raise — they return None on
any failure):
  * stdout is None                              -> 'cli_missing'
  * stdout is non-empty AND parsed result == "" -> 'empty_result'
    (CLI envelope was well-formed JSON but the model emitted nothing —
    a distinct, silent failure mode that wedges Haiku batches; see
    `_BATCH_EMPTY_RESULT` in job_enrich.py for the upstream signal.)
  * anything else                               -> 'ok'

We pick 'cli_missing' over 'non_zero' for the None case because in practice
the dominant cause is a missing CLI in dev/CI environments; the `claude_cli`
helper logs the precise reason (timeout / non-zero / missing) at the source,
so the database label is a coarse bucket. Slice C surfaces the call count
alongside cost so volume anomalies are visible regardless.

Telemetry split — `output_chars` vs `result_chars`:
  * `output_chars` is `len(stdout)` — the wire-level subprocess stdout
    (the entire JSON envelope when `--output-format json` was used).
  * `result_chars` is `len(extract_assistant_text(stdout))` — the model's
    parsed assistant text. When the envelope is well-formed but `result`
    is the empty string, this is 0 even though `output_chars > 0`.
The two columns combined are the smoking gun for "model silently failed":
ops/commands.py and the digest can flag windows where they diverge.

Default store resolution: when `store=None` is passed (the common call-site
shape that doesn't have a MonitorStore handy), we lazily build one from the
canonical `state/jobs.db` path. This means every Claude CLI call across the
project is captured without forcing every module to thread a `store` arg
down its call chain.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_cli import extract_assistant_text, pop_last_failure, run_p, run_p_with_tools

if TYPE_CHECKING:
    # Telemetry resolves at integration time; keep the runtime import lazy
    # so this module remains importable even if slice A hasn't merged yet
    # (wrappers themselves are only called at integration sites which depend
    # on a real `MonitorStore`).
    from telemetry import MonitorStore


_DEFAULT_STORE: "MonitorStore | None" = None

def _resolve_store(store: "MonitorStore | None") -> "MonitorStore | None":
    """Return the explicit store, or the lazy default built from
    `STATE_DIR/jobs.db`. Returns None on any failure so the wrapped call
    still proceeds — telemetry is never load-bearing for the user's request.
    """
    if store is not None:
        return store
    global _DEFAULT_STORE
    if _DEFAULT_STORE is not None:
        return _DEFAULT_STORE
    try:
        from db import DB
        from telemetry import MonitorStore as _MonitorStore
        # Project root: this file is at <root>/skill/job-search/scripts/instrumentation/wrappers.py
        project_root = Path(__file__).resolve().parents[4]
        state_dir = project_root / os.environ.get("STATE_DIR", "state")
        db = DB(state_dir / "jobs.db")
        _DEFAULT_STORE = _MonitorStore(db)
        return _DEFAULT_STORE
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "wrappers: failed to build default MonitorStore — telemetry off",
        )
        return None


def _safe_result_chars(stdout: str | None) -> int:
    """Length of the parsed assistant text from `stdout`, defensively.

    Calls `claude_cli.extract_assistant_text`, which unwraps the CLI
    envelope and returns the literal `result` field (including ""). Any
    exception here — corrupt unicode, future envelope format changes —
    falls back to 0 rather than crashing the wrapper. Telemetry is
    never load-bearing.
    """
    if stdout is None:
        return 0
    try:
        result_text = extract_assistant_text(stdout)
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "wrappers: extract_assistant_text raised — recording result_chars=0",
        )
        return 0
    return len(result_text or "")


def _safe_usage_from_envelope(stdout: str | None) -> dict[str, Any]:
    """Pull the real usage/cost numbers out of the CLI's JSON envelope.

    `claude -p --output-format json` returns the TRUTH alongside the
    `result` text:

        {"result": "...",
         "total_cost_usd": 0.0123,
         "num_turns": 1,
         "usage": {"input_tokens": 1200, "output_tokens": 340,
                   "cache_creation_input_tokens": 0,
                   "cache_read_input_tokens": 980}}

    We turn that into the kwargs `record_claude_call` expects. Every field
    is optional and defaults to None — the wrappers' `--output-format json`
    default means the envelope is normally present, but a caller that asked
    for `--output-format text`, an older CLI, or a corrupt line must
    degrade to None (the surrogate then remains the only cost signal).

    `total_cost_usd` is dollars → converted to int micro-USD to match the
    `cost_estimate_us` unit. Token counts pass through as ints. Anything
    non-numeric / missing → None, never an exception. Telemetry is never
    load-bearing for the user-facing call.

    Returns a dict with keys:
        cost_actual_us, input_tokens, output_tokens,
        cache_read_tokens, cache_creation_tokens, num_turns
    """
    out: dict[str, Any] = {
        "cost_actual_us": None,
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
        "num_turns": None,
    }
    if not stdout:
        return out
    try:
        import json

        s = stdout.strip()
        if not s or s[0] not in "{[":
            # Not a JSON envelope (e.g. --output-format text). No usage to
            # lift; skip the parse entirely.
            return out
        envelope = json.loads(s)
        if not isinstance(envelope, dict):
            return out

        def _opt_int(v: Any) -> int | None:
            # Accept int / float / numeric-str; reject bool (a stray True
            # would otherwise coerce to 1) and anything non-numeric.
            if v is None or isinstance(v, bool):
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        cost_usd = envelope.get("total_cost_usd")
        if isinstance(cost_usd, (int, float)) and not isinstance(cost_usd, bool):
            # dollars → micro-USD (same unit as cost_estimate_us).
            out["cost_actual_us"] = int(round(float(cost_usd) * 1_000_000))

        out["num_turns"] = _opt_int(envelope.get("num_turns"))

        usage = envelope.get("usage")
        if isinstance(usage, dict):
            out["input_tokens"] = _opt_int(usage.get("input_tokens"))
            out["output_tokens"] = _opt_int(usage.get("output_tokens"))
            # The CLI names the cache fields *_input_tokens; we store them
            # under the shorter column names.
            out["cache_read_tokens"] = _opt_int(
                usage.get("cache_read_input_tokens")
            )
            out["cache_creation_tokens"] = _opt_int(
                usage.get("cache_creation_input_tokens")
            )
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "wrappers: failed to parse usage/cost from envelope — "
            "recording surrogate only",
        )
        # Reset to all-None so a half-parsed envelope never persists a
        # mix of real and bogus numbers.
        return {k: None for k in out}
    return out


# `claude_cli` publishes the reason for the most recent failure on a
# thread-local; these are the reasons it emits (see `_set_failure` there).
# We map each to the DB `status` value 1:1 so the coarse historical
# `cli_missing` bucket splits into honest causes. `cli_absent` is the ONLY
# reason that still means "the binary was missing".
_FAILURE_REASON_STATUS = frozenset({
    "cli_absent", "timeout", "start_error", "nonzero_exit", "api_error",
})


def _infer_status(
    stdout: str | None,
    output_chars: int,
    result_chars: int,
    failure: dict[str, Any] | None = None,
) -> str:
    """Bucket a Claude CLI invocation into a status enum.

    * `stdout is None` → the honest failure reason from `claude_cli`'s
      thread-local (`cli_absent` / `timeout` / `start_error` / `nonzero_exit`
      / `api_error`). Historically this was the catch-all `cli_missing`, which
      lied whenever the binary was present and the real cause was a timeout or
      a mid-run API error (rate limit / overloaded). We fall back to
      `cli_missing` only when the reason is unavailable (raw caller bypassed
      the wrapper's pop, or an older `claude_cli` without the channel).
    * `output_chars > 0` AND `result_chars == 0` → 'empty_result'. The CLI
      returned a well-formed envelope (we got bytes on stdout) but the
      parsed `result` field was the empty string — the model emitted
      nothing. This is the silent-failure mode that used to be invisible
      because both this and the None case landed as `output_chars == 0`.
      Now `output_chars > 0` proves the subprocess ran, and `result_chars ==
      0` isolates the model-side failure.
    * Anything else → 'ok'.
    """
    if stdout is None:
        reason = (failure or {}).get("reason")
        if reason in _FAILURE_REASON_STATUS:
            return reason
        return "cli_missing"
    if output_chars > 0 and result_chars == 0:
        return "empty_result"
    return "ok"


def _forensic_log_call(
    *,
    op: str,
    caller: str,
    prompt: str,
    stdout: str | None,
    model: str | None,
    chat_id: int | None,
    pipeline_run_id: int | None,
    elapsed_ms: int,
    status: str,
    extra_kwargs: dict[str, Any],
) -> None:
    """Append a forensic JSONL line for this Claude CLI call.

    Captures the prompt head + stdout head so post-hoc analysis can answer
    "what exactly was sent to the model and what did it return for chat X
    on day Y." Heads only — full text would balloon the log; the truncation
    in forensic.py handles the rest. Never raises.
    """
    try:
        from forensic import log_step
        # Only carry common kwargs forward; arbitrary opaque kwargs from the
        # CLI helper (timeout_s, allowed_tools) are useful too, so include
        # them as 'extra'.
        log_step(
            op,
            input={
                "caller": caller,
                "model": model,
                "prompt_chars": len(prompt or ""),
                "prompt_head": (prompt or "")[:1500],
                "kwargs": {
                    k: v for k, v in extra_kwargs.items()
                    if k in ("timeout_s", "allowed_tools", "disallowed_tools",
                             "output_format", "max_turns", "model")
                },
            },
            output={
                "status": status,
                "output_chars": len(stdout or ""),
                "stdout_head": (stdout or "")[:1500] if stdout is not None else None,
            },
            chat_id=chat_id,
            run_id=pipeline_run_id,
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception(
            "wrappers: forensic log failed for caller=%s", caller,
        )


def _resolve_route(caller: str, model: str | None) -> tuple[str, str | None]:
    """Resolve (provider, model) for this caller. Single-provider build:
    always the Claude CLI."""
    return "claude", model


def _dispatch_text(
    caller: str,
    prompt: str,
    *,
    provider: str = "claude",
    model: str | None = None,
    **kwargs: Any,
) -> str | None:
    """Run a single-shot text prompt through the chosen provider, returning
    the same CLI-style envelope string regardless.

    `provider` and `model` are already resolved by `_resolve_route`.
    Single-provider build: always `claude_cli.run_p` (subscription CLI).
    """
    # Provider-neutral knob accepted for call-site compatibility;
    # `claude_cli.run_p` has no such parameter, so pop it rather than
    # leaking a TypeError into the call.
    kwargs.pop("json_mode", None)
    return run_p(prompt, model=model, **kwargs)


def _resolve_tools_route(caller: str, model: str | None) -> tuple[str, str | None]:
    """Provider route for tool-style calls. Single-provider build: always
    the Claude CLI."""
    return "claude", model


def _tag_value(prompt: str, tag: str) -> str:
    m = re.search(rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>", prompt or "", re.DOTALL)
    return " ".join(m.group(1).split()) if m else ""


def _dispatch_tools(
    caller: str,
    prompt: str,
    *,
    provider: str,
    model: str | None,
    fallback_model: str | None,
    **kwargs: Any,
) -> tuple[str | None, str | None, str]:
    return run_p_with_tools(prompt, model=fallback_model, **kwargs), fallback_model, "claude_cli"


def wrapped_run_p(
    store: "MonitorStore | None",
    caller: str,
    prompt: str,
    *,
    pipeline_run_id: int | None = None,
    chat_id: int | None = None,
    model: str | None = None,
    **kwargs: Any,
) -> str | None:
    """Time + record a `claude_cli.run_p` invocation. Same return contract.

    Two side-effects on top of the underlying call: (1) `claude_calls` row
    via MonitorStore (counters), (2) one JSONL line in forensic_logs (full
    prompt head + stdout head for post-hoc analysis). Both are best-effort
    and never break the call path.
    """
    # Per-stage routing: pick provider + (possibly overridden) model. The
    # resolved model is what we ACTUALLY call and what telemetry records, so
    # /stats reflects reality when a stage is re-pointed.
    provider, model = _resolve_route(caller, model)
    started_at = time.time()
    stdout = _dispatch_text(caller, prompt, provider=provider, model=model, **kwargs)
    finished_at = time.time()
    # Pop the structured failure reason claude_cli published on this thread
    # (always pop to clear it; only meaningful when stdout is None and the
    # claude path ran — a Mistral success leaves it stale-but-ignored).
    failure = pop_last_failure()
    elapsed_ms = int((finished_at - started_at) * 1000)
    # Parse the envelope BEFORE recording so the status inference and
    # `result_chars` column see the same view of the output. Wrapped in
    # `_safe_result_chars` so a parser exception cannot blow up
    # telemetry — the underlying user-facing call already returned.
    output_chars = len(stdout or "")
    result_chars = _safe_result_chars(stdout)
    status = _infer_status(stdout, output_chars, result_chars, failure)
    exit_code = failure.get("exit_code") if (stdout is None and failure) else None
    # Real token counts + actual cost from the CLI envelope when present;
    # all-None on a text/empty/corrupt envelope (surrogate stays the
    # fallback). Never raises.
    usage = _safe_usage_from_envelope(stdout)

    resolved = _resolve_store(store)
    if resolved is not None:
        try:
            resolved.record_claude_call(
                pipeline_run_id=pipeline_run_id,
                chat_id=chat_id,
                caller=caller,
                model=model,
                prompt_chars=len(prompt or ""),
                output_chars=output_chars,
                result_chars=result_chars,
                elapsed_ms=elapsed_ms,
                exit_code=exit_code,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                **usage,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "wrapped_run_p: failed to record caller=%s", caller,
            )

    _forensic_log_call(
        op="claude_cli.run_p",
        caller=caller,
        prompt=prompt,
        stdout=stdout,
        model=model,
        chat_id=chat_id,
        pipeline_run_id=pipeline_run_id,
        elapsed_ms=elapsed_ms,
        status=status,
        extra_kwargs=kwargs,
    )
    return stdout


def wrapped_run_p_with_tools(
    store: "MonitorStore | None",
    caller: str,
    prompt: str,
    *,
    pipeline_run_id: int | None = None,
    chat_id: int | None = None,
    model: str | None = None,
    **kwargs: Any,
) -> str | None:
    """Time + record a `claude_cli.run_p_with_tools` invocation."""
    provider, routed_model = _resolve_tools_route(caller, model)
    started_at = time.time()
    stdout, model, actual_provider = _dispatch_tools(
        caller,
        prompt,
        provider=provider,
        model=routed_model,
        fallback_model=model,
        **kwargs,
    )
    finished_at = time.time()
    failure = pop_last_failure()
    elapsed_ms = int((finished_at - started_at) * 1000)
    # Same envelope-parsing dance as wrapped_run_p — see its comment.
    output_chars = len(stdout or "")
    result_chars = _safe_result_chars(stdout)
    status = _infer_status(stdout, output_chars, result_chars, failure)
    exit_code = failure.get("exit_code") if (stdout is None and failure) else None
    usage = _safe_usage_from_envelope(stdout)

    resolved = _resolve_store(store)
    if resolved is not None:
        try:
            resolved.record_claude_call(
                pipeline_run_id=pipeline_run_id,
                chat_id=chat_id,
                caller=caller,
                model=model,
                prompt_chars=len(prompt or ""),
                output_chars=output_chars,
                result_chars=result_chars,
                elapsed_ms=elapsed_ms,
                exit_code=exit_code,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                **usage,
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "wrapped_run_p_with_tools: failed to record caller=%s", caller,
            )

    _forensic_log_call(
        op="claude_cli.run_p_with_tools",
        caller=caller,
        prompt=prompt,
        stdout=stdout,
        model=model,
        chat_id=chat_id,
        pipeline_run_id=pipeline_run_id,
        elapsed_ms=elapsed_ms,
        status=status,
        extra_kwargs=kwargs,
    )
    return stdout
