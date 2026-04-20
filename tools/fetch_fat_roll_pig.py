#!/usr/bin/env python3
"""Fetch the fat_roll_pig sticker pack and auto-categorize it by emoji tag.

Prints a ready-to-paste `STICKER_FILE_IDS = {...}` block you can drop
straight into `skill/job-search/scripts/pig_stickers.py`. Much faster
than forwarding stickers one by one.

How it works
------------
Telegram's Bot API method `getStickerSet(name=...)` returns every sticker
in the pack — file_id, file_unique_id, emoji tag, animated/video flags.
Each sticker's `emoji` field tells us which standard emoji the sticker
creator tagged it with (e.g. a sleeping pig sticker is usually tagged
with 😴 or 💤). We bucket stickers into our moments based on those tags.

Stickers that don't match any known tag go into the UNCATEGORIZED block
so you can eyeball them in the pack and reassign by hand if needed.

Usage
-----
    # Make sure the bot is STOPPED (getUpdates is single-consumer but
    # this script uses a different endpoint — still, safer to stop it).
    python tools/fetch_fat_roll_pig.py

    # Other packs work too:
    python tools/fetch_fat_roll_pig.py PEPPAAA
    python tools/fetch_fat_roll_pig.py PigStickerYQ

Paste the printed output (between BEGIN/END markers) over the current
`STICKER_FILE_IDS = {...}` block in pig_stickers.py, then restart the
bot. Done — no manual forwarding required.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT_ROOT / "skill" / "job-search" / "scripts"
sys.path.insert(0, str(SCRIPTS))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

from telegram_client import TelegramClient  # noqa: E402


DEFAULT_PACK = "fat_roll_pig"

# Emoji → moment heuristics. Order matters: first match wins, so more
# specific categories should come before generic "happy" catch-alls.
#
# Keys are standard emoji strings (the same ones sticker creators tag
# their stickers with). Values are the moment constants from
# pig_stickers.py. Pack authors are inconsistent about tagging, so we
# cast a wide net per moment.
EMOJI_TO_MOMENT: list[tuple[str, str]] = [
    # ---- SAD — tears, crying, sobbing, gloom, exhaustion ----
    ("😭", "SAD"),
    ("😢", "SAD"),
    ("🥺", "SAD"),
    ("😞", "SAD"),
    ("💔", "SAD"),
    ("☹️", "SAD"),
    ("😔", "SAD"),
    ("😫", "SAD"),        # exhausted — tighter to SAD than NO_MATCHES
    ("😩", "SAD"),
    ("😖", "SAD"),
    ("🙁", "SAD"),

    # ---- NO_MATCHES — sleeping, zzz, tired, bored, dead-eye ----
    ("😴", "NO_MATCHES"),
    ("💤", "NO_MATCHES"),
    ("🥱", "NO_MATCHES"),
    ("😑", "NO_MATCHES"),
    ("😒", "NO_MATCHES"),
    ("🫠", "NO_MATCHES"),
    ("🛌", "NO_MATCHES"),

    # ---- GOOD_MORNING — morning rituals, drinks, coffee/tea, food ----
    # Pack authors often tag "pig with drink" as 🍵 or 🥤 even when it's
    # clearly a coffee/tea scene. Cast a wide net here.
    ("☕", "GOOD_MORNING"),
    ("🍵", "GOOD_MORNING"),   # tea — big hit in fat_roll_pig
    ("🥛", "GOOD_MORNING"),
    ("🥤", "GOOD_MORNING"),   # generic beverage
    ("🍹", "GOOD_MORNING"),
    ("🧋", "GOOD_MORNING"),   # bubble tea
    ("🍩", "GOOD_MORNING"),
    ("🥐", "GOOD_MORNING"),
    ("🧇", "GOOD_MORNING"),
    ("🥞", "GOOD_MORNING"),
    ("🍞", "GOOD_MORNING"),
    ("🍳", "GOOD_MORNING"),
    ("🌅", "GOOD_MORNING"),
    ("🌞", "GOOD_MORNING"),
    ("☀️", "GOOD_MORNING"),
    ("🌄", "GOOD_MORNING"),

    # ---- THUMBS_UP — approval, OK, yes, thanks, fist bump ----
    ("👍", "THUMBS_UP"),
    ("👌", "THUMBS_UP"),
    ("🙌", "THUMBS_UP"),
    ("✅", "THUMBS_UP"),
    ("💪", "THUMBS_UP"),
    ("👊", "THUMBS_UP"),      # fist bump — was in unmatched
    ("🙏", "THUMBS_UP"),

    # ---- CELEBRATE — party, cheer, chill-win, peace, love reactions ----
    ("🎉", "CELEBRATE"),
    ("🎊", "CELEBRATE"),
    ("🥳", "CELEBRATE"),
    ("🏆", "CELEBRATE"),
    ("🎁", "CELEBRATE"),
    ("🍾", "CELEBRATE"),
    ("✨", "CELEBRATE"),
    ("🎈", "CELEBRATE"),
    ("🎂", "CELEBRATE"),
    ("✌️", "CELEBRATE"),     # peace sign — chill win, in unmatched
    ("✌", "CELEBRATE"),      # bare (no VS16) variant, same meaning
    ("👏", "CELEBRATE"),     # clapping / applause — in unmatched (n=2)
    ("😌", "CELEBRATE"),     # relieved / content — soft celebrate
    ("🥰", "CELEBRATE"),
    ("😍", "CELEBRATE"),
    ("🤩", "CELEBRATE"),
    ("💕", "CELEBRATE"),
    ("❤️", "CELEBRATE"),
    ("😄", "CELEBRATE"),
    ("😊", "CELEBRATE"),
    ("😁", "CELEBRATE"),

    # ---- WAVE — hi, hello, hug, open arms ----
    ("👋", "WAVE"),
    ("🤗", "WAVE"),
    ("👐", "WAVE"),       # open hands — "come here" / greeting
    ("😘", "WAVE"),       # flying kiss — friendly send-off
    ("😙", "WAVE"),

    # ---- SHRUG — confused, unsure, "I dunno" ----
    # Narrowed: 🤔 ("hmm, let me look at this") is NOT really shrug-y —
    # it maps better to SNIFF (curious/investigating). The pack has
    # plenty of 🤷 variants to cover the actual shrug moment.
    ("🤷", "SHRUG"),
    ("😕", "SHRUG"),
    ("❓", "SHRUG"),
    ("😬", "SHRUG"),       # grimace — "eh, awkward" close enough to shrug

    # ---- SNIFF — curious, searching, pondering, staring ----
    # 🤔 is the critical catch here: sticker packs rarely tag anything
    # with 🔍/👀 but 🤔 is ubiquitous and semantically fits "pig
    # investigating the job boards."
    ("🤔", "SNIFF"),
    ("🔍", "SNIFF"),
    ("👀", "SNIFF"),
    ("🕵️", "SNIFF"),
    ("🧐", "SNIFF"),
    ("🔎", "SNIFF"),
]

# Supplementary emoji → moment mappings that run AFTER the primary list
# above. Used for tags that should contribute to a moment but only when
# the moment hasn't already hit its cap from tighter-fitting tags.
# (Not yet wired — the primary list stays linear. Kept here as a
# decision-log hint if the pack ever needs multi-tier matching.)


def classify(sticker_emoji: str | None) -> str | None:
    """Return the moment name for this sticker's emoji tag, or None."""
    if not sticker_emoji:
        return None
    for emoji, moment in EMOJI_TO_MOMENT:
        if emoji in sticker_emoji:
            return moment
    return None


