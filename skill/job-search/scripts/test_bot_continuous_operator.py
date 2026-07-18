"""Self-check for the OINK_CONTINUOUS_INCLUDE_OPERATOR escape hatch in
bot._resolve_continuous_chat_ids (self-host: operator chat_id == the one
real user, so the hosted-deployment default of excluding the operator from
continuous auto-search must be overridable).

Run directly: python3 skill/job-search/scripts/test_bot_continuous_operator.py
"""
from __future__ import annotations

import os

import bot


class _FakeDB:
    def onboarded_chat_ids(self) -> list[int]:
        return []


def demo() -> None:
    chat_id = "8283816977"
    db = _FakeDB()

    for key in ("OPERATOR_CHAT_ID", "OINK_CONTINUOUS_CHAT_ID", "OINK_CONTINUOUS_INCLUDE_OPERATOR"):
        os.environ.pop(key, None)

    try:
        os.environ["OPERATOR_CHAT_ID"] = chat_id
        os.environ["OINK_CONTINUOUS_CHAT_ID"] = chat_id

        assert bot._resolve_continuous_chat_ids(db) == [], (
            "default (hosted-deployment) behavior must still exclude the operator"
        )

        os.environ["OINK_CONTINUOUS_INCLUDE_OPERATOR"] = "1"
        assert bot._resolve_continuous_chat_ids(db) == [int(chat_id)], (
            "self-host opt-out must let the operator's own chat_id through"
        )
    finally:
        for key in ("OPERATOR_CHAT_ID", "OINK_CONTINUOUS_CHAT_ID", "OINK_CONTINUOUS_INCLUDE_OPERATOR"):
            os.environ.pop(key, None)

    print("ok")


if __name__ == "__main__":
    demo()
