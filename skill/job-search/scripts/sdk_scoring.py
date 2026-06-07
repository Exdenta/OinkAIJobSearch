"""Anthropic-SDK scoring path with prompt caching — GATED behind ANTHROPIC_API_KEY.

WHY THIS EXISTS
===============
The static scoring rubric (`job_enrich._PROMPT`, ~36k chars / ~9.1k tokens)
plus the per-user resume+prefs is re-sent on EVERY Haiku/Sonnet scoring
batch. Input dominates output ~8:1, so that fixed prefix is the bulk of
the spend. The `claude -p` CLI cannot cache a user-prompt prefix; the
Anthropic SDK can, via `cache_control: {type: "ephemeral"}`. Caching the
rubric+profile prefix cuts ~75-90% of the per-batch input cost.

BILLING GATE (read before touching this)
========================================
The live bot authenticates via the user's Claude Code SUBSCRIPTION
(CLAUDE_CODE_* OAuth) — there is NO ANTHROPIC_API_KEY on the process, and
the subscription path bills the user's plan, not dollars. The SDK path
needs an API key and bills REAL money. So this module is a NO-OP unless
ANTHROPIC_API_KEY is set:

  * key ABSENT (current production)  -> `sdk_available()` is False ->
    `job_enrich` keeps the EXACT existing CLI path. Zero behavior change.
  * key PRESENT                       -> `sdk_available()` is True ->
    `job_enrich` routes pure-text scoring batches here.

CONTRACT
========
`score_batch()` mirrors the failure-tolerant contract of
`instrumentation.wrappers.wrapped_run_p`: it returns a CLI-STYLE JSON
ENVELOPE STRING ({"result": "...", "usage": {...}, ...}) on success, or
None on ANY error. Returning the same envelope shape the CLI emits means
`job_enrich._enrich_one_chunk` parses both paths with the SAME code
(`extract_assistant_text` + `_is_empty_result_envelope` + `parse_json_block`)
and the produced verdicts are byte-for-byte the same shape. None triggers
the caller's existing graceful CLI fallback.

The prompt CONTENT, JSON output contract, model tier, and scoring
semantics are IDENTICAL to the CLI path — this module only changes the
TRANSPORT (SDK vs subprocess) and adds caching. Quality must not move.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model-ID mapping
# ---------------------------------------------------------------------------
# The `claude` CLI accepts short aliases ("haiku"/"sonnet") and resolves
# them to whatever generation is current at runtime. The Anthropic SDK
# (the Messages API) needs a CONCRETE model ID — short aliases 404. So we
# map the project's existing aliases to current model IDs here.
#
# Current IDs (per the /claude-api skill, cached 2026-05-26):
#   haiku  -> claude-haiku-4-5   (200K ctx, $1/$5 per Mtok)
#   sonnet -> claude-sonnet-4-6  (1M ctx,  $3/$15 per Mtok)
#
# Operators can override either via env var without a code edit (same
# escape-hatch pattern as CLAUDE_SMALLEST_MODEL / CLAUDE_MID_MODEL in
# claude_cli.py). A model string the CLI knows but the SDK doesn't (e.g.
# a bare "haiku") falls through unmapped and the SDK 400s — we catch that
# and fall back to the CLI, so an unknown alias degrades, never crashes.
_HAIKU_SDK_MODEL = os.environ.get("CLAUDE_HAIKU_SDK_MODEL", "claude-haiku-4-5")
_SONNET_SDK_MODEL = os.environ.get("CLAUDE_SONNET_SDK_MODEL", "claude-sonnet-4-6")

# Map the CLI aliases (claude_cli.SMALLEST_MODEL / MID_MODEL values) to
# SDK model IDs. Anything not in this map is passed through verbatim so a
# caller that already hands us a concrete ID still works.
_ALIAS_TO_SDK_MODEL = {
    "haiku": _HAIKU_SDK_MODEL,
    "sonnet": _SONNET_SDK_MODEL,
}

# Per-request output cap. The scoring response is a compact JSON array of
# verdicts; even a 10-job batch with full why_match/why_mismatch is a few
# thousand tokens. 8192 leaves generous headroom without inviting runaway
# generation. (Matches the spirit of the CLI path, which has no explicit
# cap but produces the same short JSON.)
_MAX_OUTPUT_TOKENS = 8192


def _resolve_sdk_model(model: str | None) -> str | None:
    """Map a CLI alias to a concrete SDK model ID.

    Returns None when `model` is falsy (the SDK requires an explicit
    model, unlike the CLI's implicit default) so the caller falls back to
    the CLI rather than guessing a tier.
    """
    if not model:
        return None
    return _ALIAS_TO_SDK_MODEL.get(model, model)


def sdk_available() -> bool:
    """True iff the SDK scoring path should be used.

    Requires BOTH:
      * ANTHROPIC_API_KEY present in the environment — the billing gate.
        Absent it, we MUST stay on the subscription CLI path (the no-risk
        production default).
      * the `anthropic` package importable — degrade to CLI if the dep
        isn't installed in this environment.

    Cheap enough to call per batch; the import is cached by Python after
    the first success.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except Exception:
        # Dep missing / broken — silently prefer the CLI. Logged once at
        # DEBUG so an operator who SET the key but forgot the install can
        # find the reason without log spam on every batch.
        log.debug(
            "sdk_scoring: ANTHROPIC_API_KEY set but `anthropic` import "
            "failed — falling back to CLI path", exc_info=True,
        )
        return False
    return True


# Module-level client cache. The Anthropic client is cheap to construct
# but reusing one keeps the HTTP connection pool warm across batches in a
# run. Rebuilt lazily; never raises out of the getter.
_CLIENT: Any = None


def _get_client() -> Any | None:
    """Return a cached `anthropic.Anthropic()` client, or None on failure.

    The client reads ANTHROPIC_API_KEY from the environment itself. We
    set a conservative max_retries so the SDK's built-in 429/5xx backoff
    runs but a sustained outage still surfaces as None quickly enough for
    the CLI fallback to take over within the batch's time budget.
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    try:
        import anthropic
        _CLIENT = anthropic.Anthropic(max_retries=2)
        return _CLIENT
    except Exception:
        log.warning("sdk_scoring: failed to construct Anthropic client; "
                    "falling back to CLI", exc_info=True)
        return None


def _envelope_from_message(message: Any) -> str:
    """Build a CLI-style JSON envelope string from an SDK Message.

    The CLI's `--output-format json` shape that the rest of the pipeline
    already parses:

        {"result": "<assistant text>",
         "total_cost_usd": <float|absent>,
         "num_turns": 1,
         "usage": {"input_tokens": N, "output_tokens": M,
                   "cache_creation_input_tokens": C,
                   "cache_read_input_tokens": R}}

    We reconstruct it from `message.content` (concatenating text blocks)
    and `message.usage` so `extract_assistant_text` / parse_json_block in
    job_enrich see an identical envelope regardless of transport. We do
    NOT populate `total_cost_usd` — the SDK reports tokens, not a dollar
    figure; job_enrich's telemetry path computes cost from tokens
    instead (see score_batch).
    """
    text_parts: list[str] = []
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text_parts.append(getattr(block, "text", "") or "")
    result_text = "".join(text_parts)

    usage = getattr(message, "usage", None)
    usage_out = {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_creation_input_tokens": int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
        "cache_read_input_tokens": int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        ),
    } if usage is not None else {}

    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": result_text,
        "stop_reason": getattr(message, "stop_reason", None),
        "num_turns": 1,
        "usage": usage_out,
    }
    return json.dumps(envelope, ensure_ascii=False)


def score_batch(
    store: Any,
    caller: str,
    stable_prefix: str,
    volatile_suffix: str,
    *,
    model: str | None = None,
    timeout_s: int = 240,
    pipeline_run_id: int | None = None,
    chat_id: int | None = None,
) -> str | None:
    """Run ONE scoring batch through the Anthropic SDK with prompt caching.

    Args:
      store:            MonitorStore | None — telemetry sink (best-effort).
      caller:           telemetry label, e.g. "job_enrich:haiku".
      stable_prefix:    the rubric + resume + prefs text. BYTE-STABLE across
                        every batch in a pass (same resume/prefs) → carries
                        the cache_control breakpoint.
      volatile_suffix:  the per-batch jobs JSON. Goes AFTER the breakpoint
                        so it never invalidates the cached prefix.
      model:            CLI alias ("haiku"/"sonnet") or concrete SDK id.
      timeout_s:        per-request wall-clock cap (maps to the SDK client's
                        per-call timeout).

    Returns a CLI-style JSON envelope string on success, or None on ANY
    error (so the caller falls back to the CLI path). Never raises.

    CACHE LAYOUT
    ------------
    A single user-turn message with two content blocks:
        block[0] = stable_prefix   + cache_control: {type: "ephemeral"}
        block[1] = volatile_suffix (no cache_control)
    Prompt caching is a prefix match (tools -> system -> messages render
    order). With no tools and no system prompt, block[0] is the entire
    cacheable prefix; the breakpoint on it caches the rubric+profile, and
    block[1]'s per-batch job briefs sit after the breakpoint and never
    invalidate it. The prefix is ~9.1k tokens — comfortably above the
    4096-token Haiku / 2048-token Sonnet minimum cacheable prefix, so the
    cache actually engages.

    We deliberately put the prefix in a MESSAGE block, not the `system`
    field: the existing CLI prompt is one user prompt with the rubric
    inline, so keeping it a user-turn content block makes the SDK and CLI
    inputs semantically identical (same text, same role) → identical
    model behavior. The only delta is the cache_control marker, which does
    not change what the model sees.
    """
    sdk_model = _resolve_sdk_model(model)
    if not sdk_model:
        log.debug("sdk_scoring: no SDK model for alias %r — CLI fallback", model)
        return None

    client = _get_client()
    if client is None:
        return None

    started_at = time.time()
    message = None
    try:
        # Per-call timeout via with_options so we don't mutate the shared
        # client. The SDK raises APITimeoutError on overrun, caught below
        # → None → CLI fallback.
        message = client.with_options(timeout=float(timeout_s)).messages.create(
            model=sdk_model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": stable_prefix,
                        # Cache breakpoint on the byte-stable rubric+profile
                        # prefix. Default 5-minute TTL is plenty — every
                        # batch in a run hits within seconds of the first.
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": volatile_suffix,
                    },
                ],
            }],
        )
    except Exception:
        # ANY SDK error (auth, rate limit after retries, timeout, network,
        # 400 on an unknown model id) → None so job_enrich falls back to
        # the CLI path. Never crash the scoring cycle on the SDK.
        log.warning(
            "sdk_scoring: SDK call failed for caller=%s model=%s — "
            "falling back to CLI", caller, sdk_model, exc_info=True,
        )
        _record_telemetry(
            store, caller=caller, model=model, prompt_chars=len(stable_prefix) + len(volatile_suffix),
            stdout=None, started_at=started_at, finished_at=time.time(),
            pipeline_run_id=pipeline_run_id, chat_id=chat_id,
        )
        return None

    finished_at = time.time()
    envelope = _envelope_from_message(message)
    _record_telemetry(
        store, caller=caller, model=model,
        prompt_chars=len(stable_prefix) + len(volatile_suffix),
        stdout=envelope, message=message,
        started_at=started_at, finished_at=finished_at,
        pipeline_run_id=pipeline_run_id, chat_id=chat_id,
    )
    return envelope


# ---------------------------------------------------------------------------
# Telemetry (best-effort — a failure here must NEVER break the call path)
# ---------------------------------------------------------------------------

def _record_telemetry(
    store: Any,
    *,
    caller: str,
    model: str | None,
    prompt_chars: int,
    stdout: str | None,
    started_at: float,
    finished_at: float,
    pipeline_run_id: int | None,
    chat_id: int | None,
    message: Any = None,
) -> None:
    """Record the SDK call into `claude_calls` via MonitorStore.

    Captures the REAL token counts the SDK reports — including
    cache_read / cache_creation — plus a cost figure computed from those
    tokens (the SDK gives tokens, not a dollar total). Mirrors the column
    set `instrumentation.wrappers` writes for the CLI path so `/stats`,
    `/runlog`, and the daily digest aggregate both transports coherently.

    Degrades gracefully on every axis:
      * store None (no MonitorStore handy) -> resolve the lazy default,
        exactly like the wrappers do.
      * a missing column / older record_claude_call signature -> retry
        with only the always-present positional+core args.
      * any exception -> swallow + log. Telemetry is never load-bearing.
    """
    try:
        from instrumentation.wrappers import _resolve_store  # reuse lazy default
        resolved = _resolve_store(store)
    except Exception:
        resolved = store
    if resolved is None:
        return

    output_chars = len(stdout or "")
    # result_chars / status mirror the wrappers' inference so both
    # transports' rows are comparable.
    try:
        from claude_cli import extract_assistant_text
        result_text = extract_assistant_text(stdout) if stdout is not None else ""
    except Exception:
        result_text = ""
    result_chars = len(result_text or "")
    if stdout is None:
        status = "cli_missing"   # SDK failed; same bucket the CLI uses for None
    elif output_chars > 0 and result_chars == 0:
        status = "empty_result"
    else:
        status = "ok"

    elapsed_ms = int((finished_at - started_at) * 1000)

    # Real usage numbers + token-derived cost from the SDK message.
    usage_kwargs: dict[str, Any] = {}
    if message is not None:
        usage = getattr(message, "usage", None)
        if usage is not None:
            in_tok = int(getattr(usage, "input_tokens", 0) or 0)
            out_tok = int(getattr(usage, "output_tokens", 0) or 0)
            cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
            cache_creation = int(
                getattr(usage, "cache_creation_input_tokens", 0) or 0
            )
            usage_kwargs = {
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_read_tokens": cache_read,
                "cache_creation_tokens": cache_creation,
                "num_turns": 1,
                "cost_actual_us": _cost_us_from_tokens(
                    model, in_tok, out_tok, cache_read, cache_creation
                ),
            }

    base_kwargs = dict(
        caller=caller,
        prompt_chars=prompt_chars,
        output_chars=output_chars,
        elapsed_ms=elapsed_ms,
        status=status,
        pipeline_run_id=pipeline_run_id,
        chat_id=chat_id,
        model=model,
        exit_code=None,
        started_at=started_at,
        finished_at=finished_at,
        result_chars=result_chars,
    )
    try:
        resolved.record_claude_call(**base_kwargs, **usage_kwargs)
    except TypeError:
        # An older MonitorStore.record_claude_call that predates the Tier 3
        # usage columns. Degrade to the core signature so we still record
        # the call (volume + surrogate cost) rather than losing the row.
        try:
            resolved.record_claude_call(**base_kwargs)
        except Exception:
            log.debug("sdk_scoring: telemetry record (core) failed; continuing",
                      exc_info=True)
    except Exception:
        log.debug("sdk_scoring: telemetry record failed; continuing",
                  exc_info=True)

    # Forensic line — mirrors wrappers._forensic_log_call so the SDK
    # path is queryable in the same JSONL stream.
    try:
        from forensic import log_step
        log_step(
            "sdk_scoring.score_batch",
            input={
                "caller": caller,
                "model": model,
                "prompt_chars": prompt_chars,
            },
            output={
                "status": status,
                "output_chars": output_chars,
                "result_chars": result_chars,
                "cache_read_tokens": usage_kwargs.get("cache_read_tokens"),
                "cache_creation_tokens": usage_kwargs.get("cache_creation_tokens"),
            },
            chat_id=chat_id,
            run_id=pipeline_run_id,
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        log.debug("sdk_scoring: forensic emit failed; continuing", exc_info=True)


# Per-Mtok pricing in micro-USD, mirroring telemetry.cost._PRICES. Kept
# here (not imported) because telemetry.cost.estimate_cost_us works off
# CHARS, while the SDK gives us real TOKENS and a separately-priced cache
# tier — a different computation. Cache reads bill at ~0.1x input;
# cache writes (creation) at ~1.25x input (5-minute TTL). These multipliers
# are Anthropic's published cache economics.
_PRICE_PER_MTOK = {
    "haiku":  {"in": 1.00,  "out": 5.00},
    "sonnet": {"in": 3.00,  "out": 15.00},
    None:     {"in": 15.00, "out": 75.00},  # CLI-default / unknown → opus rates
}
_CACHE_READ_MULT = 0.10
_CACHE_WRITE_MULT = 1.25


def _cost_us_from_tokens(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
) -> int:
    """Compute micro-USD cost from real SDK token counts.

    The Anthropic Messages API splits input into three billed tiers:
      * `input_tokens`               — uncached input, full input rate.
      * `cache_read_input_tokens`    — served from cache, ~0.1x input.
      * `cache_creation_input_tokens`— written to cache, ~1.25x input.
    Output bills at the output rate. Returns int micro-USD (USD * 1e6),
    matching the `cost_actual_us` column unit.
    """
    rates = _PRICE_PER_MTOK.get(model, _PRICE_PER_MTOK[None])
    in_rate = rates["in"]
    out_rate = rates["out"]
    # rate is USD per 1e6 tokens; cost_us = USD * 1e6 = tokens * rate.
    cost_us = (
        max(0, input_tokens) * in_rate
        + max(0, cache_read_tokens) * in_rate * _CACHE_READ_MULT
        + max(0, cache_creation_tokens) * in_rate * _CACHE_WRITE_MULT
        + max(0, output_tokens) * out_rate
    )
    return int(round(cost_us))
