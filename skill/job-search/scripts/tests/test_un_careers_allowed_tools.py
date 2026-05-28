#!/usr/bin/env python3
"""Tests for the 2026-05 fix to ``sources/un_careers.py``.

Root cause we're guarding against
---------------------------------
Before the fix, ``un_careers._run_claude`` called the plain ``run_p`` (via
``wrapped_run_p``) which does not pass ``--allowed-tools`` to the ``claude``
CLI. The CLI therefore prompted the user interactively for WebFetch
permission, and in a non-interactive subprocess that prompt became a
textual response ("I need permission to use WebFetch to scrape the UN
careers portal…") instead of the JSON envelope the adapter expected. Every
iter logged a ``response was not a JSON object`` warning, recorded an
``ok``-status row in ``claude_calls``, and returned zero postings.

What this test asserts
----------------------
1.  The adapter routes through the tool-aware wrapper
    (``wrapped_run_p_with_tools`` when available, ``run_p_with_tools``
    otherwise) — NOT the plain ``run_p``/``wrapped_run_p`` path that
    omits the tool flags.
2.  The kwargs passed include ``allowed_tools="WebFetch"`` and
    ``disallowed_tools="Bash,Edit,Write,Read"`` so the CLI grants the
    fetch tool non-interactively while keeping the attack surface narrow.
3.  ``curated_boards`` (which had the same bug) routes through
    ``wrapped_run_p_with_tools`` with the same allow/deny strings.
4.  The model pin (``haiku`` via ``SMALLEST_MODEL``) survives the
    migration — without an explicit ``--model`` the CLI defaults to Opus
    and the call blows past the bot's 180s timeout (commit 1a18422).

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
# un_careers
# ---------------------------------------------------------------------------

def test_un_careers_routes_through_wrapped_with_tools() -> None:
    """The adapter must call ``wrapped_run_p_with_tools`` — never the plain
    wrapper — so the CLI receives ``--allowed-tools`` and stops prompting
    interactively for WebFetch permission."""
    from sources import un_careers

    calls: list[dict[str, Any]] = []
    stub = _capture_factory(calls)

    # The adapter looks up _wrapped_run_p_with_tools at module scope; patch
    # the module attribute directly. _HAS_WRAPPED guards the branch — flip
    # it on too so we don't silently fall through to run_p_with_tools.
    original_fn = un_careers._wrapped_run_p_with_tools
    original_flag = un_careers._HAS_WRAPPED
    un_careers._wrapped_run_p_with_tools = stub
    un_careers._HAS_WRAPPED = True
    try:
        jobs = un_careers.fetch({"max_per_source": 5, "ai_scrape_timeout_s": 90})
    finally:
        un_careers._wrapped_run_p_with_tools = original_fn
        un_careers._HAS_WRAPPED = original_flag

    assert len(calls) == 1, f"expected one CLI invocation, got {len(calls)}"
    call = calls[0]
    assert call["caller"] == "un_careers", call["caller"]

    kw = call["kwargs"]
    # The load-bearing assertion: WebFetch must be granted, otherwise the
    # CLI prompts interactively and we're right back at the 2026-05 bug.
    assert kw.get("allowed_tools") == "WebFetch", (
        f"allowed_tools must be 'WebFetch', got {kw.get('allowed_tools')!r}"
    )
    assert kw.get("disallowed_tools") == "Bash,Edit,Write,Read", (
        f"disallowed_tools mismatch: {kw.get('disallowed_tools')!r}"
    )
    # Model pin must survive — without it the CLI defaults to Opus and
    # times out the bot's 180s window (see devex/web_search comments).
    assert kw.get("model") == "haiku", f"expected model='haiku', got {kw.get('model')!r}"
    # Timeout flows through from the filters dict.
    assert kw.get("timeout_s") == 90, f"timeout_s mismatch: {kw.get('timeout_s')!r}"

    # And the canned JSON parsed into one Job, proving the JSON envelope
    # path still works end-to-end.
    assert len(jobs) == 1, f"expected 1 parsed job, got {len(jobs)}"
    assert jobs[0].source == "un_careers"
    assert jobs[0].url == "https://careers.un.org/jobs/12345"


def test_un_careers_fallback_uses_run_p_with_tools() -> None:
    """When the wrapper is unavailable (lean checkout / isolated env), the
    adapter must STILL pass ``allowed_tools`` to the underlying CLI helper.
    This guards against a regression where someone might restore plain
    ``run_p`` in the fallback path."""
    from sources import un_careers
    import claude_cli

    calls: list[dict[str, Any]] = []

    def _stub(prompt, **kwargs):  # noqa: ANN001 — stub
        calls.append({
            "prompt_head": (prompt or "")[:200],
            "kwargs": kwargs,
        })
        return _FAKE_CLI_STDOUT

    original_fn = claude_cli.run_p_with_tools
    original_flag = un_careers._HAS_WRAPPED
    claude_cli.run_p_with_tools = _stub  # type: ignore[assignment]
    # Also patch the binding imported into un_careers.
    original_import = un_careers.run_p_with_tools
    un_careers.run_p_with_tools = _stub  # type: ignore[assignment]
    un_careers._HAS_WRAPPED = False
    try:
        jobs = un_careers.fetch({"max_per_source": 5, "ai_scrape_timeout_s": 90})
    finally:
        claude_cli.run_p_with_tools = original_fn
        un_careers.run_p_with_tools = original_import
        un_careers._HAS_WRAPPED = original_flag

    assert len(calls) == 1, f"expected one CLI invocation, got {len(calls)}"
    kw = calls[0]["kwargs"]
    assert kw.get("allowed_tools") == "WebFetch", (
        f"fallback path must still grant WebFetch; got {kw.get('allowed_tools')!r}"
    )
    assert kw.get("disallowed_tools") == "Bash,Edit,Write,Read"
    assert kw.get("model") == "haiku"
    assert len(jobs) == 1


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
        test_un_careers_routes_through_wrapped_with_tools,
        test_un_careers_fallback_uses_run_p_with_tools,
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
