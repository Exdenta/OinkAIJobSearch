"""Instrumentation primitives for monitoring (slice B).

Reusable context managers and wrappers used by hand at existing call sites.
Imports from `telemetry` only (slice A); slice C imports from here but never
the other way around.

Public surface:
    pipeline_run, source_run, claude_call, error_capture     — context managers
    wrapped_run_p, wrapped_run_p_with_tools                  — claude CLI wrappers
    AlertEnvelope                                            — payload for alert_sink
"""
from __future__ import annotations

from .contexts import (
    AlertEnvelope,
    claude_call,
    error_capture,
    pipeline_run,
    source_run,
)
from .wrappers import wrapped_run_p, wrapped_run_p_with_tools

__all__ = [
    "AlertEnvelope",
    "pipeline_run",
    "source_run",
    "claude_call",
    "error_capture",
    "wrapped_run_p",
    "wrapped_run_p_with_tools",
]
