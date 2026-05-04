"""Fit analysis: evaluate how well a user's resume aligns with a job posting.

The button this module powers sits next to "Tailor resume" on every job card.
The two serve different intents:

  * "Tailor resume" produces a REWRITTEN resume tuned to the posting.
  * "Analyze fit" produces an EVALUATION — strengths, gaps, a score, and
    a recommendation on whether to apply. No rewrite.

Flow
----
bot.py::handle_callback catches `fit:<job_id>`. It drops a placeholder
message into the chat, fires a background thread that calls
`build_fit_analysis_ai`, and edits the placeholder with the rendered
analysis (or an error note). Same pattern as the tailor flow — keeps
callback handling fast and avoids blocking the poll loop.

Caching
-------
Fit analyses are cached in `fit_analyses` (DB) keyed by (chat_id, job_id)
so repeat taps are instant. The cache is invalidated when the user's
resume changes (tracked via a sha1 of `resume_text`).
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from claude_cli import run_p, extract_assistant_text, parse_json_block
from instrumentation.wrappers import wrapped_run_p
import forensic

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
_PROMPT_PATH = HERE / "prompts" / "fit_analysis.txt"

# Cap on input sizes sent to the model. Matches resume_tailor's caps so we
# stay within a single model's context comfortably and the prompt is
# deterministic in size regardless of input shape. Resumes past 12k chars
# are almost always boilerplate at the tail — truncation is safe.
_MAX_RESUME_CHARS  = 12_000
_MAX_SNIPPET_CHARS = 1_200
_MAX_TITLE_CHARS   = 200
_MAX_COMPANY_CHARS = 120
_MAX_LOC_CHARS     = 120
_MAX_URL_CHARS     = 400

_VALID_VERDICTS   = {"strong_match", "solid_match", "stretch", "weak_match"}
_VALID_SEVERITIES = {"critical", "moderate", "minor"}


def resume_sha1(resume_text: str) -> str:
    """Stable 40-char hash used as the cache invalidation key. An updated
    resume → new hash → cache miss → fresh analysis."""
    return hashlib.sha1((resume_text or "").encode("utf-8")).hexdigest()


def _load_prompt_template() -> str:
    """Read the prompt file once per call. Cheap, and reloading on each
    call means prompt edits don't require a bot restart."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _normalize(analysis: Any) -> dict | None:
    """Coerce the model's response into the canonical schema.

    Returns a cleaned dict on success, None on structural failure. We are
    lenient with values (clamp score, fix bad verdicts, cap list lengths)
    but strict on types — a non-dict, or missing 'verdict' / 'fit_score',
    is treated as a parse failure and the caller falls back to an error
    message.
    """
    if not isinstance(analysis, dict):
        return None

    # ---- verdict ----
    verdict = str(analysis.get("verdict") or "").strip().lower()
    if verdict not in _VALID_VERDICTS:
        return None

    # ---- fit_score (clamp to [0, 5]) ----
    try:
        score = int(analysis.get("fit_score") or 0)
    except (TypeError, ValueError):
        return None
    score = max(0, min(5, score))

    # ---- headline ----
    headline = str(analysis.get("headline") or "").strip()[:240]
    if not headline:
        # Synthesize something rather than returning None — the user
        # should never see an empty card.
        headline = {
            "strong_match": "Strong fit on the core requirements.",
            "solid_match":  "Solid fit with a few manageable gaps.",
            "stretch":      "A stretch — some real gaps to close.",
            "weak_match":   "Significant mismatch on key requirements.",
        }[verdict]

    # ---- strengths ----
    strengths_in = analysis.get("strengths") or []
    strengths: list[dict] = []
    if isinstance(strengths_in, list):
        for s in strengths_in[:6]:
            if not isinstance(s, dict):
                continue
            area = str(s.get("area") or "").strip()[:120]
            evidence = str(s.get("evidence") or "").strip()[:240]
            if area or evidence:
                strengths.append({"area": area, "evidence": evidence})

    # ---- gaps ----
    gaps_in = analysis.get("gaps") or []
    gaps: list[dict] = []
    if isinstance(gaps_in, list):
        for g in gaps_in[:6]:
            if not isinstance(g, dict):
                continue
            area = str(g.get("area") or "").strip()[:120]
            severity = str(g.get("severity") or "").strip().lower()
            if severity not in _VALID_SEVERITIES:
                severity = "moderate"   # safer default — user still notices
            evidence = str(g.get("evidence") or "").strip()[:240]
            mitigation = str(g.get("mitigation") or "").strip()[:240]
            if area or evidence:
                gaps.append({
                    "area": area,
                    "severity": severity,
                    "evidence": evidence,
                    "mitigation": mitigation,
                })

    # ---- hidden_requirements ----
    hidden_in = analysis.get("hidden_requirements") or []
    hidden: list[str] = []
    if isinstance(hidden_in, list):
        for h in hidden_in[:4]:
            s = str(h or "").strip()[:240]
            if s:
                hidden.append(s)

    # ---- recommendation ----
    recommendation = str(analysis.get("recommendation") or "").strip()[:600]

    return {
        "verdict":              verdict,
        "fit_score":            score,
        "headline":             headline,
        "strengths":            strengths,
        "gaps":                 gaps,
        "hidden_requirements":  hidden,
        "recommendation":       recommendation,
    }


