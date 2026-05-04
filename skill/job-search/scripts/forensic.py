"""Per-step forensic logger.

Goal: post-hoc analysis of every operation in the pipeline. Each step
records its inputs, outputs, errors, and intermediate state to disk as a
single JSON line. Files rotate by size so the disk footprint is bounded
and old runs can be inspected with `jq` / `grep` / `sqlite3` (one-line
JSON loads cleanly into pandas / DuckDB too).

WHY THIS EXISTS
---------------
The existing `claude_calls` / `pipeline_runs` / `source_runs` tables
record COUNTERS — number of calls, elapsed_ms, status. They don't record
the prompt that was sent, the score Claude returned for a specific job,
or the input that triggered a false-positive match. Forensic logs answer:

  * "Why did Claude score job X as a 5/5 fit when Aleksandr clearly isn't
    a Vue developer?"  →  grep that job_id, see the exact prompt + Claude's
    JSON verdict.
  * "Why did the LinkedIn scraper return 0 results today?"  →  open the
    `linkedin.fetch_for_user` line, see the queries that were run + the
    HTTP status / response head.
  * "Why did the safety_check let an injection through?"  →  read the
    safety_check line, see the candidate text + the model verdict.

USAGE
-----
Two interfaces:

1. Function: `log_step(op, **fields)` — fire-and-forget single line.

       forensic.log_step(
           "linkedin.fetch_for_user",
           input={"queries": queries, "chat_id": chat_id},
           output={"raw_count": len(jobs)},
           chat_id=chat_id,
       )

2. Context manager: `step(op, *, input=None, chat_id=None, run_id=None)` —
   automatically captures elapsed_ms + output + error. Use when you want
   exception capture too.

       with forensic.step("safety_check", input={"text": text[:200]},
                          chat_id=chat_id) as ctx:
           verdict = _ai_verdict(text)
           ctx.set_output({"verdict": verdict})

ROTATION
--------
Files live at `state/forensic_logs/log.<N>.jsonl`. When the active file
exceeds `FORENSIC_MAX_BYTES` (default 10 MB), the writer closes it and
opens the next number. On startup we resume into the highest-numbered
existing file (and roll over immediately if it's already too big).

THREAD SAFETY
-------------
A single module-level lock serializes appends. JSONL is line-oriented so
contended interleaving still produces parseable output, but the lock keeps
each line atomic. The file is opened in `O_APPEND` mode, so multi-process
appends are also safe (POSIX guarantees atomic single-line writes under
PIPE_BUF, and our lines stay well under that).

TRUNCATION
----------
Each `input`/`output`/`error`/`intermediate` field is JSON-truncated to
`FORENSIC_MAX_FIELD_BYTES` (default 4 KiB) at write time so a single
adversarial 1 MB resume doesn't fill the disk. The full text remains in
memory; only the log line is trimmed. Set `FORENSIC_FULL=1` in env to
disable truncation when you're chasing a specific bug.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# 10 MB per file by default; rotate to log.<N+1>.jsonl beyond that.
FORENSIC_MAX_BYTES = max(
    1024,
    int(os.environ.get("FORENSIC_MAX_BYTES", str(10 * 1024 * 1024))),
)

# Hard cap on each captured field (input/output/error/intermediate). 4 KiB
# is enough to preserve a Claude verdict + a job posting head; anything
# bigger is almost always a prompt body that's already in claude_calls
# via prompt_chars (length-only).
FORENSIC_MAX_FIELD_BYTES = max(
    256,
    int(os.environ.get("FORENSIC_MAX_FIELD_BYTES", "4096")),
)

# When set, disable truncation (for targeted debugging — large files).
FORENSIC_FULL = os.environ.get("FORENSIC_FULL", "").strip() not in ("", "0", "false", "False")

# Disable entirely when set (perf escape hatch).
FORENSIC_OFF = os.environ.get("FORENSIC_OFF", "").strip() not in ("", "0", "false", "False")


def _resolve_log_dir() -> Path:
    """Resolve the forensic log directory. Mirrors `STATE_DIR/forensic_logs/`
    so deployments that override STATE_DIR (e.g. test harnesses) get isolated
    logs without leaking into production. Created lazily on first write.
    """
    here = Path(__file__).resolve().parent
    project_root = here.parent.parent.parent
    state_dir = project_root / os.environ.get("STATE_DIR", "state")
    return state_dir / "forensic_logs"


# ---------------------------------------------------------------------------
# Writer (thread-safe singleton)
# ---------------------------------------------------------------------------

class _Writer:
    """Append-only JSONL writer with size-based rotation.

    State is kept in-memory:
      * current file path
      * current file size (queried on open, updated on every write)
    On startup we scan for the highest-numbered existing file and resume
    into it. If it's already > FORENSIC_MAX_BYTES we roll forward
    immediately.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dir: Path | None = None
        self._index: int = 0
        self._path: Path | None = None
        self._size: int = 0
        self._opened: bool = False

    def _ensure_open(self) -> None:
        """First-use init: pick the file index, create the dir."""
        if self._opened:
            return
        self._dir = _resolve_log_dir()
        self._dir.mkdir(parents=True, exist_ok=True)
        # Find highest existing log.<N>.jsonl
        highest = -1
        for entry in self._dir.iterdir():
            name = entry.name
            if name.startswith("log.") and name.endswith(".jsonl"):
                try:
                    n = int(name[len("log."):-len(".jsonl")])
                except ValueError:
                    continue
                if n > highest:
                    highest = n
        self._index = highest if highest >= 0 else 0
        self._path = self._dir / f"log.{self._index}.jsonl"
        try:
            self._size = self._path.stat().st_size if self._path.exists() else 0
        except OSError:
            self._size = 0
        if self._size >= FORENSIC_MAX_BYTES:
            self._index += 1
            self._path = self._dir / f"log.{self._index}.jsonl"
            self._size = 0
        self._opened = True

    def _rotate_if_needed(self, line_size: int) -> None:
        if self._size + line_size <= FORENSIC_MAX_BYTES:
            return
        # Roll forward.
        assert self._dir is not None
        self._index += 1
        self._path = self._dir / f"log.{self._index}.jsonl"
        self._size = 0

    def write(self, record: dict) -> None:
        if FORENSIC_OFF:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, default=_json_default)
        except (TypeError, ValueError):
            # Last-resort: dump repr of the record. Never raise from a logger.
            try:
                line = json.dumps({"_unserializable": True, "repr": repr(record)[:8000]})
            except Exception:
                return
        encoded = (line + "\n").encode("utf-8")
        with self._lock:
            try:
                self._ensure_open()
                self._rotate_if_needed(len(encoded))
                assert self._path is not None
                # O_APPEND => atomic single-line append on POSIX (lines small enough).
                with open(self._path, "ab") as f:
                    f.write(encoded)
                self._size += len(encoded)
            except Exception:
                # The forensic logger MUST never crash the caller.
                log.exception("forensic: write failed (record dropped)")


