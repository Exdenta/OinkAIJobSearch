#!/usr/bin/env python3
"""Tests for honest Claude-CLI failure labeling.

`claude_cli.run_p` / `run_p_with_tools` return a bare `str | None` — `None`
on ANY failure. Historically `instrumentation.wrappers._infer_status` labelled
every `None` as `cli_missing`, which lied whenever the binary was present and
the real cause was a timeout or a mid-run API error (rate limit / overloaded).
The prod signature that motivated this: `exit=1`, EMPTY stderr, and the real
error hidden in the stdout JSON envelope that `run_p_with_tools` discarded on
`rc != 0`.

The fix keeps the `str | None` return contract and publishes the failure
reason out-of-band on a thread-local (`_set_failure` / `pop_last_failure`).
These tests pin each reason path and the wrapper's status/exit_code mapping.

Run directly (`python test_claude_cli_failure_labeling.py`) or via pytest.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import claude_cli  # noqa: E402
from instrumentation import wrappers as _w  # noqa: E402


class _Proc:
    def __init__(self, rc: int, out: str, err: str) -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _restore():
    """Undo any monkeypatching so tests stay independent."""
    import importlib
    importlib.reload(claude_cli)


# ---------------------------------------------------------------------------
# claude_cli: each failure path sets the honest reason on the thread-local
# ---------------------------------------------------------------------------

def test_cli_absent_reason():
    claude_cli.shutil.which = lambda n: None
    try:
        assert claude_cli.run_p_with_tools("x") is None
        d = claude_cli.pop_last_failure()
        assert d["reason"] == "cli_absent", d
        # popped → cleared
        assert claude_cli.pop_last_failure() is None
    finally:
        _restore()


def test_api_error_mines_discarded_envelope():
    """The prod case: exit=1, empty stderr, error in the stdout envelope."""
    claude_cli.shutil.which = lambda n: "/usr/bin/claude"
    claude_cli.subprocess.run = lambda cmd, **kw: _Proc(
        1,
        '{"type":"result","is_error":true,'
        '"api_error_status":"overloaded_error","result":"Overloaded"}',
        "",  # empty stderr — the whole point
    )
    try:
        assert claude_cli.run_p_with_tools("x", allowed_tools="WebSearch") is None
        d = claude_cli.pop_last_failure()
        assert d["reason"] == "api_error", d
        assert d["exit_code"] == 1, d
        assert d["api_error_status"] == "overloaded_error", d
    finally:
        _restore()


def test_nonzero_exit_empty_envelope():
    claude_cli.shutil.which = lambda n: "/usr/bin/claude"
    claude_cli.subprocess.run = lambda cmd, **kw: _Proc(1, "", "")
    try:
        assert claude_cli.run_p("x") is None
        d = claude_cli.pop_last_failure()
        assert d["reason"] == "nonzero_exit" and d["exit_code"] == 1, d
    finally:
        _restore()


def test_timeout_reason():
    claude_cli.shutil.which = lambda n: "/usr/bin/claude"

    def _boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)

    claude_cli.subprocess.run = _boom
    try:
        assert claude_cli.run_p_with_tools("x") is None
        d = claude_cli.pop_last_failure()
        assert d["reason"] == "timeout", d
    finally:
        _restore()


def test_start_error_reason():
    claude_cli.shutil.which = lambda n: "/usr/bin/claude"

    def _boom(cmd, **kw):
        raise OSError("cannot spawn")

    claude_cli.subprocess.run = _boom
    try:
        assert claude_cli.run_p("x") is None
        d = claude_cli.pop_last_failure()
        assert d["reason"] == "start_error", d
    finally:
        _restore()


def test_success_clears_detail():
    claude_cli.shutil.which = lambda n: "/usr/bin/claude"
    claude_cli.subprocess.run = lambda cmd, **kw: _Proc(0, '{"result":"hi"}', "")
    try:
        out = claude_cli.run_p_with_tools("x")
        assert out == '{"result":"hi"}', out
        assert claude_cli.pop_last_failure() is None
    finally:
        _restore()


def test_prewarm_ignores_anthropic_api_key():
    old_key = claude_cli.os.environ.get("ANTHROPIC_API_KEY")
    seen_env = {}
    claude_cli.os.environ["ANTHROPIC_API_KEY"] = "sk-test-bad"
    claude_cli.shutil.which = lambda n: "/usr/bin/claude"

    def _run(cmd, **kw):
        seen_env.update(kw["env"])
        return _Proc(0, '{"result":"ok"}', "")

    claude_cli.subprocess.run = _run
    try:
        assert claude_cli.prewarm_token(timeout_s=5) == ("ok", "")
        assert "ANTHROPIC_API_KEY" not in seen_env
    finally:
        if old_key is None:
            claude_cli.os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            claude_cli.os.environ["ANTHROPIC_API_KEY"] = old_key
        _restore()


def test_prewarm_transient_401_clears_on_retry():
    """A refresh-race 401 on the FIRST cold call must clear on the retry and
    NOT page the operator — the enrich fan-out right after proves creds are OK."""
    claude_cli.shutil.which = lambda n: "/usr/bin/claude"
    calls = {"n": 0}

    def _run(cmd, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _Proc(1, '{"is_error":true,"api_error_status":"401"}', "")
        return _Proc(0, '{"result":"ok"}', "")

    claude_cli.subprocess.run = _run
    try:
        assert claude_cli.prewarm_token(timeout_s=5) == ("ok", "")
        assert calls["n"] == 2, calls  # retried exactly once
    finally:
        _restore()


def test_prewarm_persistent_401_still_pages():
    """Truly broken creds 401 on BOTH attempts → auth (operator still paged)."""
    claude_cli.shutil.which = lambda n: "/usr/bin/claude"
    calls = {"n": 0}

    def _run(cmd, **kw):
        calls["n"] += 1
        return _Proc(1, '{"is_error":true,"api_error_status":"401"}', "")

    claude_cli.subprocess.run = _run
    try:
        cat, _ = claude_cli.prewarm_token(timeout_s=5)
        assert cat == "auth", cat
        assert calls["n"] == 2, calls  # retried once, then gave up
    finally:
        _restore()


# ---------------------------------------------------------------------------
# wrappers._infer_status: reason → honest status; fallback preserved
# ---------------------------------------------------------------------------

def test_infer_status_maps_reason():
    assert _w._infer_status(None, 0, 0, {"reason": "api_error", "exit_code": 1}) == "api_error"
    assert _w._infer_status(None, 0, 0, {"reason": "timeout"}) == "timeout"
    assert _w._infer_status(None, 0, 0, {"reason": "cli_absent"}) == "cli_absent"
    assert _w._infer_status(None, 0, 0, {"reason": "nonzero_exit"}) == "nonzero_exit"
    assert _w._infer_status(None, 0, 0, {"reason": "start_error"}) == "start_error"


def test_infer_status_fallback_and_ok_paths():
    # No reason available (raw caller bypassed the pop) → legacy bucket.
    assert _w._infer_status(None, 0, 0, None) == "cli_missing"
    # Unknown reason string is not trusted as a status.
    assert _w._infer_status(None, 0, 0, {"reason": "bogus"}) == "cli_missing"
    # Well-formed envelope, empty result → empty_result (unchanged).
    assert _w._infer_status('{"result":""}', 12, 0, None) == "empty_result"
    # Normal success (unchanged).
    assert _w._infer_status('{"result":"x"}', 14, 1, None) == "ok"


_TESTS = [
    test_cli_absent_reason,
    test_api_error_mines_discarded_envelope,
    test_nonzero_exit_empty_envelope,
    test_timeout_reason,
    test_start_error_reason,
    test_success_clears_detail,
    test_prewarm_ignores_anthropic_api_key,
    test_prewarm_transient_401_clears_on_retry,
    test_prewarm_persistent_401_still_pages,
    test_infer_status_maps_reason,
    test_infer_status_fallback_and_ok_paths,
]


def main() -> int:
    failed = 0
    for t in _TESTS:
        try:
            t()
            print(f"  OK   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {t.__name__}: {e!r}")
            import traceback
            traceback.print_exc()
    if failed:
        print(f"\n{failed} test(s) failed.")
        return 1
    print(f"\nAll {len(_TESTS)} claude-cli failure-labeling tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