def build_fit_analysis_ai(
    resume_text: str,
    job: dict,
    timeout_s: int = 180,
) -> dict | None:
    """Ask the `claude` CLI for a structured fit analysis of resume vs. JD.

    Returns the normalized dict on success, or None on any failure (CLI
    missing, timeout, non-JSON response, validation failure). Callers
    should show a concise "couldn't analyze" message in the UI when this
    returns None — the bot continues unharmed.
    """
    with forensic.step(
        "fit_analyzer.build_fit_analysis_ai",
        input={
            "job_title": (job.get("title") or "")[:120],
            "job_company": (job.get("company") or "")[:80],
            "job_url": (job.get("url") or "")[:200],
            "resume_chars": len(resume_text or ""),
            "resume_sha1": resume_sha1(resume_text or ""),
        },
    ) as fctx:
        prompt_template = _load_prompt_template()
        prompt = prompt_template.format(
            title=(job.get("title") or "").replace("\n", " ")[:_MAX_TITLE_CHARS],
            company=(job.get("company") or "")[:_MAX_COMPANY_CHARS],
            location=(job.get("location") or "")[:_MAX_LOC_CHARS],
            url=(job.get("url") or "")[:_MAX_URL_CHARS],
            snippet=(job.get("snippet") or "").replace("\n", " ")[:_MAX_SNIPPET_CHARS],
            resume=(resume_text or "")[:_MAX_RESUME_CHARS],
        )
        stdout = wrapped_run_p(None, "fit_analyzer", prompt, timeout_s=timeout_s)
        if not stdout:
            fctx.set_output({"result": None, "reason": "cli_missing_or_empty"})
            return None
        body = extract_assistant_text(stdout)
        parsed = parse_json_block(body)
        analysis = _normalize(parsed)
        if analysis is None:
            log.error("fit_analyzer: parse/validate failed (head=%r)", (body or "")[:200])
            fctx.set_output({
                "result": None,
                "reason": "parse_or_validate_failed",
                "body_head": (body or "")[:300],
            })
            return None
        fctx.set_output({
            "verdict": analysis.get("verdict"),
            "fit_score": analysis.get("fit_score"),
            "headline": (analysis.get("headline") or "")[:200],
            "strength_count": len(analysis.get("strengths") or []),
            "gap_count": len(analysis.get("gaps") or []),
        })
        return analysis


# ---------- MDv2 renderer ----------

_SEVERITY_ICON = {
    "critical": "●",   # solid — hardest to miss
    "moderate": "◐",   # half — partial blocker
    "minor":    "○",   # hollow — nice-to-have
}

