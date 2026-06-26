#!/usr/bin/env python3
"""Tests for the hiring-contact lookup ("who to write to" on job cards).

Covers:
  A. ``_normalize`` — transport-invariant contract only: found flag,
     name + http(s) profile_url required, length caps, confidence enum
     fallback. Person QUALITY is the prompt's job and is not tested here.
  B. ``find_hiring_contact`` — CLI envelope parsing and the
     found / not_found / error trichotomy (errors must NOT be cached;
     model verdicts must).
  C. ``lookup_contacts_for_jobs`` — DB cache hits short-circuit the LLM,
     fresh verdicts are persisted, HIRING_CONTACT_OFF kills the pass.
  D. ``format_job_mdv2`` — the contact block renders a linked name +
     reason line; no contact leaves the card unchanged.
  E. ``send_per_job_digest`` — contacts attach to outgoing card text.
  F. ``db.DB`` round-trip for the ``hiring_contacts`` table.

Run directly (``python test_hiring_contact.py``) or via pytest.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import hiring_contact as hc  # noqa: E402
from dedupe import Job  # noqa: E402


def _job(ext_id: str = "x1", **kw) -> Job:
    defaults = dict(
        source="linkedin", external_id=ext_id,
        title="Frontend Developer", company="Acme GmbH",
        location="Berlin, Germany",
        url=f"https://example.com/jobs/{ext_id}",
        posted_at="2099-01-01",
        snippet="React + TypeScript role on the payments team.",
    )
    defaults.update(kw)
    return Job(**defaults)


def _envelope(payload: dict) -> str:
    """Fake `claude -p --output-format json` stdout."""
    return json.dumps({"result": json.dumps(payload)})


_FOUND_PAYLOAD = {
    "found": True,
    "name": "Jane Doe",
    "title": "Senior Technical Recruiter",
    "profile_url": "https://www.linkedin.com/in/janedoe/",
    "reason": "recruits frontend engineers at Acme, covers DACH",
    "confidence": "high",
}


class FakeDB:
    """Duck-typed stand-in for db.DB's two hiring-contact methods."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.upserts: list[tuple] = []

    def get_hiring_contact(self, job_id):
        return self.rows.get(job_id)

    def upsert_hiring_contact(self, job_id, status, contact_json):
        self.upserts.append((job_id, status, contact_json))
        self.rows[job_id] = {"status": status, "contact_json": contact_json}


# ---------------------------------------------------------------------------
# A. _normalize
# ---------------------------------------------------------------------------

def test_normalize_accepts_valid_contact() -> None:
    out = hc._normalize(dict(_FOUND_PAYLOAD))
    assert out is not None
    assert out["name"] == "Jane Doe"
    assert out["profile_url"] == "https://www.linkedin.com/in/janedoe/"
    assert out["confidence"] == "high"


def test_normalize_rejects_found_false_and_non_dict() -> None:
    assert hc._normalize({"found": False, "reason": "nobody public"}) is None
    assert hc._normalize(None) is None
    assert hc._normalize(["found"]) is None


def test_normalize_requires_name_and_http_url() -> None:
    no_name = {**_FOUND_PAYLOAD, "name": "  "}
    assert hc._normalize(no_name) is None
    no_url = {**_FOUND_PAYLOAD, "profile_url": ""}
    assert hc._normalize(no_url) is None
    bad_scheme = {**_FOUND_PAYLOAD, "profile_url": "mailto:jane@acme.com"}
    assert hc._normalize(bad_scheme) is None
    no_netloc = {**_FOUND_PAYLOAD, "profile_url": "https://"}
    assert hc._normalize(no_netloc) is None


def test_normalize_caps_and_confidence_fallback() -> None:
    noisy = {
        **_FOUND_PAYLOAD,
        "reason": "x" * 1000,
        "confidence": "absolutely certain",
    }
    out = hc._normalize(noisy)
    assert out is not None
    assert len(out["reason"]) == hc._MAX_REASON_CHARS
    assert out["confidence"] == "medium"


# ---------------------------------------------------------------------------
# B. find_hiring_contact
# ---------------------------------------------------------------------------

def _patch_cli(monkeypatch, stdout):
    """Route the lazy in-function import to a canned CLI response."""
    from instrumentation import wrappers
    calls = []

    def fake(store, caller, prompt, **kwargs):
        calls.append({"caller": caller, "prompt": prompt, "kwargs": kwargs})
        return stdout

    monkeypatch.setattr(wrappers, "wrapped_run_p_with_tools", fake)
    return calls


