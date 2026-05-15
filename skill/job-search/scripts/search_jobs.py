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
    # EU frontend-focused sources (added 2026-05-06).
    justjoinit, nofluffjobs,
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
from job_enrich import (                                   # noqa: E402
    enrich_jobs_ai,
    by_job_id,
    pick_most_relevant_ai,
    reanalyze_scoring_ai,
)
from user_profile import (                                 # noqa: E402
    profile_from_json,
    is_empty_profile,
    effective_filters,
    project_to_prefs,
)
import user_files                                          # noqa: E402
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
    "justjoinit":       justjoinit,       # Polish/CEE tech JSON API
    "nofluffjobs":      nofluffjobs,      # Polish/CEE tech JSON API
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

def _fetch_one_source(
    key: str,
    mod,
    filters: dict,
    *,
    store: MonitorStore | None = None,
    pipeline_run_id: int | None = None,
) -> tuple[str, list[Job], str | None]:
    """Run ONE source adapter inside its telemetry contexts.

    Returns (key, jobs, err_str). `err_str=None` on success; string
    summary on failure (adapter exception) — the parallel orchestrator
    collects errors and surfaces them via the legacy `errors` list.
    """
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
        return key, fetched, None
    except Exception as e:
        log.exception("%s fetch raised: %s", key, e)
        return key, [], f"{type(e).__name__}: {str(e)[:200]}"


