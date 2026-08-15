#!/bin/bash
set -euo pipefail
# Alpaca paper daily run near US cash close (15:50 ET, Mon-Fri)
# NOTE: must run while the cash session is still open so DAY market orders can fill.
ROOT="${QUANTDESK_ROOT:-.}"
LOCK=/var/lock/quantdesk-paper-cron.lock
LOG="$ROOT/data/store/paper_runner.log"
mkdir -p "$(dirname "$LOG")" /var/lock

# Prevent overlap if a previous run is stuck
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) SKIP paper_cron already running" >>"$LOG"
  exit 0
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) START paper_cron" >>"$LOG"
docker exec -w /app -e PYTHONPATH=/app quantdesk python /app/scripts/phase6_runner.py >>"$LOG" 2>&1
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) END paper_cron exit=$?" >>"$LOG"
