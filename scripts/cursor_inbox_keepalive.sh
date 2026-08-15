#!/bin/bash
set -euo pipefail
ROOT="${QUANTDESK_ROOT:-.}"
INBOX="$ROOT/data/store/alpha_miner/cursor_inbox.jsonl"
LOG="$ROOT/data/store/alpha_miner/cursor_keepalive.log"
SEED_BIG="$ROOT/scripts/seed_cursor_big.py"
SEED_CLASSIC="$ROOT/scripts/seed_classic_top.py"
n=0
if [ -f "$INBOX" ]; then
  n=$(wc -l < "$INBOX" | tr -d ' ')
fi
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) inbox=$n" >>"$LOG"
if [ "$n" -lt 150 ]; then
  # Alternate classic literature recipes vs param grids
  minute=$(date +%M)
  if [ $((10#$minute % 10)) -lt 5 ] && [ -f "$SEED_CLASSIC" ]; then
    python3 "$SEED_CLASSIC" >>"$LOG" 2>&1
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) reseeded_classic" >>"$LOG"
  else
    python3 "$SEED_BIG" >>"$LOG" 2>&1
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) reseeded_grid" >>"$LOG"
  fi
fi