_WRITER = _Writer()


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _json_default(o: Any) -> Any:
    """Best-effort fallback for non-JSON-serializable values."""
    try:
        if isinstance(o, (set, frozenset)):
            return sorted(list(o), key=str)
        if isinstance(o, bytes):
            return o.decode("utf-8", errors="replace")
        if hasattr(o, "__dict__"):
            return {"__type__": type(o).__name__, "__dict__": vars(o)}
        return repr(o)
    except Exception:
        return "<unserializable>"


def _truncate(value: Any) -> Any:
    """Trim a field to FORENSIC_MAX_FIELD_BYTES of JSON. No-op when full mode is on."""
    if FORENSIC_FULL or value is None:
        return value
    try:
        encoded = json.dumps(value, ensure_ascii=False, default=_json_default)
    except (TypeError, ValueError):
        return repr(value)[:FORENSIC_MAX_FIELD_BYTES]
    if len(encoded.encode("utf-8")) <= FORENSIC_MAX_FIELD_BYTES:
        return value
    # For dicts/lists, trim string members aggressively before falling back
    # to a flat string truncation. Keeps structure for downstream parsers.
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            sv = _truncate_scalar(v)
            out[str(k)] = sv
        # If still too big, dump-and-truncate the whole thing.
        try:
            if len(json.dumps(out, ensure_ascii=False, default=_json_default).encode("utf-8")) <= FORENSIC_MAX_FIELD_BYTES:
                return out
        except (TypeError, ValueError):
            pass
    if isinstance(value, list):
        out_list: list = [_truncate_scalar(v) for v in value[:50]]
        try:
            if len(json.dumps(out_list, ensure_ascii=False, default=_json_default).encode("utf-8")) <= FORENSIC_MAX_FIELD_BYTES:
                return out_list
        except (TypeError, ValueError):
            pass
    # Final fallback: trimmed JSON string with a marker.
    return encoded[:FORENSIC_MAX_FIELD_BYTES] + "...[truncated]"


