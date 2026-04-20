"""Job dataclass + per-user dedupe against the SQLite DB.

The legacy `state/seen.json` file is no longer used — the DB is the source of
truth now. Everything that used to call `SeenStore.filter_new()` now calls
`JobStore.filter_new_for(chat_id, jobs)` instead.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, asdict
from typing import Iterable

from db import DB


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
