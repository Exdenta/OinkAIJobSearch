#!/usr/bin/env python3
"""One-shot capture tool for sticker file_ids and custom_emoji_ids.

Usage
-----
    python tools/capture_sticker_ids.py

Leave the main bot STOPPED while this runs — Telegram's getUpdates is
single-consumer and they'll fight over messages. Then open Telegram:

  * To capture a sticker's file_id:
        Forward any sticker to your bot. This script prints the id.

  * To capture a custom_emoji_id (Premium only):
        Have a Premium account send a message containing a single custom
        emoji from the pig pack (e.g. t.me/addemoji/PigEmoji) to your bot.
        This script prints the entity.custom_emoji_id.

Paste the printed ids into `skill/job-search/scripts/pig_stickers.py`:

    STICKER_FILE_IDS = {
        pig_stickers.CELEBRATE: "CAACAg...",   # paste the sticker id
        pig_stickers.SNIFF:     "CAACAg...",
        # ...
    }

    CUSTOM_EMOJI_IDS = {
        pig_stickers.CELEBRATE: "5435957248314298068",   # paste the id
    }

Ctrl-C when done. Restart the main bot.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# We vendor the project's TelegramClient rather than reimplementing
# getUpdates. That keeps this tool useful even as the API thin-wrapper
# evolves.
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent
sys.path.insert(0, str(PROJECT_ROOT / "skill" / "job-search" / "scripts"))

from telegram_client import TelegramClient  # noqa: E402


def main() -> int:
    if load_dotenv:
        load_dotenv(PROJECT_ROOT / ".env")
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN missing in .env", file=sys.stderr)
        return 1

    tg = TelegramClient(token=token)
    print("Capture mode active. Forward stickers or send custom-emoji "
          "messages to your bot.")
    print("Press Ctrl-C to quit.\n")

    offset: int | None = None
    try:
        while True:
            try:
                updates = tg.get_updates(offset=offset, timeout=25)
            except Exception as e:
                print(f"getUpdates failed: {e}", file=sys.stderr)
                time.sleep(3)
                continue

            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                if not msg:
                    continue

                # --- STICKER ---
                sticker = msg.get("sticker")
                if sticker:
                    print("=== STICKER ===")
                    print(f"  file_id:        {sticker.get('file_id')}")
                    print(f"  file_unique_id: {sticker.get('file_unique_id')}")
                    print(f"  emoji:          {sticker.get('emoji')}")
                    print(f"  set_name:       {sticker.get('set_name')}")
                    print(f"  is_animated:    {sticker.get('is_animated')}")
                    print(f"  is_video:       {sticker.get('is_video')}")
                    print()
                    continue

                # --- CUSTOM EMOJI inside a text message ---
                ents = msg.get("entities") or msg.get("caption_entities") or []
                custom = [e for e in ents if e.get("type") == "custom_emoji"]
                if custom:
                    text = msg.get("text") or msg.get("caption") or ""
                    print("=== CUSTOM EMOJI(S) ===")
                    for e in custom:
                        off = int(e.get("offset") or 0)
                        ln = int(e.get("length") or 0)
                        # Offsets are UTF-16 code units; reconstruct the
                        # fallback glyph the same way Telegram encodes it.
                        utf16 = text.encode("utf-16-le")
                        fb = utf16[off*2: (off+ln)*2].decode("utf-16-le")
                        print(f"  fallback:        {fb!r}")
                        print(f"  custom_emoji_id: {e.get('custom_emoji_id')}")
                        print(f"  offset:          {off}")
                        print(f"  length:          {ln}")
                        print()
                    continue

                # Quiet noise — don't log every plain text message.

    except KeyboardInterrupt:
        print("\nDone.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
