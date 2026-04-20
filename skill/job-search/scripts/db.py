"""SQLite persistence layer.

Schema
------
users           — Telegram users who've onboarded (one row per chat_id).
jobs            — Every job posting we've ever seen, across all users/sources.
applications    — Per-user, per-job status ("applied" | "skipped" | "interested").
sent_messages   — Maps a sent Telegram message → the job_id it represents, so we
                  can resolve a button press back to a job.
profile_builds  — Audit log of Opus profile rebuild attempts (success or fail).

We use a single DB file at state/jobs.db. All methods are synchronous — low
volume personal bot, no need for async.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id          INTEGER PRIMARY KEY,
    username         TEXT,
    first_name       TEXT,
    last_name        TEXT,
    resume_path      TEXT,
    resume_text      TEXT,
    -- Raw free-text description from /prefs. Fed into the Opus profile
    -- builder alongside the resume. Kept separate from user_profile so
    -- it survives profile rebuilds.
    prefs_free_text  TEXT,
    -- The user's structured profile (Opus-built). Serialized JSON; see
    -- user_profile.py for shape.
    user_profile     TEXT,
    profile_revision INTEGER DEFAULT 0,
    profile_built_at REAL,
    -- Conversational state (e.g. 'awaiting_prefs') for the bot's text handler.
    awaiting_state   TEXT,
    -- Guided-onboarding progress. JSON blob (see onboarding.py): current step,
    -- collected answers (role / seniority / remote / location / min_score),
    -- started_at, last_step_at. NULL once the wizard is complete or was never
    -- started.
    onboarding_state        TEXT,
    onboarding_completed_at REAL,
    registered_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    external_id   TEXT,
    title         TEXT,
    company       TEXT,
    location      TEXT,
    url           TEXT,
    posted_at     TEXT,
    snippet       TEXT,
    salary        TEXT,
    first_seen_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    chat_id    INTEGER NOT NULL,
    job_id     TEXT    NOT NULL,
    status     TEXT    NOT NULL,    -- applied | skipped | interested
    updated_at REAL    NOT NULL,
    PRIMARY KEY (chat_id, job_id)
);

CREATE TABLE IF NOT EXISTS sent_messages (
    chat_id    INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    job_id     TEXT    NOT NULL,
    sent_at    REAL    NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE TABLE IF NOT EXISTS resume_suggestions (
    chat_id      INTEGER NOT NULL,
    job_id       TEXT    NOT NULL,
    plan_json    TEXT    NOT NULL,   -- serialized {summary, suggestions, tailored_resume_markdown}
    status       TEXT    NOT NULL,   -- pending | applied | dismissed
    message_id   INTEGER,            -- message showing the suggestions dialog (for edits)
    updated_at   REAL    NOT NULL,
    PRIMARY KEY (chat_id, job_id)
);

-- Cache of fit-analysis results. One row per (user, job); refreshed when
-- the user's resume changes. `resume_sha1` is the cache-invalidation key.
-- Callers can always bypass the cache by passing force=True; otherwise
-- tapping "Analyze fit" twice is near-instant after the first hit.
CREATE TABLE IF NOT EXISTS fit_analyses (
    chat_id       INTEGER NOT NULL,
    job_id        TEXT    NOT NULL,
    analysis_json TEXT    NOT NULL,   -- serialized normalized fit_analyzer dict
    resume_sha1   TEXT,                -- hash of resume_text at analysis time
    updated_at    REAL    NOT NULL,
    PRIMARY KEY (chat_id, job_id)
);

-- Audit trail of profile_builder runs. One row per Opus rebuild attempt,
-- success or failure. The live profile lives on users.user_profile; this
-- table is for "did a build happen, how did it go, how long did it take".
CREATE TABLE IF NOT EXISTS profile_builds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    trigger       TEXT    NOT NULL,    -- resume_upload | prefs_change | manual
    status        TEXT    NOT NULL,    -- ok | timeout | parse_error | validation_error | cli_missing | exception
    elapsed_ms    INTEGER,
    resume_sha1   TEXT,
    prefs_sha1    TEXT,
    model         TEXT,
    error_head    TEXT,                -- first 200 chars of any error
    profile_json  TEXT,                -- full profile on success, NULL otherwise
    built_at      REAL    NOT NULL
);

-- Audit trail of /marketresearch runs. One row per orchestrator invocation,
-- success or failure. The rendered DOCX (when present) lives on disk under
-- state/users/<chat_id>/research/; docx_path stores the absolute path so the
-- user can find previous reports even after the in-memory ResearchRun is gone.
CREATE TABLE IF NOT EXISTS research_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    status         TEXT    NOT NULL,     -- ok | partial | failed | exception | cli_missing
    location_used  TEXT,
    model          TEXT,
    elapsed_ms     INTEGER,
    workers_ok     TEXT,                  -- JSON array of topic strings
    workers_failed TEXT,                  -- JSON array of {topic, status, error_head}
    docx_path      TEXT,                  -- absolute path to the saved .docx (if any)
    resume_sha1    TEXT,
    prefs_sha1     TEXT,
    error_head     TEXT,
    started_at     REAL    NOT NULL,
    finished_at    REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_app_status ON applications(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_sent_job ON sent_messages(chat_id, job_id);
CREATE INDEX IF NOT EXISTS idx_profile_builds_chat ON profile_builds(chat_id, built_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_runs_chat ON research_runs(chat_id, finished_at DESC);
"""


