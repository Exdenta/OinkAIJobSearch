"""Run the repo's Apify actors locally as subprocesses (no platform compute).

Dispatch lives inside ``apify_fetch._post_actor_run``: with ``APIFY_RUN_MODE=
local`` every ``run-sync-get-dataset-items`` POST is first attempted as a local
subprocess of the same actor package (``apify/<actor>/src``), falling back to
the platform call on ANY local failure — fail-open, so a broken venv or a
missing actor dir can never zero the feed (see the 0-sends lesson).

The Apify Python SDK runs natively off-platform: file-backed storage under
``CRAWLEE_STORAGE_DIR``, input read from the default key-value store's INPUT
record, ``push_data`` appending one JSON file per item under ``datasets/``.
Each actor gets ONE persistent storage dir (so its named ``fetch-cache-*`` KV
store and delta state survive across runs), while each RUN gets unique default
dataset/KV ids — query fan-out runs the same actor concurrently, and without
per-run ids those runs would clobber each other's INPUT and dataset.

Env knobs (all optional):
  APIFY_RUN_MODE=local        turn local runs on (anything else = platform)
  APIFY_LOCAL_PYTHON          interpreter with the actors' deps; default
                              <actors>/.venv/bin/python, else sys.executable
  APIFY_LOCAL_ACTORS_DIR      actor sources; default <repo>/apify
  APIFY_LOCAL_STORAGE_ROOT    per-actor storage; default <STATE_DIR>/apify-local
  APIFY_LOCAL_CONCURRENCY     max concurrent subprocesses, default 4
"""
from __future__ import annotations

import functools
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("job-search.apify-local")

# Headful Playwright + Xvfb — they need the platform's browser base image.
LOCAL_DENY: frozenset[str] = frozenset({
    "academicpositions-scraper",
    "wellfound-scraper",
})

# ponytail: global cap, per-actor weights if the box swaps.
_SEM = threading.BoundedSemaphore(
    max(1, int(os.environ.get("APIFY_LOCAL_CONCURRENCY") or "4"))
)


def enabled() -> bool:
    return (os.environ.get("APIFY_RUN_MODE") or "").strip().lower() == "local"


def actors_dir() -> Path | None:
    """The apify/ actor-sources tree — env override, else walk to repo root."""
    override = (os.environ.get("APIFY_LOCAL_ACTORS_DIR") or "").strip()
    if override:
        p = Path(override)
        return p if p.is_dir() else None
    for root in Path(__file__).resolve().parents:
        cand = root / "apify"
        if (cand / "hackernews-scraper" / "src").is_dir():
            return cand
    return None


def actor_from_url(url: str) -> str | None:
    """Actor name out of an ``.../acts/<owner>~<actor>/run-sync-...`` URL."""
    try:
        ref = url.split("/acts/", 1)[1].split("/", 1)[0]
        owner_actor = ref.split("~", 1)
        return owner_actor[1] if len(owner_actor) == 2 else None
    except (IndexError, AttributeError):
        return None


def _python(base: Path | None) -> str:
    p = (os.environ.get("APIFY_LOCAL_PYTHON") or "").strip()
    if p:
        return p
    if base is not None:
        cand = base / ".venv" / "bin" / "python"
        if cand.exists():
            return str(cand)
    return sys.executable


@functools.lru_cache(maxsize=4)
def _cert_file(python: str) -> str | None:
    """certifi bundle of the actor venv — actors that fetch via urllib get no
    CA store on macOS python.org builds, so hand them requests' bundle."""
    try:
        out = subprocess.run(
            [python, "-c", "import certifi; print(certifi.where())"],
            capture_output=True, text=True, timeout=30,
        )
        path = (out.stdout or "").strip()
        return path if out.returncode == 0 and path else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _storage_root() -> Path:
    """Where actor storage lives. Defaults under STATE_DIR, NOT $HOME: the
    systemd unit runs with ProtectHome=read-only and only STATE_DIR/LOG_DIR in
    ReadWritePaths, so a ~/-based default fails `storage setup` on every run and
    fails open to the platform — silently, forever (it did: 708 runs, 0 local)."""
    root = (os.environ.get("APIFY_LOCAL_STORAGE_ROOT") or "").strip()
    if root:
        return Path(root)
    project_root = Path(__file__).resolve().parents[3]
    return project_root / os.environ.get("STATE_DIR", "state") / "apify-local"


