"""Self-host preflight ("doctor") for the download-and-run bot.

The public bot is BYO-keys. Two independent cost lines, each on YOUR account:

  * SCORING — runs through the `claude` CLI (your Claude Code subscription /
    your ANTHROPIC_API_KEY). Cost is yours, paid to Anthropic directly.
    $0 to this project.
  * SOURCES — when FETCH_BACKEND=apify (the default), the job sources are
    fetched by this project's PUBLISHED Apify actors, run under YOUR
    APIFY_TOKEN against the project's APIFY_ACTOR_OWNER account. You pay Apify
    for the runs; running our maintained actors is what supports the project.
    Set FETCH_BACKEND=local to scrape in-process instead (free, no Apify, but
    you own keeping the brittle scrapers alive).

This module validates that the keys/tools for the selected mode are present,
optionally test-pings one actor, and prints the money flow. Run it before the
first `bot.py` / `search_jobs.py` launch:

    python skill/job-search/scripts/selfhost_preflight.py
    python skill/job-search/scripts/selfhost_preflight.py --ping   # also hit one actor

Exit code 0 = ready; 1 = a REQUIRED check failed.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

import apify_fetch


@dataclass
class Check:
    name: str
    ok: bool
    required: bool
    detail: str


def _check_scoring() -> Check:
    """Scoring needs the `claude` CLI on PATH."""
    claude_ok = shutil.which("claude") is not None
    if claude_ok:
        where = shutil.which("claude")
        return Check("scoring", True, True, f"`claude` CLI found at {where}")
    return Check(
        "scoring", False, True,
        "`claude` CLI not on PATH. Install Claude Code and log in "
        "(https://claude.com/claude-code).",
    )


def _check_apify(backend: str) -> Check:
    """Apify token is REQUIRED only when fetching via the apify backend."""
    token = apify_fetch.resolve_token()
    required = backend == "apify"
    if token:
        owner = os.environ.get("APIFY_ACTOR_OWNER", apify_fetch.DEFAULT_ACTOR_OWNER)
        return Check("apify_token", True, required,
                     f"APIFY_TOKEN resolved; actors run under owner '{owner}'")
    if required:
        return Check("apify_token", False, True,
                     "FETCH_BACKEND=apify but no APIFY_TOKEN. Get one at "
                     "https://console.apify.com/account/integrations and "
                     "`export APIFY_TOKEN=...` (or set FETCH_BACKEND=local).")
    return Check("apify_token", True, False,
                 "FETCH_BACKEND=local — Apify not used (sources scraped in-process)")


def _ping_actor() -> Check:
    """Optional live check: run the cheapest actor (hackernews) for 1 item."""
    token = apify_fetch.resolve_token()
    if not token:
        return Check("apify_ping", False, False, "skipped — no APIFY_TOKEN")
    key = "hackernews"
    owner = os.environ.get("APIFY_ACTOR_OWNER", apify_fetch.DEFAULT_ACTOR_OWNER)
    _k, jobs, err, secs = apify_fetch.call_actor(
        key, {"max_per_source": 1},
        token=token, owner=owner, cache_ttl=0, run_timeout=60, anthropic_key=None,
    )
    if err:
        return Check("apify_ping", False, False,
                     f"actor '{apify_fetch.ACTOR_MAP[key]}' failed: {err}")
    return Check("apify_ping", True, False,
                 f"actor '{apify_fetch.ACTOR_MAP[key]}' returned {len(jobs)} "
                 f"item(s) in {secs:.1f}s")


def gather(*, backend: str | None = None, ping: bool = False) -> dict:
    """Run all checks. Returns ``{backend, provider, checks: [Check], ok}``."""
    backend = (backend or os.environ.get("FETCH_BACKEND") or "apify").strip().lower()
    if backend not in ("local", "apify"):
        backend = "apify"
    checks = [_check_scoring(), _check_apify(backend)]
    if ping and backend == "apify":
        checks.append(_ping_actor())
    ok = all(c.ok for c in checks if c.required)
    return {"backend": backend, "provider": "claude",
            "checks": checks, "ok": ok}


_MONEY_FLOW = """\
How the costs flow (everything below runs on YOUR accounts):
  • Scoring  → your Claude Code subscription / API key. Paid to the model
               provider directly. $0 to this project.
  • Sources  → our published Apify actors, run under your APIFY_TOKEN. You pay
               Apify per run; running our maintained scrapers supports the
               project. Switch off with FETCH_BACKEND=local (free, DIY scraping).
"""


def render(result: dict) -> str:
    lines = [f"Self-host preflight  (FETCH_BACKEND={result['backend']}, "
             f"scoring provider={result['provider']})", ""]
    for c in result["checks"]:
        mark = "✓" if c.ok else "✗"
        tag = "" if c.required else " (optional)"
        lines.append(f"  {mark} {c.name}{tag}: {c.detail}")
    lines.append("")
    lines.append(_MONEY_FLOW)
    lines.append("READY ✓" if result["ok"] else "NOT READY ✗ — fix the required (✗) checks above.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ping = "--ping" in argv
    backend = None
    for a in argv:
        if a.startswith("--backend="):
            backend = a.split("=", 1)[1]
    result = gather(backend=backend, ping=ping)
    print(render(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
