#!/usr/bin/env python3
"""Tests for emailer.py — SMTP transport + web digest-notification gates.

No real SMTP: `smtplib.SMTP` / `SMTP_SSL` are replaced with a recording
fake. DB-dependent gates run against a real `DB` on a tmp file.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import emailer  # noqa: E402
from db import DB  # noqa: E402


class _FakeSMTP:
    """Records the call sequence; class-level log so tests can assert
    after the instance goes out of scope inside send_email."""

    calls: list[tuple] = []
    raise_on_send: bool = False

    def __init__(self, host, port, timeout=None):
        type(self).calls.append(("connect", host, port))

    def starttls(self):
        type(self).calls.append(("starttls",))

    def login(self, username, password):
        type(self).calls.append(("login", username, password))

    def send_message(self, msg):
        if type(self).raise_on_send:
            raise RuntimeError("relay refused")
        # Messages may be multipart (plain + HTML alternative) — record
        # both parts so tests can assert on either.
        plain_part = msg.get_body(preferencelist=("plain",))
        html_part = msg.get_body(preferencelist=("html",))
        plain = plain_part.get_content() if plain_part is not None else ""
        html = (
            html_part.get_content()
            if html_part is not None and html_part is not plain_part
            else None
        )
        type(self).calls.append(
            ("send", msg["From"], msg["To"], msg["Subject"], plain, html)
        )

    def quit(self):
        type(self).calls.append(("quit",))


@pytest.fixture(autouse=True)
def _clean_env_and_fake(monkeypatch):
    for var in (
        "OINK_SMTP_HOST", "OINK_SMTP_PORT", "OINK_SMTP_USERNAME",
        "OINK_SMTP_PASSWORD", "OINK_SMTP_FROM", "OINK_SMTP_SECURITY",
        "OINK_EMAIL_NOTIFY_MIN_INTERVAL_S", "OINK_PUBLIC_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    _FakeSMTP.calls = []
    _FakeSMTP.raise_on_send = False
    monkeypatch.setattr(emailer.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(emailer.smtplib, "SMTP_SSL", _FakeSMTP)
    yield


def _configure(monkeypatch, **extra):
    monkeypatch.setenv("OINK_SMTP_HOST", "smtp.test")
    monkeypatch.setenv("OINK_SMTP_USERNAME", "robot@test")
    monkeypatch.setenv("OINK_SMTP_PASSWORD", "hunter2")
    for k, v in extra.items():
        monkeypatch.setenv(k, v)


def test_unconfigured_is_disabled():
    assert emailer.smtp_configured() is False
    assert emailer.send_email("a@b.c", "s", "b") is False
    assert _FakeSMTP.calls == []


def test_send_starttls_happy_path(monkeypatch):
    _configure(monkeypatch)
    assert emailer.smtp_configured() is True
    assert emailer.send_email("user@example.com", "Hello", "Body text") is True
    kinds = [c[0] for c in _FakeSMTP.calls]
    assert kinds == ["connect", "starttls", "login", "send", "quit"]
    send = next(c for c in _FakeSMTP.calls if c[0] == "send")
    assert send[1] == "robot@test"          # From falls back to username
    assert send[2] == "user@example.com"
    assert send[3] == "Hello"
    assert "Body text" in send[4]


def test_send_ssl_skips_starttls(monkeypatch):
    _configure(monkeypatch, OINK_SMTP_SECURITY="ssl", OINK_SMTP_FROM="hi@hryu.app")
    assert emailer.send_email("user@example.com", "S", "B") is True
    kinds = [c[0] for c in _FakeSMTP.calls]
    assert "starttls" not in kinds
    send = next(c for c in _FakeSMTP.calls if c[0] == "send")
    assert send[1] == "hi@hryu.app"


def test_send_failure_returns_false(monkeypatch):
    _configure(monkeypatch)
    _FakeSMTP.raise_on_send = True
    assert emailer.send_email("user@example.com", "S", "B") is False


# ---------------------------------------------------------------------------
# Digest-notification gates
# ---------------------------------------------------------------------------

def _web_user(tmp_path) -> tuple[DB, int]:
    db = DB(tmp_path / "jobs.db")
    chat_id = db.allocate_web_chat_id()
    db.upsert_user(chat_id, username="web:tester")
    db.set_email(chat_id, "tester@example.com", verified_at=time.time())
    return db, chat_id


def _jobs():
    return [
        SimpleNamespace(job_id="j1", title="Frontend Dev", company="Acme"),
        SimpleNamespace(job_id="j2", title="React Engineer", company="Globex"),
    ]


def _enrichments():
    return {
        "j1": {"match_score": 4},
        "j2": {"match_score": 5},
    }


def test_digest_email_happy_path_and_rate_limit(monkeypatch, tmp_path):
    _configure(monkeypatch)
    monkeypatch.setenv("OINK_PUBLIC_URL", "https://hryu.app")
    db, chat_id = _web_user(tmp_path)

    assert emailer.maybe_send_web_digest_email(db, chat_id, _jobs(), _enrichments()) is True
    send = next(c for c in _FakeSMTP.calls if c[0] == "send")
    assert "2" in send[3]                       # subject mentions the count
    assert "React Engineer" in send[4]
    assert "https://hryu.app/" in send[4]
    assert db.get_last_email_notified_at(chat_id) is not None

    # Second run inside the min interval → suppressed.
    _FakeSMTP.calls = []
    assert emailer.maybe_send_web_digest_email(db, chat_id, _jobs(), _enrichments()) is False
    assert _FakeSMTP.calls == []

    # …but goes out again once the interval has elapsed.
    db.set_last_email_notified_at(chat_id, time.time() - 21 * 3600)
    assert emailer.maybe_send_web_digest_email(db, chat_id, _jobs(), _enrichments()) is True


def test_digest_email_gates(monkeypatch, tmp_path):
    db, chat_id = _web_user(tmp_path)

    # SMTP unconfigured → no email even with jobs.
    assert emailer.maybe_send_web_digest_email(db, chat_id, _jobs(), _enrichments()) is False

    _configure(monkeypatch)

    # No jobs → no email.
    assert emailer.maybe_send_web_digest_email(db, chat_id, [], {}) is False

    # notify_email off → no email.
    db.set_notify_email(chat_id, False)
    assert emailer.maybe_send_web_digest_email(db, chat_id, _jobs(), _enrichments()) is False
    db.set_notify_email(chat_id, True)

    # No email address (Telegram-style user) → no email.
    tg_user = 777
    db.upsert_user(tg_user, username="tg:someone")
    assert emailer.maybe_send_web_digest_email(db, tg_user, _jobs(), _enrichments()) is False

    # Happy path still works after all that.
    assert emailer.maybe_send_web_digest_email(db, chat_id, _jobs(), _enrichments()) is True


# ---------------------------------------------------------------------------
# Digest content — full cards for every job, plain + HTML
# ---------------------------------------------------------------------------

def _full_job(i: int) -> SimpleNamespace:
    return SimpleNamespace(
        job_id=f"j{i}",
        title=f"Backend Engineer {i}",
        company=f"Corp{i}",
        location="Berlin, Germany",
        salary="",
        url=f"https://jobs.example.com/{i}",
        snippet=f"Snippet for role {i} with plenty of context.",
        source="linkedin",
    )


def _full_enrichment(i: int, score: int) -> dict:
    return {
        "match_score": score,
        "why_match": f"Strong Python overlap for role {i}",
        "why_mismatch": "Salary unstated",
        "key_details": {
            "stack": "Python, FastAPI",
            "seniority": "Senior",
            "remote_policy": "remote (EU)",
            "location": "Berlin",
            "salary": "€80–95k",
            "visa_support": "yes",
            "language": "English",
            "standout": "4-day work week",
        },
    }


def test_digest_email_includes_all_jobs_full_cards(monkeypatch, tmp_path):
    """No top-N cap: every job of the run lands in the email with its
    full card — rationale, chips, snippet, link, source."""
    _configure(monkeypatch)
    monkeypatch.setenv("OINK_PUBLIC_URL", "https://oink.test")
    db, chat_id = _web_user(tmp_path)

    jobs = [_full_job(i) for i in range(1, 8)]  # 7 jobs — past the old cap of 5
    enrichments = {f"j{i}": _full_enrichment(i, score=(i % 5) + 1) for i in range(1, 8)}

    assert emailer.maybe_send_web_digest_email(db, chat_id, jobs, enrichments) is True
    send = next(c for c in _FakeSMTP.calls if c[0] == "send")
    subject, plain, html = send[3], send[4], send[5]

    assert "7" in subject
    for i in range(1, 8):
        assert f"Backend Engineer {i}" in plain
        assert f"Backend Engineer {i}" in html
        assert f"https://jobs.example.com/{i}" in plain
        assert f"https://jobs.example.com/{i}" in html
    # Card details present in both parts.
    assert "Strong Python overlap" in plain and "Strong Python overlap" in html
    assert "Salary unstated" in plain and "Salary unstated" in html
    assert "Python, FastAPI" in plain and "Python, FastAPI" in html
    assert "visa support" in plain
    assert "4-day work week" in plain and "4-day work week" in html
    assert "Snippet for role 3" in plain and "Snippet for role 3" in html
    assert "via linkedin" in plain
    assert "https://oink.test/" in plain and "https://oink.test/" in html
    # Sorted best-fit first: a 5/5 job precedes a 1/5 job in the body.
    five = plain.index("5/5 match")
    one = plain.index("1/5 match")
    assert five < one


def test_digest_email_html_escapes_job_fields(monkeypatch, tmp_path):
    _configure(monkeypatch)
    db, chat_id = _web_user(tmp_path)
    jobs = [SimpleNamespace(
        job_id="jx", title="C++ <script>alert(1)</script> Dev", company="A&B",
    )]
    assert emailer.maybe_send_web_digest_email(db, chat_id, jobs, {}) is True
    send = next(c for c in _FakeSMTP.calls if c[0] == "send")
    html = send[5]
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "A&amp;B" in html


# ---------------------------------------------------------------------------
# Sign-in email template
# ---------------------------------------------------------------------------

def test_render_sign_in_email():
    link = "https://oink.test/api/auth/verify?t=tok123"
    subject, text, html = emailer.render_sign_in_email(link, "042137")
    assert "042137" in subject
    assert "042137" in text and link in text
    assert "042137" in html and link in html
    assert "15 minutes" in text and "15 minutes" in html