class DB:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    def _migrate(self, c: sqlite3.Connection) -> None:
        """Idempotent schema migrations.

        `CREATE TABLE IF NOT EXISTS` won't add columns to a pre-existing
        users table, so we ALTER TABLE here defensively. This function must
        be safe to re-run on any DB state (fresh, partially-migrated, fully
        migrated) — every step is guarded on PRAGMA table_info.

        Historical note: early builds stored a flat v1 prefs dict in
        `user_prefs`, then grew a parallel Opus-built profile in
        `user_profile_v2` gated by `consume_profile_v2` / `v2_opt_out`.
        This migration collapses that history to a single `user_profile`
        column and drops the rollout bookkeeping.
        """
        have_cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}

        # Ensure canonical columns exist on pre-existing DBs where
        # `CREATE TABLE IF NOT EXISTS` above was a no-op.
        if "user_profile" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN user_profile TEXT")
        if "prefs_free_text" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN prefs_free_text TEXT")
        if "awaiting_state" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN awaiting_state TEXT")
        if "profile_revision" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN profile_revision INTEGER DEFAULT 0")
        if "profile_built_at" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN profile_built_at REAL")
        # Guided-onboarding wizard columns. Added late — pre-existing users
        # will have these as NULL (= never onboarded via the wizard), which
        # the onboarding module treats as "offer to run the wizard" on first
        # /start but leaves silent on day-to-day interactions.
        if "onboarding_state" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN onboarding_state TEXT")
        if "onboarding_completed_at" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN onboarding_completed_at REAL")

        # Refresh; the ADD COLUMN statements above may have changed things.
        have_cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}

        # One-time migration from the old v2 column name.
        if "user_profile_v2" in have_cols:
            c.execute(
                "UPDATE users SET user_profile = user_profile_v2 "
                "WHERE user_profile IS NULL AND user_profile_v2 IS NOT NULL"
            )
            c.execute("ALTER TABLE users DROP COLUMN user_profile_v2")

        # One-time migration: the old user_prefs JSON had a `free_text` subfield
        # that we want to preserve as the raw free-text input for future
        # rebuilds. Pull it out before we drop the column.
        if "user_prefs" in have_cols:
            for row in c.execute(
                "SELECT chat_id, user_prefs FROM users "
                "WHERE user_prefs IS NOT NULL AND user_prefs <> ''"
            ).fetchall():
                try:
                    import json as _json
                    parsed = _json.loads(row["user_prefs"]) or {}
                except (TypeError, ValueError):
                    continue
                ft = (parsed or {}).get("free_text")
                if isinstance(ft, str) and ft.strip():
                    c.execute(
                        "UPDATE users SET prefs_free_text = ? "
                        "WHERE chat_id = ? AND (prefs_free_text IS NULL OR prefs_free_text = '')",
                        (ft.strip(), row["chat_id"]),
                    )

        # Drop legacy v1 / rollout columns. Requires SQLite ≥ 3.35.
        for legacy in ("user_prefs", "consume_profile_v2", "v2_opt_out"):
            if legacy in have_cols:
                c.execute(f"ALTER TABLE users DROP COLUMN {legacy}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, detect_types=sqlite3.PARSE_DECLTYPES, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- users ----------

    def upsert_user(
        self,
        chat_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO users (chat_id, username, first_name, last_name, registered_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name
                """,
                (chat_id, username, first_name, last_name, time.time()),
            )

    def set_resume(self, chat_id: int, resume_path: str, resume_text: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE users SET resume_path = ?, resume_text = ? WHERE chat_id = ?",
                (resume_path, resume_text, chat_id),
            )

    def get_user(self, chat_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()

    def all_users(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM users"))

    def users_with_resume(self) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM users WHERE resume_path IS NOT NULL AND resume_path <> ''"
            ))

    # ---------- prefs free-text (raw input for the profile builder) ----------

    def set_prefs_free_text(self, chat_id: int, text: str | None) -> None:
        """Persist the raw /prefs description verbatim. Pass None to clear."""
        with self._conn() as c:
            c.execute(
                "UPDATE users SET prefs_free_text = ? WHERE chat_id = ?",
                (text, chat_id),
            )

    def get_prefs_free_text(self, chat_id: int) -> str | None:
        """Return the raw /prefs description, or None if unset."""
        with self._conn() as c:
            row = c.execute(
                "SELECT prefs_free_text FROM users WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if row is None:
                return None
            return row["prefs_free_text"]

    # ---------- user profile (Opus-built, structured) ----------

    def set_user_profile(
        self,
        chat_id: int,
        profile_json: str | None,
        built_at: float | None = None,
    ) -> int:
        """Persist the user's profile and bump profile_revision.

        Returns the new revision number. Pass profile_json=None to clear.
        `built_at` defaults to now() so callers don't have to compute it.
        """
        if built_at is None:
            built_at = time.time()
        with self._conn() as c:
            c.execute(
                """
                UPDATE users
                SET user_profile     = ?,
                    profile_built_at = ?,
                    profile_revision = COALESCE(profile_revision, 0) + 1
                WHERE chat_id = ?
                """,
                (profile_json, built_at, chat_id),
            )
            row = c.execute(
                "SELECT profile_revision FROM users WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            return int(row["profile_revision"] or 0) if row else 0

    def get_user_profile(self, chat_id: int) -> str | None:
        """Return the raw profile JSON string, or None if unset / unknown user."""
        with self._conn() as c:
            row = c.execute(
                "SELECT user_profile FROM users WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if row is None:
                return None
            return row["user_profile"]

    def log_profile_build(
        self,
        chat_id: int,
        trigger: str,
        status: str,
        *,
        elapsed_ms: int | None = None,
        resume_sha1: str | None = None,
        prefs_sha1: str | None = None,
        model: str | None = None,
        error_head: str | None = None,
        profile_json: str | None = None,
    ) -> int:
        """Append a row to profile_builds. Returns the new row id."""
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO profile_builds
                  (chat_id, trigger, status, elapsed_ms, resume_sha1, prefs_sha1,
                   model, error_head, profile_json, built_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id, trigger, status,
                    elapsed_ms, resume_sha1, prefs_sha1,
                    model,
                    (error_head or "")[:200] if error_head else None,
                    profile_json,
                    time.time(),
                ),
            )
            return int(cur.lastrowid or 0)

    def last_profile_build(self, chat_id: int) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                """
                SELECT * FROM profile_builds
                WHERE chat_id = ?
                ORDER BY built_at DESC LIMIT 1
                """,
                (chat_id,),
            ).fetchone()

    def recent_profile_builds(self, limit: int = 20) -> list[sqlite3.Row]:
        """Most-recent N rows across all users — used by the /stats admin cmd."""
        with self._conn() as c:
            return list(c.execute(
                "SELECT * FROM profile_builds ORDER BY built_at DESC LIMIT ?",
                (int(limit),),
            ))

    # ---------- market-research runs ----------

    def log_research_run(
        self,
        chat_id: int,
        status: str,
        *,
        location_used: str | None = None,
        model: str | None = None,
        elapsed_ms: int | None = None,
        workers_ok: list | None = None,
        workers_failed: list | None = None,
        docx_path: str | None = None,
        resume_sha1: str | None = None,
        prefs_sha1: str | None = None,
        error_head: str | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> int:
        """Append a row to research_runs. Returns the new row id.

        `workers_ok` / `workers_failed` accept Python lists and are
        JSON-serialized for storage — callers hand in the raw
        `ResearchRun.workers_ok` / `.workers_failed` values directly. Pass
        `None` to store SQL NULL. `started_at` / `finished_at` default to
        `time.time()` so trivial callers don't have to compute them.
        """
        import json as _json
        now = time.time()
        if started_at is None:
            started_at = now
        if finished_at is None:
            finished_at = now

        def _ser(v):
            if v is None:
                return None
            try:
                return _json.dumps(v, ensure_ascii=False)
            except (TypeError, ValueError):
                return None

        with self._conn() as c:
            cur = c.execute(
                """
                INSERT INTO research_runs
                  (chat_id, status, location_used, model, elapsed_ms,
                   workers_ok, workers_failed, docx_path,
                   resume_sha1, prefs_sha1, error_head,
                   started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id, status, location_used, model, elapsed_ms,
                    _ser(workers_ok), _ser(workers_failed), docx_path,
                    resume_sha1, prefs_sha1,
                    (error_head or "")[:200] if error_head else None,
                    float(started_at), float(finished_at),
                ),
            )
            return int(cur.lastrowid or 0)

    def last_research_run(self, chat_id: int) -> sqlite3.Row | None:
        """Most-recent research_runs row for this user, or None."""
        with self._conn() as c:
            return c.execute(
                """
                SELECT * FROM research_runs
                WHERE chat_id = ?
                ORDER BY finished_at DESC LIMIT 1
                """,
                (chat_id,),
            ).fetchone()

    def recent_research_runs(self, chat_id: int, limit: int = 20) -> list[sqlite3.Row]:
        """Most-recent N research_runs rows for this user."""
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT * FROM research_runs
                WHERE chat_id = ?
                ORDER BY finished_at DESC LIMIT ?
                """,
                (chat_id, int(limit)),
            ))

    def count_research_runs(self, chat_id: int) -> int:
        """Count of research_runs rows for this user."""
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM research_runs WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            return int(row["n"] or 0) if row else 0

    def delete_research_runs(self, chat_id: int) -> int:
        """Delete every research_runs row for this user. Returns rows removed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM research_runs WHERE chat_id = ?", (chat_id,),
            )
            return int(cur.rowcount or 0)

    # ---------- per-user cleanup ("🧹 Clean my data") ----------
    #
    # Each helper is narrowly scoped so the clean-data menu can wipe one
    # category at a time without touching siblings. They do NOT delete the
    # user row itself (chat_id + username survive) so the user can keep
    # using the bot without re-running /start unless they choose the full
    # wipe path, which delete_user() covers.
    #
    # Filesystem cleanup (resume PDF, tailored notes) lives in bot.py —
    # the DB layer stays storage-agnostic.

    def clear_resume(self, chat_id: int) -> None:
        """Null out resume_path + resume_text. Does NOT touch disk files."""
        with self._conn() as c:
            c.execute(
                "UPDATE users SET resume_path = NULL, resume_text = NULL WHERE chat_id = ?",
                (chat_id,),
            )

    def clear_user_profile(self, chat_id: int) -> None:
        """Wipe the profile + its bookkeeping fields AND the raw /prefs
        free-text that drove it. profile_builds history is preserved (it's
        an audit log, not user data the person identifies with) — if you
        need to blow that away too, use the full wipe path."""
        with self._conn() as c:
            c.execute(
                """
                UPDATE users
                SET user_profile     = NULL,
                    prefs_free_text  = NULL,
                    profile_built_at = NULL,
                    profile_revision = 0
                WHERE chat_id = ?
                """,
                (chat_id,),
            )

    def delete_applications(self, chat_id: int) -> int:
        """Delete every applied/skipped/interested row for this user.
        Returns the number of rows removed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM applications WHERE chat_id = ?", (chat_id,),
            )
            return int(cur.rowcount or 0)

    def delete_sent_messages(self, chat_id: int) -> int:
        """Delete the per-user digest sent-log. Returns rows removed.

        Heads up: wiping this means postings the user had already seen
        become eligible for re-sending on the next digest run (the dedupe
        gate in JobStore.filter_new_for reads this table)."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM sent_messages WHERE chat_id = ?", (chat_id,),
            )
            return int(cur.rowcount or 0)

    def delete_suggestions(self, chat_id: int) -> int:
        """Delete the user's stored tailor plans. Returns rows removed.

        The on-disk rendered resume markdown files are cleaned separately —
        this only covers the resume_suggestions table."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM resume_suggestions WHERE chat_id = ?", (chat_id,),
            )
            return int(cur.rowcount or 0)

    def delete_profile_builds(self, chat_id: int) -> int:
        """Delete this user's profile_builds audit rows. Used only by the
        full-wipe path — the per-category cleaner keeps build history."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM profile_builds WHERE chat_id = ?", (chat_id,),
            )
            return int(cur.rowcount or 0)

    def delete_user(self, chat_id: int) -> None:
        """Full wipe: every row in every table that references this chat_id.

        Leaves the `jobs` table alone (jobs are shared across users; a single
        user's goodbye should not evict postings other users can still see).
        The `users` row itself goes too — the user reverts to "never seen" and
        will need to /start again to onboard a new account."""
        with self._conn() as c:
            c.execute("DELETE FROM applications    WHERE chat_id = ?", (chat_id,))
            c.execute("DELETE FROM sent_messages   WHERE chat_id = ?", (chat_id,))
            c.execute("DELETE FROM resume_suggestions WHERE chat_id = ?", (chat_id,))
            c.execute("DELETE FROM fit_analyses    WHERE chat_id = ?", (chat_id,))
            c.execute("DELETE FROM profile_builds  WHERE chat_id = ?", (chat_id,))
            c.execute("DELETE FROM research_runs   WHERE chat_id = ?", (chat_id,))
            c.execute("DELETE FROM users           WHERE chat_id = ?", (chat_id,))

    def count_user_data(self, chat_id: int) -> dict:
        """Snapshot of per-user storage — used by the clean-data menu so the
        user can see what's there before they wipe anything.

        Returns keys:
            has_resume, has_profile, has_free_text,
            applications, sent_messages, suggestions, research_runs
        """
        with self._conn() as c:
            row = c.execute(
                """
                SELECT
                    (resume_path     IS NOT NULL AND resume_path     <> '') AS has_resume,
                    (user_profile    IS NOT NULL AND user_profile    <> '') AS has_profile,
                    (prefs_free_text IS NOT NULL AND prefs_free_text <> '') AS has_free_text
                FROM users WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            n_apps = c.execute(
                "SELECT COUNT(*) AS n FROM applications    WHERE chat_id = ?", (chat_id,),
            ).fetchone()["n"]
            n_sent = c.execute(
                "SELECT COUNT(*) AS n FROM sent_messages   WHERE chat_id = ?", (chat_id,),
            ).fetchone()["n"]
            n_sugg = c.execute(
                "SELECT COUNT(*) AS n FROM resume_suggestions WHERE chat_id = ?", (chat_id,),
            ).fetchone()["n"]
            n_rr = c.execute(
                "SELECT COUNT(*) AS n FROM research_runs WHERE chat_id = ?", (chat_id,),
            ).fetchone()["n"]
        return {
            "has_resume":    bool(row["has_resume"])    if row else False,
            "has_profile":   bool(row["has_profile"])   if row else False,
            "has_free_text": bool(row["has_free_text"]) if row else False,
            "applications":  int(n_apps or 0),
            "sent_messages": int(n_sent or 0),
            "suggestions":   int(n_sugg or 0),
            "research_runs": int(n_rr or 0),
        }

    # ---------- guided onboarding wizard ----------
    #
    # The onboarding wizard stores its transient state as a JSON blob in
    # users.onboarding_state. Shape (see onboarding.py for the authoritative
    # definition):
    #
    #   {
    #     "step": "role" | "remote" | "location" | "minscore" | "preview" | "done",
    #     "answers": {
    #       "role": "...",
    #       "seniority": "junior"|"mid"|"senior"|"staff"|"principal",
    #       "remote": "remote"|"hybrid"|"onsite"|"any",
    #       "location": "...",
    #       "min_score": 0..5,
    #     },
    #     "started_at": <float>,
    #     "last_step_at": <float>,
    #     "completed_at": <float|null>
    #   }
    #
    # We keep this as a single JSON column (not a wide schema) because the
    # wizard is short-lived, the shape evolves, and the bot already uses the
    # same pattern for user_profile.

    def set_onboarding_state(self, chat_id: int, state_json: str | None) -> None:
        """Persist the onboarding wizard's state. Pass None to clear (used on
        finish / abort)."""
        with self._conn() as c:
            c.execute(
                "UPDATE users SET onboarding_state = ? WHERE chat_id = ?",
                (state_json, chat_id),
            )

    def get_onboarding_state(self, chat_id: int) -> str | None:
        """Return the raw onboarding JSON blob, or None if unset / unknown."""
        with self._conn() as c:
            row = c.execute(
                "SELECT onboarding_state FROM users WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if row is None:
                return None
            return row["onboarding_state"]

    def mark_onboarding_complete(self, chat_id: int, completed_at: float | None = None) -> None:
        """Stamp users.onboarding_completed_at and clear onboarding_state. Idempotent."""
        if completed_at is None:
            completed_at = time.time()
        with self._conn() as c:
            c.execute(
                """
                UPDATE users
                SET onboarding_completed_at = COALESCE(onboarding_completed_at, ?),
                    onboarding_state = NULL
                WHERE chat_id = ?
                """,
                (completed_at, chat_id),
            )

    def get_onboarding_completed_at(self, chat_id: int) -> float | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT onboarding_completed_at FROM users WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if row is None:
                return None
            val = row["onboarding_completed_at"]
            return float(val) if val is not None else None

    # ---------- awaiting-state (bot conversational state) ----------

    def set_awaiting_state(self, chat_id: int, state: str | None) -> None:
        """e.g. 'awaiting_prefs' while the bot expects the next text message to
        be the user's free-form preferences. Pass None to clear."""
        with self._conn() as c:
            c.execute(
                "UPDATE users SET awaiting_state = ? WHERE chat_id = ?",
                (state, chat_id),
            )

    def get_awaiting_state(self, chat_id: int) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT awaiting_state FROM users WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if row is None:
                return None
            return row["awaiting_state"]

    # ---------- jobs ----------

    def upsert_job(self, job: dict) -> bool:
        """Insert the job if new. Returns True if inserted, False if already existed."""
        now = time.time()
        with self._conn() as c:
            cur = c.execute(
                """
                INSERT OR IGNORE INTO jobs
                (job_id, source, external_id, title, company, location, url, posted_at, snippet, salary, first_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["job_id"], job["source"], job.get("external_id"),
                    job.get("title"), job.get("company"), job.get("location"),
                    job.get("url"), job.get("posted_at"), job.get("snippet"),
                    job.get("salary"), now,
                ),
            )
            return cur.rowcount > 0

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    def is_known_job(self, job_id: str) -> bool:
        return self.get_job(job_id) is not None

    # ---------- applications ----------

    def set_application_status(self, chat_id: int, job_id: str, status: str) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO applications (chat_id, job_id, status, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, job_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (chat_id, job_id, status, time.time()),
            )

    def get_application_status(self, chat_id: int, job_id: str) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT status FROM applications WHERE chat_id = ? AND job_id = ?",
                (chat_id, job_id),
            ).fetchone()
            return row["status"] if row else None

    def applied_job_ids(self, chat_id: int) -> set[str]:
        with self._conn() as c:
            return {
                r["job_id"]
                for r in c.execute(
                    "SELECT job_id FROM applications WHERE chat_id = ? AND status = 'applied'",
                    (chat_id,),
                )
            }

    def handled_job_ids(self, chat_id: int) -> set[str]:
        """Job ids the user has actioned in any way — applied, skipped, interested."""
        with self._conn() as c:
            return {
                r["job_id"]
                for r in c.execute(
                    "SELECT job_id FROM applications WHERE chat_id = ?", (chat_id,)
                )
            }

    def applied_jobs(self, chat_id: int) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute(
                """
                SELECT j.*, a.updated_at AS applied_at
                FROM applications a
                JOIN jobs j ON j.job_id = a.job_id
                WHERE a.chat_id = ? AND a.status = 'applied'
                ORDER BY a.updated_at DESC
                """,
                (chat_id,),
            ))

    # ---------- sent_messages ----------

    def log_sent(self, chat_id: int, message_id: int, job_id: str) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO sent_messages (chat_id, message_id, job_id, sent_at)
                VALUES (?, ?, ?, ?)
                """,
                (chat_id, message_id, job_id, time.time()),
            )

    def job_id_for_message(self, chat_id: int, message_id: int) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT job_id FROM sent_messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            ).fetchone()
            return row["job_id"] if row else None

    def user_has_seen_job(self, chat_id: int, job_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM sent_messages WHERE chat_id = ? AND job_id = ? LIMIT 1",
                (chat_id, job_id),
            ).fetchone()
            return row is not None

    # ---------- resume_suggestions ----------

    def upsert_suggestion(
        self,
        chat_id: int,
        job_id: str,
        plan_json: str,
        status: str = "pending",
        message_id: int | None = None,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO resume_suggestions (chat_id, job_id, plan_json, status, message_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, job_id) DO UPDATE SET
                    plan_json = excluded.plan_json,
                    status = excluded.status,
                    message_id = COALESCE(excluded.message_id, resume_suggestions.message_id),
                    updated_at = excluded.updated_at
                """,
                (chat_id, job_id, plan_json, status, message_id, time.time()),
            )

    def get_suggestion(self, chat_id: int, job_id: str) -> sqlite3.Row | None:
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM resume_suggestions WHERE chat_id = ? AND job_id = ?",
                (chat_id, job_id),
            ).fetchone()

    def set_suggestion_status(self, chat_id: int, job_id: str, status: str) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE resume_suggestions SET status = ?, updated_at = ? WHERE chat_id = ? AND job_id = ?",
                (status, time.time(), chat_id, job_id),
            )

    # ---------- fit_analyses (cache for "Analyze fit" button) ----------

    def upsert_fit_analysis(
        self,
        chat_id: int,
        job_id: str,
        analysis_json: str,
        resume_sha1: str | None = None,
    ) -> None:
        """Store a fit-analysis result for this (user, job). Overwrites any
        previous entry so the cache always holds the most recent analysis."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO fit_analyses (chat_id, job_id, analysis_json, resume_sha1, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, job_id) DO UPDATE SET
                    analysis_json = excluded.analysis_json,
                    resume_sha1   = excluded.resume_sha1,
                    updated_at    = excluded.updated_at
                """,
                (chat_id, job_id, analysis_json, resume_sha1, time.time()),
            )

    def get_fit_analysis(
        self,
        chat_id: int,
        job_id: str,
        current_resume_sha1: str | None = None,
    ) -> sqlite3.Row | None:
        """Return the cached analysis row iff it exists AND (when
        current_resume_sha1 is provided) the resume hasn't changed since the
        analysis was written. Caller passes the live hash to enforce cache
        invalidation on resume edits.

        Returns None when no row exists or the resume hash differs."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM fit_analyses WHERE chat_id = ? AND job_id = ?",
                (chat_id, job_id),
            ).fetchone()
            if row is None:
                return None
            if current_resume_sha1 is not None and row["resume_sha1"] != current_resume_sha1:
                return None
            return row

    def delete_fit_analyses(self, chat_id: int) -> int:
        """Wipe every cached fit analysis for this user. Called by the
        /cleardata "resume" path since the analyses reference the resume
        version that was current when they ran. Returns rows removed."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM fit_analyses WHERE chat_id = ?", (chat_id,),
            )
            return int(cur.rowcount or 0)