def test_find_contact_found(monkeypatch) -> None:
    calls = _patch_cli(monkeypatch, _envelope(_FOUND_PAYLOAD))
    contact, status = hc.find_hiring_contact(_job())
    assert status == "found"
    assert contact["name"] == "Jane Doe"
    assert len(calls) == 1
    # Tool posture: web tools only, shell/fs denied (injection defense).
    assert calls[0]["kwargs"]["allowed_tools"] == "WebSearch,WebFetch"
    assert "Bash" in calls[0]["kwargs"]["disallowed_tools"]
    # Job fields land in the prompt.
    assert "Acme GmbH" in calls[0]["prompt"]
    assert "Frontend Developer" in calls[0]["prompt"]


def test_find_contact_not_found_verdict(monkeypatch) -> None:
    _patch_cli(monkeypatch, _envelope({"found": False, "reason": "nobody"}))
    contact, status = hc.find_hiring_contact(_job())
    assert contact is None
    assert status == "not_found"


def test_find_contact_broken_fields_is_not_found(monkeypatch) -> None:
    payload = {**_FOUND_PAYLOAD, "profile_url": "linkedin.com/in/janedoe"}  # no scheme
    _patch_cli(monkeypatch, _envelope(payload))
    contact, status = hc.find_hiring_contact(_job())
    assert contact is None
    assert status == "not_found"


def test_find_contact_transport_errors(monkeypatch) -> None:
    _patch_cli(monkeypatch, None)  # CLI missing / timed out
    contact, status = hc.find_hiring_contact(_job())
    assert contact is None
    assert status == "error:cli_unavailable"

    _patch_cli(monkeypatch, json.dumps({"result": "no json here at all"}))
    contact, status = hc.find_hiring_contact(_job())
    assert contact is None
    assert status == "error:parse_failed"


# ---------------------------------------------------------------------------
# C. lookup_contacts_for_jobs
# ---------------------------------------------------------------------------

def test_lookup_disabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("HIRING_CONTACT_OFF", "1")
    from instrumentation import wrappers

    def boom(*a, **kw):  # the LLM must never be reached
        raise AssertionError("lookup ran while HIRING_CONTACT_OFF=1")

    monkeypatch.setattr(wrappers, "wrapped_run_p_with_tools", boom)
    assert hc.lookup_contacts_for_jobs([_job()], db=FakeDB()) == {}


def test_lookup_caches_verdicts_and_skips_cached(monkeypatch) -> None:
    monkeypatch.setenv("HIRING_CONTACT_OFF", "0")
    db = FakeDB()
    calls = _patch_cli(monkeypatch, _envelope(_FOUND_PAYLOAD))

    j = _job("c1")
    out = hc.lookup_contacts_for_jobs([j], db=db)
    assert out[j.job_id]["name"] == "Jane Doe"
    assert len(calls) == 1
    assert db.upserts and db.upserts[0][0] == j.job_id
    assert db.upserts[0][1] == "found"

    # Second pass: cache hit, no new CLI call.
    out2 = hc.lookup_contacts_for_jobs([j], db=db)
    assert out2[j.job_id]["name"] == "Jane Doe"
    assert len(calls) == 1


def test_lookup_caches_not_found_but_not_errors(monkeypatch) -> None:
    monkeypatch.setenv("HIRING_CONTACT_OFF", "0")
    db = FakeDB()
    _patch_cli(monkeypatch, _envelope({"found": False, "reason": "nobody"}))
    j = _job("c2")
    assert hc.lookup_contacts_for_jobs([j], db=db) == {}
    assert db.rows[j.job_id]["status"] == "not_found"

    db2 = FakeDB()
    _patch_cli(monkeypatch, None)  # transport error
    j2 = _job("c3")
    assert hc.lookup_contacts_for_jobs([j2], db=db2) == {}
    assert db2.upserts == []  # errors retry next send — never cached


def test_lookup_survives_worker_exception(monkeypatch) -> None:
    monkeypatch.setenv("HIRING_CONTACT_OFF", "0")
    from instrumentation import wrappers

    def boom(*a, **kw):
        raise RuntimeError("subprocess exploded")

    monkeypatch.setattr(wrappers, "wrapped_run_p_with_tools", boom)
    db = FakeDB()
    assert hc.lookup_contacts_for_jobs([_job("c4")], db=db) == {}
    assert db.upserts == []


# ---------------------------------------------------------------------------
# D. format_job_mdv2 rendering
# ---------------------------------------------------------------------------

def test_card_renders_contact_block() -> None:
    import telegram_client as tc
    contact = hc._normalize(dict(_FOUND_PAYLOAD))
    text = tc.format_job_mdv2(_job(), hiring_contact=contact)
    assert "👤" in text
    assert "(https://www.linkedin.com/in/janedoe/)" in text
    assert "Jane Doe" in text
    assert "recruits frontend engineers" in text
    # Reason renders italic.
    assert "_recruits frontend engineers at Acme, covers DACH_" in text


def test_card_without_contact_unchanged() -> None:
    import telegram_client as tc
    base = tc.format_job_mdv2(_job())
    explicit_none = tc.format_job_mdv2(_job(), hiring_contact=None)
    assert base == explicit_none
    assert "👤" not in base


