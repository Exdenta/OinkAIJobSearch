"""Drop-in wrappers around `claude_cli.run_p` / `run_p_with_tools`.

These do NOT modify `claude_cli` — they wrap it. Each wrapper times the
underlying call, infers a status from the return value, and writes a
`claude_calls` row via `MonitorStore.record_claude_call`.

Status inference (the wrapped functions never raise — they return None on
any failure):
  * non-empty stdout    -> 'ok'
  * empty stdout ('')   -> 'ok'   (CLI succeeded, model just produced nothing)
  * None return         -> 'cli_missing'

We pick 'cli_missing' over 'non_zero' for the None case because in practice
the dominant cause is a missing CLI in dev/CI environments; the `claude_cli`
helper logs the precise reason (timeout / non-zero / missing) at the source,
so the database label is a coarse bucket. Slice C surfaces the call count
alongside cost so volume anomalies are visible regardless.

Default store resolution: when `store=None` is passed (the common call-site
shape that doesn't have a MonitorStore handy), we lazily build one from the
canonical `state/jobs.db` path. This means every Claude CLI call across the
project is captured without forcing every module to thread a `store` arg
down its call chain.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_cli import run_p, run_p_with_tools

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


def _infer_status(stdout: str | None) -> str:
    """None -> 'cli_missing'; anything else -> 'ok'."""
    if stdout is None:
        return "cli_missing"
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
    started_at = time.time()
    stdout = run_p(prompt, model=model, **kwargs)
    finished_at = time.time()
    elapsed_ms = int((finished_at - started_at) * 1000)
    status = _infer_status(stdout)

    resolved = _resolve_store(store)
    if resolved is not None:
        try:
            resolved.record_claude_call(
                pipeline_run_id=pipeline_run_id,
                chat_id=chat_id,
                caller=caller,
                model=model,
                prompt_chars=len(prompt or ""),
                output_chars=len(stdout or ""),
                elapsed_ms=elapsed_ms,
                exit_code=None,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
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
    started_at = time.time()
    stdout = run_p_with_tools(prompt, model=model, **kwargs)
    finished_at = time.time()
    elapsed_ms = int((finished_at - started_at) * 1000)
    status = _infer_status(stdout)

    resolved = _resolve_store(store)
    if resolved is not None:
        try:
            resolved.record_claude_call(
                pipeline_run_id=pipeline_run_id,
                chat_id=chat_id,
                caller=caller,
                model=model,
                prompt_chars=len(prompt or ""),
                output_chars=len(stdout or ""),
                elapsed_ms=elapsed_ms,
                exit_code=None,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
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
