#!/usr/bin/env python3
"""Smoke test for the forum / discussion-page URL filter in
``telegram_client._url_is_real_posting`` + ``send_per_job_digest``.

Eight jobs cover the matrix of cases:

  1. https://reddit.com/r/cscareerquestions/comments/abc/...
       → BLOCKED by host (reddit.com is on the default blocklist).
  2. https://news.ycombinator.com/item?id=42, source="hackernews"
       → ALLOWED by source-exempt carve-out.
  3. https://news.ycombinator.com/item?id=43, source="web_search"
       → BLOCKED by host (carve-out is source-scoped, not host-scoped).
  4. https://twitter.com/foo/status/123, source="web_search"
       → BLOCKED by host.
  5. https://github.com/openai/openai-cookbook/issues/45, source="web_search"
       → BLOCKED — github.com is path-conditional and /issues/ rejects.
  6. https://github.com/openai/cookbook/blob/main/CAREERS.md, source="web_search"
       → ALLOWED — github.com /blob/ paths are real careers pages.
  7. https://jobs.lever.co/foo/abc-123, source="web_search"
       → ALLOWED — clean ATS posting.
  8. https://reddit.com/r/cscareerquestions/comments/xyz/, source="web_search"
       with FORUM_FILTER_OFF=1 → ALLOWED (filter disabled).

Asserts:
  - Cases 1, 3, 4, 5 are BLOCKED at send-time (4 jobs).
  - Cases 2, 6, 7 pass the filter and reach tg.send_message (3 jobs).
  - Case 8, run separately under FORUM_FILTER_OFF=1, also reaches send.
  - Forensic ``job.forum_url`` lines exist for each blocked job, with the
    right reason prefix (forum_host: or forum_path:).
  - Summary line carries ``forum_url_count == 4`` for the main run.

Isolation: ``URL_VALIDATION_OFF=1`` and ``JOB_AGE_FILTER_OFF=1`` so the
liveness probe and age-window gate don't interfere with the assertions.
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
# Fake Telegram client — records every send_message call.
# ---------------------------------------------------------------------------

class _FakeTG:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, dict | None]] = []
        self._next_id = 1000

    def send_message(self, chat_id, text, parse_mode="MarkdownV2",
                     reply_markup=None, disable_preview=True) -> int:
        self.calls.append((chat_id, text, reply_markup))
        self._next_id += 1
        return self._next_id


# ---------------------------------------------------------------------------
# 1. Direct unit-tests on _url_is_real_posting
# ---------------------------------------------------------------------------

def test_helper_direct() -> None:
    section("1. _url_is_real_posting: per-case verdicts")
    # Make sure FORUM_FILTER_OFF isn't set from a previous run before import.
    os.environ.pop("FORUM_FILTER_OFF", None)
    os.environ.pop("FORUM_HOST_BLOCKLIST", None)
    for mod in ("forensic", "telegram_client"):
        if mod in sys.modules:
            del sys.modules[mod]
    import telegram_client as tc

    cases = [
        # (url, source, expect_real, reason_prefix_or_None)
        ("https://reddit.com/r/cscareerquestions/comments/abc/title",
         "web_search", False, "forum_host:"),
        ("https://news.ycombinator.com/item?id=42",
         "hackernews", True, "source_exempt"),
        ("https://news.ycombinator.com/item?id=43",
         "web_search", False, "forum_host:"),
        ("https://twitter.com/foo/status/123",
         "web_search", False, "forum_host:"),
        ("https://github.com/openai/openai-cookbook/issues/45",
         "web_search", False, "forum_host:"),
        ("https://github.com/openai/cookbook/blob/main/CAREERS.md",
         "web_search", True, "ok"),
        ("https://jobs.lever.co/foo/abc-123",
         "web_search", True, "ok"),
        ("https://www.reddit.com/r/cscareerquestions/",
         "web_search", False, "forum_host:"),
        # Subdomain match: careers.reddit.com → parent reddit.com hit.
        ("https://careers.reddit.com/job/123",
         "web_search", False, "forum_host:"),
        # forums.* prefix match.
        ("https://forums.adobe.com/some-thread",
         "web_search", False, "forum_host:"),
        # Path pattern: /threads/
        ("https://example.com/threads/45",
         "web_search", False, "forum_path:"),
        # Path pattern: bare /r/<sub>
        ("https://example.com/r/python",
         "web_search", False, "forum_path:"),
    ]
    for url, source, expected_real, expected_reason_prefix in cases:
        real, reason = tc._url_is_real_posting(url, source)
        _assert(real is expected_real,
                f"{source} {url[:70]} → real={real} (expected {expected_real}); reason={reason}")
        _assert(reason == expected_reason_prefix or reason.startswith(expected_reason_prefix),
                f"  reason {reason!r} matches prefix {expected_reason_prefix!r}")


# ---------------------------------------------------------------------------
# 2. End-to-end send_per_job_digest with the 7-job matrix.
# ---------------------------------------------------------------------------

def test_end_to_end() -> None:
    section("2. send_per_job_digest: forum URLs are dropped")

    # Isolate forensic dir + disable adjacent gates so we measure the forum
    # filter in isolation.
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    os.environ["URL_VALIDATION_OFF"] = "1"
    os.environ["JOB_AGE_FILTER_OFF"] = "1"
    os.environ.pop("FORUM_FILTER_OFF", None)
    os.environ.pop("FORENSIC_OFF", None)
    os.environ.pop("FORUM_HOST_BLOCKLIST", None)
    for mod in ("forensic", "telegram_client"):
        if mod in sys.modules:
            del sys.modules[mod]
    import telegram_client as tc
    from dedupe import Job

    jobs = [
        # 1. Blocked by reddit host.
        Job(source="web_search", external_id="e1",
            title="Reddit-thread leak",
            company="Co", location="", posted_at="",
            url="https://reddit.com/r/cscareerquestions/comments/abc/title"),
        # 2. ALLOWED — hackernews source exempt.
        Job(source="hackernews", external_id="e2",
            title="HN whoishiring item", company="Co", location="", posted_at="",
            url="https://news.ycombinator.com/item?id=42"),
        # 3. Blocked — same host but different source.
        Job(source="web_search", external_id="e3",
            title="HN leak via web_search", company="Co", location="", posted_at="",
            url="https://news.ycombinator.com/item?id=43"),
        # 4. Blocked — twitter status.
        Job(source="web_search", external_id="e4",
            title="Twitter status leak", company="Co", location="", posted_at="",
            url="https://twitter.com/foo/status/123"),
        # 5. Blocked — github issues path.
        Job(source="web_search", external_id="e5",
            title="GH issues leak", company="Co", location="", posted_at="",
            url="https://github.com/openai/openai-cookbook/issues/45"),
        # 6. ALLOWED — github /blob/ careers page.
        Job(source="web_search", external_id="e6",
            title="GH careers.md", company="Co", location="", posted_at="",
            url="https://github.com/openai/cookbook/blob/main/CAREERS.md"),
        # 7. ALLOWED — clean lever ATS posting.
        Job(source="web_search", external_id="e7",
            title="Lever role", company="Co", location="", posted_at="",
            url="https://jobs.lever.co/foo/abc-123"),
    ]
    blocked_ids = {jobs[0].job_id, jobs[2].job_id, jobs[3].job_id, jobs[4].job_id}
    allowed_ids = {jobs[1].job_id, jobs[5].job_id, jobs[6].job_id}

    fake_tg = _FakeTG()
    sent_callbacks: list[tuple[int, str]] = []

    def on_sent(msg_id: int, job: Job) -> None:
        sent_callbacks.append((msg_id, job.job_id))

    n = tc.send_per_job_digest(
        fake_tg, chat_id=12345, jobs=jobs, cfg={}, on_sent=on_sent,
    )

    # 3 allowed sends + 1 header.
    _assert(n == 4, f"return value = sent count incl. header (got {n})")
    _assert(len(fake_tg.calls) == 4,
            f"tg.send_message called for header + 3 allowed (got {len(fake_tg.calls)})")
    sent_ids = {jid for (_, jid) in sent_callbacks}
    _assert(sent_ids == allowed_ids,
            f"on_sent fired for allowed job_ids only (got {sent_ids})")
    _assert(not (sent_ids & blocked_ids),
            "no on_sent for any blocked job_id")

    # Read forensic JSONL.
    log_dir = Path(td) / "forensic_logs"
    files = sorted(log_dir.glob("log.*.jsonl"))
    _assert(len(files) >= 1, f"forensic log file written (got {len(files)})")
    lines = []
    for f in files:
        for raw in f.read_text().splitlines():
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))

    forum_lines = [r for r in lines if r.get("op") == "job.forum_url"]
    _assert(len(forum_lines) == 4,
            f"four job.forum_url lines emitted (got {len(forum_lines)})")
    forum_logged_ids = {r["input"]["job_id"] for r in forum_lines}
    _assert(forum_logged_ids == blocked_ids,
            f"forum lines reference the right job_ids (got {forum_logged_ids})")
    for r in forum_lines:
        reason = r["output"]["reason"]
        _assert(reason.startswith("forum_host:") or reason.startswith("forum_path:"),
                f"reason has expected prefix (got {reason!r})")

    summary = [r for r in lines if r.get("op") == "telegram.send_per_job_digest.summary"]
    _assert(len(summary) == 1, f"one summary line (got {len(summary)})")
    s = summary[0]["output"]
    _assert(s.get("forum_url_count") == 4,
            f"summary.forum_url_count == 4 (got {s.get('forum_url_count')!r})")
    _assert(s.get("sent") == 3, f"summary.sent == 3 (got {s.get('sent')!r})")
    _assert(s.get("dead_url_count") == 0,
            f"summary.dead_url_count == 0 (got {s.get('dead_url_count')!r})")


# ---------------------------------------------------------------------------
# 3. FORUM_FILTER_OFF=1 disables the gate.
# ---------------------------------------------------------------------------

def test_filter_off() -> None:
    section("3. FORUM_FILTER_OFF=1 disables the gate")
    os.environ["FORUM_FILTER_OFF"] = "1"
    os.environ["URL_VALIDATION_OFF"] = "1"
    os.environ["JOB_AGE_FILTER_OFF"] = "1"
    td = tempfile.mkdtemp()
    os.environ["STATE_DIR"] = td
    for mod in ("forensic", "telegram_client"):
        if mod in sys.modules:
            del sys.modules[mod]
    import telegram_client as tc
    from dedupe import Job

    jobs = [
        Job(source="web_search", external_id="e1",
            title="Would-be-blocked", company="Co", location="", posted_at="",
            url="https://reddit.com/r/cscareerquestions/comments/abc/"),
    ]
    fake_tg = _FakeTG()
    n = tc.send_per_job_digest(
        fake_tg, chat_id=99, jobs=jobs, cfg={}, on_sent=lambda *_a, **_k: None,
    )
    _assert(n == 2, f"opt-out: send count incl. header (got {n})")
    _assert(len(fake_tg.calls) == 2,
            f"opt-out: tg.send_message called for header + 1 job (got {len(fake_tg.calls)})")
    os.environ.pop("FORUM_FILTER_OFF", None)


def main() -> int:
    test_helper_direct()
    test_end_to_end()
    test_filter_off()
    print("\nAll forum-filter smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
