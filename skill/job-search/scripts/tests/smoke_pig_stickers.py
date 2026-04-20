#!/usr/bin/env python3
"""Smoke test for pig_stickers: list-picking, rate-limit, fail-soft fallback."""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import pig_stickers as ps  # noqa: E402


class FakeTG:
    """Minimal TG stub — only implements `_call` for sendSticker."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _call(self, method: str, payload: dict) -> dict:
        self.calls.append({"method": method, "payload": payload})
        return {}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)


def main() -> int:
    tg = FakeTG()

    # ---- 1. No registered sticker → fail-soft returns False ----
    ps.STICKER_FILE_IDS.clear()
    ps._LAST_SENT.clear()
    ok = ps.send_sticker(tg, 1, ps.CELEBRATE)
    _assert(ok is False, "unregistered moment should return False")
    _assert(len(tg.calls) == 0, "unregistered moment should not call API")

    # ---- 2. Single-id registration → sends once ----
    ps.STICKER_FILE_IDS[ps.CELEBRATE] = "FILE_ID_A"
    ok = ps.send_sticker(tg, 1, ps.CELEBRATE)
    _assert(ok is True, "single-id registration should send")
    _assert(len(tg.calls) == 1, "should call sendSticker once")
    _assert(tg.calls[0]["payload"]["sticker"] == "FILE_ID_A",
            "payload should carry the registered file_id")

    # ---- 3. List registration → picks from the set ----
    ps.STICKER_FILE_IDS[ps.GOOD_MORNING] = ["A", "B", "C", "D"]
    ps._LAST_SENT.clear()
    tg.calls.clear()
    seen: set[str] = set()
    # GOOD_MORNING has no cooldown so we can fire it repeatedly.
    for _ in range(60):
        ps.send_sticker(tg, 42, ps.GOOD_MORNING)
    for c in tg.calls:
        seen.add(c["payload"]["sticker"])
    _assert(seen.issubset({"A", "B", "C", "D"}),
            f"picks should come only from the registered list, got {seen}")
    _assert(len(seen) >= 2,
            f"60 samples should hit ≥2 variants (got {len(seen)}); if this "
            f"flakes sometimes the list rotation is broken")

    # ---- 4. Rate-limited moment → second call inside cooldown is suppressed ----
    ps.STICKER_FILE_IDS[ps.THUMBS_UP] = "THUMBS"
    ps._LAST_SENT.clear()
    tg.calls.clear()
    ok1 = ps.send_sticker(tg, 99, ps.THUMBS_UP)
    ok2 = ps.send_sticker(tg, 99, ps.THUMBS_UP)
    _assert(ok1 is True, "first THUMBS_UP should send")
    _assert(ok2 is False, "second THUMBS_UP in cooldown should suppress")
    _assert(len(tg.calls) == 1, "rate-limited second call should not hit API")

    # ---- 5. Rate-limit is per-chat ----
    ps._LAST_SENT.clear()
    tg.calls.clear()
    a = ps.send_sticker(tg, 101, ps.THUMBS_UP)
    b = ps.send_sticker(tg, 202, ps.THUMBS_UP)   # different chat
    _assert(a and b, "different chats should NOT share cooldown")
    _assert(len(tg.calls) == 2, "both chats should send")

    # ---- 6. Rate-limit expires ----
    ps._LAST_SENT.clear()
    tg.calls.clear()
    ps.send_sticker(tg, 303, ps.THUMBS_UP)
    # Backdate last-sent so the cooldown has elapsed.
    ps._LAST_SENT[(303, ps.THUMBS_UP)] = time.time() - 9999.0
    ok_after = ps.send_sticker(tg, 303, ps.THUMBS_UP)
    _assert(ok_after, "after cooldown expires, should send again")

    # ---- 7. send_animated_unicode uses send_plain ----
    class PlainTG:
        def __init__(self):
            self.sent = []
        def send_plain(self, chat_id, text):
            self.sent.append((chat_id, text))
            return 1
    ptg = PlainTG()
    ok = ps.send_animated_unicode(ptg, 1, "🐷")
    _assert(ok, "animated unicode should succeed")
    _assert(ptg.sent == [(1, "🐷")], "animated send should be a lone emoji")

    # ---- 8. send_with_custom_emoji: no id registered → falls back to plain send ----
    class FallbackTG:
        def __init__(self):
            self.sent = []
            self.calls = []
        def send_message(self, chat_id, text, parse_mode="MarkdownV2",
                         reply_markup=None, disable_preview=True):
            self.sent.append(("send_message", chat_id, text))
            return 1
        def _call(self, method, payload):
            self.calls.append((method, payload))
    ftg = FallbackTG()
    ps.CUSTOM_EMOJI_IDS.clear()
    ok = ps.send_with_custom_emoji(ftg, 1, "🐷 hi", ps.CELEBRATE)
    _assert(ok is False, "no custom_emoji_id → returns False")
    _assert(len(ftg.sent) == 1 and ftg.sent[0][2] == "🐷 hi",
            "should fall back to send_message with the same text")

    # ---- 9. send_with_custom_emoji: with id → uses _call('sendMessage', ...) with entities ----
    ps.CUSTOM_EMOJI_IDS[ps.CELEBRATE] = "5435957248314298068"
    ftg2 = FallbackTG()
    ok = ps.send_with_custom_emoji(ftg2, 1, "🐷 hi", ps.CELEBRATE)
    _assert(ok is True, "with custom_emoji_id → returns True")
    _assert(len(ftg2.calls) == 1 and ftg2.calls[0][0] == "sendMessage",
            "should use raw sendMessage with entities")
    payload = ftg2.calls[0][1]
    _assert("entities" in payload and payload["entities"][0]["type"] == "custom_emoji",
            "payload should carry a custom_emoji entity")
    _assert(payload["entities"][0]["custom_emoji_id"] == "5435957248314298068",
            "entity should reference the registered id")

    print("PASS  — 9 assertions across registry, rate-limit, rotation, and custom-emoji fallback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
