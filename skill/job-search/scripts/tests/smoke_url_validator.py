#!/usr/bin/env python3
"""Smoke test for the pre-send URL liveness gate in telegram_client.

Covers:
  1. ``_url_is_alive`` against a known-live URL (https://example.com).
  2. ``_url_is_alive`` against a known-404 (https://httpbin.org/status/404)
     when reachable; falls back to a stubbed-out probe if the network is
     restricted so the test still proves the (False, "404") path.
  3. ``_url_is_alive`` against an unrouteable IP with a tight timeout —
     proves the (False, "timeout"|"connect_error") path.
  4. End-to-end ``send_per_job_digest`` with 5 jobs (3 alive, 2 dead) using a
     fake ``tg`` client and a stubbed validator. Asserts:
       - only 3 jobs reached ``tg.send_message``
       - exactly 2 ``telegram.url_dead`` forensic lines were written, with
         the right job_ids
       - the summary forensic line carries ``dead_url_count=2``
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
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
# 1. Helper-direct tests
# ---------------------------------------------------------------------------

def test_helper_alive() -> None:
    section("1. _url_is_alive: known-live URL")
    import telegram_client as tc
    try:
        alive, reason = tc._url_is_alive("https://example.com", timeout_s=10.0)
    except Exception as e:
        print(f"  SKIP  network unavailable: {e}")
        return
    _assert(alive is True, f"example.com is alive (reason={reason})")
    _assert(reason in ("ok", "head_method_blocked"),
            f"reason in expected set (got {reason!r})")


def test_helper_404() -> None:
    section("2. _url_is_alive: known-404 URL")
    import telegram_client as tc
    # Try the live network first; if we can't reach httpbin, fall back to
    # a stubbed probe so the test still exercises the dead-URL branch.
    try:
        alive, reason = tc._url_is_alive(
            "https://httpbin.org/status/404", timeout_s=10.0,
        )
        if alive is False and reason == "404":
            _assert(True, "httpbin/404 → (False, '404')")
            return
        # httpbin sometimes 503s under load — fall through to the stub path.
        print(f"  NOTE  live httpbin returned ({alive!r}, {reason!r}); using stub")
    except Exception as e:
        print(f"  NOTE  network unavailable ({e}); using stub")

    # Stub: monkey-patch _validation_request to return a 404 response.
    class _Stub:
        status_code = 404

    orig = tc._validation_request
    tc._validation_request = lambda *a, **kw: _Stub()
    try:
        alive, reason = tc._url_is_alive("https://stub.invalid/404", timeout_s=1.0)
    finally:
        tc._validation_request = orig
    _assert(alive is False, "stubbed 404 → not alive")
    _assert(reason == "404", f"stubbed 404 reason (got {reason!r})")


def test_helper_timeout() -> None:
    section("3. _url_is_alive: timeout/connect_error path")
    import telegram_client as tc
    # 240.0.0.0/4 is reserved (Class E) — packets get black-holed, so the
    # connect attempt never completes. With a 1 s budget we hit either
    # `timeout` or `connect_error` deterministically.
    alive, reason = tc._url_is_alive("http://240.0.0.1/", timeout_s=1.0)
    _assert(alive is False, f"unrouteable IP → not alive (reason={reason})")
    _assert(reason in ("timeout", "connect_error")
            or reason.startswith("exception:"),
            f"reason indicates network failure (got {reason!r})")


def test_helper_head_blocked_falls_back_to_get() -> None:
    section("4. _url_is_alive: HEAD blocked → ranged GET fallback")
    import telegram_client as tc

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    calls: list[tuple] = []

    def _fake(method, url, timeout_s, *, headers=None, verify=True):
        calls.append((method, headers.get("Range") if headers else None))
        if method == "HEAD":
            return _Resp(405)
        # Ranged GET should succeed.
        return _Resp(206)

    orig = tc._validation_request
    tc._validation_request = _fake
    try:
        alive, reason = tc._url_is_alive("https://stub.invalid/blocked", timeout_s=1.0)
    finally:
        tc._validation_request = orig
    _assert(alive is True, f"HEAD-blocked + 206 GET → alive (reason={reason})")
    _assert(reason == "head_method_blocked", f"reason marks fallback (got {reason!r})")
    _assert(calls[0][0] == "HEAD", "first call was HEAD")
    _assert(calls[1][0] == "GET" and calls[1][1] == "bytes=0-1023",
            "second call was ranged GET")


def test_helper_exception_safety() -> None:
    section("5. _url_is_alive: arbitrary exception is caught")
    import telegram_client as tc

    def _boom(*a, **kw):
        raise RuntimeError("simulated catastrophe")

    orig = tc._validation_request
    tc._validation_request = _boom
    try:
        alive, reason = tc._url_is_alive("https://stub.invalid/boom", timeout_s=1.0)
    finally:
        tc._validation_request = orig
    _assert(alive is False, "arbitrary exception → not alive")
    _assert(reason.startswith("exception:RuntimeError"),
            f"reason carries class name (got {reason!r})")


# ---------------------------------------------------------------------------
# 6. End-to-end send_per_job_digest gating
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


def test_end_to_end_digest_gating() -> None:
    section("6. send_per_job_digest drops dead URLs end-to-end")

    # Isolated forensic dir so we can read back what was logged.
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ.pop("FORENSIC_OFF", None)
    os.environ.pop("FORENSIC_FULL", None)
    os.environ.pop("URL_VALIDATION_OFF", None)
    # Reload forensic so the env override takes effect.
    for mod in ("forensic", "telegram_client"):
        if mod in sys.modules:
            del sys.modules[mod]
    import telegram_client as tc
    from dedupe import Job

    # 5 jobs: 3 with "alive" URLs, 2 with "dead" URLs. We tag them via the
    # URL itself so the stub validator can decide.
    jobs = [
        Job(source="src", external_id=f"e{i}",
            title=f"Role {i}", company="Co", location="", url=u, posted_at="")
        for i, u in enumerate([
            "https://alive.example/1",
            "https://dead.example/2",
            "https://alive.example/3",
            "https://dead.example/4",
            "https://alive.example/5",
        ])
    ]
    dead_job_ids = {jobs[1].job_id, jobs[3].job_id}
    alive_job_ids = {jobs[0].job_id, jobs[2].job_id, jobs[4].job_id}

    def _fake_alive(url: str, timeout_s: float = 5.0) -> tuple[bool, str]:
        if "dead" in url:
            return (False, "404")
        return (True, "ok")

    tc._url_is_alive = _fake_alive  # type: ignore[assignment]

    fake_tg = _FakeTG()
    sent_callbacks: list[tuple[int, str]] = []

    def on_sent(msg_id: int, job: Job) -> None:
        sent_callbacks.append((msg_id, job.job_id))

    n = tc.send_per_job_digest(
        fake_tg, chat_id=12345, jobs=jobs, cfg={}, on_sent=on_sent,
    )

    # 3 job sends + 1 header.
    _assert(n == 4, f"return value = sent count incl. header (got {n})")
    # send_message: 1 header + 3 alive jobs = 4 calls.
    _assert(len(fake_tg.calls) == 4,
            f"tg.send_message called 4 times (1 header + 3 alive); got {len(fake_tg.calls)}")
    # on_sent fires only for alive jobs.
    sent_ids = {jid for (_, jid) in sent_callbacks}
    _assert(sent_ids == alive_job_ids,
            f"on_sent fired for alive job_ids only (got {sent_ids})")
    _assert(not (sent_ids & dead_job_ids),
            "no on_sent for any dead job_id")

    # Read back forensic JSONL and assert url_dead lines + summary.
    log_dir = Path(td) / "forensic_logs"
    files = sorted(log_dir.glob("log.*.jsonl"))
    _assert(len(files) >= 1, f"forensic log file written (got {len(files)})")
    lines = []
    for f in files:
        for raw in f.read_text().splitlines():
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))

    dead_lines = [r for r in lines if r.get("op") == "telegram.url_dead"]
    _assert(len(dead_lines) == 2,
            f"two telegram.url_dead lines emitted (got {len(dead_lines)})")
    dead_logged_ids = {r["input"]["job_id"] for r in dead_lines}
    _assert(dead_logged_ids == dead_job_ids,
            f"dead lines reference the right job_ids (got {dead_logged_ids})")
    for r in dead_lines:
        _assert(r["output"]["reason"] == "404",
                f"dead reason captured (got {r['output'].get('reason')!r})")

    summary = [r for r in lines if r.get("op") == "telegram.send_per_job_digest.summary"]
    _assert(len(summary) == 1, f"one summary line (got {len(summary)})")
    s = summary[0]["output"]
    _assert(s.get("dead_url_count") == 2,
            f"summary.dead_url_count == 2 (got {s.get('dead_url_count')!r})")
    _assert(s.get("sent") == 3, f"summary.sent == 3 (got {s.get('sent')!r})")
    _assert(s.get("failed") == 0, f"summary.failed == 0 (got {s.get('failed')!r})")


def test_url_validation_off_skips_gate() -> None:
    section("7. URL_VALIDATION_OFF=1 disables the gate")
    os.environ["URL_VALIDATION_OFF"] = "1"
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    for mod in ("forensic", "telegram_client"):
        if mod in sys.modules:
            del sys.modules[mod]
    import telegram_client as tc
    from dedupe import Job

    # Validator that would say everything is dead — proves we never call it.
    def _never_alive(url, timeout_s=5.0):
        return (False, "404")
    tc._url_is_alive = _never_alive  # type: ignore[assignment]

    jobs = [
        Job(source="src", external_id="x1",
            title="Only role", company="Co", location="", url="https://x/1", posted_at=""),
    ]
    fake_tg = _FakeTG()
    n = tc.send_per_job_digest(
        fake_tg, chat_id=99, jobs=jobs, cfg={}, on_sent=lambda *_a, **_k: None,
    )
    _assert(n == 2, f"opt-out: send count incl. header (got {n})")
    _assert(len(fake_tg.calls) == 2,
            f"opt-out: tg.send_message called for header + 1 job (got {len(fake_tg.calls)})")
    os.environ.pop("URL_VALIDATION_OFF", None)


def main() -> int:
    test_helper_alive()
    test_helper_404()
    test_helper_timeout()
    test_helper_head_blocked_falls_back_to_get()
    test_helper_exception_safety()
    test_end_to_end_digest_gating()
    test_url_validation_off_skips_gate()
    print("\nAll URL-validator smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
