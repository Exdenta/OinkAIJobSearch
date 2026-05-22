"""Job dataclass + per-user dedupe against the SQLite DB.

The legacy `state/seen.json` file is no longer used — the DB is the source of
truth now. Everything that used to call `SeenStore.filter_new()` now calls
`JobStore.filter_new_for(chat_id, jobs)` instead.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict
from typing import Iterable

from db import DB


# Regex used by `_normalize_field`. Compiled once at module load.
# Matches a trailing parenthesised suffix like ``(Remote)`` / ``(EU)`` /
# ``(m/f/d)`` — common ATS noise that source feeds tack onto the title.
# Multiple trailing parens are stripped iteratively in `_normalize_field`.
_TRAILING_PAREN_RE = re.compile(r"\s*\([^()]*\)\s*$")
# Collapse any run of whitespace (spaces, tabs, NBSP, etc.) to a single
# ASCII space. The `\s` class covers them all under default `re` flags.
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_field(s: str | None) -> str:
    """Normalize a single keying field for cross-source dedupe.

    Rules — minimal, transport-layer ONLY (see `dedupe_cross_source` for
    the justification — these are closed-set facts about how feeds format
    the SAME posting, not heuristics about whether a job is a good fit):

      * lowercase
      * strip leading/trailing whitespace
      * iteratively strip trailing parenthesised suffixes
        (``"Foo (Remote)"`` → ``"foo"``,
         ``"Foo (m/f/d) (Remote)"`` → ``"foo"``)
      * collapse any internal whitespace run to a single space

    Returns "" when input is None or empty.
    """
    if not s:
        return ""
    t = str(s).strip().lower()
    # Strip trailing paren groups iteratively so "Foo (m/f/d) (Remote)"
    # collapses fully. The regex anchors to end-of-string and matches a
    # single bracketed group; a while-loop applies it repeatedly until
    # no more trailing groups remain.
    while True:
        nxt = _TRAILING_PAREN_RE.sub("", t)
        if nxt == t:
            break
        t = nxt.rstrip()
    t = _WHITESPACE_RE.sub(" ", t).strip()
    return t


def dedupe_cross_source(jobs: list["Job"]) -> list["Job"]:
    """Collapse postings that appear in multiple feeds.

    Keying: ``(_normalize_field(company), _normalize_field(title),
    _normalize_field(location))``. The same engineering role from Acme
    posted to justjoinit, nofluffjobs, AND LinkedIn-PL surfaces as three
    rows with different `source` / `external_id` / `url` but identical
    normalised keys — collapsed to one.

    For each cluster we KEEP the job whose ``snippet`` is the longest
    (a cheap proxy for "richest body" — the version most likely to
    survive Sonnet's contextual-fit reasoning intact). Ties are broken
    by input order (first occurrence wins).

    Returns a new list, preserving the relative order of the kept jobs
    as they appeared in the input.

    Design-principle note (CLAUDE.md — "prefer AI, avoid hardcoded
    heuristics"):
    ============================================================
    This dedupe operates at the TRANSPORT layer — it is collapsing
    DUPLICATE POSTINGS that two or more feeds independently report,
    not making a fit judgement about any job. The normalisation rules
    (lowercase, strip trailing parens, collapse whitespace) are closed-
    set facts about how source feeds format the same posting:
    LinkedIn appends " (Remote)" to titles, justjoinit normalises
    locations to lowercase, nofluffjobs sometimes double-spaces titles.
    None of these rules judge whether a posting is a good match for
    any user — that remains entirely the LLM scorer's job, which still
    runs against the (single) representative job we keep per cluster.
    ============================================================
    """
    if not jobs:
        return []

    # `clusters` keeps the BEST job per key. Insertion order in a Python
    # 3.7+ dict equals first-seen order, which is exactly the ordering
    # contract we want for the output.
    clusters: dict[tuple[str, str, str], "Job"] = {}
    collapsed = 0
    for j in jobs:
        key = (
            _normalize_field(getattr(j, "company", "")),
            _normalize_field(getattr(j, "title", "")),
            _normalize_field(getattr(j, "location", "")),
        )
        cur = clusters.get(key)
        if cur is None:
            clusters[key] = j
            continue
        collapsed += 1
        # Replace only when the candidate has a STRICTLY longer snippet.
        # Ties keep the first occurrence (preserves input ordering and
        # avoids churn in deterministic tests).
        if len(getattr(j, "snippet", "") or "") > len(getattr(cur, "snippet", "") or ""):
            clusters[key] = j
    # `collapsed` is captured as a return-side effect via the caller's log
    # statement; we just return the kept jobs in insertion order.
    return list(clusters.values())


@dataclass
class Job:
    source: str
    external_id: str
    title: str
    company: str
    location: str
    url: str
    posted_at: str
    snippet: str = ""
    salary: str = ""

    @property
    def job_id(self) -> str:
        raw = f"{self.source}::{self.external_id or self.url}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    def as_db_dict(self) -> dict:
        d = asdict(self)
        d["job_id"] = self.job_id
        return d


class JobStore:
    """Thin facade around db.DB for the orchestrator's needs."""

    def __init__(self, db: DB):
        self.db = db

    def save_all(self, jobs: Iterable[Job]) -> int:
        """Upsert every job into the jobs table. Returns count of NEW rows."""
        new_count = 0
        for j in jobs:
            if self.db.upsert_job(j.as_db_dict()):
                new_count += 1
        return new_count

    def filter_new_for(self, chat_id: int, jobs: Iterable[Job]) -> list[Job]:
        """Return jobs that this user hasn't yet been sent AND hasn't already
        applied/skipped.
        """
        handled = self.db.handled_job_ids(chat_id)
        out: list[Job] = []
        for j in jobs:
            if j.job_id in handled:
                continue
            if self.db.user_has_seen_job(chat_id, j.job_id):
                continue
            out.append(j)
        return out
