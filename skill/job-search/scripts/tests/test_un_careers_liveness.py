#!/usr/bin/env python3
"""Offline unit tests for `sources.un_careers.check_listing_open`.

careers.un.org is a JS-SPA the text-only liveness verifier can't read (it gets
the empty app shell and false-CLOSES live roles — jobId 280387 was dropped
2026-07-04 with its deadline 5 days out). `check_listing_open` instead hits the
public per-job JSON API for an authoritative verdict. These tests pin the
open / closed / fail-open trichotomy WITHOUT network by faking `_detail_get`.

Contract:
  * (True,  "ok")                   — record resolves, deadline not passed.
  * (False, "closed:un_not_found")  — HTTP 500 or {"status":0,"not found"}.
  * (False, "closed:un_deadline_passed") — endDate in the past.
  * (None,  "unknown:un_api_error") — 4xx / bad body / bad url → caller fails OPEN.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sources import un_careers as un  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def section(label: str) -> None:
    print(f"\n=== {label} ===")


class _FakeResp:
    def __init__(self, code: int, payload=None, *, raise_json: bool = False) -> None:
        self.status_code = code
        self._payload = payload
        self._raise_json = raise_json
        self.text = str(payload)

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


def _patch(resp):
    """Install a fake `_detail_get` returning `resp`; return restore()."""
    orig = un._detail_get

    def fake(api, *, timeout_s):
        return resp

    un._detail_get = fake  # type: ignore[assignment]
    return lambda: setattr(un, "_detail_get", orig)


_URL = "https://careers.un.org/jobSearchDescription/280387?language=en"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_open_future_deadline() -> None:
    section("1. resolves + future deadline → open")
    future = _iso(datetime.now(timezone.utc) + timedelta(days=10))
    restore = _patch(_FakeResp(200, {"status": 1, "data": {"status": "A", "endDate": future}}))
    try:
        v = un.check_listing_open(_URL)
    finally:
        restore()
    _assert(v == (True, "ok"), f"open verdict (got {v!r})")


def test_open_no_deadline() -> None:
    section("2. resolves + no endDate → open")
    restore = _patch(_FakeResp(200, {"status": 1, "data": {"status": "A"}}))
    try:
        v = un.check_listing_open(_URL)
    finally:
        restore()
    _assert(v == (True, "ok"), f"open verdict when undated (got {v!r})")


def test_closed_http_500_not_found() -> None:
    section("3. HTTP 500 {status:0, JobId Not found} → closed:un_not_found")
    restore = _patch(_FakeResp(500, {"status": 0, "message": "JobId Not found"}))
    try:
        v = un.check_listing_open(_URL)
    finally:
        restore()
    _assert(v == (False, "closed:un_not_found"), f"not-found → closed (got {v!r})")


def test_closed_status0_body() -> None:
    section("4. HTTP 200 {status:0} → closed:un_not_found")
    restore = _patch(_FakeResp(200, {"status": 0, "message": "JobId Not found"}))
    try:
        v = un.check_listing_open(_URL)
    finally:
        restore()
    _assert(v == (False, "closed:un_not_found"), f"status:0 → closed (got {v!r})")


def test_closed_deadline_passed() -> None:
    section("5. resolves but endDate in the past → closed:un_deadline_passed")
    past = _iso(datetime.now(timezone.utc) - timedelta(days=3))
    restore = _patch(_FakeResp(200, {"status": 1, "data": {"status": "A", "endDate": past}}))
    try:
        v = un.check_listing_open(_URL)
    finally:
        restore()
    _assert(v == (False, "closed:un_deadline_passed"), f"expired → closed (got {v!r})")


def test_fail_open_on_4xx() -> None:
    section("6. HTTP 429 (rate-limit) → fail-open unknown:un_api_error")
    restore = _patch(_FakeResp(429, {"message": "slow down"}))
    try:
        v = un.check_listing_open(_URL)
    finally:
        restore()
    _assert(v == (None, "unknown:un_api_error"), f"4xx → fail-open (got {v!r})")


def test_fail_open_on_bad_json() -> None:
    section("7. 200 but body not JSON → fail-open unknown:un_api_error")
    restore = _patch(_FakeResp(200, None, raise_json=True))
    try:
        v = un.check_listing_open(_URL)
    finally:
        restore()
    _assert(v == (None, "unknown:un_api_error"), f"bad json → fail-open (got {v!r})")


def test_fail_open_on_unparseable_url() -> None:
    section("8. URL with no jobId → fail-open unknown:un_api_error (no request made)")
    v = un.check_listing_open("https://careers.un.org/jobSearch?language=en")
    _assert(v == (None, "unknown:un_api_error"), f"no-id → fail-open (got {v!r})")


def main() -> int:
    test_open_future_deadline()
    test_open_no_deadline()
    test_closed_http_500_not_found()
    test_closed_status0_body()
    test_closed_deadline_passed()
    test_fail_open_on_4xx()
    test_fail_open_on_bad_json()
    test_fail_open_on_unparseable_url()
    print("\nAll UN-careers liveness unit tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