_VERDICT_LABEL = {
    "strong_match": "Strong match",
    "solid_match":  "Solid match",
    "stretch":      "Stretch",
    "weak_match":   "Weak match",
}


def render_analysis_mdv2(analysis: dict, job: dict, max_chars: int = 3800) -> str:
    """Render a fit analysis dict as a Telegram MarkdownV2 message.

    Layout (scannable top→bottom):
        *Verdict: Strong match*   ▰▰▰▰▱  4/5
        _headline sentence_

        *Strengths*
          • Area — evidence
        *Gaps*
          ● Area — evidence
              → mitigation
        *Hidden requirements*
          — item

        _recommendation paragraph_

    Truncates at `max_chars` to stay under Telegram's 4096-char cap. Uses
    the same block-element score bar as the job card for consistency.
    """
    # Local import avoids a top-level circular-import risk if
    # telegram_client ever grows a back-reference.
    from telegram_client import mdv2_escape, _score_bar

    verdict = analysis.get("verdict") or "solid_match"
    label = _VERDICT_LABEL.get(verdict, "Fit analysis")
    score = int(analysis.get("fit_score") or 0)
    headline = analysis.get("headline") or ""

    job_title = (job or {}).get("title") or ""
    job_company = (job or {}).get("company") or ""

    lines: list[str] = []
    # Title stripe — context + verdict + score bar on two rows so the bar
    # never wraps awkwardly on narrow phones.
    if job_title or job_company:
        subtitle_bits = [b for b in (job_title, job_company) if b]
        lines.append("_" + mdv2_escape("  ·  ".join(subtitle_bits)[:180]) + "_")
    lines.append(f"*{mdv2_escape(label)}*  ·  {_score_bar(score)}  {score}/5")
    if headline:
        lines.append("_" + mdv2_escape(headline) + "_")

    # ---- Strengths ----
    strengths = analysis.get("strengths") or []
    if strengths:
        lines.append("")
        lines.append("*Strengths*")
        for s in strengths:
            area = (s.get("area") or "").strip()
            ev = (s.get("evidence") or "").strip()
            if area and ev:
                lines.append(f"  • *{mdv2_escape(area)}* — {mdv2_escape(ev)}")
            elif area:
                lines.append(f"  • *{mdv2_escape(area)}*")
            elif ev:
                lines.append(f"  • {mdv2_escape(ev)}")

    # ---- Gaps ----
    gaps = analysis.get("gaps") or []
    if gaps:
        lines.append("")
        lines.append("*Gaps*")
        for g in gaps:
            area = (g.get("area") or "").strip()
            ev = (g.get("evidence") or "").strip()
            mit = (g.get("mitigation") or "").strip()
            icon = _SEVERITY_ICON.get((g.get("severity") or "").lower(), "○")
            head = f"  {icon} *{mdv2_escape(area)}*" if area else f"  {icon}"
            if ev:
                head += " — " + mdv2_escape(ev)
            lines.append(head)
            if mit:
                lines.append("      → _" + mdv2_escape(mit) + "_")

    # ---- Hidden requirements ----
    hidden = analysis.get("hidden_requirements") or []
    if hidden:
        lines.append("")
        lines.append("*Watch for*")
        for h in hidden:
            lines.append("  — " + mdv2_escape(h))

    # ---- Recommendation ----
    rec = (analysis.get("recommendation") or "").strip()
    if rec:
        lines.append("")
        lines.append("_" + mdv2_escape(rec) + "_")

    body = "\n".join(lines)
    if len(body) > max_chars:
        # Truncate at the last newline we can afford, appending a short note.
        note = "\n\n…" + mdv2_escape(" (trimmed to fit Telegram's size limit)")
        cutoff = body.rfind("\n", 0, max_chars - len(note) - 1)
        if cutoff > 0:
            body = body[:cutoff] + note
        else:
            body = body[: max_chars - len(note)] + note
    return body
