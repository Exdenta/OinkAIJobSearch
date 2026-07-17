#!/usr/bin/env python3
"""Smoke test: job-card URL buttons route through the signed click-tracker.

Contract points:

  1. With REDIRECT_BASE_URL + REDIRECT_HMAC_SECRET set, job_keyboard and
     suggestions_keyboard wrap the "View posting" button in a signed /r
     link carrying the job_id + chat_id, and the signature verifies.
  2. Without the env (or without a chat_id), the raw URL is used — bad
     config degrades to direct links, never broken buttons.

Run directly or via pytest.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

os.environ["REDIRECT_BASE_URL"] = "https://api.example.com"
os.environ["REDIRECT_HMAC_SECRET"] = "test-secret"

import telegram_client as tc  # noqa: E402
from redirect_server import sign_url_payload  # noqa: E402

RAW = "https://jobs.example.org/posting/42"


def _assert(cond: bool, msg: str) -> None:
    print(("  OK  " if cond else "  FAIL ") + msg)
    if not cond:
        raise AssertionError(msg)


def _url_button(kb: dict) -> str:
    return kb["inline_keyboard"][0][0]["url"]


def main() -> int:
    u = _url_button(tc.job_keyboard("abc123", url=RAW, chat_id=777))
    _assert(u.startswith("https://api.example.com/r?j=abc123&u=777&s="),
            "job_keyboard wraps the posting URL in a signed /r link")
    sig = u.rsplit("s=", 1)[1]
    _assert(sig == sign_url_payload("test-secret", "abc123", 777),
            "signature verifies against the secret")

    u = _url_button(tc.suggestions_keyboard("abc123", url=RAW, chat_id=777))
    _assert(u.startswith("https://api.example.com/r?j=abc123&u=777&s="),
            "suggestions_keyboard wraps the posting URL too")

    _assert(_url_button(tc.job_keyboard("abc123", url=RAW)) == RAW,
            "no chat_id → raw URL (tests / dry-runs)")

    os.environ["REDIRECT_BASE_URL"] = ""
    _assert(_url_button(tc.job_keyboard("abc123", url=RAW, chat_id=777)) == RAW,
            "env unset → raw URL fallback")

    print("\nAll redirect-wrap smoke tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
