"""Claude CLI cost surrogate — micro-USD per call.

The `claude` CLI we shell out to does NOT report token counts back, so
we estimate. char-count → token-count via a 4 chars/token rule of thumb
(English mix; understates ~15% on code-heavy prompts), then multiply by
the published per-Mtok rates. The result is stored as int micro-USD on
`claude_calls.cost_estimate_us`.

Disclaim it everywhere it surfaces: `surrogate · ±25% · until SDK migration`.
"""
from __future__ import annotations

from typing import Optional


# Rule-of-thumb char→token conversion. 4.0 fits Anthropic's docs for
# English; raise for code-heavy callers if we ever segment by caller.
_CHARS_PER_TOKEN = 4.0

# REPLACE with current Anthropic public pricing at deploy time.
# USD per 1,000,000 tokens. None entry is the CLI default — assume opus
# (pessimistic) so we never under-bill ourselves.
_PRICES = {
    "haiku":  {"in": 1.00,  "out": 5.00},
    "sonnet": {"in": 3.00,  "out": 15.00},
    "opus":   {"in": 15.00, "out": 75.00},
    None:     {"in": 15.00, "out": 75.00},
}


def estimate_cost_us(
    model: Optional[str],
    prompt_chars: int,
    output_chars: int,
) -> int:
    """Return a micro-USD cost estimate for one CLI call.

    micro-USD = USD * 1_000_000. So a $0.000123 call returns 123. We
    round to int so storage stays compact and SUM()s exact.

    Unknown model aliases fall back to the `None` entry (opus rates) —
    same pessimism as the CLI-default path. This keeps cost reports a
    true ceiling.
    """
    rates = _PRICES.get(model, _PRICES[None])
    p_tok = max(0, int(prompt_chars)) / _CHARS_PER_TOKEN
    o_tok = max(0, int(output_chars)) / _CHARS_PER_TOKEN
    # Per-Mtok rates → per-token = rate / 1e6 USD = rate USD per Mtok.
    # cost_usd = (p_tok * in_rate + o_tok * out_rate) / 1_000_000
    # cost_us  = cost_usd * 1_000_000 = p_tok * in_rate + o_tok * out_rate
    cost_us = p_tok * rates["in"] + o_tok * rates["out"]
    return int(round(cost_us))
