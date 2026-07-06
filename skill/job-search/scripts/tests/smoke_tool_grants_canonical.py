#!/usr/bin/env python3
"""Regression test: every tool-granting call site must use the canonical
constants from ``claude_cli`` (not ad-hoc strings).

Why this exists
---------------

Before centralization, two call sites in the codebase used a different
shape from every other call site:

  - ``sources/ub_doctoral.py`` passed ``allowed_tools="WebFetch WebSearch"``
    (space-separated) with NO ``disallowed_tools``.
  - ``telegram_client._web_search_listing_still_open`` did the same.

The ``claude`` CLI accepts both comma- and space-separated lists, so the
space form wasn't *broken* — but it bypassed the project's prompt-injection
defense (the explicit ``Bash,Edit,Write,Read`` deny list every other call
site enforced) and made the codebase harder to audit (two formats to grep
for).

The fix:
  1. Add canonical constants to ``claude_cli`` (``TOOLS_WEB_BOTH``,
     ``TOOLS_DENY_SHELL_FS``, ``TOOLS_DENY_WEB_AND_SHELL_FS``).
  2. Migrate every existing call site to import + use those constants.
  3. This test asserts that no ad-hoc tool-grant strings have been
     reintroduced — a future PR that drifts back to literals like
     ``allowed_tools="WebFetch WebSearch"`` or
     ``disallowed_tools="Bash,Edit,Write,Read"`` (instead of the named
     constant) will fail here.

The test is intentionally textual (regex over source files) rather than
runtime (importing each module + inspecting their constants). Reasons:

  - Each call site has slightly different import shape (some lazy-import
    inside a function, some module-level). Runtime introspection would
    require executing partial modules with heavy side effects.
  - The drift we want to catch is *literal strings in source* — exactly
    what a grep can prove absent.
  - Cheap to maintain: when a new tool-granting call site is added, the
    author either imports the constant (test passes) or hardcodes a
    literal (test fails with a clear "drifted to ad-hoc string" message).

This is structural / orchestration code, not scoring doctrine, so the
``CLAUDE.md`` "AI-prompt-over-heuristic" rule does not apply.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS_ROOT = HERE.parent

# ---------------------------------------------------------------------------
# Canonical constants the codebase MUST import + reference.
# Kept here as plain strings so the test self-contains the expected values
# and can detect drift even if someone renames the constants.
# ---------------------------------------------------------------------------
_CANONICAL_NAMES = (
    "TOOLS_WEB_BOTH",
    "TOOLS_WEB_FETCH_ONLY",
    "TOOLS_DENY_SHELL_FS",
    "TOOLS_DENY_WEB_AND_SHELL_FS",
)

# Literal strings that, if found anywhere outside ``claude_cli.py`` /
# this test / test fixtures, indicate someone reintroduced an ad-hoc
# tool-grant string instead of importing the canonical constant.
#
# We deliberately match the raw wire values (comma + space variants for
# the WebSearch+WebFetch pair, since that was the original drift). The
# space-separated form was specifically the bug we fixed in
# ``sources/ub_doctoral.py`` and ``telegram_client.py`` — catching it
# here prevents silent regression.
_BANNED_LITERALS = (
    '"WebSearch,WebFetch"',
    '"WebFetch,WebSearch"',
    '"WebFetch WebSearch"',
    '"WebSearch WebFetch"',
    '"Bash,Edit,Write,Read"',
    '"WebSearch,WebFetch,Bash,Edit,Write,Read"',
)

# Files we EXPECT to contain the literals (the canonical definitions
# themselves, and tests that exercise the wire format end-to-end).
_LITERAL_EXEMPT = {
    SCRIPTS_ROOT / "claude_cli.py",                              # defines them
    Path(__file__),                                              # this test
    SCRIPTS_ROOT / "tests" / "smoke_market_research.py",         # asserts CLI argv
    SCRIPTS_ROOT / "tests" / "smoke_web_search_aleksandr.py",    # doc-string only
}

# Files that grant tools via the ``run_p_with_tools`` /
# ``wrapped_run_p_with_tools`` entry points. Each one must:
#   - import at least one of the canonical constants from ``claude_cli``
#   - NOT contain any banned literal anywhere in the file
_TOOL_GRANTING_FILES = (
    SCRIPTS_ROOT / "market_research.py",
    SCRIPTS_ROOT / "sources" / "curated_boards.py",
    SCRIPTS_ROOT / "sources" / "devex.py",
    SCRIPTS_ROOT / "sources" / "ub_doctoral.py",
    SCRIPTS_ROOT / "sources" / "web_search.py",
    SCRIPTS_ROOT / "telegram_client.py",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _assert(cond: bool, msg: str) -> None:
    status = "  OK  " if cond else "  FAIL"
    print(f"{status} {msg}")
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


def test_claude_cli_defines_constants() -> None:
    section("1. claude_cli.py defines the canonical constants")
    src = _read(SCRIPTS_ROOT / "claude_cli.py")
    for name in _CANONICAL_NAMES:
        # Match an assignment like ``TOOLS_WEB_BOTH = "..."``.
        pat = re.compile(rf"^{re.escape(name)}\s*=\s*\"", re.MULTILINE)
        _assert(
            bool(pat.search(src)),
            f"claude_cli.py defines {name} as a module-level string constant",
        )


def test_each_call_site_imports_canonical_constants() -> None:
    section("2. each tool-granting file imports a canonical constant")
    for path in _TOOL_GRANTING_FILES:
        src = _read(path)
        # Look for ``from claude_cli import ...`` lines that mention any
        # canonical name. We allow multi-line imports by collapsing
        # whitespace inside ``from claude_cli import ( ... )`` blocks.
        # Cheap normalization: read the file, look at any line range
        # that starts with ``from claude_cli import`` and check for the
        # constants in the surrounding 10 lines.
        rel = path.relative_to(SCRIPTS_ROOT)
        imported = False
        # Multi-line ``from claude_cli import (`` block.
        for m in re.finditer(
            r"from\s+claude_cli\s+import\s*(?:\(([^)]*)\)|([^\n]*))",
            src,
        ):
            block = (m.group(1) or m.group(2) or "")
            if any(name in block for name in _CANONICAL_NAMES):
                imported = True
                break
        _assert(
            imported,
            f"{rel} imports at least one canonical TOOLS_* constant "
            f"from claude_cli",
        )


def test_no_banned_literals_outside_exempt() -> None:
    section("3. no ad-hoc tool-grant string literals outside claude_cli")
    # Walk every .py file under the scripts root.
    failures: list[str] = []
    for path in SCRIPTS_ROOT.rglob("*.py"):
        if path in _LITERAL_EXEMPT:
            continue
        # Skip the smoke test for market_research itself — it stubs
        # ``run_p_with_tools`` with comma-separated wire values to
        # exercise the CLI fallback path, which is the format we want.
        src = _read(path)
        for lit in _BANNED_LITERALS:
            if lit in src:
                # Find line number for a clear failure message.
                idx = src.find(lit)
                line_no = src.count("\n", 0, idx) + 1
                rel = path.relative_to(SCRIPTS_ROOT)
                failures.append(f"{rel}:{line_no} contains ad-hoc literal {lit}")
    for f in failures:
        print(f"  FAIL {f}")
    _assert(
        not failures,
        f"no banned literals reintroduced ({len(failures)} drift sites found)",
    )


def test_constants_have_expected_wire_values() -> None:
    section("4. canonical constants have the expected comma-separated values")
    # Import at runtime to confirm the strings are what call sites expect.
    sys.path.insert(0, str(SCRIPTS_ROOT))
    import claude_cli  # noqa: E402

    _assert(
        claude_cli.TOOLS_WEB_BOTH == "WebSearch,WebFetch",
        f"TOOLS_WEB_BOTH == 'WebSearch,WebFetch' (got {claude_cli.TOOLS_WEB_BOTH!r})",
    )
    _assert(
        claude_cli.TOOLS_WEB_FETCH_ONLY == "WebFetch",
        f"TOOLS_WEB_FETCH_ONLY == 'WebFetch' (got {claude_cli.TOOLS_WEB_FETCH_ONLY!r})",
    )
    _assert(
        claude_cli.TOOLS_DENY_SHELL_FS == "Bash,Edit,Write,Read",
        f"TOOLS_DENY_SHELL_FS == 'Bash,Edit,Write,Read' "
        f"(got {claude_cli.TOOLS_DENY_SHELL_FS!r})",
    )
    _assert(
        claude_cli.TOOLS_DENY_WEB_AND_SHELL_FS
        == "WebSearch,WebFetch,Bash,Edit,Write,Read",
        f"TOOLS_DENY_WEB_AND_SHELL_FS combines both lists "
        f"(got {claude_cli.TOOLS_DENY_WEB_AND_SHELL_FS!r})",
    )


def main() -> int:
    test_claude_cli_defines_constants()
    test_each_call_site_imports_canonical_constants()
    test_no_banned_literals_outside_exempt()
    test_constants_have_expected_wire_values()
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
