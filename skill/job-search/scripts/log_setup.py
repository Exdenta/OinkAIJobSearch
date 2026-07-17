"""One rotating log file per process — daily rotation, 10-day retention.

Each entrypoint (search_jobs.py, bot.py) calls ``configure_logging(component)``
once at startup. Rotates at local midnight (1 file = 1 day), keeps 10 backups
(day 11 deletes day 1) via stdlib ``TimedRotatingFileHandler`` — no cron/
logrotate needed. Also keeps a stderr handler so ``journalctl -u <service>``
still shows live output.

Safe to call more than once per process: ``search_jobs.run()`` is invoked
in-process per user by the continuous searcher and by on-demand "check jobs
now" threads (see bot.py), not just as its own subprocess — the second call
onward is a no-op so job-search log lines land wherever the process's first
``configure_logging()`` call pointed (the bot's own file, when embedded).

Log directory: ``$LOG_DIR``, falling back to ``<repo_root>/logs``. Production
sets ``LOG_DIR=/home/oink/logs`` in ``.env`` (see deploy/env.example) — a path
already writable by every systemd unit (see deploy/bootstrap.sh).
"""
from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_HANDLER_NAME = "oink-rotating-file"
_RETENTION_DAYS = 10


def configure_logging(component: str, log_dir: str | os.PathLike | None = None) -> None:
    root = logging.getLogger()
    if any(h.get_name() == _HANDLER_NAME for h in root.handlers):
        return

    directory = Path(log_dir or os.environ.get("LOG_DIR") or _default_log_dir())
    directory.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = TimedRotatingFileHandler(
        directory / f"{component}.log",
        when="midnight",
        backupCount=_RETENTION_DAYS,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.set_name(_HANDLER_NAME)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    root.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def _default_log_dir() -> Path:
    # scripts/ -> job-search/ -> skill/ -> repo root
    return Path(__file__).resolve().parents[3] / "logs"
