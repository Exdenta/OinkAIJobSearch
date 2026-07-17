"""Chrome-agent fallback tier — drive the OPERATOR's real desktop Chrome.

This is the LAST-RESORT recovery tier, one level beyond the headless-playwright
fallback in ``browser_fetch.py``. Some sites (Cloudflare / DataDome / login-
walled paywalls — wellfound.com, academicpositions.com, devex.com, undp.org)
serve a block or challenge page even to a headless chromium. The operator's
*real* desktop Chrome, however, is already past those defences: it has the
operator's cookies, a warmed-up TLS/JS fingerprint, and (often) a logged-in
session. So instead of launching our own browser, this tier asks ``claude -p
--chrome`` — wired to the ``claude-in-chrome`` MCP — to navigate the operator's
existing Chrome to the URL and extract what we need from the rendered page.

This module exposes TWO entry points, mirroring the two shapes the pipeline
needs:

  * :func:`fetch_listings_via_chrome` — for list/index pages: returns up to
    ``max_items`` posting dicts (``list[dict]``).
  * :func:`fetch_page_text_via_chrome` — for a single detail page: returns the
    cleaned visible body text (``str``).

Hard safety / robustness contract (mirrors ``browser_fetch.py`` and
``sources/_detail_fetch.py``):
  * **GATED + opt-in.** Lazily read ``DEFAULTS["chrome_agent_fallback_enabled"]``
    on every call. If False or absent → return ``[]`` / ``""`` IMMEDIATELY with
    NO subprocess spawned. The default is False because this tier commandeers
    the operator's desktop browser; it must never fire unless explicitly turned
    on. When OFF, every adapter behaves EXACTLY as today.
  * **SSRF first.** Call ``safe_url.is_safe_url(url)`` BEFORE spawning anything;
    on an unsafe URL return ``[]`` / ``""``. The agentic browser bypasses the
    ``safe_request`` per-hop guard, so this is the sole SSRF check on this path.
  * **Never raises.** Any exception (CLI missing, timeout, unparseable output,
    SSRF block) is swallowed → ``[]`` / ``""``. The caller treats ``[]`` / ``""``
    identically to "fallback didn't help" and keeps its original result.
  * **Bounded.** ``timeout_s`` is applied via ``subprocess.run(timeout=...)``;
    on ``TimeoutExpired`` (or any error) we return ``[]`` / ``""``.

DEPLOY NOTE (one-time, NOT done here): the host must have the ``claude`` CLI
with ``--chrome`` support AND the ``claude-in-chrome`` MCP configured, AND a
desktop Chrome running with the claude-in-chrome extension connected. Until all
of that is in place (and ``chrome_agent_fallback_enabled`` is True) this tier
stays dormant and the pipeline behaves exactly as before.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import os
from typing import Any

from safe_url import is_safe_url
from claude_cli import (
    extract_assistant_text,
    parse_json_block,
    TOOLS_DENY_SHELL_FS,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# claude-in-chrome MCP tools this tier is allowed to call. Deliberately narrow:
# navigation + reading the rendered DOM/text/network + browser selection. We do
# NOT grant the mutating tools (form_input, computer, file_upload, javascript
# beyond read-only extraction is still allowed because some SPAs only expose
# content through scripted scroll/extract). Pairing this allow-list with
# ``TOOLS_DENY_SHELL_FS`` keeps the prompt-injection surface minimal: the sub-
# agent is loading untrusted third-party pages, so it must never be able to
# touch the shell or filesystem (see claude_cli.TOOLS_DENY_SHELL_FS rationale).
_CHROME_TOOLS = (
    "mcp__claude-in-chrome__tabs_context_mcp",
    "mcp__claude-in-chrome__tabs_create_mcp",
    "mcp__claude-in-chrome__navigate",
    "mcp__claude-in-chrome__get_page_text",
    "mcp__claude-in-chrome__javascript_tool",
    "mcp__claude-in-chrome__read_network_requests",
    "mcp__claude-in-chrome__select_browser",
    "mcp__claude-in-chrome__list_connected_browsers",
    "mcp__claude-in-chrome__find",
    "mcp__claude-in-chrome__tabs_close_mcp",
)
ALLOWED = ",".join(_CHROME_TOOLS)

# The six listing fields we coerce every job dict to. Order matters only for
# readability; callers index by key.
_LISTING_KEYS = ("title", "company", "location", "url", "posted_at", "snippet")


def _chrome_agent_config() -> tuple[bool, str | None, int]:
    """Read the (enabled, device_id, timeout_s) config from defaults, lazily.

    Lazy + defensive so this module never gains a hard import dependency on
    defaults' shape: any import/lookup problem → disabled, which is exactly
    today's behavior (no subprocess, []/"" returned).
    """
    try:
        from defaults import DEFAULTS
        enabled = bool(DEFAULTS.get("chrome_agent_fallback_enabled", False))
        device_id = DEFAULTS.get("chrome_agent_device_id", "") or None
        timeout_s = int(DEFAULTS.get("chrome_agent_timeout_s", 240) or 240)
        return enabled, device_id, timeout_s
    except Exception:
        return False, None, 240


def _browser_selection_clause(device_id: str | None) -> str:
    """Prompt fragment telling the agent how to pick which Chrome to drive."""
    if device_id:
        return (
            f"First call select_browser with deviceId \"{device_id}\" to target "
            "that specific connected Chrome."
        )
    return (
        "First call tabs_context_mcp to use the active browser group "
        "(do NOT prompt for a device; use the single/active browser)."
    )


def _build_listings_prompt(
    *, url: str, instruction: str, max_items: int, device_id: str | None
) -> str:
    return (
        "You are a headless extraction agent driving a real desktop Chrome via "
        "the claude-in-chrome MCP tools. Do exactly the following, then stop.\n"
        f"{_browser_selection_clause(device_id)}\n"
        "Then create a new tab (tabs_create_mcp) and navigate to the URL below.\n"
        f"URL: {url}\n"
        "IMPORTANT: this is a JavaScript app — the job listings render a few "
        "seconds AFTER the page first loads. Do NOT read the page immediately. "
        "Read it (get_page_text or javascript_tool); if you do not yet see job "
        "cards/listings, wait ~3 seconds and read again, repeating up to 3 times "
        "until listings are present.\n"
        f"Once listings are present, extract up to {max_items} job postings that "
        f"match this instruction: {instruction}\n"
        "When you have the data (or have concluded the page is blocked), close "
        "the tab you created with tabs_close_mcp so tabs do not accumulate.\n"
        "Return STRICT JSON and nothing else (no prose, no code fences):\n"
        '{"jobs":[{"title":"","company":"","location":"","url":"",'
        '"posted_at":"","snippet":""}]}\n'
        'Only return {"jobs":[]} if, after those retries, the page genuinely '
        "shows a block/challenge/captcha or has no matching postings. A "
        "non-blocking cookie banner is NOT a block — extract the listings behind "
        'it. Every field must be a string; use "" when unknown.'
    )


def _build_page_text_prompt(*, url: str, device_id: str | None) -> str:
    return (
        "You are a headless extraction agent driving a real desktop Chrome via "
        "the claude-in-chrome MCP tools. Do exactly the following, then stop.\n"
        f"{_browser_selection_clause(device_id)}\n"
        "Then create a new tab (tabs_create_mcp) and navigate to the URL below.\n"
        f"URL: {url}\n"
        "IMPORTANT: many sites render their main content via JavaScript a few "
        "seconds after the initial load. Do NOT read immediately — read the "
        "page; if the body looks empty or like a loading state, wait ~3 seconds "
        "and read again, up to 3 times, until the real content is present.\n"
        "When you have read the content (or concluded it is blocked), close the "
        "tab you created with tabs_close_mcp so tabs do not accumulate.\n"
        "Return ONLY the cleaned visible body text of the page as plain text "
        "(no HTML, no navigation chrome, no prose of your own, no code fences). "
        "Return an empty response only if the page genuinely shows a "
        "block/challenge or has no content after those retries."
    )


def _run_chrome_agent(prompt: str, *, timeout_s: int) -> str | None:
    """Invoke ``claude -p <prompt> --chrome --output-format json``.

    Returns stdout on a clean (rc==0) run, or None on missing CLI, non-zero
    exit, timeout, or any spawn error. NEVER raises — the caller degrades to
    today's []/"" behavior on None.
    """
    if not shutil.which("claude"):
        log.debug("chrome_agent: `claude` CLI not found on PATH; skipping")
        return None
    cmd = [
        "claude", "-p", prompt,
        "--chrome",
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--allowed-tools", ALLOWED,
        "--disallowed-tools", TOOLS_DENY_SHELL_FS,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=dict(os.environ),
        )
    except subprocess.TimeoutExpired:
        log.info("chrome_agent: timed out after %ds", timeout_s)
        return None
    except Exception as e:
        log.info("chrome_agent: failed to start: %s", e)
        return None
    if proc.returncode != 0:
        log.info(
            "chrome_agent: exit=%s err=%s",
            proc.returncode, (proc.stderr or "")[:200],
        )
        return None
    return proc.stdout or ""


def _coerce_listing(item: Any) -> dict:
    """Coerce one raw job item to the six-key string dict shape."""
    out = {k: "" for k in _LISTING_KEYS}
    if isinstance(item, dict):
        for k in _LISTING_KEYS:
            v = item.get(k, "")
            out[k] = v if isinstance(v, str) else ("" if v is None else str(v))
    return out


def _looks_like_agent_failure_text(text: str) -> bool:
    """True when the Chrome agent returned its own failure/refusal prose.

    ``fetch_page_text_via_chrome`` must return page text or ""; otherwise the
    downstream liveness LLM treats transport errors as job-page evidence.
    """
    s = " ".join((text or "").lower().split())
    return (
        ("chrome" in s and (
            "specified chrome device" in s
            or "no matching browser" in s
            or "no reachable chrome" in s
            or "browser target is unavailable" in s
            or ("deviceid" in s and "not currently connected" in s)
            or ("device" in s and "isn't currently connected" in s)
        ))
        or "i can't proceed with the extraction" in s
        or "i can't complete this task" in s
        or "headless extraction agent" in s
        or "injected prompt" in s
        or "silent scraping instructions" in s
        or ("prompt injection" in s and "not execut" in s)
    )


def fetch_listings_via_chrome(
    *,
    url: str,
    instruction: str,
    max_items: int = 20,
    timeout_s: int | None = None,
    device_id: str | None = None,
) -> list[dict]:
    """Recover a list/index page's postings by driving the operator's Chrome.

    Returns up to ``max_items`` dicts, each with keys ``title``, ``company``,
    ``location``, ``url``, ``posted_at``, ``snippet`` (all ``str``; missing →
    ``""``). Returns ``[]`` on ANY failure (disabled, SSRF-blocked, CLI
    missing, timeout, unparseable output, no matches). NEVER raises.
    """
    enabled, cfg_device, cfg_timeout = _chrome_agent_config()
    if not enabled:
        log.debug("chrome_agent: listings fallback disabled; not spawning")
        return []
    if not url:
        return []
    # SSRF guard FIRST — the agentic browser bypasses safe_request, so this is
    # the only guard on this path and must run before any subprocess.
    ok, reason = is_safe_url(url)
    if not ok:
        log.debug("chrome_agent: SSRF-blocked %s (%s); not spawning", url, reason)
        return []

    device = device_id if device_id is not None else cfg_device
    timeout = int(timeout_s) if timeout_s is not None else cfg_timeout

    prompt = _build_listings_prompt(
        url=url, instruction=instruction, max_items=max_items, device_id=device,
    )
    log.info("chrome_agent: attempting listings fetch %s (timeout %ds)", url, timeout)
    stdout = _run_chrome_agent(prompt, timeout_s=timeout)
    if not stdout:
        return []
    data = parse_json_block(extract_assistant_text(stdout))
    if not isinstance(data, dict):
        log.debug("chrome_agent: listings output not a JSON object for %s", url)
        return []
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        log.debug("chrome_agent: listings output missing 'jobs' array for %s", url)
        return []
    out = [_coerce_listing(j) for j in jobs[:max_items]]
    if out:
        log.info("chrome_agent: recovered %d listings from %s", len(out), url)
    else:
        log.info("chrome_agent: %s returned no listings", url)
    return out


def fetch_page_text_via_chrome(
    *,
    url: str,
    timeout_s: int | None = None,
    device_id: str | None = None,
) -> str:
    """Recover a single page's body text by driving the operator's Chrome.

    Returns the cleaned visible body text, or ``""`` on ANY failure (disabled,
    SSRF-blocked, CLI missing, timeout, empty/unparseable output). NEVER raises.
    """
    enabled, cfg_device, cfg_timeout = _chrome_agent_config()
    if not enabled:
        log.debug("chrome_agent: page-text fallback disabled; not spawning")
        return ""
    if not url:
        return ""
    # SSRF guard FIRST (see fetch_listings_via_chrome).
    ok, reason = is_safe_url(url)
    if not ok:
        log.debug("chrome_agent: SSRF-blocked %s (%s); not spawning", url, reason)
        return ""

    device = device_id if device_id is not None else cfg_device
    timeout = int(timeout_s) if timeout_s is not None else cfg_timeout

    prompt = _build_page_text_prompt(url=url, device_id=device)
    log.info("chrome_agent: attempting page-text fetch %s (timeout %ds)", url, timeout)
    stdout = _run_chrome_agent(prompt, timeout_s=timeout)
    if not stdout:
        return ""
    text = extract_assistant_text(stdout)
    text = (text or "").strip()
    if _looks_like_agent_failure_text(text):
        log.info("chrome_agent: %s returned agent failure text; treating as empty", url)
        return ""
    if text:
        log.info("chrome_agent: recovered page text from %s (%d chars)", url, len(text))
    else:
        log.info("chrome_agent: %s returned empty page text", url)
    return text
