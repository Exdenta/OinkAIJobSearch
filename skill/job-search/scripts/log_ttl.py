"""Log TTL cleanup — runs at the start of every search_jobs.py invocation.

Why this exists
---------------
Three sources of disk clutter accumulate during normal operation:

  1. ``state/forensic_logs/log.<N>.jsonl`` — rotates by size (10 MB) but
     never expires. Old rotations stay forever. We want last-2-days only.

  2. ``state/forensic_logs.archive-<unix-ts>/`` — entire directories
     created by `mv state/forensic_logs ...` between manual smoke runs.
     Useful for a few hours; landfill after that.

  3. ``state/.fuse_hidden*`` — macOS / FUSE-style "deleted but still open"
     file artifacts created when SQLite WAL/journal files are unlinked
     while still mapped. They're already orphans; safe to remove.

  4. ``/tmp/*.log`` — output captures from interactive sessions
     (aleksandrN.log, digest_*.log, smoke*.log). Also TTL-expirable.

Each rule runs in best-effort mode: any single failure is swallowed and
logged at debug level — TTL cleanup is never load-bearing for the
digest pipeline. The function returns a dict of counts so callers can
log a one-liner.

Configurable via env:
  * ``LOG_TTL_DAYS``   — int, default 2
  * ``LOG_TTL_OFF=1``  — disable cleanup entirely (testing / paranoia)

Wired in: ``search_jobs.run()`` calls ``cleanup_logs()`` immediately after
``load_env()`` so each cron fire prunes before doing real work.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 2


def _ttl_days() -> float:
    raw = os.environ.get("LOG_TTL_DAYS", "").strip()
    if not raw:
        return DEFAULT_TTL_DAYS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TTL_DAYS


def _is_off() -> bool:
    return os.environ.get("LOG_TTL_OFF", "").strip() not in ("", "0", "false", "False")


def _project_root() -> Path:
    """Resolve the project root from this file's location.

    skill/job-search/scripts/log_ttl.py → ../../../
    """
    return Path(__file__).resolve().parents[3]


def cleanup_logs(state_dir: Path | None = None, ttl_days: float | None = None) -> dict:
    """Delete files / dirs older than ttl_days. Returns a counts dict.

    Best-effort throughout: every filesystem op is in a try/except so a
    permission error or race doesn't crash the caller. The function never
    raises.
    """
    counts = {
        "forensic_archives_removed":  0,
        "forensic_archive_bytes":     0,
        "forensic_old_rotations":     0,
        "fuse_hidden_removed":        0,
        "tmp_logs_removed":           0,
        "skipped_active_log":         0,
    }
    if _is_off():
        log.info("log_ttl: disabled via LOG_TTL_OFF=1")
        return counts

    days = ttl_days if ttl_days is not None else _ttl_days()
    cutoff = time.time() - days * 86400

    sd = state_dir or (_project_root() / os.environ.get("STATE_DIR", "state"))
    if not sd.is_dir():
        log.debug("log_ttl: state dir %s missing; skipping", sd)
        return counts

    # 1. forensic_logs.archive-<ts>/  (whole directories)
    for entry in sd.iterdir():
        if not entry.is_dir() or not entry.name.startswith("forensic_logs.archive-"):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        # Sum bytes before delete for the report.
        try:
            sz = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        except OSError:
            sz = 0
        try:
            shutil.rmtree(entry, ignore_errors=True)
            counts["forensic_archives_removed"] += 1
            counts["forensic_archive_bytes"] += sz
        except Exception:
            log.debug("log_ttl: failed to rmtree %s", entry, exc_info=True)

    # 2. Old rotations inside forensic_logs/log.<N>.jsonl
    fl = sd / "forensic_logs"
    if fl.is_dir():
        # The HIGHEST-numbered file is the active write target. Don't touch it
        # even if its mtime is old (the writer would happily resurrect it on
        # next append, but cleaning it mid-stream invites lost data).
        try:
            files = sorted(
                fl.glob("log.*.jsonl"),
                key=lambda p: int(p.stem.split(".")[1]) if p.stem.split(".")[1].isdigit() else -1,
            )
        except Exception:
            files = []
        active = files[-1] if files else None
        for f in files:
            if f == active:
                continue
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    counts["forensic_old_rotations"] += 1
            except OSError:
                log.debug("log_ttl: failed to unlink %s", f, exc_info=True)
        if active is not None:
            counts["skipped_active_log"] = 1

    # 3. .fuse_hidden* artifacts under state/ (macOS / FUSE deleted-but-open).
    # These are orphans — safe to remove regardless of mtime.
    for f in sd.rglob(".fuse_hidden*"):
        try:
            f.unlink()
            counts["fuse_hidden_removed"] += 1
        except OSError:
            pass

    # 4. /tmp/*.log captures from interactive sessions.
    tmp = Path("/tmp")
    if tmp.is_dir():
        for pat in ("*.log",):
            for f in tmp.glob(pat):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        counts["tmp_logs_removed"] += 1
                except OSError:
                    pass

    log.info(
        "log_ttl: archives=%d (%d bytes) rotations=%d fuse=%d tmp=%d (TTL=%.1fd)",
        counts["forensic_archives_removed"],
        counts["forensic_archive_bytes"],
        counts["forensic_old_rotations"],
        counts["fuse_hidden_removed"],
        counts["tmp_logs_removed"],
        days,
    )
    return counts
