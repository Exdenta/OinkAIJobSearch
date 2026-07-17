"""Pre-enrichment URL liveness verification (algorithm v2.1).

Some sources (LinkedIn, ATS SPAs, web_search) return HTTP 200 even for
closed postings — the page renders "no longer accepting applications"
inside the body but the HEAD probe sees a fine response. This module
runs a Haiku-backed verifier (WebFetch + WebSearch tools) against each
posting BEFORE we score it, so we don't burn enrichment tokens on dead
listings and so the user never sees a closed posting.

Public API:

    verify_listing_open(job, timeout_s=...) -> (bool|None, "<reason>")
        True  → listing visibly open
        False → listing visibly closed (drop)
        None  → cannot tell — caller should let the posting through and
                let the cheaper send-time gates have one more crack.

    filter_alive_jobs(jobs, *, max_workers=8, ...) -> (alive, dead_log)
        Concurrent verification across `jobs`. Returns the alive subset
        plus a list of `(job, reason)` for the dropped ones.

`verify_listing_open` is a thin re-export of the pre-existing
`_web_search_listing_still_open` helper from `telegram_client.py` —
historically it ran only for `source=web_search` jobs at send time, but
the same prompt + tool wiring works for any source. Bringing it
in-process pre-enrich means we filter dead postings BEFORE Haiku/Sonnet
ever sees them.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

log = logging.getLogger(__name__)


# Toggle for the older pre-enrichment verifier. Disabled by default while
# send-time liveness is disabled; set PRE_ENRICH_LIVENESS_OFF=0 to re-enable.
PRE_ENRICH_LIVENESS_OFF: bool = os.environ.get("PRE_ENRICH_LIVENESS_OFF", "1").strip() not in (
    "", "0", "false", "False",
)


def verify_listing_open(
    job, timeout_s: int = 90, *, chat_id: int | None = None,
) -> tuple[bool | None, str]:
    """Decide whether `job.url` still accepts applications.

    Delegates to the long-standing helper in `telegram_client` so we
    keep one source of truth for the prompt + fetch/backend wiring.
    `chat_id` threads through to the `LIVENESS_CLAUDE_CHAT_IDS` rollback
    lever (see `telegram_client._liveness_forced_to_claude`).
    """
    try:
        from telegram_client import _web_search_listing_still_open as _check
    except ImportError:
        return (None, "unknown:telegram_client_import")
    try:
        return _check(job, timeout_s=timeout_s, chat_id=chat_id)
    except Exception as e:
        log.warning("liveness: verifier raised on %s: %s", job.url, e)
        return (None, f"unknown:exception:{type(e).__name__}")


def filter_alive_jobs(
    jobs: Iterable,
    *,
    max_workers: int = 8,
    timeout_s: int = 90,
    chat_id: int | None = None,
    forensic_logger=None,
) -> tuple[list, list[tuple[object, str]]]:
    """Run `verify_listing_open` concurrently across `jobs`.

    Returns:
        alive:    list of jobs the verifier said `open` OR `unknown`.
                  Unknown verdicts are KEPT — never drop on uncertainty.
        dead_log: list of (job, reason) for jobs the verifier said
                  `closed`.

    `forensic_logger` is an optional `forensic.log_step`-style callable
    used to emit one `liveness.checked` line per job.
    """
    jobs = list(jobs)
    if PRE_ENRICH_LIVENESS_OFF or not jobs:
        return jobs, []

    alive: list = []
    dead: list[tuple[object, str]] = []

    def _one(j):
        return j, verify_listing_open(j, timeout_s=timeout_s, chat_id=chat_id)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_one, j) for j in jobs]
        for fut in as_completed(futures):
            try:
                j, (status, reason) = fut.result()
            except Exception as e:
                log.warning("liveness: future raised: %s", e)
                continue
            if forensic_logger is not None:
                try:
                    forensic_logger(
                        "liveness.checked",
                        input={
                            "job_id": getattr(j, "job_id", ""),
                            "source": getattr(j, "source", ""),
                            "url": getattr(j, "url", "")[:200],
                            "title": (getattr(j, "title", "") or "")[:120],
                        },
                        output={"status": str(status), "reason": reason},
                        chat_id=chat_id,
                    )
                except Exception:
                    log.debug("liveness: forensic emit failed", exc_info=True)
            if status is False:
                dead.append((j, reason))
            else:
                # True or None → let through. We never drop on uncertainty.
                alive.append(j)

    log.info(
        "liveness: %d/%d alive · %d dropped · pool=%d",
        len(alive), len(jobs), len(dead), max_workers,
    )
    return alive, dead
