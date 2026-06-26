"""Hiring-contact lookup — "who to write to" for each job card.

For every job that survived the send prefilter we ask a Claude
subprocess (WebSearch + WebFetch) to find ONE person currently at the
hiring company who most plausibly owns the opening — the recruiter who
posted it, the talent-acquisition partner covering the function/region,
the hiring manager, or a founder at a tiny startup. The result lands on
the job card as a linked name + a one-line "why this person".

Design notes
------------
- Person selection is ENTIRELY prompt-driven (prompts/hiring_contact.txt)
  per the CLAUDE.md design principle — no title regexes, no per-company
  lists. Python validates only transport invariants: JSON shape, required
  fields, http(s) scheme on the profile URL, length caps, and the
  confidence enum.
- Results are cached in the `hiring_contacts` table keyed by job_id —
  the right contact is a property of the opening, not of the user.
  Negative verdicts ("not_found") are cached too, so the same posting
  doesn't re-burn web searches on a digest replay or a second user.
  Errors (CLI missing / timeout / parse failure) are NOT cached —
  transient failures retry on the next send attempt.
- Lookups fan out on a small thread pool. Precedent for wrapped CLI
  calls from worker threads is job_enrich's Sonnet batch path
  (`forensic.log_step` is thread-safe). DB reads/writes and forensic
  emits stay on the caller's thread.
- Model defaults to MID_MODEL (sonnet): the task is multi-query person
  research with a hallucination risk — a wrong person shipped to the
  user is worse than none — and the volume is tiny (only jobs that
  actually ship get a lookup). Override via HIRING_CONTACT_MODEL.
- Fail-soft everywhere: any failure means the card ships without a
  contact block. The lookup must never block a send.

Env knobs (read at call time, so flips don't need a bot restart):
  HIRING_CONTACT_OFF=1        disable the feature entirely
  HIRING_CONTACT_TIMEOUT_S    per-lookup budget (default 180)
  HIRING_CONTACT_WORKERS      thread-pool width (default 3)
  HIRING_CONTACT_MODEL        model alias (default: claude_cli.MID_MODEL)
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:  # import kept type-only to avoid any import-cycle risk
    from dedupe import Job

log = logging.getLogger("hiring_contact")

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "hiring_contact.txt"

# Input caps mirror fit_analyzer's — deterministic prompt size.
_MAX_TITLE_CHARS   = 200
_MAX_COMPANY_CHARS = 120
_MAX_LOC_CHARS     = 120
_MAX_URL_CHARS     = 400
_MAX_SNIPPET_CHARS = 800

# Output caps — schema invariants, not judgment calls.
_MAX_NAME_CHARS   = 120
_MAX_CTITLE_CHARS = 160
_MAX_REASON_CHARS = 220

_VALID_CONFIDENCE = {"high", "medium", "low"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip() not in ("", "0", "false", "False")


def hiring_contact_off() -> bool:
    """Feature kill-switch, read per call so ops can flip it live."""
    return _env_flag("HIRING_CONTACT_OFF")


def _load_prompt_template() -> str:
    """Read the prompt file once per call. Cheap, and reloading on each
    call means prompt edits don't require a bot restart."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def build_prompt(job: "Job") -> str:
    return _load_prompt_template().format(
        title=(job.title or "").replace("\n", " ")[:_MAX_TITLE_CHARS],
        company=(job.company or "").replace("\n", " ")[:_MAX_COMPANY_CHARS],
        location=(job.location or "")[:_MAX_LOC_CHARS],
        url=(job.url or "")[:_MAX_URL_CHARS],
        snippet=(job.snippet or "").replace("\n", " ")[:_MAX_SNIPPET_CHARS],
    )


