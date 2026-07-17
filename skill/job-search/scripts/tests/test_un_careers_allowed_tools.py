#!/usr/bin/env python3
"""Tests for the 2026-05 ``--allowed-tools`` fix to the WebFetch adapters.

NOTE: ``sources/un_careers.py`` was rewritten in 2026-06 from the WebFetch
Claude-delegation approach to a direct HTTP-JSON adapter against the UN
careers public API (see ``test_un_careers_api.py``). The old un_careers
tests that asserted the WebFetch-tool wiring were removed with that rewrite;
only the ``curated_boards`` case (same original root-cause class) remains
here.

Root cause this guards against
------------------------------
Before the fix, an adapter's ``_run_claude`` called the plain ``run_p`` (via
``wrapped_run_p``) which does not pass ``--allowed-tools`` to the ``claude``
CLI. The CLI therefore prompted the user interactively for WebFetch
permission, and in a non-interactive subprocess that prompt became a
textual response ("I need permission to use WebFetch…") instead of the JSON
envelope the adapter expected.

What this test asserts
----------------------
``curated_boards`` routes through ``wrapped_run_p_with_tools`` with
``allowed_tools="WebFetch"`` and ``disallowed_tools="Bash,Edit,Write,Read"``
and the ``haiku`` model pin.

Invoke either directly or via pytest:

    python3 skill/job-search/scripts/tests/test_un_careers_allowed_tools.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))


# A minimal JSON envelope shaped like what `claude -p --output-format json`
# emits — the adapter unwraps `result` then parses the inner string.
_FAKE_CLI_STDOUT = (
    '{"result": "{\\"jobs\\": ['
    '{\\"title\\": \\"Programme Officer\\", '
    '\\"company\\": \\"UNHCR\\", '
    '\\"location\\": \\"Geneva, Switzerland\\", '
    '\\"url\\": \\"https://careers.un.org/jobs/12345\\", '
    '\\"posted_at\\": \\"\\", '
    '\\"snippet\\": \\"\\"}'
    ']}"}'
)


def _capture_factory(calls: list[dict[str, Any]]) -> Any:
    """Build a stub that records every call's positional + keyword args and
    returns the canned JSON envelope above. The real wrapper has the shape
    ``wrapped_run_p_with_tools(store, caller, prompt, **kwargs)``."""

    def _stub(store, caller, prompt, **kwargs):  # noqa: ANN001 — stub
        calls.append({
            "store": store,
            "caller": caller,
            "prompt_head": (prompt or "")[:200],
            "kwargs": kwargs,
        })
        return _FAKE_CLI_STDOUT
    return _stub


# ---------------------------------------------------------------------------
# curated_boards (same root-cause class — also previously used plain run_p)
# ---------------------------------------------------------------------------

def test_curated_boards_routes_through_wrapped_with_tools() -> None:
    """``curated_boards`` had the same bug — its prompt says "Use the
    WebFetch tool" but the wiring went through ``wrapped_run_p`` (plain
    ``run_p``, no tool flags). Assert the migration to
    ``wrapped_run_p_with_tools`` and the same allow/deny strings."""
    from sources import curated_boards

    calls: list[dict[str, Any]] = []
    stub = _capture_factory(calls)

    original = curated_boards.wrapped_run_p_with_tools
    curated_boards.wrapped_run_p_with_tools = stub
    try:
        # Force exactly one board on so we know how many calls to expect.
        filters = {
            "sources": {"remocate": True, "wantapply": False, "remoterocketship": False},
            "max_per_source": 5,
            "ai_scrape_timeout_s": 90,
        }
        jobs = curated_boards.fetch(filters)
    finally:
        curated_boards.wrapped_run_p_with_tools = original

    assert len(calls) == 1, f"expected one CLI invocation for one enabled board, got {len(calls)}"
    call = calls[0]
    assert call["caller"] == "curated_boards:remocate"
    kw = call["kwargs"]
    assert kw.get("allowed_tools") == "WebFetch"
    assert kw.get("disallowed_tools") == "Bash,Edit,Write,Read"
    assert kw.get("model") == "haiku"
    assert kw.get("timeout_s") == 90
    # Sanity: stub canned ONE job, so we get one back.
    assert len(jobs) == 1
    assert jobs[0].source == "remocate"


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def _run() -> int:
    tests = [
        test_curated_boards_routes_through_wrapped_with_tools,
    ]
    failed: list[str] = []
    for t in tests:
        name = t.__name__
        try:
            t()
            print(f"[PASS] {name}")
        except AssertionError as e:
            print(f"[FAIL] {name}: {e}")
            failed.append(name)
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"[ERROR] {name}: {e!r}")
            traceback.print_exc()
            failed.append(name)
    if failed:
        print(f"\n{len(failed)}/{len(tests)} tests failed: {failed}")
        return 1
    print(f"\nAll {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_run())
