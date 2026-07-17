"""Per-chat search lock, shared by every caller of `search_jobs.run`.

Two independent code paths invoke the pipeline for a given chat_id:
  * `bot.trigger_job_check` — manual "Check Now" / `/jobs` / `/check`.
  * `ContinuousSearcher.run_forever` — the scheduled auto-search loop.

Before this module existed each path had its own (or no) lock, so a manual
check firing while the scheduled iteration was mid-flight ran two
concurrent `search_jobs.run` calls for the same chat. `sent_messages`
dedupe is check-then-act with no `UNIQUE(chat_id, job_id)` constraint (PK
is `(chat_id, message_id)`), so both runs saw "not sent yet" and both
delivered — the user got duplicate job messages and the operator got two
overlapping digests. One registry, shared by both callers, closes the race.
"""
from __future__ import annotations

import threading

_LOCKS: dict[int, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def lock_for(chat_id: int) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(chat_id)
        if lock is None:
            lock = _LOCKS[chat_id] = threading.Lock()
        return lock
