#!/bin/bash
set -euo pipefail
ROOT="${QUANTDESK_ROOT:-.}"
LOG="$ROOT/data/store/alpha_miner/stop_schedule.log"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

echo "[$(TZ=Asia/Shanghai date '+%F %T %Z')] stopper started"

SECS=$(python3 - <<'PY'
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    cn = ZoneInfo("Asia/Shanghai")
except Exception:
    cn = timezone(timedelta(hours=8))
now = datetime.now(cn)
target = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
import sys
print(int((target - now).total_seconds()))
print("stop_at", target.isoformat(), file=sys.stderr)
PY
)

SLEEP_SECS="$SECS"
echo "sleep ${SLEEP_SECS}s until CN midnight"
sleep "$SLEEP_SECS"

echo "[$(TZ=Asia/Shanghai date '+%F %T %Z')] stopping quantdesk-alpha-miner"
docker update --restart=no quantdesk-alpha-miner || true
docker stop quantdesk-alpha-miner || true
STATUS=$(docker inspect -f '{{.State.Status}}' quantdesk-alpha-miner 2>/dev/null || echo missing)
echo "[$(TZ=Asia/Shanghai date '+%F %T %Z')] miner stopped status=${STATUS}"
