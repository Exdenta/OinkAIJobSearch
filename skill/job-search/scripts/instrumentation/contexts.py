"""Context managers that capture telemetry around existing call sites.

Four primitives, all stdlib-only, all writing through `telemetry.MonitorStore`:

  * pipeline_run(store, kind, triggered_by=None)
        Wraps an orchestrator invocation. Yields a `PipelineCtx` whose
        setters accumulate counters; on __exit__ a single `pipeline_runs`
        row is written. Status: ok / partial / exception.

  * source_run(store, run_id, source_key, *, user_chat_id=None)
        Wraps one adapter call. Yields a `SourceRunCtx`; `.set_count(n)`
        records how many items the adapter produced. Status mapping:
          - exception          -> 'failed'        (and re-raise)
          - success, count > 0 -> 'ok'
          - success, count = 0
              and consecutive_zero_runs(source_key, 3) -> 'suspicious_zero'
              else                                     -> 'ok'

  * claude_call(store, caller, *, chat_id=None, pipeline_run_id=None, model=None)
        Wraps a single subprocess invocation of the `claude` CLI. The caller
        MUST invoke `.record(...)` exactly once. If the with-block exits
        without `.record()` having been called, we record a best-effort
        `status='exception'` row so we don't lose data on caller bugs. On
        exception inside the with-block we record best-effort and re-raise.
        Never raises from its own bookkeeping.

  * error_capture(store, where, *, chat_id=None, alert_sink=None)
        Catches `Exception` (NOT BaseException), fingerprints, calls
        `store.try_record_error`. If newly-recorded AND `alert_sink`
        provided, invokes `alert_sink(envelope)`. ALWAYS re-raises. If
        rate-limited (try_record_error returned None), the alert_sink is
        NOT called — duplicate-suppression short-circuits delivery.

`AlertEnvelope` is the payload shape slice C consumes; it lives here so
slice C can import it from `instrumentation` without importing telemetry.
"""
from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

