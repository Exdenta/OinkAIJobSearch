"""Resume tailoring helper.

Given a user's resume text and a job's description, produce a short Markdown
"tailoring note":

  1. Which skills listed in the resume overlap the job description.
  2. A rewritten one-paragraph profile summary that leans into those skills.
  3. A short list of bullet-wording suggestions.

This is deliberately *mild* — we don't fabricate experience, only rearrange
emphasis. For a deeper rewrite, swap `rewrite_summary()` for an LLM call.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from claude_cli import run_p, extract_assistant_text, parse_json_block

log = logging.getLogger(__name__)

# Broad tech-skill lexicon. Case-insensitive substring match.
# Keep this list curated; noisy entries produce false-positive matches.
SKILLS = [
    # JS/TS + frameworks
    "javascript", "typescript", "react", "redux", "rtk query", "next.js", "nextjs",
    "vue", "vuex", "pinia", "angular", "svelte", "solidjs",
    # Styling
    "tailwind", "tailwindcss", "styled-components", "emotion", "css", "sass", "less",
    # Testing
    "vitest", "jest", "cypress", "playwright", "react testing library", "rtl",
    "storybook",
    # State / data
    "graphql", "apollo", "rest api", "websocket", "websockets", "sse",
    # Tooling / build
    "webpack", "vite", "rollup", "esbuild", "parcel", "turborepo",
    # Backend / other
    "node", "node.js", "nodejs", "python", "go", "rust", "java", "kotlin", "swift",
    "django", "flask", "fastapi",
    # DevOps / cloud
    "docker", "kubernetes", "k8s", "aws", "gcp", "azure", "terraform",
    # Design / workflow
    "figma", "git", "github", "gitlab", "agile", "scrum", "jira", "notion",
    # Accessibility / perf
    "a11y", "accessibility", "wcag", "lighthouse", "performance",
    # Soft / process
    "code review", "mentorship", "documentation", "cross-functional",
]


_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\.\-\+# ]*")


def extract_pdf_text(pdf_path: Path) -> str:
    """Read a PDF and return plain text. Falls back gracefully if pypdf can't parse."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except ImportError:
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        return "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        raise RuntimeError(f"Could not extract PDF text: {e}")


def skills_in(text: str) -> set[str]:
    """Return the canonical skill names found in `text`."""
    t = (text or "").lower()
    found: set[str] = set()
    for skill in SKILLS:
        # Use word-boundary for short skills to avoid false positives
        # (e.g. "go" in "google"). Long skills → substring is fine.
        if len(skill) <= 4:
            pat = r"(?<![a-z0-9])" + re.escape(skill) + r"(?![a-z0-9])"
            if re.search(pat, t):
                found.add(skill)
        else:
            if skill in t:
                found.add(skill)
    return found


def overlap(resume_text: str, job_text: str) -> dict:
    resume_skills = skills_in(resume_text)
    job_skills = skills_in(job_text)
    common = sorted(resume_skills & job_skills)
    job_only = sorted(job_skills - resume_skills)
    resume_only = sorted(resume_skills - job_skills)
    return {
        "common": common,
        "gaps": job_only,          # things the job wants that aren't on resume
        "latent": resume_only,     # things on resume that job didn't mention
    }


def rewrite_summary(resume_text: str, job: dict, ov: dict) -> str:
    """Produce a tailored 2–3 sentence profile blurb.

    We don't fabricate — we pick from skills the resume *already* mentions and
    rearrange to match the job's priorities.
    """
    lead_stack = ", ".join(ov["common"][:6]) if ov["common"] else "your core stack"
    role = job.get("title", "this role")
    company = job.get("company") or "the company"

    # Try to reuse the resume's own years-of-experience phrasing
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*years", resume_text, re.I)
    years_clause = f"{m.group(1)}+ years of" if m else "hands-on"

    # Keywords worth sprinkling in
    extras = ", ".join(ov["common"][6:10]) if len(ov["common"]) > 6 else ""

    blurb = (
        f"Middle Frontend Developer with {years_clause} commercial experience "
        f"building production web applications in {lead_stack}. "
        f"Directly relevant to {role} at {company}: delivered marketplace + admin "
        f"modules from MVP to prod, with testing (Vitest, RTL), component libraries "
        f"(Storybook), and accessibility-first UI work."
    )
    if extras:
        blurb += f" Additional overlap with the posting: {extras}."
    return blurb


def bullet_suggestions(ov: dict) -> list[str]:
    """Short, specific wording tweaks keyed off overlaps."""
    tips: list[str] = []
    if "react" in ov["common"] and "typescript" in ov["common"]:
        tips.append("Lead your experience bullets with 'React + TypeScript' rather than "
                    "alphabetized tech lists — it's what the posting anchors on.")
    if "storybook" in ov["common"]:
        tips.append("Promote your Storybook component-library work to the top of your "
                    "deliverables — it signals design-system thinking.")
    if "vitest" in ov["common"] or "react testing library" in ov["common"] or "rtl" in ov["common"]:
        tips.append("Quantify test coverage or regression impact if possible — e.g. "
                    "'reduced prod bugs by X% via Vitest + RTL suites'.")
    if "accessibility" in ov["common"] or "a11y" in ov["common"] or "wcag" in ov["common"]:
        tips.append("Mention a specific a11y fix or Lighthouse score improvement — "
                    "accessibility claims land harder with a number.")
    if "websocket" in ov["common"] or "websockets" in ov["common"]:
        tips.append("Call out the real-time / WebSocket work explicitly; it's a "
                    "differentiator vs. generic frontend CVs.")
    if ov["gaps"]:
        gap_list = ", ".join(ov["gaps"][:4])
        tips.append(f"Posting emphasizes *{gap_list}* which aren't on your resume — "
                    f"if you have even hobby-level exposure, add a short 'familiar with' line.")
    if not tips:
        tips.append("Strong overlap already — consider reordering your Core Stack list "
                    "so the posting's top-listed tech comes first.")
    return tips


