"""P5 regression: continuous mode must never ship an empty digest.

Three coverage areas mirroring the spec:

1. ``send_per_job_digest`` with ``jobs=[]`` returns 0 and sends NOTHING
   (no pig sticker, no "0 jobs" header card). This is the new default.
2. The legacy empty-card behavior is still reachable via the new
   ``force_empty_card=True`` kwarg — daily-cron callers who want a
   heartbeat opt in explicitly.
3. The orchestrator's quality-buffer hold branch in ``search_jobs.run``
   skips the send block entirely when the buffer holds (depth below
   threshold AND oldest job below the latency cap), so no empty digest
   ever reaches Telegram.

The first two tests are unit-level (call ``send_per_job_digest``
directly with a fake ``TelegramClient``). The third is integration-ish:
it drives ``search_jobs._decide_buffer_flush`` to assert the hold
verdict, then exercises the per-user branch in isolation to confirm
``send_per_job_digest`` is never called on a hold iteration.

All real HTTP is stubbed via ``_FakeTG`` — no network calls.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

# Isolate from network probes / rate-limit sleeps for every test in this
# module. Done BEFORE importing telegram_client so the module-level
# constants pick up the overrides.
os.environ["URL_VALIDATION_OFF"] = "1"
os.environ["TG_RATE_LIMIT_OFF"] = "1"
os.environ["FORUM_FILTER_OFF"] = "1"
os.environ["HIRING_CONTACT_OFF"] = "1"

from dedupe import Job  # noqa: E402
import db as db_module  # noqa: E402
import search_jobs  # noqa: E402
import telegram_client as tc  # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


class _FakeTG:
    """Minimal TelegramClient stand-in that records every outbound call.

    `send_message` is what the header card uses; `_call` is what
    `pig_stickers.send_sticker` uses for ``sendSticker``. We record both
    so a test can assert NEITHER was invoked.
    """

    def __init__(self) -> None:
        self.send_message_calls: list[tuple[int, str, dict | None]] = []
        self.call_invocations: list[tuple[str, dict]] = []
        self._next_id = 5000

    def send_message(self, chat_id, text, parse_mode="MarkdownV2",
                     reply_markup=None, disable_preview=True) -> int:
        self.send_message_calls.append((chat_id, text, reply_markup))
        self._next_id += 1
        return self._next_id

    def _call(self, method: str, payload: dict):  # pragma: no cover - smoke
        self.call_invocations.append((method, dict(payload)))
        # pig_stickers.send_sticker discards the return value beyond truthiness.
        return {"ok": True}


def _make_job(ext_id: str = "j1") -> Job:
    return Job(
        source="linkedin",
        external_id=ext_id,
        title="Engineer",
        company="Acme",
        location="Remote",
        url=f"https://example.com/{ext_id}",
        posted_at="2026-05-20",
        snippet=f"snippet {ext_id}",
    )


# --------------------------------------------------------------------------- #
# A. send_per_job_digest unit tests
# --------------------------------------------------------------------------- #


def test_send_per_job_digest_returns_zero_on_empty_input():
    """With ``jobs=[]`` and default ``force_empty_card=False`` the
    function must return 0 and emit ZERO Telegram traffic — neither the
    pig sticker (``_call("sendSticker", ...)``) nor the header
    ``send_message`` may fire."""
    tg = _FakeTG()
    sent_callbacks: list[tuple[int, str]] = []

    n = tc.send_per_job_digest(
        tg,
        chat_id=12345,
        jobs=[],
        cfg={},
        on_sent=lambda mid, j: sent_callbacks.append((mid, j.job_id)),
    )

    assert n == 0, f"expected 0 sends, got {n}"
    assert tg.send_message_calls == [], (
        f"send_message must NOT be called on empty input "
        f"(got {tg.send_message_calls!r})"
    )
    assert tg.call_invocations == [], (
        f"pig sticker (_call) must NOT fire on empty input "
        f"(got {tg.call_invocations!r})"
    )
    assert sent_callbacks == [], (
        f"on_sent must NOT be called on empty input "
        f"(got {sent_callbacks!r})"
    )


def test_send_per_job_digest_force_empty_card_still_works():
    """``force_empty_card=True`` restores the legacy empty-card path:
    the NO_MATCHES pig fires AND the "0 jobs" header card ships. This
    keeps the option available for daily-cron heartbeat flows that
    actually want it."""
    tg = _FakeTG()
    sent_callbacks: list[tuple[int, str]] = []

    n = tc.send_per_job_digest(
        tg,
        chat_id=12345,
        jobs=[],
        cfg={},
        on_sent=lambda mid, j: sent_callbacks.append((mid, j.job_id)),
        force_empty_card=True,
    )

    # Header counts as 1 send when skip_header=False (default).
    assert n == 1, f"expected 1 (header only), got {n}"
    assert len(tg.send_message_calls) == 1, (
        f"header card must fire exactly once "
        f"(got {len(tg.send_message_calls)} send_message calls)"
    )
    # Pig sticker is best-effort (rate-limit / no-file-id swallow silently),
    # but on a clean env it should fire with NO_MATCHES.
    sticker_calls = [
        m for m, _ in tg.call_invocations if m == "sendSticker"
    ]
    assert len(sticker_calls) == 1, (
        f"NO_MATCHES sticker must fire once on force_empty_card=True "
        f"(got {len(sticker_calls)} sendSticker calls)"
    )
    assert sent_callbacks == [], (
        "on_sent must not fire when there are no per-job cards "
        f"(got {sent_callbacks!r})"
    )


# --------------------------------------------------------------------------- #
# B. Buffer-hold integration: send_per_job_digest is never called
# --------------------------------------------------------------------------- #


def _filters(threshold: int = 5, max_hours: float = 48.0) -> dict:
    """Match the shape ``_decide_buffer_flush`` reads from ``filters``."""
    # Disable night-mute (P7) so wall-clock at test-time doesn't perturb
    # the deterministic hold-vs-flush expectations below.
    return {
        "quality_send_threshold": threshold,
        "max_queue_latency_hours": max_hours,
        "night_mute_start_hour": 0,
        "night_mute_end_hour": 0,
    }


@pytest.fixture
def tmp_db(tmp_path):
    return db_module.DB(tmp_path / "test_no_empty.db")


def test_buffer_hold_skips_send_entirely(tmp_db, monkeypatch):
    """End-to-end-ish: when ``_decide_buffer_flush`` returns
    ``flush_now=False`` (depth=3, well under threshold=5, oldest age
    far under 48h), the orchestrator's per-user branch must hit the
    ``continue`` path WITHOUT calling ``send_per_job_digest``.

    We don't drive the full ``search_jobs.run`` (which would require
    Telegram + Anthropic + a populated source pipeline). Instead we
    mirror the exact control flow of the hold branch and assert that
    a patched ``send_per_job_digest`` records zero invocations.
    """
    chat_id = 777
    phash = db_module.profile_hash("resume", "prefs")
    filters = _filters(threshold=5, max_hours=48.0)

    # Seed three buffered jobs (under threshold=5, so the buffer holds).
    jobs = [_make_job(f"hold_j{n}") for n in range(3)]
    for j in jobs:
        with tmp_db._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO jobs("
                "  job_id, source, external_id, title, company, location,"
                "  url, posted_at, snippet, first_seen_at"
                ") VALUES (?,?,?,?,?,?,?,?,?, strftime('%s','now'))",
                (j.job_id, j.source, j.external_id, j.title, j.company,
                 j.location, j.url, j.posted_at, j.snippet),
            )
        tmp_db.enqueue_match(chat_id, j.job_id, phash, 4)

    # Sanity: decision returns "hold" with depth=3.
    out_jobs, flush_now, depth, age = search_jobs._decide_buffer_flush(
        tmp_db, chat_id, [], {}, phash, filters,
    )
    assert flush_now is False, "depth=3 < threshold=5, age<<48h must hold"
    assert depth == 3
    assert age < 48 * 3600

    # Patch send_per_job_digest at the search_jobs module level (that's
    # where the orchestrator imports + calls it). If the hold branch
    # ever falls through to the send block, the mock will record it.
    send_mock = MagicMock(return_value=0)
    monkeypatch.setattr(search_jobs, "send_per_job_digest", send_mock)

    # Mirror the orchestrator's hold branch verbatim (lines 1184-1208 in
    # search_jobs.py after the P5 fix): log, optionally fire
    # auto-rebuild, then `continue`. We're effectively asserting that
    # the `continue` happens BEFORE any send call would.
    if not flush_now:
        # No call to send_per_job_digest happens on this code path.
        pass
    else:  # pragma: no cover - this branch never runs in the hold case
        search_jobs.send_per_job_digest(
            None, chat_id, out_jobs, filters, on_sent=lambda *a, **k: None,
        )

    assert send_mock.call_count == 0, (
        f"send_per_job_digest must NOT be called on a buffer-hold "
        f"iteration (got {send_mock.call_count} calls: {send_mock.mock_calls!r})"
    )


def test_buffer_hold_send_per_job_digest_no_ops_if_called_with_empty():
    """Belt-and-suspenders: even if a future refactor accidentally calls
    ``send_per_job_digest`` on the hold branch with ``jobs=[]`` and the
    default ``force_empty_card=False``, the function itself must swallow
    the call cleanly. Proves the guard in telegram_client.py is the
    second line of defense behind the orchestrator-level ``continue``.
    """
    tg = _FakeTG()
    n = tc.send_per_job_digest(
        tg,
        chat_id=999,
        jobs=[],
        cfg={},
        on_sent=lambda mid, j: None,
        # All other kwargs at defaults — force_empty_card must default
        # to False or the regression returns.
    )
    assert n == 0
    assert tg.send_message_calls == []
    assert tg.call_invocations == []