def fetch_all(
    filters: dict,
    *,
    store: MonitorStore | None = None,
    pipeline_run_id: int | None = None,
) -> tuple[list[Job], list[str]]:
    """Fan out to every enabled global source adapter, IN PARALLEL.

    Algorithm v2.2: switched from a serial for-loop to a thread pool
    (configurable via `defaults.ai_source_workers`, default 6). Source
    adapters are network-IO bound (HTTP/RSS/JSON gets, occasional
    Claude CLI subprocess for curated_boards/devex/un_careers/
    ub_doctoral) so threading scales them well — wall time drops from
    ~11-23 min serial to ~2-4 min for 23 adapters.

    Thread-safety notes:
      * `forensic.log_step` is explicitly thread-safe (per-line append).
      * `MonitorStore.source_run` opens a fresh sqlite3 connection per
        call via `DB._conn`, so concurrent inserts don't collide.
      * Adapters keep their own internal concurrency (LinkedIn paces
        1.5s between requests, impactpool fans out 8 detail-page
        workers). Running multiple adapters in parallel multiplies
        outbound traffic — keep `ai_source_workers` modest.

    When `store` and `pipeline_run_id` are supplied, each adapter call
    is still wrapped in a `source_run` telemetry context. The dry-run
    path passes neither and skips telemetry.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    enabled = filters.get("sources") or {}
    tasks: list[tuple[str, object]] = []
    for key, mod in SOURCES.items():
        if key == "remote_boards":
            if not any(enabled.get(k, True) for k in ("remoteok", "remotive", "weworkremotely")):
                continue
        elif key == "curated_boards":
            if not any(enabled.get(k, False) for k in ("remocate", "wantapply", "remoterocketship")):
                continue
        elif not enabled.get(key, True):
            continue
        tasks.append((key, mod))

    workers = int(filters.get("ai_source_workers") or 6)
    log.info("fetch_all: dispatching %d sources across %d workers",
             len(tasks), workers)

    all_jobs: list[Job] = []
    errors: list[str] = []
    if workers <= 1 or len(tasks) <= 1:
        for key, mod in tasks:
            _k, fetched, err = _fetch_one_source(
                key, mod, filters,
                store=store, pipeline_run_id=pipeline_run_id,
            )
            all_jobs.extend(fetched)
            if err is not None:
                errors.append(_k)
        return all_jobs, errors

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _fetch_one_source, key, mod, filters,
                store=store, pipeline_run_id=pipeline_run_id,
            )
            for key, mod in tasks
        ]
        for fut in as_completed(futures):
            try:
                key, fetched, err = fut.result()
            except Exception as e:
                # _fetch_one_source already catches & returns errs;
                # this is the belt-and-braces guard.
                log.exception("fetch_all worker raised: %s", e)
                errors.append("worker_exception")
                continue
            all_jobs.extend(fetched)
            if err is not None:
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

                # Algorithm v2: profile JSON is now a thin envelope
                # (search_seeds + bookkeeping). Scoring inputs come from the
                # per-user files state/users/<chat_id>/{resume.txt,prefs.txt}.
                # The DB column users.min_match_score holds the ⭐ floor
                # (survives profile rebuilds, unlike the JSON one used to).
                profile = profile_from_json(db.get_user_profile(chat_id))
                resume_text = user_files.read_resume(chat_id)
                prefs_text = user_files.read_prefs(chat_id)
                free_text = prefs_text  # legacy alias for downstream callers

                effective = effective_filters(filters, profile)
                log.info(
                    "User %s: resume_chars=%d prefs_chars=%d seeds=%s",
                    chat_id, len(resume_text), len(prefs_text),
                    bool((profile or {}).get("search_seeds")),
                )

                user_pool = post_filter(jobs_raw, effective)
                log.info("User %s: post_filter %d → %d", chat_id, len(jobs_raw), len(user_pool))

                # Algorithm v2.3: per-user source toggles. If the user
                # has an `enabled_sources` list (set by the profile
                # builder at build time, optionally edited via the
                # /sources UI), drop pool entries whose source isn't in
                # the list. Saves Sonnet enrichment tokens on off-family
                # sources (e.g. ai_jobs_net for a researcher, jobs_ac_uk
                # for a frontend dev).
                enabled = db.get_enabled_sources(chat_id)
                if enabled:
                    allowed = set(enabled)
                    before_src = len(user_pool)
                    user_pool = [j for j in user_pool if j.source in allowed]
                    if before_src != len(user_pool):
                        log.info(
                            "User %s: source-toggle filter %d → %d "
                            "(allowed=%d sources)",
                            chat_id, before_src, len(user_pool), len(allowed),
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

                # NOTE: liveness gate runs at send-time on the few postings
                # that survive scoring + the ⭐ floor — see telegram_client.
                # send_per_job_digest. Running it pre-enrich on the full
                # 500-1000 job pool wasted ~90 minutes of Haiku WebFetch
                # calls for a handful of dead postings — most candidates
                # are filtered cheaper by score or floor anyway.

                enrichments_by_job_id: dict[str, dict] = {}
                if ai_enrich and user_jobs:
                    try:
                        with forensic.step(
                            "enrich_jobs_ai",
                            input={
                                "job_count": len(user_jobs),
                                "resume_chars": len(resume_text),
                                "prefs_chars": len(prefs_text),
                                "min_match_score": db.get_min_match_score(chat_id),
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
                                prefs_text,
                                timeout_s=enrich_timeout_s,
                                max_jobs_per_call=int(filters.get("ai_max_jobs_per_call") or 10),
                                two_pass=bool(filters.get("ai_two_pass", False)),
                                triage_floor=int(filters.get("ai_triage_floor") or 2),
                                workers=int(filters.get("ai_enrich_workers") or 4),
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
                                        "why_mismatch": (enr.get("why_mismatch") or "")[:300],
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

                enriched_count = len(enrichments_by_job_id)
                # Floor lives on the DB column now (algorithm v2). Falls
                # back to the global default when the user hasn't tapped ⭐.
                user_min_score = int(db.get_min_match_score(chat_id) or 0)
                effective_min_score = user_min_score if user_min_score > 0 else default_min_score
                dropped_below_score = 0
                # Keep the pre-floor pool around for the v2.4 fallback
                # path — when nothing clears the user's ⭐ floor, we still
                # surface the single most-relevant near-miss.
                pre_floor_jobs = list(user_jobs)
                fallback_mode = False
                fallback_top_score = 0
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

                # ----- v2.4 fallback (refactored 2026-05-15) ---------
                # Run the send-time prefilter (age / dead-URL / forum /
                # LLM-liveness) on the FLOOR-survivors first. Only if
                # NO job survives the gates do we fall back to the
                # closest miss from `pre_floor_jobs`. Previous version
                # checked `if not user_jobs` BEFORE the prefilter, which
                # skipped the fallback whenever the floor cut returned
                # at least 1 job — but that job often died inside
                # `send_per_job_digest`'s inline prefilter, leaving the
                # user with 0 sent and no fallback.
                from telegram_client import prefilter_for_send
                alive_floor: list[Job] = []
                if user_jobs:
                    alive_floor, _drop_counts = prefilter_for_send(
                        user_jobs, chat_id, forensic=forensic,
                    )
                    if len(alive_floor) != len(user_jobs):
                        log.info(
                            "User %s: send-time prefilter %d → %d (gate drops)",
                            chat_id, len(user_jobs), len(alive_floor),
                        )
                if effective_min_score > 0 and not alive_floor and enrichments_by_job_id and pre_floor_jobs:
                    # Pull candidates from the pre-floor pool, sorted by
                    # score DESC. For each score tier with >=2 tied
                    # candidates, the Sonnet tie-breaker picks the best.
                    # Then validate the pick via prefilter; on death,
                    # try the next candidate in the same tier, then the
                    # next tier.
                    scored = [
                        (int((enrichments_by_job_id.get(j.job_id) or {}).get("match_score") or 0), j)
                        for j in pre_floor_jobs
                        # Don't re-try jobs we already validated in
                        # alive_floor above (none survived); they're
                        # also in pre_floor_jobs.
                    ]
                    scored = [(s, j) for s, j in scored if s >= 1]
                    # Group by score tier (desc).
                    tiers: dict[int, list[Job]] = {}
                    for s, j in scored:
                        tiers.setdefault(s, []).append(j)
                    picked_fallback = None
                    picked_score = 0
                    for tier_score in sorted(tiers.keys(), reverse=True):
                        candidates = tiers[tier_score]
                        # Sonnet tie-break when >1; for size 1, no call.
                        try:
                            top = pick_most_relevant_ai(
                                candidates, resume_text, prefs_text,
                                timeout_s=enrich_timeout_s,
                            )
                        except Exception:
                            log.exception("User %s: fallback picker raised", chat_id)
                            top = candidates[0]
                        # Try the Sonnet pick first, then siblings in
                        # the same tier. Each one through prefilter to
                        # confirm liveness.
                        ordered = ([top] if top else []) + [
                            j for j in candidates if top is None or j.external_id != top.external_id
                        ]
                        for cand in ordered:
                            survivors, _ = prefilter_for_send(
                                [cand], chat_id, forensic=forensic,
                            )
                            if survivors:
                                picked_fallback = survivors[0]
                                picked_score = tier_score
                                break
                        if picked_fallback is not None:
                            break
                    if picked_fallback is not None:
                        alive_floor = [picked_fallback]
                        fallback_mode = True
                        fallback_top_score = picked_score
                        log.info(
                            "User %s: floor=%d had 0 alive matches; "
                            "fallback card top_score=%d title=%s",
                            chat_id, effective_min_score, picked_score,
                            picked_fallback.title[:60],
                        )
                    else:
                        log.info(
                            "User %s: floor=%d had 0 alive matches AND "
                            "no living fallback candidate; sending NO_MATCHES",
                            chat_id, effective_min_score,
                        )
                # All downstream stages use `alive_floor` as the final
                # send list. user_jobs kept for digest_run_jobs caching.
                user_jobs = alive_floor
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

                # Pig sticker moved INTO send_per_job_digest in v2.2 so
                # it fires after the age/url/forum/liveness prefilter and
                # reflects the actual count the user will see, not the
                # pre-gate score-floor count.

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
                        fallback_mode=fallback_mode,
                        fallback_top_score=fallback_top_score,
                        pre_filtered=True,
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

                # --- v2.5 audit stage --------------------------------
                # AFTER the cards have shipped, re-grade the score-≥1
                # verdicts with a second-opinion model (Opus by default)
                # and persist any disagreements. This catches scoring
                # drift (Sonnet over- or under-scoring) without blocking
                # the digest. Disagreements land in forensic as
                # `scoring_audit.review` lines so we can grep for
                # systematic misses post-hoc and decide whether a
                # follow-up manual top-up is warranted.
                if filters.get("ai_scoring_audit", True) and enrichments_by_job_id:
                    try:
                        ext_to_job = {j.external_id: j for j in user_pool}
                        # Audit score-≥1 only — score-0s are firmly
                        # negative and re-checking them is rarely useful.
                        ext_id_to_enrich = {
                            ext_id: enr for ext_id, enr in enrichments_by_job_id.items()
                            # enrichments_by_job_id is keyed by *job_id*
                            # not external_id; rebuild via the
                            # external_id keys we kept above.
                        }
                        # Actually rebuild a key-by-external_id map
                        # because reanalyze_scoring_ai wants external_ids.
                        ext_enr: dict[str, dict] = {}
                        audit_jobs: list[Job] = []
                        for j in user_pool:
                            enr = enrichments_by_job_id.get(j.job_id) or {}
                            score = int(enr.get("match_score") or 0)
                            if score >= 1:
                                ext_enr[j.external_id] = enr
                                audit_jobs.append(j)
                        if audit_jobs:
                            audit_timeout = int(
                                filters.get("ai_scoring_audit_timeout_s") or enrich_timeout_s
                            )
                            audit_model = str(
                                filters.get("ai_scoring_audit_model") or "opus"
                            )
                            reviews = reanalyze_scoring_ai(
                                audit_jobs, ext_enr, resume_text, prefs_text,
                                timeout_s=audit_timeout, model=audit_model,
                            )
                            disagreements = [
                                r for r in reviews
                                if r["verdict"] != "agree"
                            ]
                            log.info(
                                "User %s: scoring audit — %d reviewed, "
                                "%d disagreements (raises=%d lowers=%d)",
                                chat_id, len(reviews), len(disagreements),
                                sum(1 for r in disagreements if r["verdict"] == "raise"),
                                sum(1 for r in disagreements if r["verdict"] == "lower"),
                            )
                            # One forensic line per review entry so ops
                            # can grep specific patterns later.
                            for r in reviews:
                                j = ext_to_job.get(r["id"])
                                forensic.log_step(
                                    "scoring_audit.review",
                                    input={
                                        "external_id": r["id"],
                                        "title": (getattr(j, "title", "") or "")[:120],
                                        "company": (getattr(j, "company", "") or "")[:80],
                                        "source": getattr(j, "source", ""),
                                    },
                                    output={
                                        "original_score": r["original_score"],
                                        "revised_score":  r["revised_score"],
                                        "verdict":        r["verdict"],
                                        "comment":        r["comment"],
                                    },
                                    chat_id=chat_id,
                                    run_id=pctx.run_id,
                                )
                    except Exception:
                        log.exception("User %s: scoring audit raised; "
                                      "continuing", chat_id)
                # -----------------------------------------------------

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