def _read_dataset(ds_dir: Path) -> list[dict]:
    """Collect pushed items — one JSON file per record, metadata files skipped.
    Reads whatever landed on disk, so a killed run still yields its partial
    results (same semantics as the platform's timeout behaviour)."""
    records: list[dict] = []
    if not ds_dir.is_dir():
        return records
    for f in sorted(ds_dir.glob("*.json")):
        if f.name.startswith("__"):
            continue
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
        elif isinstance(rec, list):  # defensive: batch-per-file layout
            records.extend(r for r in rec if isinstance(r, dict))
    return records


def run_actor_local(
    actor: str,
    payload: dict,
    *,
    run_timeout: int,
    max_items: int = 0,
) -> tuple[list[dict] | None, str | None]:
    """Run ONE actor as a local subprocess. Returns ``(records, err)``.

    ``err`` is None on success (records may be []). Partial results beat none:
    a non-zero exit with items already on disk returns those items, matching
    how the pipeline treats a platform run that timed out after pushing.
    Never raises.
    """
    base = actors_dir()
    if base is None:
        return None, "actors dir not found (set APIFY_LOCAL_ACTORS_DIR)"
    actor_dir = base / actor
    if not (actor_dir / "src" / "__main__.py").exists():
        return None, f"no local source for {actor}"

    storage = _storage_root() / actor / "storage"
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    kv_dir = storage / "key_value_stores" / run_id
    ds_dir = storage / "datasets" / run_id
    try:
        kv_dir.mkdir(parents=True, exist_ok=True)
        (kv_dir / "INPUT.json").write_text(json.dumps(payload))
    except OSError as e:
        return None, f"storage setup failed: {e}"

    python = _python(base)
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(seconds=run_timeout)
    env = os.environ.copy()
    env.pop("APIFY_IS_AT_HOME", None)
    if not env.get("SSL_CERT_FILE"):
        cert = _cert_file(python)
        if cert:
            env["SSL_CERT_FILE"] = cert
    env.update({
        "APIFY_LOCAL_STORAGE_DIR": str(storage),
        "CRAWLEE_STORAGE_DIR": str(storage),
        # Fresh per-run ids make purge pointless; leaving it on would let the
        # SDK sweep sibling runs' default storages mid-flight.
        "CRAWLEE_PURGE_ON_START": "false",
        "APIFY_PURGE_ON_START": "false",
        "ACTOR_ID": actor,
        "APIFY_ACTOR_ID": actor,
        "ACTOR_DEFAULT_DATASET_ID": run_id,
        "APIFY_DEFAULT_DATASET_ID": run_id,
        "ACTOR_DEFAULT_KEY_VALUE_STORE_ID": run_id,
        "APIFY_DEFAULT_KEY_VALUE_STORE_ID": run_id,
        "ACTOR_STARTED_AT": now.isoformat(),
        "APIFY_STARTED_AT": now.isoformat(),
        "ACTOR_TIMEOUT_AT": deadline.isoformat(),
        "APIFY_TIMEOUT_AT": deadline.isoformat(),
    })

    rc, tail = 0, ""
    started = time.time()
    with _SEM:
        try:
            proc = subprocess.run(
                [python, "-m", "src"],
                cwd=actor_dir, env=env,
                capture_output=True, text=True,
                timeout=run_timeout + 30,
            )
            rc = proc.returncode
            tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        except subprocess.TimeoutExpired:
            rc, tail = -1, "run-timeout-exceeded (local subprocess killed)"
        except OSError as e:
            rc, tail = -1, f"spawn failed: {e}"

    records = _read_dataset(ds_dir)
    shutil.rmtree(kv_dir, ignore_errors=True)
    shutil.rmtree(ds_dir, ignore_errors=True)

    if max_items and max_items > 0:
        records = records[:max_items]
    if records:
        if rc != 0:
            log.warning("apify-local %s exited %d but pushed %d item(s); "
                        "keeping partial results: %s", actor, rc, len(records), tail)
        else:
            # The journal-greppable proof a run stayed local (call_actor's own
            # log line is identical for local and platform runs).
            log.info("apify-local %s → %d record(s) in %.1fs",
                     actor, len(records), time.time() - started)
        return records, None
    if rc != 0:
        return None, f"exit {rc}: {tail}" if tail else f"exit {rc}"
    log.info("apify-local %s → 0 records in %.1fs", actor, time.time() - started)
    return [], None
