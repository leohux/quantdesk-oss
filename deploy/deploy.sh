#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   export DEPLOY_HOST=root@YOUR_SERVER_IP
#   ./deploy/deploy.sh

HOST="${DEPLOY_HOST:?Set DEPLOY_HOST e.g. root@YOUR_SERVER_IP}"
REMOTE_DIR="${REMOTE_DIR:-/opt/quantdesk}"

echo "==> Sync project to $HOST:$REMOTE_DIR"
rsync -avz --delete \
  --exclude '.venv' \
  --exclude 'web/node_modules' \
  --exclude 'web/dist' \
  --exclude '__pycache__' \
  --exclude '.git' \
  ./ "$HOST:$REMOTE_DIR/"

echo "==> Build & restart on server"
ssh "$HOST" "cd $REMOTE_DIR && docker compose up -d --build"

echo "==> Done. Open http://quantdesk.example.com (after nginx is configured)"
