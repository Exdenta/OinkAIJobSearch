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

import hashlib
import json as _json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator


def profile_hash(resume_text: str, prefs_text: str) -> str:
    """Stable cache key over the inputs that drive scoring.

    sha1 truncated to 16 hex chars is plenty at our scale (≪ 2^32 cached
    profiles per user) and keeps the per-row overhead in `job_scores`
    small. Strips both inputs first so trivial whitespace edits don't
    invalidate the cache.
    """
    resume = (resume_text or "").strip()
    prefs = (prefs_text or "").strip()
    blob = f"{resume}\n----\n{prefs}".encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:16]


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
    -- Optional email + verification timestamp, populated only via the web
    -- magic-link login. Telegram-onboarded users keep these NULL. Uniqueness
    -- is enforced by `idx_users_email_lower` (partial, case-insensitive).
    email                   TEXT,
    email_verified_at       REAL,
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
-- Persistent per-user score cache, keyed by (chat_id, job_id,
-- profile_hash). Lets a re-run skip the Haiku/Sonnet two-pass for any
-- (user, job) we've already scored against the user's CURRENT profile
-- inputs (resume + prefs). `profile_hash` is the 16-char sha1 prefix
-- produced by `profile_hash()` above; any edit to either input
-- invalidates the cache for that user automatically. `model` records
-- the final pass that produced the verdict ("haiku" or "sonnet") so
-- we can audit cache content post-hoc.
CREATE TABLE IF NOT EXISTS job_scores (
    chat_id       INTEGER NOT NULL,
    job_id        TEXT    NOT NULL,
    profile_hash  TEXT    NOT NULL,
    match_score   INTEGER NOT NULL,
    why_match     TEXT    NOT NULL DEFAULT '',
    why_mismatch  TEXT    NOT NULL DEFAULT '',
    key_details   TEXT    NOT NULL DEFAULT '{}',
    model         TEXT    NOT NULL DEFAULT '',
    scored_at     REAL    NOT NULL,
    PRIMARY KEY (chat_id, job_id, profile_hash)
);
-- Per-(user, job) record of "View posting" link clicks. Populated by the
-- in-process redirect server when a user taps the URL button on a job
-- card. Multiple clicks for the same (chat, job) produce multiple rows
-- so we can analyze re-engagement patterns. user_agent / referer help
-- distinguish phone vs desktop browser sessions.
CREATE TABLE IF NOT EXISTS posting_clicks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    job_id      TEXT    NOT NULL,
    clicked_at  REAL    NOT NULL,
    user_agent  TEXT,
    referer     TEXT
);

