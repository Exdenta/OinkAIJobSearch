#!/usr/bin/env python3
"""Daily job-alert orchestrator (multi-user, DB-backed, inline buttons).

Flow:
  1. Load config/filters.yaml and .env
  2. Fan out to all enabled source adapters, collect raw postings
  3. Cross-cut filters (seniority/salary/company exclusions)
  4. Upsert each posting into the jobs table
  5. For every registered user (who has uploaded a resume):
       a. Skip postings they've already been sent OR already actioned
       b. Send each remaining posting as its own Telegram message with buttons
       c. Log sent_messages(chat_id, message_id → job_id)

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
from telegram_client import TelegramClient, send_per_job_digest  # noqa: E402
from sources import hackernews, remote_boards, indeed, linkedin, curated_boards, web_search  # noqa: E402
# NOTE: `linkedin` is imported above so we can also call it per-user via
# `linkedin.fetch_for_user` with the profile's stored seeds. Its `fetch`
# function stays in the `SOURCES` dispatch below for the global pass.
from job_enrich import enrich_jobs_ai, by_job_id           # noqa: E402
from user_profile import (                                 # noqa: E402
    profile_from_json,
    is_empty_profile,
    effective_filters,
    project_to_prefs,
)
import pig_stickers as _pigs                               # noqa: E402

try:
    import yaml
except ImportError:
    print("Missing dependency: pyyaml. Run: pip install --break-system-packages pyyaml",
          file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

log = logging.getLogger("job-search")


SOURCES = {
    "hackernews":      hackernews,
    "remote_boards":   remote_boards,
    "indeed":          indeed,
    "linkedin":        linkedin,
    "curated_boards":  curated_boards,
    # NOTE: `web_search` is deliberately NOT in this dict. It now runs
    # per-user in the recipient loop so the sub-agent can use each user's
    # free-text description to form targeted queries. See run() below.
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


def load_filters(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


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
    across every source. Keyword/title/company/salary fields in filters.yaml
    and in the per-user profile are IGNORED here — Claude reads the user's
    resume + stated preferences and decides fit holistically.

    Left as an identity function (not deleted) so every historical call site
    keeps compiling and so ops can re-enable a cheap pre-filter later by
    editing this one spot. The `filters` arg is deliberately unused.
    """
    del filters  # intentionally unused — AI is the gate
    return list(jobs)


# ---------- fetch ----------

