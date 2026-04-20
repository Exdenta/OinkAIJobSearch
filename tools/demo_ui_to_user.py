#!/usr/bin/env python3
"""One-shot UI demo sender — ships the redesigned flow to a chat_id so
you can eyeball it in Telegram.

Usage
-----
    python tools/demo_ui_to_user.py              # sends to DEFAULT_CHAT_ID
    python tools/demo_ui_to_user.py 123456789    # override chat_id

Prereqs
-------
* The bot token must be in .env at the project root.
* The target user MUST have sent /start to your bot at least once —
  Telegram rejects sends to users who've never opened a dialog with the bot.

What it sends (6 messages, 1s apart)
------------------------------------
  1. Animated pig (lone 🐷 auto-animates)
  2. Welcome bubble + inline keyboard
  3. Seniority step (progress dots + inline choices)
  4. Sample enriched job card (title, score bar, chip rows)
  5. Setup-complete summary bubble
  6. Main-menu attach with the redesigned reply keyboard
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Default recipient. Override via command-line argument or DEMO_CHAT_ID env var.
DEFAULT_CHAT_ID = int(os.environ.get("DEMO_CHAT_ID", "0") or 0)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_ROOT / "skill" / "job-search" / "scripts"
sys.path.insert(0, str(SCRIPTS))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    print("WARN: python-dotenv not installed — hoping TELEGRAM_BOT_TOKEN is "
          "already in your environment.", file=sys.stderr)

from telegram_client import (                             # noqa: E402
    TelegramClient, format_job_mdv2, job_keyboard, mdv2_escape,
)
from dedupe import Job                                    # noqa: E402
import onboarding as ob                                   # noqa: E402
import pig_stickers as pigs                               # noqa: E402


REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "🔍  Search"},   {"text": "📋  Applied"}],
        [{"text": "👤  Profile"},  {"text": "🔬  Research"}],
        [{"text": "⚙️  Settings"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def pace(seconds: float = 1.0) -> None:
    time.sleep(seconds)


def main(argv: list[str]) -> int:
    chat_id = DEFAULT_CHAT_ID
    if len(argv) > 1:
        try:
            chat_id = int(argv[1])
        except ValueError:
            print(f"Invalid chat_id: {argv[1]!r}", file=sys.stderr)
            return 1

    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("FATAL: TELEGRAM_BOT_TOKEN missing in .env", file=sys.stderr)
        return 1

    tg = TelegramClient(token=token)

    # Disable per-moment rate limits for the demo — otherwise SNIFF /
    # THUMBS_UP would be suppressed when this is run back-to-back with
    # recent real bot traffic.
    pigs._MIN_INTERVAL_S.clear()

    def sticker_or_log(moment: str) -> None:
        """Try the registered sticker for `moment`. If none is registered
        (empty / fresh registry), note it in stderr so the dev knows why
        nothing animated showed up."""
        sent = pigs.send_sticker(tg, chat_id, moment)
        if not sent:
            print(f"      (no sticker registered for {moment} — skipped; "
                  f"populate pig_stickers.STICKER_FILE_IDS to enable)",
                  file=sys.stderr)

    # --- 1. Greeting — WAVE sticker from the pack, Unicode pig fallback ---
    # Try the pack first so the first pig on screen matches the rest of the
    # fat_roll_pigs. Only fall back to Telegram's built-in auto-animated 🐷
    # when no WAVE sticker is registered or the send errors.
    sent = False
    try:
        sent = pigs.send_sticker(tg, chat_id, pigs.WAVE)
    except Exception as e:
        print(f"WAVE sticker send errored: {e}", file=sys.stderr)
    if sent:
        print(f"[1/8] WAVE sticker → {chat_id}")
    else:
        try:
            tg.send_plain(chat_id, "🐷")
        except Exception as e:
            msg = str(e)
            print(f"FAILED on animated pig fallback: {e}", file=sys.stderr)
            if "initiate conversation" in msg.lower() or "forbidden" in msg.lower():
                print("Telegram blocks bots from messaging users who've "
                      "never opened a dialog with the bot. Ask the user to "
                      "send /start to your bot and re-run this script.",
                      file=sys.stderr)
            return 2
        print(f"[1/8] animated-pig Unicode fallback → {chat_id} "
              f"(no WAVE sticker registered)")
    pace()

    # --- 2. GOOD_MORNING sticker (if registered) ---
    sticker_or_log(pigs.GOOD_MORNING)
    print(f"[2/8] GOOD_MORNING sticker attempt")
    pace(0.5)

    # --- 3. Welcome bubble ---
    tg.send_message(chat_id, ob._welcome_mdv2(first_name="Alex"),
                    reply_markup=ob._welcome_keyboard())
    print(f"[3/8] welcome bubble")
    pace()

    # --- 4. Seniority step ---
    tg.send_message(chat_id, ob._seniority_prompt_mdv2(),
                    reply_markup=ob._seniority_keyboard())
    print(f"[4/8] seniority step (step 2 of 6)")
    pace()

    # --- 4. Sample enriched job card ---
    sample = Job(
        source="linkedin",
        external_id="demo-123",
        title="Senior React Engineer",
        company="Acme Inc",
        location="Remote EU",
        url="https://example.com/job/demo-123",
        posted_at="2026-04-23",
        salary="$80k–$120k",
        snippet=("We are looking for a Senior React + TypeScript engineer "
                 "to build our design system across web and mobile. You "
                 "will work closely with design and ship to production "
                 "weekly."),
    )
    enrichment = {
        "match_score": 4,
        "why_match": ("Your 7 years of React + TypeScript and design-system "
                      "work line up directly with this role."),
        "key_details": {
            "stack": "React, TypeScript, Tailwind",
            "seniority": "Senior (5–10 yrs)",
            "remote_policy": "Remote EU",
            "location": "Anywhere in EU",
            "salary": "$80k–$120k",
            "visa_support": "yes",
            "language": "English",
            "standout": "Design-system team; 4-day week",
        },
    }
    tg.send_message(chat_id, format_job_mdv2(sample, enrichment=enrichment),
                    reply_markup=job_keyboard("demo", url=sample.url))
    print(f"[5/8] sample enriched job card")
    pace()

    # --- 6. THUMBS_UP sticker (simulating Applied tap) ---
    sticker_or_log(pigs.THUMBS_UP)
    print(f"[6/8] THUMBS_UP sticker attempt (Applied simulation)")
    pace(0.5)

    # --- 7. CELEBRATE sticker + summary screen ---
    sticker_or_log(pigs.CELEBRATE)
    summary = ob._summary_mdv2({
        "role": "React Engineer",
        "seniority": "senior",
        "remote": "remote",
        "location": "Remote EU",
        "min_score": 3,
    })
    tg.send_message(chat_id, summary, reply_markup=ob._summary_keyboard())
    print(f"[7/8] CELEBRATE sticker + setup-complete summary bubble")
    pace()

    # --- 8. "You're all set" + main menu ---
    tg.send_message(
        chat_id,
        "🐷  " + mdv2_escape("Demo complete — this is the new main menu."),
        reply_markup=REPLY_KEYBOARD,
    )
    print(f"[8/8] main-menu attach with redesigned reply keyboard")
    print(f"\nDone. Messages delivered to {chat_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
