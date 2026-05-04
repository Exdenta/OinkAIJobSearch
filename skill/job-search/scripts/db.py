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

import json as _json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator


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

-- Per-run enrichment cache. Lets the digest header's "Lower floor" button
-- replay jobs that were dropped by the score gate during the live run, with
-- their full enrichment payload (match_score + why_match + key_details)
-- intact. One row per (chat, run, job). `sent_floor` is NULL until the row
-- is delivered; on delivery it stores the floor that was active when the
-- job went out, so re-clicks at the same floor are idempotent.
CREATE TABLE IF NOT EXISTS digest_run_jobs (
    chat_id         INTEGER NOT NULL,
    run_id          INTEGER NOT NULL,
    job_id          TEXT    NOT NULL,
    match_score     INTEGER NOT NULL,
    enrichment_json TEXT,
    sent_floor      INTEGER,
    recorded_at     REAL    NOT NULL,
    PRIMARY KEY (chat_id, run_id, job_id)
);
-- Per-user source health. Counts consecutive runs where a source
-- contributed >0 postings but ALL of them scored 0 in AI enrichment.
-- Three strikes → `disabled_at` is stamped and the per-user filter in
-- search_jobs.py drops that source's postings before enrichment on
-- future runs (cheaper than re-scoring known dead weight). A non-zero
-- score on any future hit clears the streak; the row stays so the
-- operator can audit the history. Manually re-enable via
-- `clear_source_strike(chat_id, source_key)`.
CREATE TABLE IF NOT EXISTS user_source_strikes (
    chat_id     INTEGER NOT NULL,
    source_key  TEXT    NOT NULL,
    miss_streak INTEGER NOT NULL DEFAULT 0,
    disabled_at REAL,
    last_run_id INTEGER,
    updated_at  REAL    NOT NULL,
    PRIMARY KEY (chat_id, source_key)
);
CREATE INDEX IF NOT EXISTS idx_app_status ON applications(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_sent_job ON sent_messages(chat_id, job_id);
CREATE INDEX IF NOT EXISTS idx_profile_builds_chat ON profile_builds(chat_id, built_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_runs_chat ON research_runs(chat_id, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_digest_run_chat ON digest_run_jobs(chat_id, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_digest_run_age ON digest_run_jobs(recorded_at);
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
            # Monitoring/telemetry tables (pipeline_runs, source_runs,
            # claude_calls, error_events, ops_toggles). Idempotent — every
            # statement uses IF NOT EXISTS. See docs/monitoring-plan.md.
            from telemetry.schema import migrate as _telemetry_migrate
            _telemetry_migrate(c)

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
        # WAL + FK enforcement + 30s busy_timeout. Two processes (bot.py +
        # search_jobs.py) share state/jobs.db; default rollback journal
        # serializes all readers behind any writer and 15s busy_timeout
        # produced occasional `database is locked`. WAL lets readers run
        # alongside a single writer; FK ensures `delete_user` cascades
        # match their declarations; busy_timeout retries silently for 30s
        # before raising. PRAGMAs are idempotent — safe per-connection.
        conn = sqlite3.connect(self.path, detect_types=sqlite3.PARSE_DECLTYPES, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
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
    #
    # Some flows need to remember a small JSON-shaped context alongside the
    # state name (e.g. the skip-reason capture needs the job_id/title/company
    # the user was reacting to). Rather than adding a parallel column, we
    # encode `<state>|<json>` into the same TEXT slot. A bare state string
    # (legacy callers) still parses cleanly because the separator can't appear
    # inside a state name. `get_awaiting_state` returns just the state name
    # for backward compatibility; callers needing the payload use
    # `get_awaiting_state_payload`.
    _AWAITING_PAYLOAD_SEP = "|"

    def set_awaiting_state(
        self,
        chat_id: int,
        state: str | None,
        payload_json: dict | str | None = None,
    ) -> None:
        """e.g. 'awaiting_prefs' while the bot expects the next text message to
        be the user's free-form preferences. Pass None to clear.

        When `payload_json` is set (dict or pre-serialized JSON string), it is
        encoded into the same TEXT column as `state|<json>`. Reads via
        `get_awaiting_state` strip the payload; reads via
        `get_awaiting_state_payload` return it parsed.
        """
        if state is None:
            stored: str | None = None
        elif payload_json is None:
            stored = state
        else:
            if isinstance(payload_json, (dict, list)):
                blob = _json.dumps(payload_json, ensure_ascii=False)
            else:
                blob = str(payload_json)
            stored = f"{state}{self._AWAITING_PAYLOAD_SEP}{blob}"
        with self._conn() as c:
            c.execute(
                "UPDATE users SET awaiting_state = ? WHERE chat_id = ?",
                (stored, chat_id),
            )

    def _read_awaiting_raw(self, chat_id: int) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT awaiting_state FROM users WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if row is None:
                return None
            return row["awaiting_state"]

    def get_awaiting_state(self, chat_id: int) -> str | None:
        raw = self._read_awaiting_raw(chat_id)
        if raw is None:
            return None
        sep = self._AWAITING_PAYLOAD_SEP
        if sep in raw:
            return raw.split(sep, 1)[0]
        return raw

    def get_awaiting_state_payload(self, chat_id: int) -> dict | None:
        """Return the parsed JSON payload bundled with awaiting_state (if any).
        Returns None when there's no payload or it's malformed."""
        raw = self._read_awaiting_raw(chat_id)
        if not raw:
            return None
        sep = self._AWAITING_PAYLOAD_SEP
        if sep not in raw:
            return None
        _, _, blob = raw.partition(sep)
        if not blob:
            return None
        try:
            parsed = _json.loads(blob)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

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

    # ---------- user_source_strikes (auto-disable dead-weight sources per user) ----------

    def get_disabled_sources(self, chat_id: int) -> set[str]:
        """Return source_keys auto-disabled for this user (3+ zero-score runs)."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT source_key FROM user_source_strikes "
                "WHERE chat_id = ? AND disabled_at IS NOT NULL",
                (chat_id,),
            ).fetchall()
            return {r["source_key"] for r in rows}

    def record_source_outcome(
        self,
        chat_id: int,
        run_id: int,
        source_key: str,
        max_score: int,
        threshold: int = 3,
    ) -> tuple[int, bool]:
        """Update strike count for one (user, source) after a run.

        Logic:
          * `max_score > 0` → reset streak to 0, clear `disabled_at`.
          * `max_score == 0` → increment streak. If streak reaches
            ``threshold`` and the source isn't already disabled, stamp
            ``disabled_at = now``.

        Returns ``(new_streak, just_disabled)``. ``just_disabled`` is True
        only on the run that flipped the source from enabled → disabled,
        so the caller can log it once.
        """
        now = time.time()
        threshold = max(1, int(threshold))
        with self._conn() as c:
            row = c.execute(
                "SELECT miss_streak, disabled_at FROM user_source_strikes "
                "WHERE chat_id = ? AND source_key = ?",
                (chat_id, source_key),
            ).fetchone()
            prev_streak = int(row["miss_streak"]) if row else 0
            prev_disabled = (row["disabled_at"] is not None) if row else False

            if int(max_score) > 0:
                new_streak = 0
                new_disabled = None
                just_disabled = False
            else:
                new_streak = prev_streak + 1
                if new_streak >= threshold:
                    new_disabled = (row["disabled_at"] if (row and row["disabled_at"]) else now)
                    just_disabled = (not prev_disabled)
                else:
                    new_disabled = None
                    just_disabled = False

            c.execute(
                """
                INSERT INTO user_source_strikes
                    (chat_id, source_key, miss_streak, disabled_at, last_run_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, source_key) DO UPDATE SET
                    miss_streak = excluded.miss_streak,
                    disabled_at = excluded.disabled_at,
                    last_run_id = excluded.last_run_id,
                    updated_at  = excluded.updated_at
                """,
                (chat_id, source_key, new_streak, new_disabled, int(run_id), now),
            )
            return new_streak, just_disabled

    def clear_source_strike(self, chat_id: int, source_key: str) -> None:
        """Manual re-enable: zero the streak and clear `disabled_at`. Used by
        operator commands or future per-user UI."""
        with self._conn() as c:
            c.execute(
                """
                UPDATE user_source_strikes
                   SET miss_streak = 0,
                       disabled_at = NULL,
                       updated_at  = ?
                 WHERE chat_id = ? AND source_key = ?
                """,
                (time.time(), chat_id, source_key),
            )

    def list_source_strikes(self, chat_id: int) -> list[dict]:
        """Snapshot of all per-user source health rows for inspection."""
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT source_key, miss_streak, disabled_at, last_run_id, updated_at
                  FROM user_source_strikes
                 WHERE chat_id = ?
                 ORDER BY (disabled_at IS NULL), source_key
                """,
                (chat_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- digest_run_jobs (per-run score cache for ⬇/⬆ filter buttons) ----------

    def record_digest_run_jobs(
        self,
        chat_id: int,
        run_id: int,
        scored_enrichments: dict[str, dict],
    ) -> int:
        """Persist every enriched job from a digest run, keyed by (chat, run, job).

        `scored_enrichments` maps job_id → enrichment dict (must carry
        ``match_score``; may carry ``why_match`` / ``key_details``). The whole
        dict is stored as JSON so the "Lower floor" callback can resurrect the
        full per-job card later. Existing rows are overwritten so re-running
        a digest for the same chat/run produces a clean snapshot.

        Returns rows written.
        """
        if not scored_enrichments:
            return 0
        now = time.time()
        rows = []
        for jid, enr in scored_enrichments.items():
            try:
                score = int((enr or {}).get("match_score") or 0)
            except (TypeError, ValueError):
                score = 0
            payload = _json.dumps(enr or {}, ensure_ascii=False) if enr else None
            rows.append((chat_id, int(run_id), str(jid), score, payload, now))
        with self._conn() as c:
            c.executemany(
                """
                INSERT INTO digest_run_jobs (chat_id, run_id, job_id, match_score, enrichment_json, sent_floor, recorded_at)
                VALUES (?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(chat_id, run_id, job_id) DO UPDATE SET
                    match_score     = excluded.match_score,
                    enrichment_json = excluded.enrichment_json,
                    recorded_at     = excluded.recorded_at
                """,
                rows,
            )
        return len(rows)

    def mark_digest_jobs_sent(
        self,
        chat_id: int,
        run_id: int,
        job_ids: Iterable[str],
        floor: int,
    ) -> int:
        """Stamp ``sent_floor=floor`` on the listed jobs so subsequent
        "Lower floor" clicks won't re-send them. No-op for ids without a
        cached row."""
        ids = [str(j) for j in job_ids if j]
        if not ids:
            return 0
        floor = max(0, min(5, int(floor)))
        with self._conn() as c:
            cur = c.executemany(
                """
                UPDATE digest_run_jobs
                   SET sent_floor = ?
                 WHERE chat_id = ? AND run_id = ? AND job_id = ?
                """,
                [(floor, chat_id, int(run_id), jid) for jid in ids],
            )
            return cur.rowcount or 0

    def latest_digest_run_id(self, chat_id: int) -> int | None:
        """Return the most recent run_id with cached digest rows for this user."""
        with self._conn() as c:
            row = c.execute(
                "SELECT MAX(run_id) AS rid FROM digest_run_jobs WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            return int(row["rid"]) if row and row["rid"] is not None else None

    def unsent_count_at_score(self, chat_id: int, run_id: int, score: int) -> int:
        """Count cached jobs with ``match_score >= score`` not yet sent.

        Powers the ⬇ button label "⬇ ≥N (+M)" — M must mirror what a click
        will actually admit. The button semantics are "show me everything I
        haven't seen at or above this score", so the count must be inclusive
        upward (a single unsent job at the user's current floor would never
        appear here — it'd already be in `sent_floor`-stamped state from the
        live digest, or it's at a score the user already hid).
        """
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS n
                  FROM digest_run_jobs
                 WHERE chat_id = ? AND run_id = ?
                   AND match_score >= ?
                   AND sent_floor IS NULL
                """,
                (chat_id, int(run_id), int(score)),
            ).fetchone()
            return int(row["n"] or 0) if row else 0

    def fetch_unsent_at_score(
        self,
        chat_id: int,
        run_id: int,
        score: int,
    ) -> list[tuple[str, int, str | None]]:
        """Return ``[(job_id, match_score, enrichment_json)]`` rows at or above
        the given score that haven't been delivered yet. Ordered by job_id
        for determinism.

        Symmetric with :meth:`unsent_count_at_score`: the count promised on the
        ⬇ button MUST equal the rows the click admits. Inclusive-upward — a
        click on "⬇ ≥1" after "⬇ ≥2" replays any score-2 row that happened to
        slip in between (e.g. enrichment landed late). Already-sent rows are
        filtered by ``sent_floor IS NULL`` so this is idempotent.
        """
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT job_id, match_score, enrichment_json
                  FROM digest_run_jobs
                 WHERE chat_id = ? AND run_id = ?
                   AND match_score >= ?
                   AND sent_floor IS NULL
                 ORDER BY job_id
                """,
                (chat_id, int(run_id), int(score)),
            ).fetchall()
            return [(r["job_id"], int(r["match_score"]), r["enrichment_json"]) for r in rows]

    def purge_digest_run_jobs_older_than(self, max_age_seconds: float) -> int:
        """Drop cached rows whose `recorded_at` is older than ``max_age_seconds``.

        Returns rows deleted. Called from the daily TTL sweep so the cache
        never grows unbounded — a 7-day window is generous: by then any
        digest the user might want to retroactively expand is long gone.
        """
        cutoff = time.time() - float(max_age_seconds)
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM digest_run_jobs WHERE recorded_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0

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