def test_card_skips_contact_missing_link() -> None:
    import telegram_client as tc
    text = tc.format_job_mdv2(
        _job(), hiring_contact={"name": "Jane Doe", "profile_url": ""},
    )
    assert "👤" not in text


# ---------------------------------------------------------------------------
# E. send_per_job_digest attaches contacts
# ---------------------------------------------------------------------------

class _FakeTG:
    def __init__(self):
        self.sent: list[str] = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append(text)
        return len(self.sent)


def test_digest_cards_carry_contact(monkeypatch) -> None:
    import telegram_client as tc
    contact = hc._normalize(dict(_FOUND_PAYLOAD))
    j = _job("d1")
    monkeypatch.setattr(
        hc, "lookup_contacts_for_jobs", lambda jobs, db=None, chat_id=None: {j.job_id: contact},
    )
    tg = _FakeTG()
    sent = tc.send_per_job_digest(
        tg, 1234, [j], {"message": {}},
        on_sent=lambda mid, job: None,
        pre_filtered=True,
    )
    assert sent == 1
    assert "Jane Doe" in tg.sent[0]
    assert "(https://www.linkedin.com/in/janedoe/)" in tg.sent[0]


def test_digest_failsoft_when_lookup_raises(monkeypatch) -> None:
    import telegram_client as tc

    def boom(jobs, db=None, chat_id=None):
        raise RuntimeError("lookup infra down")

    monkeypatch.setattr(hc, "lookup_contacts_for_jobs", boom)
    tg = _FakeTG()
    j = _job("d2")
    sent = tc.send_per_job_digest(
        tg, 1234, [j], {"message": {}},
        on_sent=lambda mid, job: None,
        pre_filtered=True,
    )
    assert sent == 1  # card still ships, just without a contact
    assert "👤" not in tg.sent[0]


# ---------------------------------------------------------------------------
# F. db.DB round-trip
# ---------------------------------------------------------------------------

def test_db_roundtrip() -> None:
    from db import DB
    td = tempfile.mkdtemp()
    db = DB(str(Path(td) / "t.db"))

    assert db.get_hiring_contact("nope") is None
    contact_json = json.dumps({"name": "Jane Doe"})
    db.upsert_hiring_contact("j1", "found", contact_json)
    row = db.get_hiring_contact("j1")
    assert row["status"] == "found"
    assert json.loads(row["contact_json"])["name"] == "Jane Doe"

    db.upsert_hiring_contact("j1", "not_found", None)
    row = db.get_hiring_contact("j1")
    assert row["status"] == "not_found"
    assert row["contact_json"] is None


# ---------------------------------------------------------------------------
# G. Operator summary mirrors the contact
# ---------------------------------------------------------------------------

class _FakeStore:
    """Stand-in for telemetry MonitorStore — one run, one user source."""

    def __init__(self, chat_id: int, started: float, finished: float):
        self._run = {"started_at": started, "finished_at": finished,
                     "jobs_sent": 1}
        self._sources = [{"user_chat_id": chat_id}]

    def pipeline_run_with_sources(self, run_id):
        return self._run, self._sources


def test_operator_summary_mirrors_contact() -> None:
    import time as _time
    from db import DB
    from ops.summary import build_daily_summary

    td = tempfile.mkdtemp()
    db = DB(str(Path(td) / "t.db"))
    chat_id = 999
    j = _job("op1")
    db.upsert_job(j.as_db_dict())
    db.log_sent(chat_id, 42, j.job_id)
    db.upsert_hiring_contact(
        j.job_id, "found",
        json.dumps(hc._normalize(dict(_FOUND_PAYLOAD))),
    )

    now = _time.time()
    store = _FakeStore(chat_id, now - 60, now + 60)
    body = build_daily_summary(store, run_id=1, db=db)

    assert "Frontend Developer" in body
    assert "↳ 👤" in body
    assert '<a href="https://www.linkedin.com/in/janedoe/">' in body
    assert "Jane Doe — Senior Technical Recruiter" in body
    assert "recruits frontend engineers at Acme, covers DACH" in body


def test_operator_summary_no_contact_line_when_not_found() -> None:
    import time as _time
    from db import DB
    from ops.summary import build_daily_summary

    td = tempfile.mkdtemp()
    db = DB(str(Path(td) / "t.db"))
    chat_id = 998
    j = _job("op2")
    db.upsert_job(j.as_db_dict())
    db.log_sent(chat_id, 43, j.job_id)
    db.upsert_hiring_contact(j.job_id, "not_found", None)

    now = _time.time()
    store = _FakeStore(chat_id, now - 60, now + 60)
    body = build_daily_summary(store, run_id=1, db=db)

    assert "Frontend Developer" in body
    assert "↳" not in body


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-v"]))