def _normalize(data: object) -> dict | None:
    """Coerce the model's response into the canonical contact schema.

    Returns the cleaned dict for a usable found-contact, None otherwise.
    Strict on the contract (found, name, http(s) profile_url present);
    lenient on values (caps, confidence fallback). Anything beyond this
    — "is the person real / current / well-chosen" — is the prompt's
    job, not Python's.
    """
    if not isinstance(data, dict) or not data.get("found"):
        return None
    name = str(data.get("name") or "").strip()[:_MAX_NAME_CHARS]
    profile_url = str(data.get("profile_url") or "").strip()[:_MAX_URL_CHARS]
    if not name or not profile_url:
        return None
    try:
        parsed = urlparse(profile_url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    confidence = str(data.get("confidence") or "").strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        confidence = "medium"
    return {
        "name": name,
        "title": str(data.get("title") or "").strip()[:_MAX_CTITLE_CHARS],
        "profile_url": profile_url,
        "reason": str(data.get("reason") or "").strip()[:_MAX_REASON_CHARS],
        "confidence": confidence,
    }


def find_hiring_contact(
    job: "Job", timeout_s: int | None = None,
) -> tuple[dict | None, str]:
    """Run one LLM lookup for `job`. Pure — no DB, no forensic.

    Returns ``(contact, "found")`` on success, ``(None, "not_found")``
    when the agent says nobody / breaks the field contract, and
    ``(None, "error:<reason>")`` on transport failures. Callers cache
    found/not_found and let error retry next time.
    """
    if timeout_s is None:
        timeout_s = _env_int("HIRING_CONTACT_TIMEOUT_S", 180)
    try:
        from claude_cli import (
            MID_MODEL,
            TOOLS_DENY_SHELL_FS,
            TOOLS_WEB_BOTH,
            extract_assistant_text,
            parse_json_block,
        )
    except ImportError:
        return (None, "error:claude_cli_import")
    try:
        from instrumentation.wrappers import wrapped_run_p_with_tools
    except ImportError:
        return (None, "error:wrappers_import")

    model = os.environ.get("HIRING_CONTACT_MODEL", "").strip() or MID_MODEL
    # Same tool posture as the liveness verifier: web tools only, shell
    # and filesystem denied — WebFetch pulls untrusted page content into
    # the agent's context (prompt-injection defense).
    stdout = wrapped_run_p_with_tools(
        None,
        "hiring_contact",
        build_prompt(job),
        allowed_tools=TOOLS_WEB_BOTH,
        disallowed_tools=TOOLS_DENY_SHELL_FS,
        timeout_s=timeout_s,
        output_format="json",
        model=model,
    )
    if stdout is None:
        return (None, "error:cli_unavailable")
    body = extract_assistant_text(stdout)
    data = parse_json_block(body)
    if data is None:
        return (None, "error:parse_failed")
    contact = _normalize(data)
    if contact is None:
        # Includes found=false AND found=true-with-broken-fields. Both
        # are model verdicts on this posting — cacheable, unlike
        # transport errors above.
        return (None, "not_found")
    return (contact, "found")


def lookup_contacts_for_jobs(
    jobs: list,
    db=None,
    chat_id: int | None = None,
) -> dict[str, dict]:
    """Resolve hiring contacts for a batch of about-to-send jobs.

    Cache reads, cache writes, and forensic emits run on the caller's
    thread; only the LLM lookups fan out on the pool. Returns
    ``{job_id: contact}`` — jobs with no contact are simply absent, and
    the caller renders their cards without the contact block.
    """
    if hiring_contact_off() or not jobs:
        return {}
    try:
        import forensic
    except Exception:  # pragma: no cover
        forensic = None

    def _flog(job, status: str, contact: dict | None, cached: bool) -> None:
        if forensic is None:
            return
        try:
            forensic.log_step(
                "hiring_contact.lookup",
                input={
                    "job_id": job.job_id,
                    "title": (job.title or "")[:120],
                    "company": (job.company or "")[:80],
                },
                output={
                    "status": status,
                    "cached": cached,
                    "name": (contact or {}).get("name"),
                    "profile_url": (contact or {}).get("profile_url"),
                    "confidence": (contact or {}).get("confidence"),
                    "reason": ((contact or {}).get("reason") or "")[:120],
                },
                chat_id=chat_id,
            )
        except Exception:
            log.debug("hiring_contact forensic emit failed; continuing",
                      exc_info=True)

    contacts: dict[str, dict] = {}
    pending = []
    for job in jobs:
        row = None
        if db is not None:
            try:
                row = db.get_hiring_contact(job.job_id)
            except Exception:
                log.debug("get_hiring_contact failed; treating as miss",
                          exc_info=True)
        if row is None:
            pending.append(job)
            continue
        contact = None
        if row["status"] == "found" and row["contact_json"]:
            try:
                parsed = json.loads(row["contact_json"])
            except (TypeError, ValueError):
                parsed = None
            contact = _normalize({"found": True, **parsed}) if isinstance(parsed, dict) else None
        if contact:
            contacts[job.job_id] = contact
        _flog(job, "found" if contact else "not_found", contact, cached=True)

    results: dict[str, tuple[dict | None, str]] = {}
    if pending:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        workers = max(1, min(_env_int("HIRING_CONTACT_WORKERS", 3), len(pending)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(find_hiring_contact, j): j for j in pending}
            for fut in as_completed(futs):
                j = futs[fut]
                try:
                    results[j.job_id] = fut.result()
                except Exception as e:  # noqa: BLE001 — worker must not kill the send
                    log.warning("hiring contact lookup raised for %s: %s",
                                j.job_id, e)
                    results[j.job_id] = (None, "error:exception")

    for job in pending:
        contact, status = results.get(job.job_id, (None, "error:no_result"))
        if contact:
            contacts[job.job_id] = contact
        if db is not None and status in ("found", "not_found"):
            try:
                db.upsert_hiring_contact(
                    job.job_id,
                    status,
                    json.dumps(contact, ensure_ascii=False) if contact else None,
                )
            except Exception:
                log.debug("upsert_hiring_contact failed; continuing",
                          exc_info=True)
        _flog(job, status, contact, cached=False)
        log.info("hiring_contact: %s (%s @ %s) -> %s%s",
                 job.job_id, (job.title or "")[:60], (job.company or "")[:40],
                 status,
                 f" ({contact['name']})" if contact else "")
    return contacts
