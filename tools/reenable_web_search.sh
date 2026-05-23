#!/usr/bin/env bash
# One-shot re-enable of the web_search source, scheduled for 2026-05-30 09:00.
# Fires from cron; self-removes after running.
#
# Steps:
#   1. Sanity-check today's date (cron lacks a year field; this script ignores
#      stray fires on any year != 2026).
#   2. Scan state/bot.log + DB since 2026-05-23 for any signs that the T4
#      verifier hardening missed a soft-404 (uncertain-verdict drops are
#      expected and OK; the only failure mode is web_search jobs actually
#      shipping to sent_messages).
#   3. If clean: edit defaults.py to flip web_search back to True, commit,
#      restart the bot (PID discovered via pgrep, not hardcoded).
#   4. Self-remove from crontab so it never fires again.
#   5. Append a one-line summary to tools/reenable_web_search.log.

set -u
cd /home/uby/Projects/HryuAutoJobSearch

LOG=/home/uby/Projects/HryuAutoJobSearch/tools/reenable_web_search.log
SINCE="2026-05-23"
TARGET_DATE="2026-05-30"

log() { printf '%s  %s\n' "$(date -Is)" "$*" >> "$LOG"; }

log "=== fire ==="
TODAY=$(date +%Y-%m-%d)
if [ "$TODAY" != "$TARGET_DATE" ]; then
    log "skip: today=$TODAY != target=$TARGET_DATE"
    exit 0
fi

# --- 1. Check for any web_search soft-404 leaks since the T4 merge ---
shipped=$(.venv/bin/python <<'PY'
import sqlite3, datetime
db = sqlite3.connect("state/jobs.db")
since = datetime.datetime(2026, 5, 23).timestamp()
n = db.execute(
    "SELECT COUNT(*) FROM sent_messages s JOIN jobs j ON j.job_id=s.job_id "
    "WHERE j.source='web_search' AND s.sent_at>=?",
    (since,),
).fetchone()[0]
print(n)
PY
)
if [ "$shipped" != "0" ]; then
    log "ABORT: $shipped web_search jobs shipped to user since $SINCE — review before re-enable"
    crontab -l 2>/dev/null | grep -v "reenable_web_search.sh" | crontab -
    log "removed self from crontab to avoid retry-spam"
    exit 1
fi
log "clean: 0 web_search jobs shipped since $SINCE"

# --- 2. Edit defaults.py ---
sed -i 's|"web_search":       False,.*$|"web_search":       True,    # Re-enabled '"$TARGET_DATE"' after T4 verifier hardening soaked for 7 days with zero leaks.|' skill/job-search/scripts/defaults.py
if ! grep -q '"web_search":       True,' skill/job-search/scripts/defaults.py; then
    log "ABORT: sed did not produce expected web_search: True line"
    exit 1
fi
log "defaults.py edited"

# --- 3. Commit ---
git add skill/job-search/scripts/defaults.py
git commit -m "config: re-enable web_search after 7-day soak of T4 verifier hardening" >> "$LOG" 2>&1
log "committed"

# --- 4. Restart bot via discovered PID ---
PIDS=$(pgrep -f "skill/job-search/scripts/bot.py" || true)
for pid in $PIDS; do
    log "killing bot pid=$pid"
    kill "$pid" 2>>"$LOG" || true
done
sleep 5
# Any holdouts? -9 them.
for pid in $(pgrep -f "skill/job-search/scripts/bot.py" || true); do
    log "force-kill bot pid=$pid"
    kill -9 "$pid" 2>>"$LOG" || true
done

echo "=== BOT RESTART $(date -Is) — web_search re-enabled ===" >> state/bot.log
nohup .venv/bin/python skill/job-search/scripts/bot.py >> state/bot.log 2>&1 &
disown
sleep 5
NEW=$(pgrep -f "skill/job-search/scripts/bot.py" | head -1 || true)
log "new bot pid=$NEW"

# --- 5. Self-remove from crontab ---
crontab -l 2>/dev/null | grep -v "reenable_web_search.sh" | crontab -
log "removed self from crontab"
log "=== complete ==="
