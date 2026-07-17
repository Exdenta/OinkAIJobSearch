#!/usr/bin/env python3
"""Every `claude` invocation must authenticate as the subscription.

The CLI treats ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN as auth sources that
take precedence over the OAuth token in ~/.claude/.credentials.json. A stale or
wrong value there 401s every call.

This used to be an opt-in `subscription_auth_only=True` that only
`prewarm_token` passed, so:
  * `run_p_with_tools` (the liveness verifier) never stripped anything, and
  * prewarm would strip the key, report `ok`, and the enrich fan-out would
    inherit it and 401 — a green health check over a dead pipeline.

Both entry points now route through `_subscription_env()`. These tests capture
the `env=` handed to `subprocess.run` and pin that the vars are gone.

Run directly (`python test_claude_cli_subscription_env.py`) or via pytest.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import claude_cli  # noqa: E402


class _Proc:
    def __init__(self, rc: int, out: str, err: str) -> None:
        self.returncode, self.stdout, self.stderr = rc, out, err


def _restore():
    import importlib
    importlib.reload(claude_cli)


def _capture_env(call) -> dict:
    """Run `call()` with subprocess.run stubbed; return the env it was passed."""
    seen: dict = {}

    def _fake_run(cmd, **kw):
        seen.update(kw.get("env") or {})
        return _Proc(0, '{"type":"result","is_error":false,"result":"ok"}', "")

    claude_cli.shutil.which = lambda n: "/usr/bin/claude"
    claude_cli.subprocess.run = _fake_run
    try:
        call()
    finally:
        _restore()
    return seen


def test_run_p_strips_shadowing_auth_vars():
    import os
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-not-leak"
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "bearer-should-not-leak"
    os.environ["OINK_SENTINEL"] = "keep-me"
    try:
        env = _capture_env(lambda: claude_cli.run_p("x"))
        assert "ANTHROPIC_API_KEY" not in env, "API key leaked into run_p env"
        assert "ANTHROPIC_AUTH_TOKEN" not in env, "auth token leaked into run_p env"
        # Everything else must survive — we strip auth, not the environment.
        assert env.get("OINK_SENTINEL") == "keep-me"
    finally:
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OINK_SENTINEL"):
            os.environ.pop(k, None)


def test_run_p_with_tools_strips_shadowing_auth_vars():
    """The liveness-verifier path — the one the old opt-in flag never covered."""
    import os
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-not-leak"
    os.environ["ANTHROPIC_AUTH_TOKEN"] = "bearer-should-not-leak"
    try:
        env = _capture_env(
            lambda: claude_cli.run_p_with_tools("x", allowed_tools="WebSearch")
        )
        assert "ANTHROPIC_API_KEY" not in env, "API key leaked into tools env"
        assert "ANTHROPIC_AUTH_TOKEN" not in env, "auth token leaked into tools env"
    finally:
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(k, None)


def test_no_optin_flag_remains():
    """Regression guard: the per-call opt-in must not come back."""
    import inspect
    for fn in (claude_cli.run_p, claude_cli.run_p_with_tools):
        assert "subscription_auth_only" not in inspect.signature(fn).parameters, fn


if __name__ == "__main__":
    test_run_p_strips_shadowing_auth_vars()
    test_run_p_with_tools_strips_shadowing_auth_vars()
    test_no_optin_flag_remains()
    print("ok")