def fetch_all(filters: dict) -> tuple[list[Job], list[str]]:
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
        try:
            fetched = mod.fetch(filters) or []
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
    cfg_path = root / os.environ.get("CONFIG_PATH", "config/filters.yaml")
    state_dir = root / os.environ.get("STATE_DIR", "state")
    db_path = state_dir / "jobs.db"

    if not cfg_path.exists():
        log.error("Config not found at %s", cfg_path)
        return 1

    filters = load_filters(cfg_path)

    db = DB(db_path)
    job_store = JobStore(db)

    # ------------------------------------------------------------------
    # Global fetch. We pull from every enabled static adapter ONCE, but we
    # DO NOT apply post_filter globally anymore. Per the full-override
    # product decision: each user's profile produces its own effective
    # filters dict, and we run post_filter with that per-user dict. Users
    # without a profile get the same behavior as before (profile = {} →
    # effective == global filters → identical post_filter result).
    #
    # `web_search` is not in `SOURCES` — it runs per-user in the loop below
    # so the sub-agent can use each user's own free-text description.
    # ------------------------------------------------------------------
    jobs_raw, errors = fetch_all(filters)
    log.info("Raw fetched across static sources: %d postings", len(jobs_raw))

    # Persist every fetched job so button callbacks can resolve them even if
    # some users won't see them in their own digest.
    new_in_db = job_store.save_all(jobs_raw)
    log.info("DB: %d newly-inserted jobs, %d already known",
             new_in_db, len(jobs_raw) - new_in_db)

    # Figure out recipients
    if only_chat is not None:
        users = [db.get_user(only_chat)]
        users = [u for u in users if u is not None]
    else:
        users = db.users_with_resume()
    if not users:
        log.warning("No registered users with a resume. Ask them to /start the bot and upload a CV.")
        log.warning("Dry-run output below for inspection:")

    if dry_run:
        # Dry-run shows the globally-filtered pool so the operator can eyeball
        # what a no-profile user would receive.
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
    msg_cfg = filters.get("message") or {}
    quiet = bool(msg_cfg.get("quiet_if_empty"))
    ai_enrich = bool(filters.get("ai_enrich", True))
    enrich_timeout_s = int(filters.get("ai_enrich_timeout_s") or 240)
    global_cap = int(filters.get("max_total") or 0)
    # Default AI match-score floor applied when a user hasn't set their own
    # via the bot's ⭐ button. Because the keyword pre-filter has been removed
    # (AI is now the sole matching gate), we need a non-zero default so score-0
    # rejects (jobs Claude explicitly flagged as wrong-fit) don't leak through.
    # Operators can raise/lower this in filters.yaml:ai_min_match_score.
    # Clamp to [0, 5]; 0 disables the default floor.
    try:
        default_min_score = int(filters.get("ai_min_match_score") or 0)
    except (TypeError, ValueError):
        default_min_score = 0
    default_min_score = max(0, min(5, default_min_score))

    total_sent = 0
    # Rollout-observability counters. Summarised in one log line at the end
    # of the run so ops can track adoption without standing up a separate
    # metrics pipeline. Keep this block and the closing log call in sync —
    # if you add a new counter here, add it to the summary too.
    stats = {
        "users_total":        0,
        "jobs_raw_total":     len(jobs_raw),
        "jobs_sent_total":    0,
        "web_search_hits":    0,  # postings added via the AI web-search adapter
        "linkedin_user_hits": 0,  # postings added via per-user LinkedIn seeds
    }

    for u in users:
        chat_id = int(u["chat_id"])
        stats["users_total"] += 1

        # Per-user profile (Opus-built, schema_version=2). May be None or
        # effectively empty — both are treated as "inherit global defaults"
        # by `effective_filters`.
        profile = profile_from_json(db.get_user_profile(chat_id))
        # Raw /prefs text lives in its own column so it survives rebuilds
        # and feeds the web-search agent with the user's own wording.
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

        # post_filter is a pass-through — AI is the sole matching gate.
        user_pool = post_filter(jobs_raw, effective)
        log.info("User %s: post_filter %d → %d", chat_id, len(jobs_raw), len(user_pool))

        # -------- per-user LinkedIn fetch --------
        # If the user's profile contains LinkedIn seeds, run up to 3 tailored
        # queries. The global LinkedIn fetch in `fetch_all` already ran with
        # `filters["linkedin"]` and is baked into `jobs_raw`; this adds
        # per-user queries on top, dedupe via `job_store.save_all` +
        # `JobStore.filter_new_for`.
        li_seeds = ((profile or {}).get("search_seeds") or {}).get("linkedin")
        li_enabled = bool((effective.get("sources") or {}).get("linkedin", False))
        has_queries = bool(li_seeds and li_seeds.get("queries"))
        if li_enabled and has_queries:
            try:
                extra_li = linkedin.fetch_for_user(effective, li_seeds) or []
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

        # -------- per-user web_search --------
        # Profile seeds (seed_phrases / ats_domains / focus_notes) are the
        # primary driver; the user's raw free-text is passed as secondary
        # context so the agent has the user's own wording for nuance.
        web_seeds = ((profile or {}).get("search_seeds") or {}).get("web_search")
        web_search_enabled = bool((effective.get("sources") or {}).get("web_search", False))
        trigger_web = web_search_enabled and (bool(web_seeds) or bool(free_text))
        if trigger_web:
            try:
                extra = web_search.fetch(
                    effective,
                    user_free_text=free_text or None,
                    profile_seeds=web_seeds,
                ) or []
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

        # Global cap per user.
        if global_cap > 0:
            user_pool = user_pool[:global_cap]

        # Dedup against jobs already sent/actioned by this user.
        user_jobs = job_store.filter_new_for(chat_id, user_pool)
        log.info("User %s: %d new postings (of %d after per-user filter + web_search)",
                 chat_id, len(user_jobs), len(user_pool))

        if not user_jobs and quiet:
            continue

        # AI matching: one batched Haiku call per user per run. This is now
        # the SOLE matching gate — there's no regex pre-filter upstream, so
        # Claude sees every fetched posting and scores it against the user's
        # resume AND their stated preferences.
        #
        # Falls back to empty dict on any failure (missing CLI / timeout /
        # junk output). In that degraded mode every job leaks past the score
        # gate (because missing == 0) — acceptable short of a hard failure,
        # and the warn log tells the operator to look.
        enrichments_by_job_id: dict[str, dict] = {}
        # AI-enrichment prompt wants a flat dict of the user's stated
        # preferences (not the effective filters — globals would pollute
        # the signal). project_to_prefs handles None profile safely.
        prefs_for_ai = project_to_prefs(profile)
        if ai_enrich and user_jobs:
            resume_text = (u["resume_text"] or "") if "resume_text" in u.keys() else ""
            try:
                raw = enrich_jobs_ai(
                    user_jobs,
                    resume_text,
                    timeout_s=enrich_timeout_s,
                    projected_prefs=prefs_for_ai,
                )
                enrichments_by_job_id = by_job_id(raw, user_jobs)
                log.info("User %s: enriched %d/%d jobs",
                         chat_id, len(enrichments_by_job_id), len(user_jobs))
            except Exception as e:
                log.exception("User %s: enrichment failed: %s", chat_id, e)

        # Match-score gate. The user's floor comes from their profile JSON
        # (`min_match_score`, set by the ⭐ button). If zero, fall back to
        # the global `ai_min_match_score` from filters.yaml — that floor
        # exists because there's no keyword pre-filter any more; without
        # it, every job Claude scored 0 ("clearly wrong fit") would leak
        # through.
        user_min_score = int((profile or {}).get("min_match_score") or 0)
        effective_min_score = user_min_score if user_min_score > 0 else default_min_score
        if effective_min_score > 0 and user_jobs:
            before = len(user_jobs)
            user_jobs = [
                j for j in user_jobs
                if int((enrichments_by_job_id.get(j.job_id) or {}).get("match_score") or 0) >= effective_min_score
            ]
            gate_source = "user" if user_min_score > 0 else "default"
            log.info("User %s: min_score ≥ %d (%s) gate %d → %d",
                     chat_id, effective_min_score, gate_source, before, len(user_jobs))
            if not user_jobs and quiet:
                continue
        # Keep `min_score` under the legacy name so the digest renderer still
        # sees the user-facing value (the ⭐ bar), not the default floor.
        min_score = user_min_score

        # Pig mascot at the top of the digest — a "good morning" pig when
        # there are matches, a "nobody home today" pig when it's empty.
        # Both are rate-limit-free (once per day at most) and fail-soft:
        # if no sticker is registered for the moment, we just skip it.
        # Fires BEFORE the digest header so the sticker sits above the
        # metadata line in the chat history.
        try:
            if user_jobs:
                _pigs.send_sticker(tg, chat_id, _pigs.GOOD_MORNING)
            else:
                _pigs.send_sticker(tg, chat_id, _pigs.NO_MATCHES)
        except Exception:
            log.debug("digest-header sticker send failed; continuing", exc_info=True)

        try:
            sent = send_per_job_digest(
                tg, chat_id, user_jobs, filters,
                on_sent=lambda mid, j, _cid=chat_id: db.log_sent(_cid, mid, j.job_id),
                enrichments=enrichments_by_job_id,
                min_score=min_score,
            )
            total_sent += sent
        except Exception as e:
            log.exception("Failed to send digest to %s: %s", chat_id, e)
            return 3

    stats["jobs_sent_total"] = total_sent
    # One-line rollout summary. Grep-friendly — the "DIGEST_SUMMARY" prefix
    # lets you filter it out of the log stream for graphing. Shape:
    #   DIGEST_SUMMARY users=7 raw=142 sent=31 web=4 li_user=3
    log.info(
        "DIGEST_SUMMARY users=%d raw=%d sent=%d web=%d li_user=%d",
        stats["users_total"],
        stats["jobs_raw_total"],
        stats["jobs_sent_total"],
        stats["web_search_hits"],
        stats["linkedin_user_hits"],
    )
    return 2 if errors else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="Print results, don't post or persist sends.")
    ap.add_argument("--chat-id", type=int, default=None, help="Send only to this one chat_id.")
    args = ap.parse_args()
    sys.exit(run(dry_run=args.dry_run, only_chat=args.chat_id))
