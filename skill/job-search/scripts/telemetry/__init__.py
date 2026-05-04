"""Telemetry storage layer (slice A).

Pure DAL over `state/jobs.db` — defines the monitoring schema, exposes a
typed `MonitorStore` API, and ships stateless helpers for error
fingerprinting and Claude-CLI cost surrogate. This package is the seam
between slice A (storage), slice B (instrumentation), and slice C
(operator/delivery): both downstream slices import only from here, never
the reverse. Nothing in this package imports from `instrumentation`,
`ops`, `claude_cli`, `bot`, `telegram_client`, or `search_jobs`.

See docs/monitoring-plan.md §3 for the frozen API surface.
"""
from __future__ import annotations

from .schema import MONITOR_SCHEMA, migrate
from .store import MonitorStore, RunStatus, SourceStatus, CallStatus

__all__ = [
    "MONITOR_SCHEMA",
    "migrate",
    "MonitorStore",
    "RunStatus",
    "SourceStatus",
    "CallStatus",
]