# NOTE: `telemetry` is implemented by slice A in parallel; this import
# resolves at integration time when slice A lands. It is NOT a missing
# dependency in this slice's PR.
from telemetry import MonitorStore  # noqa: F401  (typing/import contract)
from telemetry.fingerprint import (
    error_fingerprint,
    format_stack_tail,
    hour_bucket,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Alert envelope — consumed by slice C
# ---------------------------------------------------------------------------

@dataclass
class AlertEnvelope:
    """Data passed to an `alert_sink` callable.

    Slice C (`ops/alerts.py`) renders this into a Telegram message; slice B
    only constructs it. `event_id` is the row id `try_record_error`
    returned, used by slice C to call `mark_alert_delivered` on success.
    """
    where: str
    error_class: str
    message_head: str
    stack_tail: str
    chat_id: int | None
    occurred_at: float
    fingerprint: str
    event_id: int


# ---------------------------------------------------------------------------
# pipeline_run
# ---------------------------------------------------------------------------

@dataclass
class PipelineCtx:
    """Mutable accumulator handed to the body of a `pipeline_run` block."""
    store: "MonitorStore"
    run_id: int
    kind: str
    triggered_by: int | None
    started_at: float

    users_total: int = 0
    jobs_raw: int = 0
    jobs_sent: int = 0
    error_count: int = 0
    exit_code: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def set_users_total(self, n: int) -> None:
        self.users_total = int(n)

    def set_jobs_raw(self, n: int) -> None:
        self.jobs_raw = int(n)

    def set_jobs_sent(self, n: int) -> None:
        self.jobs_sent = int(n)

    def incr_errors(self, n: int = 1) -> None:
        self.error_count += int(n)

    def set_exit_code(self, code: int | None) -> None:
        self.exit_code = None if code is None else int(code)

    def record_extra(self, key: str, value: Any) -> None:
        self.extra[key] = value


@contextlib.contextmanager
def pipeline_run(
    store: "MonitorStore",
    kind: str,
    triggered_by: int | None = None,
) -> Iterator[PipelineCtx]:
    """Wrap an orchestrator invocation with a `pipeline_runs` row.

    Status:
      - exception bubbled  -> 'exception'  (and re-raise)
      - error_count > 0    -> 'partial'
      - else               -> 'ok'
    """
    started_at = time.time()
    run_id = store.start_pipeline_run(kind=kind, triggered_by=triggered_by)
    ctx = PipelineCtx(
        store=store,
        run_id=run_id,
        kind=kind,
        triggered_by=triggered_by,
        started_at=started_at,
    )
    raised: BaseException | None = None
    try:
        yield ctx
    except BaseException as e:  # noqa: BLE001 — we re-raise immediately
        raised = e
        raise
    finally:
        finished_at = time.time()
        if raised is not None:
            status = "exception"
        elif ctx.error_count > 0:
            status = "partial"
        else:
            status = "ok"
        try:
            store.finish_pipeline_run(
                run_id=run_id,
                status=status,
                exit_code=ctx.exit_code,
                users_total=ctx.users_total,
                jobs_raw=ctx.jobs_raw,
                jobs_sent=ctx.jobs_sent,
                error_count=ctx.error_count,
                extra=ctx.extra,
            )
        except Exception:
            # Telemetry must never mask the real exception or break the
            # caller. Log and move on.
            log.exception("pipeline_run: failed to finalize run_id=%s", run_id)


# ---------------------------------------------------------------------------
# source_run
# ---------------------------------------------------------------------------

@dataclass
class SourceRunCtx:
    """Mutable accumulator for one source-adapter invocation."""
    store: "MonitorStore"
    pipeline_run_id: int
    source_key: str
    user_chat_id: int | None
    started_at: float
    raw_count: int = 0

    def set_count(self, n: int) -> None:
        self.raw_count = int(n) if n is not None else 0


@contextlib.contextmanager
def source_run(
    store: "MonitorStore",
    pipeline_run_id: int,
    source_key: str,
    *,
    user_chat_id: int | None = None,
) -> Iterator[SourceRunCtx]:
    """Wrap a single source-adapter invocation.

    Status mapping (per spec §3):
      - exception                                -> 'failed'  (re-raise)
      - success, raw_count > 0                   -> 'ok'
      - success, raw_count == 0
            and consecutive_zero_runs(source_key, 3)
                                                 -> 'suspicious_zero'
            else                                 -> 'ok'
    """
    started_at = time.time()
    ctx = SourceRunCtx(
        store=store,
        pipeline_run_id=pipeline_run_id,
        source_key=source_key,
        user_chat_id=user_chat_id,
        started_at=started_at,
    )
    raised: BaseException | None = None
    try:
        yield ctx
    except BaseException as e:  # noqa: BLE001 — we re-raise immediately
        raised = e
        raise
    finally:
        finished_at = time.time()
        elapsed_ms = int((finished_at - started_at) * 1000)

        error_class: str | None = None
        error_head: str | None = None

        if raised is not None and isinstance(raised, Exception):
            status = "failed"
            error_class = type(raised).__name__
            try:
                error_head = str(raised)[:200]
            except Exception:
                error_head = None
        elif raised is not None:
            # BaseException (KeyboardInterrupt, SystemExit) — record as failed
            # but don't try to stringify aggressively.
            status = "failed"
            error_class = type(raised).__name__
        else:
            if ctx.raw_count > 0:
                status = "ok"
            else:
                # Zero results — check whether this source has been
                # producing zeros for a while.
                try:
                    suspicious = bool(
                        store.consecutive_zero_runs(source_key, 3)
                    )
                except Exception:
                    log.exception(
                        "source_run: consecutive_zero_runs failed for %s",
                        source_key,
                    )
                    suspicious = False
                status = "suspicious_zero" if suspicious else "ok"

        try:
            store.record_source_run(
                pipeline_run_id,
                source_key,
                status,
                ctx.raw_count,
                elapsed_ms,
                user_chat_id=user_chat_id,
                error_class=error_class,
                error_head=error_head,
                started_at=started_at,
                finished_at=finished_at,
            )
        except Exception:
            log.exception(
                "source_run: failed to record source=%s run_id=%s",
                source_key, pipeline_run_id,
            )


# ---------------------------------------------------------------------------
# claude_call
# ---------------------------------------------------------------------------

class CallCtx:
    """Yielded by `claude_call`. Caller invokes `.record(...)` exactly once."""

    __slots__ = ("_recorded", "_payload")

    def __init__(self) -> None:
        self._recorded = False
        self._payload: dict[str, Any] = {}

    def record(
        self,
        prompt_chars: int,
        output_chars: int,
        exit_code: int | None,
        status: str,
    ) -> None:
        self._recorded = True
        self._payload = {
            "prompt_chars": int(prompt_chars or 0),
            "output_chars": int(output_chars or 0),
            "exit_code": exit_code,
            "status": status,
        }

    @property
    def recorded(self) -> bool:
        return self._recorded


@contextlib.contextmanager
def claude_call(
    store: "MonitorStore",
    caller: str,
    *,
    chat_id: int | None = None,
    pipeline_run_id: int | None = None,
    model: str | None = None,
) -> Iterator[CallCtx]:
    """Wrap a single `claude` CLI subprocess invocation.

    Contract:
      - Caller invokes `.record(prompt_chars, output_chars, exit_code, status)`
        exactly once inside the with-block.
      - If the with-block exits cleanly without `.record()`, we write a
        best-effort row with status='exception' so we don't silently lose
        the call — this is a caller bug, but recording it surfaces the bug.
      - If the with-block raises, we record best-effort with status='exception'
        and re-raise.
      - This context manager NEVER raises from its own bookkeeping.
    """
    started_at = time.time()
    ctx = CallCtx()
    raised: BaseException | None = None
    try:
        yield ctx
    except BaseException as e:  # noqa: BLE001 — we re-raise immediately
        raised = e
        raise
    finally:
        finished_at = time.time()
        elapsed_ms = int((finished_at - started_at) * 1000)
        if ctx.recorded and raised is None:
            payload = ctx._payload
            status = payload["status"]
            prompt_chars = payload["prompt_chars"]
            output_chars = payload["output_chars"]
            exit_code = payload["exit_code"]
        else:
            # Either caller never called .record(), or an exception bubbled.
            status = "exception"
            prompt_chars = ctx._payload.get("prompt_chars", 0)
            output_chars = ctx._payload.get("output_chars", 0)
            exit_code = ctx._payload.get("exit_code")
        try:
            store.record_claude_call(
                pipeline_run_id=pipeline_run_id,
                chat_id=chat_id,
                caller=caller,
                model=model,
                prompt_chars=prompt_chars,
                output_chars=output_chars,
                elapsed_ms=elapsed_ms,
                exit_code=exit_code,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
            )
        except Exception:
            log.exception(
                "claude_call: failed to record caller=%s status=%s",
                caller, status,
            )


# ---------------------------------------------------------------------------
# error_capture
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def error_capture(
    store: "MonitorStore",
    where: str,
    *,
    chat_id: int | None = None,
    alert_sink: Callable[[AlertEnvelope], None] | None = None,
) -> Iterator[None]:
    """Capture exceptions, fingerprint+rate-limit, optionally alert, re-raise.

    Catches `Exception` (not `BaseException` — KeyboardInterrupt and
    SystemExit propagate untouched). On capture:
      1. Build a fingerprint and stack tail.
      2. Call `store.try_record_error(...)`. The UNIQUE constraint on
         (fingerprint, hour_bucket) means duplicates within the same hour
         return None.
      3. If a row was inserted (id is not None) AND `alert_sink` is set,
         invoke `alert_sink(AlertEnvelope(...))`. Errors from the sink are
         logged and swallowed — alert delivery must never replace the
         original exception.
      4. ALWAYS re-raise the original exception.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — we re-raise after recording
        occurred_at = time.time()
        try:
            error_class = type(exc).__name__
            try:
                message_head = str(exc)[:200]
            except Exception:
                message_head = ""
            tb = exc.__traceback__
            stack_tail = format_stack_tail(tb, n=8) if tb is not None else ""
            fp = error_fingerprint(exc)
            bucket = hour_bucket(occurred_at)
            event_id = store.try_record_error(
                fingerprint=fp,
                hour_bucket=bucket,
                where=where,
                error_class=error_class,
                message_head=message_head,
                stack_tail=stack_tail,
                chat_id=chat_id,
            )
            if event_id is not None and alert_sink is not None:
                envelope = AlertEnvelope(
                    where=where,
                    error_class=error_class,
                    message_head=message_head,
                    stack_tail=stack_tail,
                    chat_id=chat_id,
                    occurred_at=occurred_at,
                    fingerprint=fp,
                    event_id=event_id,
                )
                try:
                    alert_sink(envelope)
                except Exception:
                    log.exception(
                        "error_capture: alert_sink raised for where=%s",
                        where,
                    )
        except Exception:
            # Bookkeeping must never replace the original exception.
            log.exception(
                "error_capture: bookkeeping failed for where=%s class=%s",
                where, type(exc).__name__,
            )
        raise