def main(argv: list[str]) -> int:
    pack = argv[1] if len(argv) > 1 else DEFAULT_PACK
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("FATAL: TELEGRAM_BOT_TOKEN missing in .env", file=sys.stderr)
        return 1

    tg = TelegramClient(token=token)

    try:
        result = tg._call("getStickerSet", {"name": pack})
    except Exception as e:
        print(f"FAILED to fetch pack '{pack}': {e}", file=sys.stderr)
        if "STICKERSET_INVALID" in str(e):
            print(f"Pack '{pack}' doesn't exist or the name is wrong. "
                  "Check t.me/addstickers/<name>.", file=sys.stderr)
        return 2

    stickers = result.get("stickers") or []
    if not stickers:
        print(f"Pack '{pack}' returned no stickers.", file=sys.stderr)
        return 3

    print(f"# Pack: {result.get('title') or pack}  ({len(stickers)} stickers)")
    print(f"# Source: t.me/addstickers/{pack}")
    print()

    # Bucket stickers by moment.
    buckets: dict[str, list[tuple[str, str]]] = {}
    uncategorized: list[tuple[str, str]] = []
    for st in stickers:
        emoji = (st.get("emoji") or "").strip()
        file_id = st.get("file_id") or ""
        if not file_id:
            continue
        moment = classify(emoji)
        entry = (file_id, emoji or "?")
        if moment:
            buckets.setdefault(moment, []).append(entry)
        else:
            uncategorized.append(entry)

    # Render a ready-to-paste block.
    print("# ============================================================")
    print("# BEGIN STICKER_FILE_IDS — paste this into pig_stickers.py")
    print("# ============================================================")
    print()
    print("STICKER_FILE_IDS: dict[str, str | list[str]] = {")
    # Order with the likely-highest-impact moments first.
    order = ["GOOD_MORNING", "NO_MATCHES", "CELEBRATE", "SAD",
             "THUMBS_UP", "SNIFF", "WAVE", "SHRUG"]
    for moment in order:
        rows = buckets.get(moment) or []
        if not rows:
            print(f"    # {moment}:       (no stickers tagged for this moment)")
            continue
        # Cap the list at 5 per moment so rotation stays coherent. The pack
        # may tag 20 stickers with 🎉 but more than 5 variants per moment
        # reads as chaos, not freshness.
        rows = rows[:5]
        print(f"    {moment}: [")
        for file_id, emoji in rows:
            # Inline comment shows the emoji tag so you can spot-check.
            print(f'        "{file_id}",  # {emoji}')
        print(f"    ],")
    print("}")
    print()
    print("# ============================================================")
    print("# END STICKER_FILE_IDS")
    print("# ============================================================")
    print()

    # Summary at the end.
    print(f"# Summary: {len(stickers)} stickers, "
          f"{sum(len(v) for v in buckets.values())} auto-categorized, "
          f"{len(uncategorized)} unmatched.")

    # Distribution of unmatched emoji tags. Helps you spot the "dominant
    # mood" of the unclassified leftovers — if you see 40 stickers tagged
    # with a single emoji you hadn't considered, that's a hint to either
    # expand the heuristics or hand-assign that emoji to a moment.
    if uncategorized:
        from collections import Counter
        counts = Counter(e for _, e in uncategorized)
        top = counts.most_common(15)
        print()
        print(f"# Top unmatched emoji tags (n={len(uncategorized)}):")
        for emoji, n in top:
            print(f"#   {emoji} × {n}")

    # Dump a flat list of every sticker when --list-all is passed.
    # Useful when you want to hand-assign specific file_ids to a moment
    # that the heuristics missed (e.g. a specific "searching" pig).
    if "--list-all" in argv:
        print()
        print("# ---- Full sticker dump ----")
        for st in stickers:
            print(f"# {st.get('emoji') or '?'}  {st.get('file_id')}")

    # Dump ONLY the unmatched stickers with their file_ids when --unmatched
    # is passed. Lighter than --list-all when you just want to salvage
    # what the heuristics missed.
    if "--unmatched" in argv:
        print()
        print(f"# ---- Unmatched stickers (n={len(uncategorized)}) ----")
        for file_id, emoji in uncategorized:
            print(f"# {emoji}  {file_id}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
