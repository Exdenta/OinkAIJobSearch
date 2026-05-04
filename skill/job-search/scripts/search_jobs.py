#!/usr/bin/env python3
"""Daily job-alert orchestrator (multi-user, DB-backed, inline buttons).

Flow:
  1. Load operational defaults (defaults.DEFAULTS) and .env
  2. Fan out to enabled source adapters (global pass: HN + remote boards +
     curated boards). LinkedIn + web_search run PER-USER inside the recipient
     loop because they need profile.search_seeds to shape queries.
  3. Upsert each posting into the jobs table
  4. For every registered user (who has uploaded a resume):
       a. Skip postings they've already been sent OR already actioned
       b. AI-score each remaining posting against the user's resume + profile
       c. Drop postings below the user's ⭐ floor (or default ai_min_match_score)
       d. Send each remaining posting as its own Telegram message with buttons
       e. Log sent_messages(chat_id, message_id → job_id)

Matching is per-user. There is no global keyword/title/location filter — the
AI score is the sole matching gate.

Run from the project root:
    python skill/job-search/scripts/search_jobs.py
    python skill/job-search/scripts/search_jobs.py --dry-run
    python skill/job-search/scripts/search_jobs.py --chat-id 123456789  # single user

Exit codes:
    0 success · 1 bad config · 2 partial (some source errors) · 3 hard fail
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from dedupe import Job, JobStore                           # noqa: E402
from db import DB                                          # noqa: E402
from defaults import DEFAULTS                              # noqa: E402
from telegram_client import TelegramClient, send_per_job_digest  # noqa: E402
from sources import (                                      # noqa: E402
    hackernews, remote_boards, linkedin, curated_boards, web_search,
    reliefweb, euraxess, un_careers, math_ku_phd, ub_doctoral,
    # Wave 2 sources (added 2026-05-01). 10 live + 3 blocked-stubs.
    # Toggle each independently in defaults.py.
    eures, infojobs, tecnoempleo, ai_jobs_net, jobs_ac_uk,
    academicpositions, ikerbasque, wellfound, ycombinator_was, wttj,
    builtin, impactpool, devex,
)
from telemetry import MonitorStore                         # noqa: E402
from instrumentation import pipeline_run, source_run, error_capture  # noqa: E402
from ops.alerts import deliver_alert                       # noqa: E402
from ops.summary import deliver_daily_summary              # noqa: E402
import forensic                                             # noqa: E402
from log_ttl import cleanup_logs                            # noqa: E402
# `linkedin` and `web_search` are imported but NOT in the global SOURCES
# dispatch below — they only run per-user inside the recipient loop, using
# each profile's stored seeds. See run() below.
from job_enrich import enrich_jobs_ai, by_job_id           # noqa: E402
from user_profile import (                                 # noqa: E402
    profile_from_json,
    is_empty_profile,
    effective_filters,
    project_to_prefs,
)
import pig_stickers as _pigs                               # noqa: E402

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

log = logging.getLogger("job-search")


SOURCES = {
    "hackernews":      hackernews,
    "remote_boards":   remote_boards,
    "curated_boards":  curated_boards,
    "reliefweb":       reliefweb,
    "euraxess":        euraxess,
    "un_careers":      un_careers,
    "math_ku_phd":     math_ku_phd,
    "ub_doctoral":     ub_doctoral,
    # Wave 2 sources. Each adapter ships default-OFF in defaults.py — the
    # operator opts each in independently after weighing yield vs cost
    # (HTML-scrape adapters have higher fragility / Cloudflare risk than
    # RSS adapters).
    "eures":            eures,            # blocked: EU Login required, stub
    "infojobs":         infojobs,         # Spain HTML scrape
    "tecnoempleo":      tecnoempleo,      # Spain tech RSS
    "ai_jobs_net":      ai_jobs_net,      # Curated AI/ML HTML
    "jobs_ac_uk":       jobs_ac_uk,       # UK/EU academic RSS
    "academicpositions": academicpositions,  # blocked: Cloudflare BFM, stub
    "ikerbasque":       ikerbasque,       # Basque research HTML
    "wellfound":        wellfound,        # blocked: DataDome, stub
    "ycombinator_was":  ycombinator_was,  # YC startups JSON
    "wttj":             wttj,             # Welcome to the Jungle Algolia
    "builtin":          builtin,          # US tech HTML
    "impactpool":       impactpool,       # UN/NGO HTML (researcher-friendly)
    "devex":            devex,            # International dev (Claude CLI)
    # `linkedin` and `web_search` are PER-USER only (run inside the recipient
    # loop with profile.search_seeds), so they're deliberately absent here.
}


def project_root() -> Path:
    return HERE.parent.parent.parent


def load_env() -> None:
    if load_dotenv:
        load_dotenv(project_root() / ".env")
    else:
        env_path = project_root() / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# ---------- helpers ----------

def _score_histogram(enrichments_by_job_id: dict[str, dict]) -> dict[str, int]:
    """Score histogram for forensic logging — { '0': 3, '1': 2, ... '5': 1 }."""
    hist: dict[str, int] = {str(i): 0 for i in range(6)}
    hist["unknown"] = 0
    for v in enrichments_by_job_id.values():
        try:
            s = int((v or {}).get("match_score") or 0)
        except (TypeError, ValueError):
            hist["unknown"] += 1
            continue
        if 0 <= s <= 5:
            hist[str(s)] += 1
        else:
            hist["unknown"] += 1
    return hist


# ---------- post-filters ----------

def seniority_matches(job: Job, wanted: str) -> bool:
    wanted = (wanted or "any").lower()
    if wanted == "any":
        return True
    return wanted in f"{job.title} {job.snippet}".lower()


def _digits(text: str):
    buf, cur = [], []
    for ch in text:
        if ch.isdigit():
            cur.append(ch)
        else:
            if cur:
                buf.append("".join(cur))
                cur = []
    if cur:
        buf.append("".join(cur))
    return buf


def post_filter(jobs: list[Job], filters: dict) -> list[Job]:
    """Pass-through.

    Historically this function ran regex/substring gates on title, body,
    company, seniority, and salary. The product decision is now: AI scoring
    (job_enrich.enrich_jobs_ai) is the single matching gate for every job
    across every source. Per-user profile fields (keywords, title gates,
    locations, companies, salary) are IGNORED here — Claude reads the user's
    resume + stated preferences and decides fit holistically.

    Left as an identity function (not deleted) so every historical call site
    keeps compiling and so ops can re-enable a cheap pre-filter later by
    editing this one spot. The `filters` arg is deliberately unused.
    """
    del filters  # intentionally unused — AI is the gate
    return list(jobs)


# ---------- fetch ----------

def fetch_all(
    filters: dict,
    *,
    store: MonitorStore | None = None,
    pipeline_run_id: int | None = None,
) -> tuple[list[Job], list[str]]:
    """Fan out to every enabled global source adapter.

    When `store` and `pipeline_run_id` are supplied, each adapter call is
    wrapped in a `source_run` telemetry context so per-source health is
    persisted. The dry-run path passes neither and skips telemetry.
    """
    enabled = filters.get("sources") or {}
    all_jobs: list[Job] = []
    errors: list[str] = []
    for key, mod in SOURCES.items():
        if key == "remote_boards":
            if not any(enabled.get(k, True) for k in ("remoteok", "remotive", "weworkremotely")):
                continue
        elif key == "curated_boards":
            if not any(enabled.get(k, False) for k in ("remocate", "wantapply", "remoterocketship")):
                continue
        elif not enabled.get(key, True):
            continue
        log.info("Fetching from %s…", key)
        # Forensic step + telemetry wrap. The source_run ctx writes one row
        # per (pipeline_run, source). The forensic.step writes a JSONL line
        # capturing inputs (filter shape) + outputs (count + sample titles)
        # so failures and 0-result runs can be analyzed post-hoc. Both are
        # opt-out via env (FORENSIC_OFF / no store). On exception we record
        # 'failed' to source_runs, capture the error to forensic, and
        # continue with the partial-run semantics the legacy path used.
        try:
            with forensic.step(
                f"sources.{key}.fetch",
                input={
                    "max_age_hours": filters.get("max_age_hours"),
                    "max_per_source": filters.get("max_per_source"),
                    "sources_enabled": list((filters.get("sources") or {}).keys()),
                },
                run_id=pipeline_run_id,
            ) as fctx:
                if store is not None and pipeline_run_id is not None:
                    with source_run(store, pipeline_run_id, key) as sctx:
                        fetched = mod.fetch(filters) or []
                        sctx.set_count(len(fetched))
                else:
                    fetched = mod.fetch(filters) or []
                fctx.set_output({
                    "count": len(fetched),
                    "sample_titles": [getattr(j, "title", "")[:80] for j in fetched[:5]],
                })
            log.info("  %s → %d raw postings", key, len(fetched))
            all_jobs.extend(fetched)
        except Exception as e:
            log.exception("%s fetch raised: %s", key, e)
            errors.append(key)
    return all_jobs, errors


# ---------- main ----------

def run(dry_run: bool = False, only_chat: int | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_env()

    root = project_root()
    state_dir = root / os.environ.get("STATE_DIR", "state")
    db_path = state_dir / "jobs.db"

    # Prune old logs at the start of every run. TTL = LOG_TTL_DAYS (default
    # 2 days). Disable via LOG_TTL_OFF=1. Best-effort: any failure is
    # swallowed and logged. See log_ttl.py for what's pruned and why.
    try:
        cleanup_logs(state_dir=state_dir)
    except Exception:
        log.exception("log_ttl: cleanup raised; continuing")

    # Operational defaults live in code (defaults.DEFAULTS). Matching is
    # per-user via the Opus-built profile — there is no global YAML config.
    filters = dict(DEFAULTS)

    db = DB(db_path)
    # TTL-prune the per-run digest enrichment cache that backs the ⬇/⬆
    # filter buttons. 7 days is generous: by then any digest the user
    # might still want to retroactively expand has long scrolled out of
    # view and the score gates likely shifted.
    try:
        purged = db.purge_digest_run_jobs_older_than(7 * 86400)
        if purged:
            log.info("digest_run_jobs: purged %d rows older than 7 days", purged)
    except Exception:
        log.debug("digest_run_jobs purge raised; continuing", exc_info=True)
    job_store = JobStore(db)
    # Telemetry store. Cheap to construct (just wraps db); used in both the
    # dry-run preview path (untelemetered) and the live path (wrapped in
    # error_capture + pipeline_run below).
    store = MonitorStore(db)

    # ----- Dry-run preview (no telemetry, no Telegram) --------------------
    if dry_run:
        jobs_raw, errors = fetch_all(filters)
        log.info("Raw fetched across static sources: %d postings", len(jobs_raw))
        new_in_db = job_store.save_all(jobs_raw)
        log.info("DB: %d newly-inserted jobs, %d already known",
                 new_in_db, len(jobs_raw) - new_in_db)
        if only_chat is not None:
            users = [u for u in [db.get_user(only_chat)] if u is not None]
        else:
            users = db.users_with_resume()
        dry_jobs = post_filter(jobs_raw, filters)
        print(f"\n=== DRY RUN — {len(dry_jobs)} postings (default filter) ===\n")
        for j in dry_jobs:
            print(f"  [{j.source}] {j.title} @ {j.company} — {j.url}")
        print()
        if users:
            print(f"Users that would receive: {[u['chat_id'] for u in users]}")
        if errors:
            print(f"Source errors: {', '.join(errors)}")
        return 0

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN missing")
        return 1

    tg = TelegramClient(token=token)

    # ------------------------------------------------------------------
    # Live run wrapped in monitoring contexts:
    #   * error_capture  — uncaught Exception → fingerprint + alert operator
    #   * pipeline_run   — one row in `pipeline_runs` with counters/status
    # `fetch_all` runs per-source telemetry inside (`source_run`) when given
    # store + run_id. After the contexts close we deliver the daily summary
    # to the operator chat (suppressed by `quiet_alerts` toggle).
    # ------------------------------------------------------------------
    def _alert_sink(env):
        deliver_alert(tg, store, env)

    exit_code = 0
    run_id_for_summary: int | None = None
    try:
      with error_capture(store, where="search_jobs.run", alert_sink=_alert_sink):
        with pipeline_run(store, "daily_digest") as pctx:
            run_id_for_summary = pctx.run_id

            jobs_raw, errors = fetch_all(filters, store=store, pipeline_run_id=pctx.run_id)
            log.info("Raw fetched across static sources: %d postings", len(jobs_raw))
            new_in_db = job_store.save_all(jobs_raw)
            log.info("DB: %d newly-inserted jobs, %d already known",
                     new_in_db, len(jobs_raw) - new_in_db)

            if only_chat is not None:
                users = [u for u in [db.get_user(only_chat)] if u is not None]
            else:
                users = db.users_with_resume()
            if not users:
                log.warning("No registered users with a resume. Ask them to /start the bot and upload a CV.")

            msg_cfg = filters.get("message") or {}
            quiet = bool(msg_cfg.get("quiet_if_empty"))
            ai_enrich = bool(filters.get("ai_enrich", True))
            enrich_timeout_s = int(filters.get("ai_enrich_timeout_s") or 240)
            global_cap = int(filters.get("max_total") or 0)
            # Default AI match-score floor applied when a user hasn't set their own
            # via the bot's ⭐ button. Because the keyword pre-filter has been removed
            # (AI is now the sole matching gate), we need a non-zero default so score-0
            # rejects (jobs Claude explicitly flagged as wrong-fit) don't leak through.
            # Operators can raise/lower this in defaults.py:ai_min_match_score.
            # Clamp to [0, 5]; 0 disables the default floor.
            try:
                default_min_score = int(filters.get("ai_min_match_score") or 0)
            except (TypeError, ValueError):
                default_min_score = 0
            default_min_score = max(0, min(5, default_min_score))

            total_sent = 0
            stats = {
                "users_total":        0,
                "jobs_raw_total":     len(jobs_raw),
                "jobs_sent_total":    0,
                "web_search_hits":    0,
                "linkedin_user_hits": 0,
            }
            send_failed_chat: int | None = None

            for u in users:
                chat_id = int(u["chat_id"])
                stats["users_total"] += 1

                profile = profile_from_json(db.get_user_profile(chat_id))
                free_text = (db.get_prefs_free_text(chat_id) or "").strip()

                effective = effective_filters(filters, profile)
                if profile is not None and not is_empty_profile(profile):
                    log.info(
                        "User %s: profile "
                        "(keywords=%s title_must=%s title_excl=%s locations=%s remote=%s seniority=%s)",
                        chat_id,
                        effective.get("keywords") or [],
                        effective.get("title_must_match") or [],
                        effective.get("title_exclude") or [],
                        effective.get("locations") or [],
                        effective.get("remote") or "any",
                        effective.get("seniority") or "any",
                    )
                else:
                    log.info("User %s: no profile yet → inherits globals", chat_id)

                user_pool = post_filter(jobs_raw, effective)
                log.info("User %s: post_filter %d → %d", chat_id, len(jobs_raw), len(user_pool))

                # Auto-disabled sources: drop postings whose source has hit
                # the zero-score strike threshold for THIS user. Saves the
                # enrichment-token cost on dead-weight (e.g. an MLOps user
                # has no use for academic-research feeds, so after 3 runs
                # of 0-score returns we stop spending Haiku tokens on them).
                #
                # PAUSED: filter is gated on `SOURCE_STRIKES_FILTER_ON=1` (default
                # OFF). Strike RECORDING continues elsewhere so the data is
                # current when the filter is re-enabled — only the actual
                # exclusion is paused. Flip the env var back to "1" when ready
                # to resume auto-pruning.
                strikes_filter_on = os.environ.get("SOURCE_STRIKES_FILTER_ON", "").strip() in ("1", "true", "True")
                if strikes_filter_on:
                    try:
                        disabled_for_user = db.get_disabled_sources(chat_id)
                    except Exception:
                        log.debug("get_disabled_sources failed; treating none as disabled",
                                  exc_info=True)
                        disabled_for_user = set()
                    if disabled_for_user:
                        before_skip = len(user_pool)
                        user_pool = [j for j in user_pool if j.source not in disabled_for_user]
                        skipped = before_skip - len(user_pool)
                        if skipped:
                            log.info(
                                "User %s: skipped %d postings from auto-disabled sources %s",
                                chat_id, skipped, sorted(disabled_for_user),
                            )

                # -------- per-user LinkedIn fetch --------
                li_seeds = ((profile or {}).get("search_seeds") or {}).get("linkedin")
                li_enabled = bool((effective.get("sources") or {}).get("linkedin", False))
                has_queries = bool(li_seeds and li_seeds.get("queries"))
                if li_enabled and has_queries:
                    try:
                        with forensic.step(
                            "linkedin.fetch_for_user",
                            input={
                                "queries": (li_seeds or {}).get("queries"),
                                "remote": effective.get("remote"),
                                "max_per_source": effective.get("max_per_source"),
                            },
                            chat_id=chat_id,
                            run_id=pctx.run_id,
                        ) as fctx:
                            with source_run(store, pctx.run_id, "linkedin", user_chat_id=chat_id) as li_sctx:
                                extra_li = linkedin.fetch_for_user(effective, li_seeds) or []
                                li_sctx.set_count(len(extra_li))
                            fctx.set_output({
                                "raw_count": len(extra_li),
                                "sample_titles": [j.title[:80] for j in extra_li[:5]],
                                "sample_urls": [j.url for j in extra_li[:5]],
                            })
                        log.info("User %s: linkedin.fetch_for_user → %d raw postings",
                                 chat_id, len(extra_li))
                        extra_li_filtered = post_filter(extra_li, effective)
                        if extra_li_filtered:
                            job_store.save_all(extra_li_filtered)
                            user_pool.extend(extra_li_filtered)
                            stats["linkedin_user_hits"] += len(extra_li_filtered)
                            log.info(
                                "User %s: linkedin.fetch_for_user added %d postings after post_filter",
                                chat_id, len(extra_li_filtered),
                            )
                    except Exception as e:
                        log.exception("User %s: per-user LinkedIn fetch failed: %s", chat_id, e)
                        pctx.incr_errors(1)

                # -------- per-user web_search --------
                web_seeds = ((profile or {}).get("search_seeds") or {}).get("web_search")
                web_search_enabled = bool((effective.get("sources") or {}).get("web_search", False))
                trigger_web = web_search_enabled and (bool(web_seeds) or bool(free_text))
                if trigger_web:
                    try:
                        with forensic.step(
                            "web_search.fetch",
                            input={
                                "free_text_head": (free_text or "")[:300],
                                "profile_seeds": web_seeds,
                                "max_per_source": effective.get("max_per_source"),
                                "ai_web_search_timeout_s": effective.get("ai_web_search_timeout_s"),
                            },
                            chat_id=chat_id,
                            run_id=pctx.run_id,
                        ) as fctx:
                            with source_run(store, pctx.run_id, "web_search", user_chat_id=chat_id) as ws_sctx:
                                extra = web_search.fetch(
                                    effective,
                                    user_free_text=free_text or None,
                                    profile_seeds=web_seeds,
                                ) or []
                                ws_sctx.set_count(len(extra))
                            fctx.set_output({
                                "raw_count": len(extra),
                                "sample_titles": [j.title[:80] for j in extra[:5]],
                                "sample_urls": [j.url for j in extra[:5]],
                            })
                        log.info("User %s: web_search returned %d raw postings",
                                 chat_id, len(extra))
                        extra_filtered = post_filter(extra, effective)
                        if extra_filtered:
                            job_store.save_all(extra_filtered)
                            user_pool.extend(extra_filtered)
                            stats["web_search_hits"] += len(extra_filtered)
                            log.info("User %s: web_search added %d postings after post_filter",
                                     chat_id, len(extra_filtered))
                    except Exception as e:
                        log.exception("User %s: per-user web_search failed: %s", chat_id, e)
                        pctx.incr_errors(1)

                if global_cap > 0:
                    user_pool = user_pool[:global_cap]

                user_jobs = job_store.filter_new_for(chat_id, user_pool)
                log.info("User %s: %d new postings (of %d after per-user filter + web_search)",
                         chat_id, len(user_jobs), len(user_pool))

                if not user_jobs and quiet:
                    continue

                enrichments_by_job_id: dict[str, dict] = {}
                prefs_for_ai = project_to_prefs(profile)
                if ai_enrich and user_jobs:
                    resume_text = (u["resume_text"] or "") if "resume_text" in u.keys() else ""
                    try:
                        with forensic.step(
                            "enrich_jobs_ai",
                            input={
                                "job_count": len(user_jobs),
                                "resume_chars": len(resume_text),
                                "projected_prefs_keys": sorted(list(prefs_for_ai.keys())),
                                "primary_role": (profile or {}).get("primary_role"),
                                "stack_primary": (profile or {}).get("stack_primary"),
                                "min_match_score": (profile or {}).get("min_match_score"),
                                "sample_input_jobs": [
                                    {"job_id": j.job_id, "title": j.title[:80],
                                     "company": j.company[:60], "source": j.source}
                                    for j in user_jobs[:8]
                                ],
                            },
                            chat_id=chat_id,
                            run_id=pctx.run_id,
                        ) as fctx:
                            raw = enrich_jobs_ai(
                                user_jobs,
                                resume_text,
                                timeout_s=enrich_timeout_s,
                                projected_prefs=prefs_for_ai,
                            )
                            enrichments_by_job_id = by_job_id(raw, user_jobs)
                            # Summary only. Per-verdict detail is emitted as
                            # one forensic line per job below — packing all
                            # verdicts into a single `output.verdicts` list
                            # blew through the 4 KiB field cap and silently
                            # dropped tail entries (a recent run lost 22/34,
                            # including all LinkedIn jobs and the only
                            # score=2 result). One-line-per-verdict keeps
                            # every job present and well under the cap.
                            fctx.set_output({
                                "enriched_count": len(enrichments_by_job_id),
                                "score_distribution": _score_histogram(enrichments_by_job_id),
                            })
                        # Per-verdict forensic lines. Emitted *after* the
                        # `forensic.step` context closes so the summary line
                        # writes first, but ordering doesn't matter for
                        # correctness — `log_step` is thread-safe and each
                        # line is fully self-describing (carries chat_id +
                        # run_id + job_id). `forensic.log_step` cannot
                        # raise; if it does we still want the digest to
                        # ship, hence the broad except.
                        try:
                            for j in user_jobs:
                                enr = enrichments_by_job_id.get(j.job_id) or {}
                                kd = enr.get("key_details") or {}
                                # Compact "k=v; k=v" summary so the verdict
                                # line stays a few hundred bytes even when
                                # `key_details` carries 6+ fields.
                                key_details_summary = "; ".join(
                                    f"{k}={str(v)[:60]}"
                                    for k, v in kd.items()
                                    if v not in (None, "", [], {})
                                )[:400]
                                forensic.log_step(
                                    "enrich_jobs_ai.verdict",
                                    input={
                                        "job_id": j.job_id,
                                        "title": j.title[:120],
                                        "company": (j.company or "")[:80],
                                        "source": j.source,
                                        "url": (j.url or "")[:200],
                                    },
                                    output={
                                        "match_score": enr.get("match_score"),
                                        "why_match": (enr.get("why_match") or "")[:300],
                                        "key_details_summary": key_details_summary,
                                    },
                                    chat_id=chat_id,
                                    run_id=pctx.run_id,
                                )
                        except Exception:
                            log.debug("per-verdict forensic emit failed; continuing",
                                      exc_info=True)
                        log.info("User %s: enriched %d/%d jobs",
                                 chat_id, len(enrichments_by_job_id), len(user_jobs))
                    except Exception as e:
                        log.exception("User %s: enrichment failed: %s", chat_id, e)
                        pctx.incr_errors(1)

                # Snapshot all enriched jobs (passed AND below-floor) into
                # `digest_run_jobs` BEFORE the score-gate filter. That lets the
                # digest header's ⬇ button replay dropped postings later, with
                # the same enrichment payload (match_score + why_match +
                # key_details) the live message would have carried. Failure here
                # never blocks the digest — the cache is a UX nicety.
                if enrichments_by_job_id:
                    try:
                        db.record_digest_run_jobs(chat_id, pctx.run_id, enrichments_by_job_id)
                    except Exception:
                        log.debug("record_digest_run_jobs failed; continuing", exc_info=True)

                # Per-source strike accounting. A source earns a strike for
                # this user only when ALL of these hold:
                #   1. it contributed ≥1 job to the user's pool this run, AND
                #   2. ≥1 of those jobs received a REAL enrichment score
                #      (i.e. Haiku actually returned a verdict — not a None
                #      from a dropped batch), AND
                #   3. the max real score across those jobs is 0.
                #
                # Why the "real score" gate: a fetch that succeeded but had
                # its Haiku batch silently fail returns score=None for every
                # job in that batch. Counting that as a strike would punish
                # the source for an LLM/network blip rather than off-target
                # content. Same logic protects against transient fetch
                # outages — if `fetch()` itself fails (raises / blocked /
                # 403 / timeout), the source contributes 0 jobs to
                # `user_jobs` and never appears in `by_source` below, so it
                # cannot accumulate strikes from connectivity issues.
                #
                # Three strikes → auto-disable for this user; next run drops
                # the source's postings before enrichment to save tokens.
                try:
                    strike_threshold = int(os.environ.get("SOURCE_STRIKE_THRESHOLD") or 3)
                except (TypeError, ValueError):
                    strike_threshold = 3
                strike_threshold = max(1, strike_threshold)
                if enrichments_by_job_id and user_jobs:
                    by_source: dict[str, int] = {}
                    for j in user_jobs:
                        enr = enrichments_by_job_id.get(j.job_id)
                        if not enr:
                            # Enrichment missing entirely (batch failure /
                            # parse error). Don't penalize the source for
                            # an LLM hiccup.
                            continue
                        raw = enr.get("match_score")
                        if raw is None:
                            # Score not provided — same defensive skip.
                            continue
                        try:
                            s = int(raw)
                        except (TypeError, ValueError):
                            continue
                        prev = by_source.get(j.source, -1)
                        if s > prev:
                            by_source[j.source] = s
                    for src, max_score in by_source.items():
                        try:
                            new_streak, just_disabled = db.record_source_outcome(
                                chat_id, pctx.run_id, src, max_score,
                                threshold=strike_threshold,
                            )
                        except Exception:
                            log.debug("record_source_outcome failed for %s/%s",
                                      chat_id, src, exc_info=True)
                            continue
                        if just_disabled:
                            log.warning(
                                "User %s: source '%s' AUTO-DISABLED after %d "
                                "consecutive zero-score runs (threshold=%d)",
                                chat_id, src, new_streak, strike_threshold,
                            )
                            try:
                                forensic.log_step(
                                    "source.auto_disabled",
                                    input={
                                        "chat_id": chat_id,
                                        "source_key": src,
                                        "miss_streak": new_streak,
                                        "threshold": strike_threshold,
                                    },
                                    output={"disabled": True},
                                    chat_id=chat_id,
                                    run_id=pctx.run_id,
                                )
                            except Exception:
                                log.debug("forensic emit failed", exc_info=True)

                enriched_count = len(enrichments_by_job_id)
                user_min_score = int((profile or {}).get("min_match_score") or 0)
                effective_min_score = user_min_score if user_min_score > 0 else default_min_score
                dropped_below_score = 0
                if effective_min_score > 0 and user_jobs:
                    before = len(user_jobs)
                    user_jobs = [
                        j for j in user_jobs
                        if int((enrichments_by_job_id.get(j.job_id) or {}).get("match_score") or 0) >= effective_min_score
                    ]
                    dropped_below_score = before - len(user_jobs)
                    gate_source = "user" if user_min_score > 0 else "default"
                    log.info("User %s: min_score ≥ %d (%s) gate %d → %d",
                             chat_id, effective_min_score, gate_source, before, len(user_jobs))
                    if not user_jobs and quiet:
                        continue
                # Header floor reflects the gate that actually fired (user
                # value if set, otherwise the global default). Surfacing 0
                # would hide the ⬆ button and lie about what was filtered.
                min_score = effective_min_score
                # Count of cached unsent jobs with score ≥ (floor-1) — feeds
                # the ⬇ button label (e.g. "⬇ ≥2 (+5)"). 0 means hide the
                # button. Inclusive-upward so the (+M) matches what a click
                # actually replays.
                lower_count_at_step = 0
                if effective_min_score > 0:
                    try:
                        lower_count_at_step = db.unsent_count_at_score(
                            chat_id, pctx.run_id, effective_min_score - 1,
                        )
                    except Exception:
                        log.debug("unsent_count_at_score failed; continuing", exc_info=True)

                try:
                    if user_jobs:
                        _pigs.send_sticker(tg, chat_id, _pigs.GOOD_MORNING)
                    else:
                        _pigs.send_sticker(tg, chat_id, _pigs.NO_MATCHES)
                except Exception:
                    log.debug("digest-header sticker send failed; continuing", exc_info=True)

                try:
                    _sent_jobs_this_run: list[str] = []
                    def _on_sent(mid, j, _cid=chat_id, _sink=_sent_jobs_this_run):
                        db.log_sent(_cid, mid, j.job_id)
                        _sink.append(j.job_id)
                    sent = send_per_job_digest(
                        tg, chat_id, user_jobs, filters,
                        on_sent=_on_sent,
                        enrichments=enrichments_by_job_id,
                        min_score=min_score,
                        run_id=pctx.run_id,
                        enriched_count=enriched_count,
                        dropped_below_score=dropped_below_score,
                        lower_count_at_step=lower_count_at_step,
                    )
                    total_sent += sent
                    if _sent_jobs_this_run:
                        try:
                            db.mark_digest_jobs_sent(
                                chat_id, pctx.run_id, _sent_jobs_this_run,
                                floor=min_score,
                            )
                        except Exception:
                            log.debug("mark_digest_jobs_sent failed; continuing", exc_info=True)
                except Exception as e:
                    log.exception("Failed to send digest to %s: %s", chat_id, e)
                    pctx.incr_errors(1)
                    send_failed_chat = chat_id
                    break

            stats["jobs_sent_total"] = total_sent
            log.info(
                "DIGEST_SUMMARY users=%d raw=%d sent=%d web=%d li_user=%d",
                stats["users_total"],
                stats["jobs_raw_total"],
                stats["jobs_sent_total"],
                stats["web_search_hits"],
                stats["linkedin_user_hits"],
            )

            pctx.set_users_total(stats["users_total"])
            pctx.set_jobs_raw(stats["jobs_raw_total"])
            pctx.set_jobs_sent(stats["jobs_sent_total"])
            pctx.record_extra("web_hits", stats["web_search_hits"])
            pctx.record_extra("linkedin_user_hits", stats["linkedin_user_hits"])
            if errors:
                pctx.incr_errors(len(errors))
                pctx.record_extra("source_errors", errors)

            if send_failed_chat is not None:
                exit_code = 3
            elif errors:
                exit_code = 2
            else:
                exit_code = 0
            pctx.set_exit_code(exit_code)
    except Exception:
        log.exception("search_jobs.run: top-level failure (escaped error_capture)")
        exit_code = 3

    if run_id_for_summary is not None:
        try:
            deliver_daily_summary(tg, store, run_id_for_summary)
        except Exception:
            log.exception("daily summary delivery raised")

    return exit_code


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Print results, don't post or persist sends.")
    ap.add_argument("--chat-id", type=int, default=None, help="Send only to this one chat_id.")
    args = ap.parse_args()
    sys.exit(run(dry_run=args.dry_run, only_chat=args.chat_id))
