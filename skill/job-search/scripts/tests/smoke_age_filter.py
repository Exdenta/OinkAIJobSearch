#!/usr/bin/env python3
"""Smoke test for the pre-send age-window gate in telegram_client.

Covers:
  1. ``_parse_posted_at`` against ISO-date / ISO-datetime / epoch / RFC2822
     / empty input — proves the parser table.
  2. ``_is_within_age_window`` policy branches (allow vs reject) for the
     missing-date case.
  3. End-to-end ``send_per_job_digest`` with 6 fake jobs across the format
     matrix:
       - ISO today                          (alive)
       - ISO 3 days ago                     (alive)
       - ISO 10 days ago                    (DROP — too old)
       - epoch 5 days ago                   (alive)
       - RFC 2822 8 days ago                (DROP — too old)
       - "" missing                         (allow vs reject — varies)
     Three runs:
       a. defaults (max_days=7, missing_policy=allow)  → 4 sent
       b. JOB_AGE_MISSING_POLICY=reject                → 3 sent
       c. JOB_AGE_FILTER_OFF=1                         → 6 sent
  4. Asserts forensic ``job.too_old`` lines carry the right ``age_days``
     for the dropped jobs and that the summary tracks ``too_old_count``.

URL_VALIDATION_OFF=1 throughout so this isolates the age gate.
"""
from __future__ import annotations

