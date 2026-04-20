#!/usr/bin/env python3
"""Detect the Telegram chat_id where the bot should post.

Prerequisites:
  1. Open Telegram and send any message to your bot (e.g. "hi") — or add the
     bot to a group/channel and send a message there.
  2. Put your TELEGRAM_BOT_TOKEN into the project's .env file.

Usage:
  python tools/get_chat_id.py

The script calls getUpdates, prints every chat it has seen, and highlights the
most recent one so you can paste its id into .env as TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass  # fall back to whatever is already in the environment


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN not found in environment or .env.", file=sys.stderr)
        return 1

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        r = requests.get(url, timeout=15)
    except requests.RequestException as e:
        print(f"❌ Request failed: {e}", file=sys.stderr)
        return 1

    data = r.json()
    if not data.get("ok"):
        print(f"❌ Telegram API error: {data.get('description')}", file=sys.stderr)
        return 1

    updates = data.get("result", [])
    if not updates:
        print(
            "⚠️  No updates found.\n"
            "   1. Send any message to your bot in Telegram (or in a group that includes it).\n"
            "   2. Re-run this script within ~24 hours.\n"
            "   (getUpdates only returns messages since the bot was started and not yet consumed by a webhook.)"
        )
        return 1

    seen: dict[int, dict] = {}
    for upd in updates:
        msg = upd.get("message") or upd.get("channel_post") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if not chat.get("id"):
            continue
        seen[chat["id"]] = {
            "type": chat.get("type"),
            "title": chat.get("title") or chat.get("username") or f"{chat.get('first_name','')} {chat.get('last_name','')}".strip(),
            "last_text": (msg.get("text") or "")[:60],
        }

    if not seen:
        print("⚠️  No chat messages in updates (only other update types).")
        return 1

    print("\nChats the bot can post to:\n")
    for cid, info in seen.items():
        print(f"  chat_id = {cid}")
        print(f"    type : {info['type']}")
        print(f"    name : {info['title']}")
        print(f"    last : {info['last_text']!r}")
        print()

    # Most recent one is typically what the user wants.
    last_id = next(reversed(seen))
    print("=" * 60)
    print(f"Most recent chat_id → {last_id}")
    print("Paste into .env as:  TELEGRAM_CHAT_ID=" + str(last_id))
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
