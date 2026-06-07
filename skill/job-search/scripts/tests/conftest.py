"""Shared test isolation guards.

Several modules in this package keep PROCESS-GLOBAL caches that a test may
legitimately (or accidentally) mutate:

  * ``instrumentation.wrappers._resolve_store`` / ``._DEFAULT_STORE`` — the
    lazy telemetry-store resolver + its cached default store.
  * ``sdk_scoring._CLIENT`` — the cached Anthropic SDK client.

At least one existing test (``test_listing_verifier_hardening.py``)
OVERWRITES ``wrappers._resolve_store`` with a capture stub via direct
module assignment and never restores it — by design, to dodge the
stale-reference problem the ``from instrumentation.wrappers import ...``
pattern would create. On its own that is harmless, but it LEAKS into any
later test whose telemetry path calls ``_resolve_store`` (e.g. the
SDK-scoring telemetry tests), silently routing their ``record_claude_call``
rows into the stub instead of the test's own store.

This autouse fixture snapshots those globals before each test and restores
them afterwards, so no test can pollute another regardless of collection
order. Restoring a lazy cache to its prior value is always safe — the
worst case is one extra rebuild on next use.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _restore_module_globals():
    # Snapshot before the test.
    try:
        from instrumentation import wrappers as _w
    except Exception:  # pragma: no cover - import should always succeed
        _w = None
    if _w is not None:
        saved_resolve = _w.__dict__.get("_resolve_store")
        saved_default = _w.__dict__.get("_DEFAULT_STORE")

    try:
        import sdk_scoring as _s
    except Exception:
        _s = None
    if _s is not None:
        saved_client = _s.__dict__.get("_CLIENT")

    yield

    # Restore after the test (pass or fail).
    if _w is not None:
        if saved_resolve is not None:
            _w._resolve_store = saved_resolve
        _w._DEFAULT_STORE = saved_default
    if _s is not None:
        _s._CLIENT = saved_client