import email.utils
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _assert(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


# ---------------------------------------------------------------------------
# Helpers — produce a posted_at string for "N days ago" in each format.
# ---------------------------------------------------------------------------

def _iso_date(days_ago: int) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return d.strftime("%Y-%m-%d")


def _iso_datetime(days_ago: int) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return d.isoformat()


def _epoch_seconds(days_ago: int) -> str:
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return str(int(d.timestamp()))


def _rfc2822(days_ago: int) -> str:
    # Use a strictly older offset to avoid clock-skew false alarms when the
    # caller asks for "exactly N days ago" — we want the gate to fire,
    # which means age_days >= max_days+1. Adding a small extra second
    # margin keeps the same calendar day for clarity.
    d = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return email.utils.format_datetime(d)


# ---------------------------------------------------------------------------
# 1. Parser coverage
# ---------------------------------------------------------------------------

def test_parser_coverage() -> None:
    section("1. _parse_posted_at: format matrix")
    import telegram_client as tc

    cases = [
        ("ISO short date",        _iso_date(3)),
        ("ISO datetime tz-aware", _iso_datetime(3)),
        ("ISO datetime naive",    "2026-04-25T12:00:00"),
        ("ISO datetime Z suffix", "2026-04-25T12:00:00Z"),
        ("Unix epoch seconds",    _epoch_seconds(5)),
        ("RFC 2822",              _rfc2822(8)),
    ]
    for label, raw in cases:
        dt = tc._parse_posted_at(raw)
        _assert(dt is not None and dt.tzinfo is not None,
                f"{label}: parsed → tz-aware datetime ({raw!r} → {dt})")

    # Unparseable / empty
    for raw in ("", "   ", "not a date", "2024"):
        dt = tc._parse_posted_at(raw)
        _assert(dt is None, f"unparseable {raw!r} → None")


def test_age_gate_policy_branches() -> None:
    section("2. _is_within_age_window: policy branches")
    import telegram_client as tc

    # Fresh
    allowed, reason = tc._is_within_age_window(_iso_date(0), max_days=7)
    _assert(allowed is True and reason == "ok",
            f"fresh job → ok (got {allowed}, {reason!r})")

    # Old
    allowed, reason = tc._is_within_age_window(_iso_date(10), max_days=7)
    _assert(allowed is False and reason.startswith("too_old:"),
            f"10-day-old → too_old (got {allowed}, {reason!r})")

    # Missing — allow
    allowed, reason = tc._is_within_age_window("", max_days=7, missing_policy="allow")
    _assert(allowed is True and reason == "missing_posted_at",
            f"missing+allow → True (got {allowed}, {reason!r})")

    # Missing — reject
    allowed, reason = tc._is_within_age_window("", max_days=7, missing_policy="reject")
    _assert(allowed is False and reason == "missing_posted_at",
            f"missing+reject → False (got {allowed}, {reason!r})")


# ---------------------------------------------------------------------------
# 3. End-to-end digest gating
# ---------------------------------------------------------------------------

class _FakeTG:
    """Minimal stand-in for TelegramClient — records every send."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str, dict | None]] = []
        self._next_id = 1000

    def send_message(self, chat_id, text, parse_mode="MarkdownV2",
                     reply_markup=None, disable_preview=True) -> int:
        self.calls.append((chat_id, text, reply_markup))
        self._next_id += 1
        return self._next_id


def _build_jobs():
    """Return 6 jobs spanning the format matrix.

    Indices:
      0 ISO today              (alive)
      1 ISO 3 days ago         (alive)
      2 ISO 10 days ago        (DROP — too old)
      3 epoch 5 days ago       (alive)
      4 RFC2822 8 days ago     (DROP — too old)
      5 missing posted_at      (policy-dependent)
    """
    from dedupe import Job
    posted_ats = [
        _iso_date(0),
        _iso_date(3),
        _iso_date(10),
        _epoch_seconds(5),
        _rfc2822(8),
        "",
    ]
    jobs = [
        Job(source="src", external_id=f"e{i}",
            title=f"Role {i}", company="Co", location="",
            url=f"https://example/{i}", posted_at=p)
        for i, p in enumerate(posted_ats)
    ]
    return jobs


def _read_forensic_lines(state_dir: str) -> list[dict]:
    log_dir = Path(state_dir) / "forensic_logs"
    if not log_dir.exists():
        return []
    out: list[dict] = []
    for f in sorted(log_dir.glob("log.*.jsonl")):
        for raw in f.read_text().splitlines():
            raw = raw.strip()
            if raw:
                out.append(json.loads(raw))
    return out


def _reset_modules(env_overrides: dict) -> None:
    """Reset env to a clean slate, apply overrides, and reload modules so
    module-level constants (MAX_JOB_AGE_DAYS, etc.) re-bind to the new env.
    """
    # Default off-bits for this test
    for k in ("JOB_AGE_FILTER_OFF", "JOB_AGE_MISSING_POLICY", "MAX_JOB_AGE_DAYS"):
        os.environ.pop(k, None)
    # We always isolate from URL probes for this test.
    os.environ["URL_VALIDATION_OFF"] = "1"
    os.environ["TG_RATE_LIMIT_OFF"] = "1"
    os.environ.pop("FORENSIC_OFF", None)
    os.environ.pop("FORENSIC_FULL", None)
    for k, v in env_overrides.items():
        os.environ[k] = v
    for mod in ("forensic", "telegram_client"):
        if mod in sys.modules:
            del sys.modules[mod]


def test_default_allow_policy() -> None:
    section("3. send_per_job_digest defaults: 4 sent / 2 dropped")
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    _reset_modules({})
    import telegram_client as tc

    jobs = _build_jobs()
    fake_tg = _FakeTG()
    sent_callbacks: list[tuple[int, str]] = []

    def on_sent(msg_id: int, job) -> None:
        sent_callbacks.append((msg_id, job.job_id))

    n = tc.send_per_job_digest(
        fake_tg, chat_id=12345, jobs=jobs, cfg={}, on_sent=on_sent,
    )

    # 1 header + 4 alive jobs (indices 0,1,3,5 — today, 3d, 5d, missing)
    expected_alive_idx = {0, 1, 3, 5}
    expected_alive_ids = {jobs[i].job_id for i in expected_alive_idx}
    sent_ids = {jid for (_, jid) in sent_callbacks}
    _assert(n == 5, f"return value = sent count incl. header (got {n})")
    _assert(len(fake_tg.calls) == 5,
            f"tg.send_message called 5 times (got {len(fake_tg.calls)})")
    _assert(sent_ids == expected_alive_ids,
            f"on_sent fired for the 4 fresh jobs (got {sent_ids})")

    # Forensic: two job.too_old lines for indices 2 (10d) and 4 (8d).
    lines = _read_forensic_lines(td)
    too_old = [r for r in lines if r.get("op") == "job.too_old"]
    _assert(len(too_old) == 2, f"two job.too_old lines (got {len(too_old)})")
    too_old_ids = {r["input"]["job_id"] for r in too_old}
    expected_dropped_ids = {jobs[2].job_id, jobs[4].job_id}
    _assert(too_old_ids == expected_dropped_ids,
            f"too-old lines reference the right job_ids (got {too_old_ids})")
    # age_days: index 2 → 10, index 4 → 8
    by_id = {r["input"]["job_id"]: r["output"] for r in too_old}
    _assert(by_id[jobs[2].job_id]["age_days"] == 10,
            f"index 2 age_days==10 (got {by_id[jobs[2].job_id]['age_days']})")
    _assert(by_id[jobs[4].job_id]["age_days"] == 8,
            f"index 4 age_days==8 (got {by_id[jobs[4].job_id]['age_days']})")
    for r in too_old:
        _assert(r["output"]["reason"].startswith("too_old:"),
                f"reason starts with too_old: (got {r['output']['reason']!r})")

    summary = [r for r in lines if r.get("op") == "telegram.send_per_job_digest.summary"]
    _assert(len(summary) == 1, f"one summary line (got {len(summary)})")
    s = summary[0]["output"]
    _assert(s.get("too_old_count") == 2,
            f"summary.too_old_count == 2 (got {s.get('too_old_count')!r})")
    _assert(s.get("sent") == 4, f"summary.sent == 4 (got {s.get('sent')!r})")


def test_reject_missing_policy() -> None:
    section("4. JOB_AGE_MISSING_POLICY=reject: 3 sent / 3 dropped")
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    _reset_modules({"JOB_AGE_MISSING_POLICY": "reject"})
    import telegram_client as tc

    jobs = _build_jobs()
    fake_tg = _FakeTG()
    sent_callbacks: list[tuple[int, str]] = []

    n = tc.send_per_job_digest(
        fake_tg, chat_id=12345, jobs=jobs, cfg={},
        on_sent=lambda mid, j: sent_callbacks.append((mid, j.job_id)),
    )

    # 1 header + 3 alive (indices 0, 1, 3 — missing now drops)
    expected_alive_idx = {0, 1, 3}
    expected_alive_ids = {jobs[i].job_id for i in expected_alive_idx}
    sent_ids = {jid for (_, jid) in sent_callbacks}
    _assert(n == 4, f"return value = sent count incl. header (got {n})")
    _assert(sent_ids == expected_alive_ids,
            f"on_sent fired for the 3 dated-fresh jobs (got {sent_ids})")

    # Forensic should now contain THREE too_old lines: 10d, 8d, missing.
    lines = _read_forensic_lines(td)
    too_old = [r for r in lines if r.get("op") == "job.too_old"]
    _assert(len(too_old) == 3, f"three job.too_old lines (got {len(too_old)})")
    reasons = sorted(r["output"]["reason"] for r in too_old)
    _assert("missing_posted_at" in reasons,
            f"missing_posted_at present in reasons (got {reasons})")

    summary = [r for r in lines if r.get("op") == "telegram.send_per_job_digest.summary"]
    s = summary[0]["output"]
    _assert(s.get("too_old_count") == 3,
            f"summary.too_old_count == 3 (got {s.get('too_old_count')!r})")
    _assert(s.get("sent") == 3, f"summary.sent == 3 (got {s.get('sent')!r})")


def test_filter_off() -> None:
    section("5. JOB_AGE_FILTER_OFF=1: all 6 sent")
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    _reset_modules({"JOB_AGE_FILTER_OFF": "1"})
    import telegram_client as tc

    jobs = _build_jobs()
    fake_tg = _FakeTG()
    sent_callbacks: list[tuple[int, str]] = []

    n = tc.send_per_job_digest(
        fake_tg, chat_id=12345, jobs=jobs, cfg={},
        on_sent=lambda mid, j: sent_callbacks.append((mid, j.job_id)),
    )

    expected_ids = {j.job_id for j in jobs}
    sent_ids = {jid for (_, jid) in sent_callbacks}
    _assert(n == 7, f"return value = 6 jobs + header (got {n})")
    _assert(sent_ids == expected_ids,
            f"on_sent fired for ALL 6 jobs (got {len(sent_ids)})")

    lines = _read_forensic_lines(td)
    too_old = [r for r in lines if r.get("op") == "job.too_old"]
    _assert(len(too_old) == 0, f"no job.too_old lines (got {len(too_old)})")

    summary = [r for r in lines if r.get("op") == "telegram.send_per_job_digest.summary"]
    s = summary[0]["output"]
    _assert(s.get("too_old_count") == 0,
            f"summary.too_old_count == 0 (got {s.get('too_old_count')!r})")
    _assert(s.get("sent") == 6, f"summary.sent == 6 (got {s.get('sent')!r})")


def main() -> int:
    test_parser_coverage()
    test_age_gate_policy_branches()
    test_default_allow_policy()
    test_reject_missing_policy()
    test_filter_off()
    print("\nAll age-filter smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