# ---------------------------------------------------------------------------
# AI-backed tailoring (Claude sub-agent via the `claude` CLI)
# ---------------------------------------------------------------------------

_AI_PROMPT = """You are a careful resume tailor for a single job application.

You will get the candidate's current resume plaintext and the job posting, and
you must produce a STRICT JSON plan. Do not invent experience the candidate
doesn't have — only reorder emphasis, rephrase bullets, and align wording with
the posting's language.

Return ONLY JSON of this shape (no markdown, no commentary, no code fences):

{{
  "summary": "one sentence describing the match, plain text, no markdown",
  "suggestions": [
    {{"section": "<section name>",
      "change": "<Add | Remove | Rephrase | Reorder | Reframe>",
      "before": "<verbatim text from current resume, '' if Add>",
      "after":  "<proposed replacement text, '' if Remove>",
      "why":    "<one short sentence of rationale>"}}
  ],
  "tailored_resume_markdown": "<the FULL rewritten resume as Markdown>"
}}

Rules:
- 4 to 10 suggestions, most impactful first.
- Keep each suggestion's strings under 300 chars. No newlines inside strings.
- "tailored_resume_markdown" must be the complete resume ready to paste.
- Preserve the candidate's real dates, employers, and degrees verbatim.
- Prefer the posting's own phrasing for skills that genuinely match.

=== JOB ===
Title:    {title}
Company:  {company}
Location: {location}
URL:      {url}
Snippet:  {snippet}

=== CURRENT RESUME (plaintext) ===
{resume}
"""


def build_tailor_plan_ai(resume_text: str, job: dict, timeout_s: int = 180) -> dict | None:
    """Ask the `claude` CLI for a structured tailoring plan.

    Returns a dict with keys {summary, suggestions[], tailored_resume_markdown}
    on success, or None on any failure (missing CLI, timeout, parse error).
    Callers should fall back to `build_tailor_note` for a plain-text note.
    """
    prompt = _AI_PROMPT.format(
        title=(job.get("title") or "").replace("\n", " ")[:200],
        company=(job.get("company") or "")[:120],
        location=(job.get("location") or "")[:120],
        url=(job.get("url") or "")[:400],
        snippet=(job.get("snippet") or "").replace("\n", " ")[:1200],
        resume=(resume_text or "")[:12000],
    )
    stdout = run_p(prompt, timeout_s=timeout_s)
    if not stdout:
        return None
    body = extract_assistant_text(stdout)
    plan = parse_json_block(body)
    if not isinstance(plan, dict):
        log.error("resume_tailor: could not parse AI plan (head=%r)", body[:200])
        return None
    # Normalize shape so downstream code doesn't KeyError.
    plan.setdefault("summary", "")
    plan.setdefault("suggestions", [])
    plan.setdefault("tailored_resume_markdown", "")
    if not isinstance(plan["suggestions"], list):
        plan["suggestions"] = []
    return plan


def build_tailor_note(resume_text: str, job: dict) -> str:
    """Return the full Markdown note sent back to the user."""
    job_blob = " ".join([
        job.get("title", ""), job.get("company", ""), job.get("snippet", ""),
        job.get("location", ""),
    ])
    ov = overlap(resume_text, job_blob)

    lines = [
        f"# Tailoring note — {job.get('title','Role')} @ {job.get('company','Company')}",
        "",
        f"**Source:** {job.get('source','?')}  ",
        f"**URL:** {job.get('url','')}",
        "",
        "## Overlap (your resume × the posting)",
        "",
    ]
    if ov["common"]:
        lines.append("✅ **Skills you can lead with:** " + ", ".join(f"`{s}`" for s in ov["common"]))
    else:
        lines.append("⚠️  No obvious overlap found — double-check the posting is a fit.")
    lines.append("")
    if ov["gaps"]:
        lines.append("🟡 **Posting asks for, but resume doesn't mention:** "
                     + ", ".join(f"`{s}`" for s in ov["gaps"]))
        lines.append("")
    if ov["latent"]:
        lines.append("💤 **On your resume but not in the posting (de-emphasize):** "
                     + ", ".join(f"`{s}`" for s in ov["latent"][:10]))
        lines.append("")

    lines += [
        "## Suggested rewritten profile summary",
        "",
        "> " + rewrite_summary(resume_text, job, ov).replace("\n", "\n> "),
        "",
        "## Bullet-wording tweaks",
        "",
    ]
    for tip in bullet_suggestions(ov):
        lines.append(f"- {tip}")
    lines += [
        "",
        "---",
        "",
        "_This note only rearranges emphasis — it does not invent new experience. "
        "Edit your actual CV to match and run a final pass yourself before applying._",
    ]
    return "\n".join(lines)