-- Quality-buffer queue (algorithm v2.6, P1 of pipeline overhaul). Continuous
-- searches enqueue jobs that pass scoring (>=4) + send-time prefilter here
-- instead of firing a digest every run; the queue is flushed in one batch
-- once depth >= quality_send_threshold OR the oldest entry exceeds
-- max_queue_latency_hours. `profile_hash` snapshots the user's scoring
-- inputs at enqueue time; resume/prefs edits flip the hash and the stale
-- rows are silently purged on the next send-decision (they may no longer
-- fit). match_score is the score that gated entry, always >=4.
CREATE TABLE IF NOT EXISTS queued_matches (
    chat_id        INTEGER NOT NULL,
    job_id         TEXT    NOT NULL,
    profile_hash   TEXT    NOT NULL,
    match_score    INTEGER NOT NULL,
    queued_at      REAL    NOT NULL,
    PRIMARY KEY (chat_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_queued_matches_user_age
    ON queued_matches(chat_id, queued_at);

-- Per-source pagination memory (algorithm v2.7, P2 of pipeline overhaul).
-- Continuous mode fires the search loop every couple of hours; without
-- memory every iteration re-fetched page 1 of every source and re-scored
-- the same listings. This table records ONE row per
-- (source, query, page, location) so adapters can advance to the next
-- unseen page on each iteration and only fall back to page 1 once the
-- previous fetch is older than `min_revisit_age_s` seconds.
--
-- Columns:
--   * jobs_seen / jobs_new — telemetry the (P4) source-cooldown logic
--     consumes via `source_novelty_ratio`. Recorded on every fetch so
--     the signal is available to operators immediately.
--   * fetched_at is REPLACED on every record_fetch — INSERT OR REPLACE
--     against the composite primary key. The previous fetch's stats are
--     overwritten; the cursor advancement reads only the latest row per
--     (source, query, page, location).
--
-- Adapters that don't paginate (HN, RemoteOK, RemoteOK clones, etc.)
-- never call record_fetch and their cursors stay empty — they keep
-- their current "fetch everything in one shot" behaviour.
CREATE TABLE IF NOT EXISTS search_fetches (
    source        TEXT    NOT NULL,
    query         TEXT    NOT NULL,
    page          INTEGER NOT NULL,
    location      TEXT    NOT NULL DEFAULT '',
    fetched_at    REAL    NOT NULL,
    jobs_seen     INTEGER NOT NULL DEFAULT 0,
    jobs_new      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, query, page, location)
);
CREATE INDEX IF NOT EXISTS idx_search_fetches_last_seen
    ON search_fetches(source, fetched_at);

-- Adaptive source cooldown state (algorithm v2.8, P4 pipeline overhaul).
-- One row per source. `state` is 'normal' (run every iteration) or
-- 'half_freq' (run only on odd cycle_index — halves the API + scrape
-- cost of sources that consistently fail to produce fresh jobs). The
-- demotion is driven by `source_novelty_ratio` over the last 24h; see
-- `should_run_source` for the rules.
--
-- `consecutive_low_novelty_cycles` counts how many checks in a row the
-- source has been below the threshold. Demotion fires only after 3
-- consecutive lows so a single quiet iteration doesn't trip the alarm.
-- Recovery is immediate: one cycle ≥ threshold flips the state back to
-- 'normal' and zeros the counter.
CREATE TABLE IF NOT EXISTS source_cooldowns (
    source                            TEXT PRIMARY KEY,
    state                             TEXT NOT NULL DEFAULT 'normal',
    last_updated                      REAL NOT NULL,
    consecutive_low_novelty_cycles    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_app_status ON applications(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_sent_job ON sent_messages(chat_id, job_id);
CREATE INDEX IF NOT EXISTS idx_profile_builds_chat ON profile_builds(chat_id, built_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_runs_chat ON research_runs(chat_id, finished_at DESC);
CREATE INDEX IF NOT EXISTS idx_digest_run_chat ON digest_run_jobs(chat_id, run_id DESC);
CREATE INDEX IF NOT EXISTS idx_digest_run_age ON digest_run_jobs(recorded_at);
-- Covers the cache-lookup query: fetch all (job_id, …) rows for one
-- user at their current profile_hash in a single index scan.
CREATE INDEX IF NOT EXISTS idx_job_scores_lookup ON job_scores(chat_id, profile_hash);
CREATE INDEX IF NOT EXISTS idx_job_scores_age ON job_scores(scored_at);
CREATE INDEX IF NOT EXISTS idx_posting_clicks_user ON posting_clicks(chat_id, clicked_at DESC);
CREATE INDEX IF NOT EXISTS idx_posting_clicks_job ON posting_clicks(job_id);
"""

# Indexes that reference columns added by `_migrate()` cannot live in the
# main SCHEMA block — `executescript(SCHEMA)` runs against a pre-existing
# table whose `users.email` column hasn't been added yet, and SQLite would
# raise `no such column: email`. We create them in `_migrate()` after the
# ALTER TABLE, where the column is guaranteed to exist.
POST_MIGRATE_INDEXES = """
-- Case-insensitive uniqueness on the optional users.email column. Used by
-- the web onboarding (magic-link login) so the same address never maps to
-- two different chat_ids. SQLite supports indexed expressions, and the
-- partial-index `WHERE email IS NOT NULL` keeps Telegram-onboarded users
-- (email IS NULL) from sharing a single phantom row.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower
    ON users(LOWER(email)) WHERE email IS NOT NULL;
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
        # Rolling buffer of recent skip-reason comments. Folded into the
        # free_text passed to the profile builder so Opus can incorporate
        # the user's accumulated rejection signals (location/stack/seniority
        # mismatches the user verbalized after pressing "not a fit").
        if "skip_notes_text" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN skip_notes_text TEXT")
        # Algorithm v2: ⭐ button writes the per-user score floor here so it
        # survives profile rebuilds. v1 stored this in the user_profile JSON
        # under `min_match_score`, but every Opus rebuild reset it to 0.
        # Sourcing the floor from a real column decouples the two.
        if "min_match_score" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN min_match_score INTEGER DEFAULT 0")
        # Algorithm v2.3: per-user source toggles (JSON list of source
        # keys). Empty / NULL → fall back to operator default (all
        # enabled sources from defaults.DEFAULTS["sources"]). Opus picks
        # the initial list at profile-build time based on resume +
        # prefs; /sources bot UI lets the user toggle individual entries.
        if "enabled_sources" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN enabled_sources TEXT")
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

        # Web-app login columns. Optional — Telegram-onboarded users may
        # never set an email; web users always do. The unique index in
        # SCHEMA enforces case-insensitive uniqueness, but only when the
        # column is non-NULL so the bot's existing rows aren't affected.
        if "email" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "email_verified_at" not in have_cols:
            c.execute("ALTER TABLE users ADD COLUMN email_verified_at REAL")

        # Auto-rebuild counter (algorithm v2.8, P4 pipeline overhaul).
        # Bumped on every `append_skip_note` call; the searcher checks
        # it after each iteration and kicks off a profile rebuild once
        # the count crosses `auto_rebuild_skip_threshold`. Reset to 0
        # on successful rebuild. See `bump_skip_events`,
        # `get_skip_events_since_rebuild`, `reset_skip_events`.
        if "skip_events_since_rebuild" not in have_cols:
            c.execute(
                "ALTER TABLE users ADD COLUMN "
                "skip_events_since_rebuild INTEGER NOT NULL DEFAULT 0"
            )

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

        # Indexes that reference columns ADD-ed above. Must come after the
        # ALTER TABLE, otherwise SQLite raises `no such column: email` when
        # called against a DB that pre-dates this migration.
        c.executescript(POST_MIGRATE_INDEXES)

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

    # ---------- web login: email lookup / allocation ----------

    def find_user_by_email(self, email: str | None) -> sqlite3.Row | None:
        """Look up a user row by email, case-insensitively. Returns None if
        the address is empty / unknown. Used by the web magic-link verify
        path: a hit means the address was already registered (either on
        an earlier web visit, or by a Telegram user who later attached
        their email)."""
        if not email or not email.strip():
            return None
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM users WHERE LOWER(email) = LOWER(?)",
                (email.strip(),),
            ).fetchone()

    def set_email(
        self,
        chat_id: int,
        email: str | None,
        verified_at: float | None = None,
    ) -> None:
        """Persist the user's email + verification timestamp.

        Pass `email=None` to clear (rare — accounts get fully deleted via
        delete_user instead). `verified_at` defaults to now() when an
        email is being set, NULL when it's being cleared.
        """
        normalized = email.strip() if isinstance(email, str) and email.strip() else None
        if normalized is not None and verified_at is None:
            verified_at = time.time()
        if normalized is None:
            verified_at = None
        with self._conn() as c:
            c.execute(
                "UPDATE users SET email = ?, email_verified_at = ? WHERE chat_id = ?",
                (normalized, verified_at, chat_id),
            )

    def allocate_web_chat_id(self) -> int:
        """Reserve a negative chat_id for a web-only user (never seen on
        Telegram). Web users register without a chat_id; we mint negative
        integers so they can't collide with the positive ints Telegram
        hands out. The next id is `-1 - <count of existing negative rows>`,
        which is deterministic and monotonic for the lifetime of the DB.
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM users WHERE chat_id < 0"
            ).fetchone()
            n = int(row["n"] or 0) if row else 0
            return -1 - n

    # ---------- prefs free-text (raw input for the profile builder) ----------

    def set_prefs_free_text(self, chat_id: int, text: str | None) -> None:
        """Persist the raw /prefs description verbatim. Pass None to clear.

        Algorithm v2.2: dual-writes to `state/users/<chat_id>/prefs.txt`
        via `user_files.write_prefs` so every caller (bot.py /prefs flow,
        web backend onboarding submit + settings PATCH, onboarding wizard,
        future entry points) keeps the on-disk source-of-truth in sync
        with the DB column. search_jobs.py reads the file at scoring
        time; if a caller skipped the file write, scoring saw an empty
        prefs blob (the 385675637 bug). Centralising here removes that
        whole class of mistake.

        The file write is best-effort: a FS hiccup never breaks the DB
        write or its caller. The DB column is still the authoritative
        record — `user_files.read_prefs` returns "" when the file is
        missing and `tools/migrate_v2_files.py` can rehydrate it.
        """
        with self._conn() as c:
            c.execute(
                "UPDATE users SET prefs_free_text = ? WHERE chat_id = ?",
                (text, chat_id),
            )
        # Lazy import keeps db.py free of a hard dependency on the
        # script-side helper at module load time (matters in test
        # harnesses that import db.py without the full bot tree on
        # sys.path).
        try:
            from user_files import write_prefs as _wp
        except ImportError:
            return
        try:
            _wp(chat_id, text or "")
        except Exception:
            import logging
            logging.getLogger(__name__).debug(
                "set_prefs_free_text: file mirror write failed; "
                "DB row already updated", exc_info=True,
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

    # ---------- skip-feedback notes (rolling buffer of user comments) ----------

    # Cap the rolling buffer so it doesn't grow unboundedly. ~2000 chars is
    # roughly 30-50 short comments — plenty of signal without bloating the
    # profile-builder prompt.
    _MAX_SKIP_NOTES_CHARS = 2000

    def append_skip_note(self, chat_id: int, text: str) -> None:
        """Append a skip-reason comment to the rolling buffer.

        Buffer is one big newline-separated string; we trim from the FRONT
        when the buffer would exceed `_MAX_SKIP_NOTES_CHARS` so the most
        recent comments win. Empty / whitespace-only `text` is a no-op.

        Algorithm v2.2: dual-writes to prefs.txt via
        `user_files.append_skip_note` so the scorer (which reads the
        file) sees the same accumulated rejection signal the DB carries.

        Algorithm v2.8 (P4): also bumps `skip_events_since_rebuild` so
        the searcher can detect when the user has accumulated enough new
        rejection signal to justify an Opus profile rebuild.
        """
        t = (text or "").strip()
        if not t:
            return
        with self._conn() as c:
            row = c.execute(
                "SELECT skip_notes_text FROM users WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            current = "" if row is None else (row["skip_notes_text"] or "")
            entry = f"- {t[:400]}"
            joined = (current + "\n" + entry) if current else entry
            if len(joined) > self._MAX_SKIP_NOTES_CHARS:
                # Drop oldest entries (FIFO) until we're under the cap.
                lines = joined.split("\n")
                while lines and len("\n".join(lines)) > self._MAX_SKIP_NOTES_CHARS:
                    lines.pop(0)
                joined = "\n".join(lines)
            c.execute(
                "UPDATE users SET skip_notes_text = ?, "
                "skip_events_since_rebuild = COALESCE(skip_events_since_rebuild, 0) + 1 "
                "WHERE chat_id = ?",
                (joined, chat_id),
            )
        try:
            from user_files import append_skip_note as _asn
        except ImportError:
            return
        try:
            _asn(chat_id, t)
        except Exception:
            import logging
            logging.getLogger(__name__).debug(
                "append_skip_note: file mirror write failed; "
                "DB row already updated", exc_info=True,
            )

    def get_skip_events_since_rebuild(self, chat_id: int) -> int:
        """Return the count of skip-feedback events since the last profile
        rebuild for this user. 0 when the row doesn't exist or the column
        is NULL (legacy rows pre-migration)."""
        with self._conn() as c:
            row = c.execute(
                "SELECT skip_events_since_rebuild FROM users "
                "WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
        if row is None:
            return 0
        return int(row["skip_events_since_rebuild"] or 0)

    def reset_skip_events_since_rebuild(self, chat_id: int) -> None:
        """Zero the auto-rebuild counter. Called after a successful
        profile rebuild so the next K events trigger the NEXT rebuild,
        not the same one again."""
        with self._conn() as c:
            c.execute(
                "UPDATE users SET skip_events_since_rebuild = 0 "
                "WHERE chat_id = ?",
                (chat_id,),
            )

    def get_skip_notes_text(self, chat_id: int) -> str | None:
        """Return the rolling skip-reason buffer, or None if empty."""
        with self._conn() as c:
            row = c.execute(
                "SELECT skip_notes_text FROM users WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if row is None:
                return None
            return row["skip_notes_text"]

    # ---------- score floor (⭐ button) ----------

    def set_min_match_score(self, chat_id: int, score: int) -> None:
        """Persist the user's ⭐ floor. Clamps to [0,5]. Survives profile
        rebuilds because it lives in its own column rather than the
        Opus-owned profile JSON."""
        s = max(0, min(5, int(score or 0)))
        with self._conn() as c:
            c.execute(
                "UPDATE users SET min_match_score = ? WHERE chat_id = ?",
                (s, chat_id),
            )

    def get_min_match_score(self, chat_id: int) -> int:
        """Return the user's ⭐ floor (0-5). 0 means inherit the global default."""
        with self._conn() as c:
            row = c.execute(
                "SELECT min_match_score FROM users WHERE chat_id = ?", (chat_id,),
            ).fetchone()
            if row is None:
                return 0
            try:
                return max(0, min(5, int(row["min_match_score"] or 0)))
            except (TypeError, ValueError):
                return 0

    # ---------- per-user source toggles (algorithm v2.3) ----------

    def set_enabled_sources(
        self, chat_id: int, sources: list[str] | None,
    ) -> None:
        """Persist the user's per-source allow-list. Pass None / empty to
        clear (= inherit operator-default `defaults.DEFAULTS["sources"]`).

        Stored as a JSON array of source keys (e.g. "linkedin", "remoteok",
        "ai_jobs_net"). Source dispatch in search_jobs.py reads this list
        and drops jobs whose source isn't in it before enrichment.
        """
        import json as _json
        if not sources:
            value = None
        else:
            value = _json.dumps(
                [str(s) for s in sources if isinstance(s, str) and s],
                ensure_ascii=False,
            )
        with self._conn() as c:
            c.execute(
                "UPDATE users SET enabled_sources = ? WHERE chat_id = ?",
                (value, chat_id),
            )

    def get_enabled_sources(self, chat_id: int) -> list[str] | None:
        """Return the user's per-source allow-list or None when unset.

        None means "inherit operator-default sources block from
        defaults.DEFAULTS". Empty list means "no sources" — unusual but
        respected literally (no jobs surfaced).
        """
        import json as _json
        with self._conn() as c:
            row = c.execute(
                "SELECT enabled_sources FROM users WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            if row is None or row["enabled_sources"] is None:
                return None
            try:
                parsed = _json.loads(row["enabled_sources"])
            except (TypeError, ValueError):
                return None
            if not isinstance(parsed, list):
                return None
            return [s for s in parsed if isinstance(s, str) and s]

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
                    skip_notes_text  = NULL,
                    min_match_score  = 0,
                    enabled_sources  = NULL,
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
            c.execute("DELETE FROM job_scores      WHERE chat_id = ?", (chat_id,))
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

    def get_jobs_by_ids(self, job_ids: Iterable[str]) -> dict[str, sqlite3.Row]:
        """Bulk-fetch `jobs` rows for the given job_ids. Returns ``{job_id: row}``.

        Missing ids are simply absent from the returned dict; callers can
        partition by membership. Used by the quality-buffer flush path to
        rehydrate `Job` dataclasses from queued ids.
        """
        ids = [str(j) for j in job_ids if j]
        if not ids:
            return {}
        out: dict[str, sqlite3.Row] = {}
        chunk_size = 500
        with self._conn() as c:
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start:start + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = c.execute(
                    f"SELECT * FROM jobs WHERE job_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                for r in rows:
                    out[r["job_id"]] = r
        return out

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

    def clear_application(self, chat_id: int, job_id: str) -> int:
        """Drop the (chat, job) applications row entirely, restoring the
        "no decision made" state. Used by the web /undo route — broader
        than `set_application_status` because it has to handle the case
        where the row didn't exist before the action being undone (then
        the previous status was simply "absent"). Returns rows removed
        (0 or 1)."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM applications WHERE chat_id = ? AND job_id = ?",
                (chat_id, str(job_id)),
            )
            return int(cur.rowcount or 0)

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

    def user_seen_jobs(
        self, chat_id: int, job_ids: Iterable[str],
    ) -> set[str]:
        """Bulk variant of `user_has_seen_job`.

        Returns the subset of `job_ids` that already appear in
        `sent_messages` for this `chat_id`. Used by the quality-buffer
        flush path to defensively drop any queue entry that was already
        delivered (e.g. in a parallel run) so the row gets cleared
        instead of lingering forever and inflating queue depth.
        """
        ids = [str(j) for j in job_ids if j]
        if not ids:
            return set()
        out: set[str] = set()
        # Same chunking pattern as `get_jobs_by_ids` / `get_cached_scores`
        # — stay under SQLite's host-parameter cap on older builds.
        chunk_size = 500
        with self._conn() as c:
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start:start + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = c.execute(
                    f"""
                    SELECT DISTINCT job_id
                      FROM sent_messages
                     WHERE chat_id = ?
                       AND job_id IN ({placeholders})
                    """,
                    (int(chat_id), *chunk),
                ).fetchall()
                for r in rows:
                    out.add(r["job_id"])
        return out

    # ---------- posting_clicks (View-posting redirector analytics) ----------

    def record_posting_click(
        self,
        chat_id: int,
        job_id: str,
        user_agent: str | None = None,
        referer: str | None = None,
    ) -> None:
        """Append one click event. Caller has already verified the HMAC."""
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO posting_clicks
                    (chat_id, job_id, clicked_at, user_agent, referer)
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(chat_id), str(job_id), time.time(),
                 (user_agent or "")[:300] or None,
                 (referer or "")[:300] or None),
            )

    def get_job_url(self, job_id: str) -> str | None:
        """Look up a posting's canonical URL by stable hash. Used by the
        redirect server right before issuing the 302."""
        with self._conn() as c:
            row = c.execute(
                "SELECT url FROM jobs WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None:
                return None
            return row["url"] or None

    def count_posting_clicks(
        self,
        chat_id: int | None = None,
        since: float | None = None,
    ) -> int:
        """Total clicks. Both args optional for narrowing."""
        sql = "SELECT COUNT(*) AS n FROM posting_clicks WHERE 1=1"
        params: list = []
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(int(chat_id))
        if since is not None:
            sql += " AND clicked_at >= ?"
            params.append(float(since))
        with self._conn() as c:
            row = c.execute(sql, params).fetchone()
            return int(row["n"]) if row else 0

    def list_posting_clicks(
        self,
        chat_id: int,
        since: float | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Recent clicks for one user, joined to job title/company for display."""
        sql = """
            SELECT pc.job_id, pc.clicked_at, pc.user_agent,
                   j.title, j.company, j.source, j.url
              FROM posting_clicks pc
         LEFT JOIN jobs j ON j.job_id = pc.job_id
             WHERE pc.chat_id = ?
        """
        params: list = [int(chat_id)]
        if since is not None:
            sql += " AND pc.clicked_at >= ?"
            params.append(float(since))
        sql += " ORDER BY pc.clicked_at DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

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

    # ---------- job_scores (persistent per-user score cache) ----------

    def get_cached_scores(
        self,
        chat_id: int,
        job_ids: list[str],
        profile_hash: str,
    ) -> dict[str, dict]:
        """Bulk-lookup cached enrichment verdicts for one user.

        Returns ``{job_id: enrichment_dict}`` for every cache hit. Missing
        job_ids are simply absent from the returned dict — callers
        partition by membership. The enrichment shape mirrors what
        `enrich_jobs_ai` produces:
            {"match_score": int, "why_match": str, "why_mismatch": str,
             "key_details": dict, "model": str}
        """
        if not job_ids or not profile_hash:
            return {}
        out: dict[str, dict] = {}
        # SQLite has a host-parameter cap (default 999 on older builds, 32766
        # on modern ones); chunk to stay well under both.
        chunk_size = 500
        seen_ids = [str(j) for j in job_ids if j]
        with self._conn() as c:
            for start in range(0, len(seen_ids), chunk_size):
                chunk = seen_ids[start:start + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                rows = c.execute(
                    f"""
                    SELECT job_id, match_score, why_match, why_mismatch,
                           key_details, model
                      FROM job_scores
                     WHERE chat_id = ?
                       AND profile_hash = ?
                       AND job_id IN ({placeholders})
                    """,
                    (int(chat_id), str(profile_hash), *chunk),
                ).fetchall()
                for r in rows:
                    try:
                        kd = _json.loads(r["key_details"] or "{}")
                        if not isinstance(kd, dict):
                            kd = {}
                    except (TypeError, ValueError):
                        kd = {}
                    out[r["job_id"]] = {
                        "match_score": int(r["match_score"] or 0),
                        "why_match":   r["why_match"] or "",
                        "why_mismatch": r["why_mismatch"] or "",
                        "key_details": kd,
                        "model":       r["model"] or "",
                    }
        return out

    def upsert_scores(
        self,
        chat_id: int,
        profile_hash: str,
        scores: dict[str, dict],
        model: str,
    ) -> int:
        """Bulk INSERT-OR-REPLACE of enrichment verdicts.

        `scores` is ``{job_id: enrichment_dict}``. `model` is the label
        that produced the verdict ("haiku" / "sonnet"). Returns rows
        written. Skips entries whose value isn't a dict so a partial /
        malformed batch can't corrupt the table.
        """
        if not scores or not profile_hash:
            return 0
        now = time.time()
        rows: list[tuple] = []
        for jid, enr in scores.items():
            if not isinstance(enr, dict):
                continue
            try:
                score = int(enr.get("match_score") or 0)
            except (TypeError, ValueError):
                score = 0
            kd = enr.get("key_details") or {}
            if not isinstance(kd, dict):
                kd = {}
            try:
                kd_json = _json.dumps(kd, ensure_ascii=False)
            except (TypeError, ValueError):
                kd_json = "{}"
            # Allow caller to record a per-row model override (Sonnet
            # re-score on top of an initial Haiku pass); fall back to
            # the bulk `model` arg otherwise.
            row_model = enr.get("model")
            if not isinstance(row_model, str) or not row_model:
                row_model = model or ""
            rows.append((
                int(chat_id), str(jid), str(profile_hash),
                max(0, min(5, score)),
                str(enr.get("why_match") or ""),
                str(enr.get("why_mismatch") or ""),
                kd_json,
                row_model,
                now,
            ))
        if not rows:
            return 0
        with self._conn() as c:
            c.executemany(
                """
                INSERT OR REPLACE INTO job_scores
                  (chat_id, job_id, profile_hash, match_score,
                   why_match, why_mismatch, key_details, model, scored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def purge_job_scores_for_user(self, chat_id: int) -> int:
        """Drop every cached score row for one user. Returns rows removed.

        Called from /cleardata branches that invalidate the user's
        scoring inputs (resume or prefs reset) — `clear_resume` and
        `clear_user_profile`.
        """
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM job_scores WHERE chat_id = ?",
                (int(chat_id),),
            )
            return int(cur.rowcount or 0)

    def purge_job_scores_older_than(self, max_age_seconds: float) -> int:
        """TTL sweep: drop rows whose `scored_at` is older than the cutoff.

        Returns rows removed. Wired into `search_jobs.py:run` alongside
        the existing `digest_run_jobs` purge so the cache can't grow
        unbounded across long stretches of inactivity.
        """
        cutoff = time.time() - float(max_age_seconds)
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM job_scores WHERE scored_at < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    # ---------- queued_matches (quality buffer, P1 pipeline overhaul) ----------
    #
    # Continuous searcher buffers scored-and-live matches here instead of
    # firing a digest every run. The send-decision path flushes the queue in
    # one batch once depth >= quality_send_threshold OR the oldest entry's
    # age exceeds max_queue_latency_hours. `profile_hash` snapshots the
    # user's scoring inputs at enqueue time; a resume/prefs edit flips the
    # hash and stale rows are silently purged before depth is computed.

    def enqueue_match(
        self,
        chat_id: int,
        job_id: str,
        profile_hash: str,
        match_score: int,
    ) -> None:
        """Buffer a scored-and-live job for batched delivery.

        INSERT OR IGNORE — if the same (chat_id, job_id) is already
        queued, this is a silent no-op. Re-queuing must not bump
        `queued_at`: the age-flush latency budget is measured from the
        first time the user saw this posting clear scoring + liveness,
        so a re-run on the same job should not reset its clock.
        """
        if not job_id or not profile_hash:
            return
        with self._conn() as c:
            c.execute(
                """
                INSERT OR IGNORE INTO queued_matches
                  (chat_id, job_id, profile_hash, match_score, queued_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(chat_id), str(job_id), str(profile_hash),
                 int(match_score), time.time()),
            )

    def queue_depth(self, chat_id: int, profile_hash: str) -> int:
        """Count queued entries matching the user's CURRENT profile_hash.

        Stale-profile rows are not counted — they're silently purged by
        `purge_stale_queue` on the send-decision path before depth is
        computed, so they never contribute to flush.
        """
        if not profile_hash:
            return 0
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM queued_matches "
                "WHERE chat_id = ? AND profile_hash = ?",
                (int(chat_id), str(profile_hash)),
            ).fetchone()
            return int(row["n"] or 0) if row else 0

    def queue_oldest_age_seconds(
        self,
        chat_id: int,
        profile_hash: str,
    ) -> float | None:
        """Age (seconds) of the oldest queued entry under the current profile_hash.

        Returns ``time.time() - min(queued_at)``, or None if the queue is
        empty for this user/profile pair. Stale-profile rows are excluded —
        same rationale as `queue_depth`.
        """
        if not profile_hash:
            return None
        with self._conn() as c:
            row = c.execute(
                "SELECT MIN(queued_at) AS oldest FROM queued_matches "
                "WHERE chat_id = ? AND profile_hash = ?",
                (int(chat_id), str(profile_hash)),
            ).fetchone()
            if row is None or row["oldest"] is None:
                return None
            return max(0.0, time.time() - float(row["oldest"]))

    def fetch_queue(
        self,
        chat_id: int,
        profile_hash: str,
    ) -> list[dict]:
        """Return all queued entries for the user's current profile_hash.

        Ordered by ``match_score DESC, queued_at ASC`` — strongest match
        goes out first (top of the digest), ties break by who's been
        waiting longest. Shape per row::

            {"job_id": str, "match_score": int, "queued_at": float}
        """
        if not profile_hash:
            return []
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT job_id, match_score, queued_at
                  FROM queued_matches
                 WHERE chat_id = ? AND profile_hash = ?
                 ORDER BY match_score DESC, queued_at ASC
                """,
                (int(chat_id), str(profile_hash)),
            ).fetchall()
        return [
            {
                "job_id":      r["job_id"],
                "match_score": int(r["match_score"] or 0),
                "queued_at":   float(r["queued_at"] or 0.0),
            }
            for r in rows
        ]

    def clear_queue(self, chat_id: int, job_ids: Iterable[str]) -> int:
        """Bulk-delete the named (chat_id, job_id) rows. Returns rows removed.

        Called after a successful flush with the job_ids that actually
        shipped. Jobs that failed mid-flush stay queued for the next
        flush attempt — the row is removed only when delivery is
        confirmed.
        """
        ids = [str(j) for j in job_ids if j]
        if not ids:
            return 0
        removed = 0
        chunk_size = 500
        with self._conn() as c:
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start:start + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                cur = c.execute(
                    f"""
                    DELETE FROM queued_matches
                     WHERE chat_id = ?
                       AND job_id IN ({placeholders})
                    """,
                    (int(chat_id), *chunk),
                )
                removed += int(cur.rowcount or 0)
        return removed

    def purge_stale_queue(self, chat_id: int, profile_hash: str) -> int:
        """Drop queued rows whose `profile_hash` != the user's current hash.

        Runs on the send-decision path BEFORE the depth check, so an
        edit to resume or prefs silently invalidates the queue — those
        matches were scored against an old profile and may no longer
        fit. Returns rows removed. Pass an empty `profile_hash` to drop
        EVERY row for the user (e.g. after a full profile reset).
        """
        with self._conn() as c:
            if not profile_hash:
                cur = c.execute(
                    "DELETE FROM queued_matches WHERE chat_id = ?",
                    (int(chat_id),),
                )
            else:
                cur = c.execute(
                    "DELETE FROM queued_matches "
                    "WHERE chat_id = ? AND profile_hash <> ?",
                    (int(chat_id), str(profile_hash)),
                )
            return int(cur.rowcount or 0)

    # ---------- search_fetches (page memory, P2 pipeline overhaul) ----------
    #
    # Each row records ONE adapter call: "source X fetched page P for query Q
    # at location L at time T, saw N postings, M were new to the jobs table".
    # The continuous-scheduler will fire iterations every couple of hours
    # (P3); without page memory each iteration re-fetched page 1 and wasted
    # scoring on the same listings. The cursor advancement in `next_page_for`
    # lets each iteration walk forward through the source's pagination until
    # the previous page-1 fetch is old enough to be worth re-running.
    #
    # Telemetry-bearing columns (`jobs_seen` / `jobs_new`) are summed by
    # `source_novelty_ratio` for the P4 source-cooldown heuristic — exposed
    # now so the data exists when P4 lands.

    def record_fetch(
        self,
        source: str,
        query: str,
        page: int,
        location: str,
        jobs_seen: int,
        jobs_new: int,
    ) -> None:
        """Record (or overwrite) one fetch's cursor + telemetry.

        INSERT OR REPLACE against the (source, query, page, location)
        primary key — re-fetching the same page later REPLACES the row
        with fresh `fetched_at` and updated counts (the previous fetch's
        stats are lost; we keep only the latest per cell).

        No-op when `source` or `query` is empty (defensive: a malformed
        adapter call must not pollute the table). `location` may legally
        be the empty string — that's how worldwide / no-location fetches
        are keyed.
        """
        if not source or query is None:
            return
        q_str = str(query)
        # The primary key allows '' but not None for `location`.
        loc = "" if location is None else str(location)
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO search_fetches
                  (source, query, page, location, fetched_at,
                   jobs_seen, jobs_new)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(source), q_str, int(page), loc, time.time(),
                    max(0, int(jobs_seen or 0)),
                    max(0, int(jobs_new or 0)),
                ),
            )

    def get_fetch(
        self,
        source: str,
        query: str,
        page: int,
        location: str,
    ) -> dict | None:
        """Return the single row for this cursor cell, or None if absent.

        Used by `next_page_for` to inspect a candidate (source, query, page,
        location)'s last fetch time before deciding whether to re-fetch.
        """
        if not source or query is None:
            return None
        loc = "" if location is None else str(location)
        with self._conn() as c:
            row = c.execute(
                """
                SELECT source, query, page, location, fetched_at,
                       jobs_seen, jobs_new
                  FROM search_fetches
                 WHERE source = ? AND query = ? AND page = ? AND location = ?
                """,
                (str(source), str(query), int(page), loc),
            ).fetchone()
        if row is None:
            return None
        return {
            "source":     row["source"],
            "query":      row["query"],
            "page":       int(row["page"] or 0),
            "location":   row["location"] or "",
            "fetched_at": float(row["fetched_at"] or 0.0),
            "jobs_seen":  int(row["jobs_seen"] or 0),
            "jobs_new":   int(row["jobs_new"] or 0),
        }

    def stale_fetches(
        self,
        source: str,
        max_age_seconds: float,
    ) -> list[dict]:
        """All rows for `source` older than the cutoff, ascending by age.

        Returns dict rows in the same shape as `get_fetch` (with no
        composite-key fields elided), ordered by `fetched_at ASC` so the
        oldest entries come first. Empty list when nothing is stale yet.
        """
        cutoff = time.time() - float(max_age_seconds)
        with self._conn() as c:
            rows = c.execute(
                """
                SELECT source, query, page, location, fetched_at,
                       jobs_seen, jobs_new
                  FROM search_fetches
                 WHERE source = ? AND fetched_at <= ?
                 ORDER BY fetched_at ASC
                """,
                (str(source), float(cutoff)),
            ).fetchall()
        return [
            {
                "source":     r["source"],
                "query":      r["query"],
                "page":       int(r["page"] or 0),
                "location":   r["location"] or "",
                "fetched_at": float(r["fetched_at"] or 0.0),
                "jobs_seen":  int(r["jobs_seen"] or 0),
                "jobs_new":   int(r["jobs_new"] or 0),
            }
            for r in rows
        ]

    def next_page_for(
        self,
        source: str,
        query: str,
        location: str,
        max_page: int,
        min_revisit_age_s: float,
    ) -> int:
        """Decide which page to fetch next for (source, query, location).

        Cursor rules:
          * Page 1 stale (older than `min_revisit_age_s`) → return 1.
            Re-fetching from the top is justified because the source has
            likely cycled in fresh top-of-list listings since.
          * Page 1 fresh, some higher page recorded → return
            ``min(max_page, highest_recorded_page + 1)``.
          * Page 1 fresh, every page in 1..max_page recorded AND fresh →
            return -1 (caller skips this (query, location) for this run).
          * Page 1 fresh, highest recorded page == max_page → return 1
            (start the cycle over rather than refuse — keeps each
            iteration productive when the source ages between runs).

        `max_page` is adapter-specific (linkedin=10, justjoinit=5, etc.);
        the cursor never goes higher.
        """
        if not source or query is None or max_page <= 0:
            return 1
        loc = "" if location is None else str(location)
        cutoff = time.time() - float(min_revisit_age_s)
        with self._conn() as c:
            # All recorded pages for this cell, freshest first.
            rows = c.execute(
                """
                SELECT page, fetched_at
                  FROM search_fetches
                 WHERE source = ? AND query = ? AND location = ?
                """,
                (str(source), str(query), loc),
            ).fetchall()
        by_page: dict[int, float] = {
            int(r["page"] or 0): float(r["fetched_at"] or 0.0)
            for r in rows
        }
        # No history → start at page 1.
        if not by_page:
            return 1
        page1_at = by_page.get(1)
        if page1_at is None or page1_at < cutoff:
            return 1
        # Page 1 is fresh. Walk pages 1..max_page; if any is missing or
        # stale, that's where we resume.
        for p in range(1, int(max_page) + 1):
            seen_at = by_page.get(p)
            if seen_at is None or seen_at < cutoff:
                return p
        # Every page covered and fresh — refuse this run.
        return -1

    def source_novelty_ratio(
        self,
        source: str,
        since_seconds_ago: float,
    ) -> float:
        """Returns ``sum(jobs_new) / sum(jobs_seen)`` over the lookback window.

        Exposed now for P4's adaptive source cooldown — a near-zero ratio
        signals a source that's not surfacing anything new and can be
        deprioritised. Returns ``0.0`` when no rows match or when
        ``jobs_seen`` sums to zero (no division by zero — a quiet source
        is treated identically to a no-data source for this signal).
        """
        if not source or since_seconds_ago <= 0:
            return 0.0
        cutoff = time.time() - float(since_seconds_ago)
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COALESCE(SUM(jobs_seen), 0) AS seen_sum,
                       COALESCE(SUM(jobs_new),  0) AS new_sum
                  FROM search_fetches
                 WHERE source = ? AND fetched_at >= ?
                """,
                (str(source), float(cutoff)),
            ).fetchone()
        if row is None:
            return 0.0
        seen = float(row["seen_sum"] or 0.0)
        if seen <= 0:
            return 0.0
        new_ = float(row["new_sum"] or 0.0)
        return new_ / seen

    # ---------- source_cooldowns (P4 adaptive cooldown) ----------

    def get_source_cooldown(self, source: str) -> dict | None:
        """Return the cooldown row for `source`, or None when no row exists.

        Returned dict: ``{"state", "last_updated", "consecutive_low_novelty_cycles"}``.
        Callers treat None identically to a fresh ``{"state": "normal",
        "consecutive_low_novelty_cycles": 0}`` — `should_run_source` does
        this by default.
        """
        if not source:
            return None
        with self._conn() as c:
            row = c.execute(
                """
                SELECT state, last_updated, consecutive_low_novelty_cycles
                  FROM source_cooldowns
                 WHERE source = ?
                """,
                (str(source),),
            ).fetchone()
        if row is None:
            return None
        return {
            "state": str(row["state"] or "normal"),
            "last_updated": float(row["last_updated"] or 0.0),
            "consecutive_low_novelty_cycles": int(
                row["consecutive_low_novelty_cycles"] or 0
            ),
        }

    def upsert_source_cooldown(
        self,
        source: str,
        state: str,
        consecutive_low_novelty_cycles: int,
    ) -> None:
        """INSERT-or-REPLACE the cooldown row for `source`.

        `state` must be 'normal' or 'half_freq' — enforced here so a
        typo can't poison the table. ``consecutive_low_novelty_cycles``
        is clamped to ``>= 0``.
        """
        if not source:
            return
        st = str(state or "normal")
        if st not in ("normal", "half_freq"):
            st = "normal"
        n = max(0, int(consecutive_low_novelty_cycles or 0))
        with self._conn() as c:
            c.execute(
                """
                INSERT OR REPLACE INTO source_cooldowns
                  (source, state, last_updated,
                   consecutive_low_novelty_cycles)
                VALUES (?, ?, ?, ?)
                """,
                (str(source), st, time.time(), n),
            )

    def count_existing_jobs(self, job_ids: Iterable[str]) -> int:
        """Return how many of `job_ids` are already present in the `jobs` table.

        Used by adapters with the P2 cursor to compute ``jobs_new`` for
        `record_fetch` cheaply — counting hits instead of materialising
        full rows the way `get_jobs_by_ids` does.
        """
        ids = [str(j) for j in job_ids if j]
        if not ids:
            return 0
        total = 0
        chunk_size = 500
        with self._conn() as c:
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start:start + chunk_size]
                placeholders = ",".join("?" for _ in chunk)
                row = c.execute(
                    f"SELECT COUNT(*) AS n FROM jobs WHERE job_id IN ({placeholders})",
                    tuple(chunk),
                ).fetchone()
                total += int(row["n"] or 0) if row else 0
        return total

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
