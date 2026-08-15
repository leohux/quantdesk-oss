#!/usr/bin/env bash
set -euo pipefail
ROOT="${QUANTDESK_ROOT:-.}"
LOG="$ROOT/data/store/restore_msft_open.log"
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WAITER_START pid=$$"

# Wait until Alpaca says market is open (poll every 30s, max ~6h)
for i in $(seq 1 720); do
  open=$(docker exec -e PYTHONPATH=/app -w /app quantdesk python -c \
    "from execution.alpaca_client import AlpacaPaperClient; print(AlpacaPaperClient().is_market_open())" \
    2>/dev/null || echo False)
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) poll=$i market_open=$open"
  if [ "$open" = "True" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) MARKET_OPEN — running restore"
    sleep 15
    set +e
    docker exec -e PYTHONPATH=/app -w /app quantdesk \
      python /app/scripts/restore_msft_surge.py --apply
    rc=$?
    set -e
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) restore exit=$rc"
    docker exec -e PYTHONPATH=/app -w /app quantdesk python - <<'PY'
from execution.alpaca_client import AlpacaPaperClient
from core.portfolio.sleeve import SleeveBook, ensure_schema
ensure_schema()
c = AlpacaPaperClient()
msft = next((p for p in c.positions() if p.get("symbol") == "MSFT"), None)
print("broker_MSFT", msft)
b = SleeveBook.load()
print("sleeves", [(p.strategy_id, p.qty, p.avg_price) for p in b.owners_of("MSFT")])
PY
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) WAITER_DONE"
    exit "$rc"
  fi
  sleep 30
done
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) TIMEOUT waiting for market open"
exit 3