def _truncate_scalar(v: Any) -> Any:
    if isinstance(v, str):
        if len(v.encode("utf-8")) > 1024:
            return v[:1024] + "...[truncated]"
        return v
    if isinstance(v, (list, tuple)) and len(v) > 50:
        return list(v[:50]) + ["...[truncated]"]
    return v


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def log_step(
    op: str,
    *,
    input: Any = None,
    output: Any = None,
    error: Any = None,
    intermediate: Any = None,
    chat_id: int | None = None,
    run_id: int | None = None,
    phase: str = "single",
    elapsed_ms: int | None = None,
    extra: dict | None = None,
) -> None:
    """Append one forensic line. Fire-and-forget; never raises."""
    record: dict[str, Any] = {
        "ts": time.time(),
        "op": op,
        "phase": phase,
    }
    if chat_id is not None:
        record["chat_id"] = chat_id
    if run_id is not None:
        record["run_id"] = run_id
    if elapsed_ms is not None:
        record["elapsed_ms"] = elapsed_ms
    if input is not None:
        record["input"] = _truncate(input)
    if output is not None:
        record["output"] = _truncate(output)
    if error is not None:
        record["error"] = _truncate(error)
    if intermediate is not None:
        record["intermediate"] = _truncate(intermediate)
    if extra:
        record["extra"] = _truncate(extra)
    _WRITER.write(record)


class _StepCtx:
    """Returned by `step(...)`. Allows the body to set output / intermediate
    before the context closes."""

    __slots__ = ("op", "_input", "_output", "_intermediate", "_chat_id",
                 "_run_id", "_started", "_extra")

    def __init__(
        self,
        op: str,
        *,
        input: Any,
        chat_id: int | None,
        run_id: int | None,
        extra: dict | None,
    ) -> None:
        self.op = op
        self._input = input
        self._output: Any = None
        self._intermediate: Any = None
        self._chat_id = chat_id
        self._run_id = run_id
        self._extra = dict(extra) if extra else None
        self._started = time.time()

    def set_output(self, output: Any) -> None:
        self._output = output

    def set_intermediate(self, intermediate: Any) -> None:
        self._intermediate = intermediate

    def add_extra(self, key: str, value: Any) -> None:
        if self._extra is None:
            self._extra = {}
        self._extra[key] = value


@contextmanager
def step(
    op: str,
    *,
    input: Any = None,
    chat_id: int | None = None,
    run_id: int | None = None,
    extra: dict | None = None,
) -> Iterator[_StepCtx]:
    """Wrap an operation; on exit write one forensic line with elapsed +
    output + error.  Re-raises exceptions after recording.
    """
    ctx = _StepCtx(op, input=input, chat_id=chat_id, run_id=run_id, extra=extra)
    raised: BaseException | None = None
    try:
        yield ctx
    except BaseException as e:  # noqa: BLE001 — re-raise after recording
        raised = e
        raise
    finally:
        elapsed_ms = int((time.time() - ctx._started) * 1000)
        if raised is None:
            log_step(
                op,
                phase="ok",
                input=ctx._input,
                output=ctx._output,
                intermediate=ctx._intermediate,
                chat_id=ctx._chat_id,
                run_id=ctx._run_id,
                elapsed_ms=elapsed_ms,
                extra=ctx._extra,
            )
        else:
            try:
                err_payload = {
                    "class": type(raised).__name__,
                    "message": str(raised)[:400],
                    "stack_tail": _format_tb_tail(raised, n=8),
                }
            except Exception:
                err_payload = {"class": type(raised).__name__}
            log_step(
                op,
                phase="error",
                input=ctx._input,
                output=ctx._output,
                error=err_payload,
                intermediate=ctx._intermediate,
                chat_id=ctx._chat_id,
                run_id=ctx._run_id,
                elapsed_ms=elapsed_ms,
                extra=ctx._extra,
            )


def _format_tb_tail(exc: BaseException, n: int = 8) -> str:
    tb = exc.__traceback__
    if tb is None:
        return ""
    frames = traceback.extract_tb(tb)
    tail = frames[-n:]
    return "\n".join(f"{f.filename}:{f.lineno} in {f.name}" for f in tail)


# ---------------------------------------------------------------------------
# Diagnostics helper
# ---------------------------------------------------------------------------

def current_log_path() -> Path | None:
    """Return the active log file path (after first write) or None."""
    if not _WRITER._opened:
        try:
            _WRITER._ensure_open()
        except Exception:
            return None
    return _WRITER._path
