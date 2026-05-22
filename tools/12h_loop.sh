#!/usr/bin/env bash
# 12-hour continuous digest stress test for chat_id 433775883.
# Stops at END_TS or when /tmp/hryu_stop_loop exists.
# Per-cycle: writes a metrics line to LOOP_LOG, full digest output to DIGEST_LOG.
set -u

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
CHAT_ID=433775883
END_TS=$(($(date +%s) + 12*3600))
LOOP_LOG="$ROOT/state/12h_loop.log"
DIGEST_LOG="$ROOT/state/digest.log"
PY="$ROOT/.venv/bin/python"
STOP_FILE=/tmp/hryu_stop_loop

i=1
echo "=== 12h LOOP START $(date -Is) end_ts=$END_TS chat_id=$CHAT_ID ===" >> "$LOOP_LOG"

while [ "$(date +%s)" -lt "$END_TS" ]; do
  if [ -f "$STOP_FILE" ]; then
    echo "=== STOP FILE PRESENT, aborting at cycle $i $(date -Is) ===" >> "$LOOP_LOG"
    break
  fi

  start=$(date +%s)
  echo "=== cycle $i start $(date -Is) ===" >> "$DIGEST_LOG"
  "$PY" skill/job-search/scripts/search_jobs.py --chat-id "$CHAT_ID" >> "$DIGEST_LOG" 2>&1
  exit_code=$?
  end=$(date +%s)
  duration=$((end - start))

  # Pull this cycle's DIGEST_SUMMARY line (last one) for the metrics line.
  summary=$(grep "DIGEST_SUMMARY" "$DIGEST_LOG" | tail -1 | sed 's/^.*DIGEST_SUMMARY //')
  cached_rows=$("$PY" -c "import sqlite3; print(sqlite3.connect('state/jobs.db').execute('SELECT COUNT(*) FROM job_scores WHERE chat_id=$CHAT_ID').fetchone()[0])" 2>/dev/null || echo "?")

  echo "cycle=$i ts=$(date -Is) duration_s=$duration exit=$exit_code cached_rows=$cached_rows summary=\"$summary\"" >> "$LOOP_LOG"
  echo "=== cycle $i end $(date -Is) duration=${duration}s exit=$exit_code ===" >> "$DIGEST_LOG"

  i=$((i + 1))
  # 30s cooldown — gives Telegram a breather and keeps the loop from re-firing
  # the instant a run errors out quickly.
  sleep 30
done

echo "=== 12h LOOP END $(date -Is) total_cycles=$((i-1)) ===" >> "$LOOP_LOG"
