"""Pig-sticker + custom-emoji registry.

Telegram gives us three ways to show a fancier pig than Unicode 🐷:

  1. **Auto-animated standard emojis** — sending just "🐷" as the sole
     content of a message makes Telegram auto-animate it. Zero setup,
     works for everyone. Implemented here as `send_animated_unicode`.

  2. **Sticker packs** — separate message bubble. Requires a one-time
     capture of each sticker's `file_id` (via `tools/capture_sticker_ids.py`).
     Populate `STICKER_FILE_IDS` below with the captures you like.

  3. **Custom emojis** — Premium-only, rendered inline inside a text
     message via `entities=[{"type":"custom_emoji", ...}]`. Requires each
     emoji's `custom_emoji_id` (Bot API can't list them — see docstring of
     `send_with_custom_emoji`).

Every path here is fail-soft: if a moment has no configured sticker or
custom_emoji_id, the helper degrades to the plain-text branch so the bot
never crashes mid-celebration.

Populating the registries
-------------------------
Run the one-time capture tool, then paste the returned IDs here:

    python tools/capture_sticker_ids.py

Forward a sticker from any chat, the tool prints its `file_id`. Assign the
id to a named moment below and restart the bot. Same pattern for custom
emojis (have a Premium user forward one; the capture tool also prints
`custom_emoji_id` entities).

Design notes
------------
Every "moment" constant is a named trigger (CELEBRATE, SNIFF, SAD, …) so
callers don't hardcode file_ids — we can swap the underlying sticker pack
later with a single-line registry edit and every site that triggers
CELEBRATE auto-inherits the new pig.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

log = logging.getLogger(__name__)


# ---------- moment names ----------
#
# Keep these semantic ("what the user is experiencing"), not visual
# ("pig_waving"). That way swapping the pack doesn't require renaming.

CELEBRATE    = "celebrate"     # setup complete, big milestone
SNIFF        = "sniff"         # loading: scanning job boards, rebuilding profile
SAD          = "sad"           # cleared data, cancelled
THUMBS_UP    = "thumbs_up"     # marked as applied (rate-limited — see below)
WAVE         = "wave"          # welcome back, first greet
SHRUG        = "shrug"         # unknown command, nothing to cancel
GOOD_MORNING = "good_morning"  # top of the daily digest when matches found
NO_MATCHES   = "no_matches"    # daily digest came back empty


# ---------- sticker registry ----------
#
# Paste file_ids captured from `tools/capture_sticker_ids.py` here. The
# file_id is a long opaque string like
# 'CAACAgIAAxkBAAIBY2Lb...'. Keep commented-out suggestions for the packs
# we surveyed so future editors know where to grab replacements.
#
# Suggested packs (add via the t.me/addstickers links):
#   * t.me/addstickers/PigStickerYQ        — general pig pack
#   * t.me/addstickers/PEPPAAA             — Peppa Pig, 23 stickers
#   * t.me/addstickers/fat_roll_pig        — 113 rolly pigs
#   * t.me/addstickers/lihkg_pig_ani_px    — animated pixel-art pigs

# Each moment maps to EITHER a single file_id OR a list of file_ids. If a
# list is provided, the helper picks one at random on each send — that's
# the main reason to pick a large pack like fat_roll_pig (113 stickers):
# variety per moment keeps the bot from feeling like a loop.
#
# Recommended for fat_roll_pig: 2-4 stickers per moment, curated from
# similar emotional expressions. Picking at random inside a tight set
# produces "alive but coherent" — picking from wildly different moods
# inside one moment just reads as chaos.
# Populated from t.me/addstickers/fat_roll_pig via tools/fetch_fat_roll_pig.py.
# Inline comments show the emoji tag the pack author used — handy when
# hand-moving a sticker between buckets later.
STICKER_FILE_IDS: dict[str, str | list[str]] = {
    GOOD_MORNING: [
        "CAACAgUAAxUAAWnrgjyg1G8BAYt01t50_9FXfpyuAAJDAAP04aII_YAW3W_jSGc7BA",  # 🍵
        "CAACAgUAAxUAAWnrgjznkLIzxgKfXS42JScdewxHAAJMAAP04aIIjajHcEcOuMs7BA",  # 🥤
        "CAACAgUAAxUAAWnrgjxRVeykp-BcI-45oAeuawjMAAK3AAP04aIIFHA9iJHIkuE7BA",  # 🥤
    ],
    NO_MATCHES: [
        "CAACAgUAAxUAAWnrgjxkosAvT0t2O0ySu6t_Ri_RAAJ2AAP04aIIOTBjSTVdOok7BA",  # 🛌
        "CAACAgUAAxUAAWnrgjwGz1bMOvCJ1hgdZsfSc-2dAAKEAAP04aII6vTCCAE7y_A7BA",  # 😴
        "CAACAgUAAxUAAWnrgjxRkOdH5SYhLR9Mx3D4tkTTAAKFAAP04aIIyuwj8wHkbbE7BA",  # 😴
    ],
    CELEBRATE: [
        "CAACAgUAAxUAAWnrgjzSIvpAp2ebOvQqAkyHpcgZAAIwAAP04aIIGGrxQj9xWPk7BA",  # 😌
        "CAACAgUAAxUAAWnrgjztCVegLZfYuhExTnoTIZ-2AAI1AAP04aIIcVp5sxJzpoo7BA",  # ✌️
        "CAACAgUAAxUAAWnrgjxI0A0mIc4z4hEUsYs_IIcqAAJKAAP04aIIXN1DAiyc7p07BA",  # ✌
        "CAACAgUAAxUAAWnrgjwNj1p5XRpN8sxiBiHcWydqAAJNAAP04aIITG5C5Q8oICo7BA",  # 👏
        "CAACAgUAAxUAAWnrgjxWCYjRz3DZdM3Eo7q5rQHCAAJlAAP04aIIiP_r3RZYapM7BA",  # 🥰
    ],
    SAD: [
        "CAACAgUAAxUAAWnrgjzXM6MtXlJoNw0DVISJw6G8AAJCAAP04aIIfmzSClmkEAk7BA",  # 😫
        "CAACAgUAAxUAAWnrgjy_0chTQbBZrQPIkZUUkMLAAAJLAAP04aII4McgLyXt8Qw7BA",  # 😭
        "CAACAgUAAxUAAWnrgjxyPOkbDZhBaWSJM7as-CDoAAJuAAP04aIIHs4NS0zaGfA7BA",  # 😭
        "CAACAgUAAxUAAWnrgjxqXyBxT2ZdZ6O8jR7-0jIYAAKAAAP04aII3vV3mmXcC547BA",  # 😩
        "CAACAgUAAxUAAWnrgjynmFO4DNxSlmcGtDfxU4z9AAJSAwACW91RVuvx12o5qMR4OwQ",  # 😢
    ],
    THUMBS_UP: [
        "CAACAgUAAxUAAWnrgjwuvYvlDSYgsmdPXPWKKUCyAAJGAAP04aIIMpNZ6KWVJCE7BA",  # 👊
        "CAACAgUAAxUAAWnrgjzOd93uwam6gCb5heo1WAGAAAJHAAP04aIIYUa5_3BnSac7BA",  # 👊
        "CAACAgUAAxUAAWnrgjyOsvugkZuVfJCmqXSveZq3AAJIAAP04aIICehEhRobLuo7BA",  # 👊
        "CAACAgUAAxUAAWnrgjzsaChoRZI51kN36cpQuwFIAAJJAAP04aIIf40woS23e0I7BA",  # 👊
        "CAACAgUAAxUAAWnrgjxGxNIBcFPuwHTFrVp4fqz1AAJSAAP04aIIn-BhML5ej0s7BA",  # 🙌
    ],
    SNIFF: [
        "CAACAgUAAxUAAWnrgjzTi6AS7Rz953kLFCvps4E-AAIyAAP04aIIigibLaQHSWk7BA",  # 🤔
        "CAACAgUAAxUAAWnrgjy49MHpbrqultMJJm3cwzmXAAJRAAP04aIIF_blKwRYX0I7BA",  # 🤔
        "CAACAgUAAxUAAWnrgjxVCrAJOHVfoCPH1fWemlwoAAKnAgACUodQVjfsQdzdK6DROwQ",  # 🤔
    ],
    WAVE: [
        "CAACAgUAAxUAAWnrgjyPCup9DQrOD4-8ma6jV0NOAAI-AAP04aII_o4KZq_Wfec7BA",  # 🤗
        "CAACAgUAAxUAAWnrgjwyey85yUqGF-sHbidyGooeAAJPAAP04aIItMaE1Vm8lHQ7BA",  # 👐
        "CAACAgUAAxUAAWnrgjzGKnNRV-mNm_SrdB25bvmCAAJwAAP04aIIYYi_Zeu3JRI7BA",  # 😘
        "CAACAgUAAxUAAWnrgjwuUd8MV_jbYbKNjbWGa2CkAAJ7AAP04aIIxHtT_i3BC2g7BA",  # 👋
        "CAACAgUAAxUAAWnrgjwySHwlKor38tNToqivZ6VKAAKHAAP04aIIkyMWvVSf6yQ7BA",  # 😙
    ],
    SHRUG: [
        "CAACAgUAAxUAAWnrgjwX52ZxCZqAmT62jgSf7l1eAAJiAAP04aIIDsGKvYKZDAY7BA",  # 😬
        "CAACAgUAAxUAAWnrgjzaUPeCBQnDBiBLymiji85gAAJ-AAP04aIIhIi01oIBNKw7BA",  # 😬
        "CAACAgUAAxUAAWnrgjz3IuNDlAZrxAZfNkIqJNfSAAKjAwACzKhIVpEqZbqpiYWxOwQ",  # 🤷
        "CAACAgUAAxUAAWnrgjxMvgT_GhGrYeGvfYgujazhAAJbBQACk0NQVuu5Ob4OW9B7OwQ",  # 🤷‍♀️
    ],
}


# ---------- custom-emoji registry ----------
#
# For `send_with_custom_emoji`: map a moment to the `custom_emoji_id`
# (the long numeric string inside a MessageEntity of type 'custom_emoji').
# Only Telegram Premium users will see the animated form — everyone else
# sees the plain-text fallback embedded at the same offset.
#
# To capture: have a Premium user send a message containing one of the
# PigEmoji pack's emojis to the capture tool. It prints the entities with
# their custom_emoji_id values.

CUSTOM_EMOJI_IDS: dict[str, str] = {
    # moment:       "5435957248314298068",
}


# ---------- sending helpers ----------

# Rate-limit state. Keyed by (chat_id, moment); value is last-send epoch.
# Kept in-process — cheap, and restarts are rare enough that a missing
# sticker on bot restart is fine. Guarded because handle_callback runs
# on the polling thread but digest sends run in background threads.
_LAST_SENT: dict[tuple[int, str], float] = {}
_LAST_SENT_LOCK = threading.Lock()

# Per-moment rate-limit in seconds. Zero means always fire. Moments that
# happen in bursts (THUMBS_UP fires on every Applied tap — a 20-job digest
# would produce 20 stickers otherwise) need a cooldown; one-shot moments
# (CELEBRATE, SAD) don't.
_MIN_INTERVAL_S: dict[str, float] = {
    THUMBS_UP: 300.0,   # 5 min — first apply of a session, not every tap
    SNIFF:     120.0,   # 2 min — user might hammer Search
}


def _pick(file_ids: str | list[str] | None) -> str | None:
    """Collapse a single id or a list of ids to one id. None/empty → None."""
    if not file_ids:
        return None
    if isinstance(file_ids, str):
        return file_ids
    if isinstance(file_ids, list):
        valid = [x for x in file_ids if x]
        return random.choice(valid) if valid else None
    return None


def _cooldown_blocks(chat_id: int, moment: str) -> bool:
    """True if this (chat, moment) was sent recently enough to suppress now."""
    interval = _MIN_INTERVAL_S.get(moment, 0.0)
    if interval <= 0:
        return False
    now = time.time()
    with _LAST_SENT_LOCK:
        last = _LAST_SENT.get((chat_id, moment), 0.0)
        if now - last < interval:
            return True
        _LAST_SENT[(chat_id, moment)] = now
    return False


def send_sticker(tg, chat_id: int, moment: str) -> bool:
    """Send a sticker for `moment`, if one is registered and the rate-limit
    gate lets us.

    Returns True if a sticker was sent. False covers three cases:
      * no file_id registered (caller falls back to text)
      * rate-limit is suppressing this moment for this chat
      * the API call itself errored (non-fatal — we log and move on)

    Callers don't need to distinguish — all three mean "didn't send, keep
    going with the text path."
    """
    picked = _pick(STICKER_FILE_IDS.get(moment))
    if not picked:
        return False
    if _cooldown_blocks(chat_id, moment):
        return False
    try:
        tg._call("sendSticker", {"chat_id": chat_id, "sticker": picked})
        return True
    except Exception as e:
        log.warning("send_sticker(%s) failed: %s", moment, e)
        return False


def send_animated_unicode(tg, chat_id: int, emoji: str = "🐷") -> bool:
    """Send a single emoji as the ONLY content of a message so Telegram
    renders the built-in auto-animation. Works for every user, no Premium
    required, no file_ids needed.

    Returns True on success, False on send error.
    """
    try:
        # send_plain (not send_message) so we skip MDv2 parsing — the
        # emoji is bare text, and escaping would mangle nothing in this
        # case but it's the cleanest call.
        tg.send_plain(chat_id, emoji)
        return True
    except Exception as e:
        log.warning("send_animated_unicode(%s) failed: %s", emoji, e)
        return False


def send_with_custom_emoji(
    tg,
    chat_id: int,
    text: str,
    moment: str,
    fallback: str = "🐷",
    reply_markup: dict | None = None,
) -> bool:
    """Send `text` with a custom-emoji entity at the start of `fallback`.

    `text` must begin with `fallback` for this to make sense — the
    custom_emoji entity replaces the fallback with the animated emoji for
    Premium users. Non-Premium users see the fallback unchanged.

    Returns True if sent with a custom emoji, False if we fell through to
    the plain send (no custom_emoji_id configured for this moment).

    Heads up
    --------
    Bot API entity offsets are in UTF-16 code units, not characters. Most
    emojis (including 🐷) are 2 UTF-16 code units. If you configure a
    different fallback, verify its length via `len(fallback.encode('utf-16-le'))//2`.
    """
    ce_id = CUSTOM_EMOJI_IDS.get(moment)
    if not ce_id or not text.startswith(fallback):
        # No custom emoji on file — just send the text as-is. The caller
        # still gets the fallback character, just without animation.
        try:
            tg.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception as e:
            log.warning("send_with_custom_emoji fallback send failed: %s", e)
        return False

    try:
        entity_len = len(fallback.encode("utf-16-le")) // 2
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "entities": [{
                "type": "custom_emoji",
                "offset": 0,
                "length": entity_len,
                "custom_emoji_id": ce_id,
            }],
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        tg._call("sendMessage", payload)
        return True
    except Exception as e:
        log.warning("send_with_custom_emoji(%s) failed: %s", moment, e)
        return False
