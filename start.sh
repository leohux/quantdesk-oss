#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
  echo "已创建 .env"
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  echo "使用 Docker 启动…"
  docker compose up -d --build
  echo "等待服务…"
  for _ in $(seq 1 80); do
    if curl -sf http://127.0.0.1:18080/health >/dev/null 2>&1; then
      break
    fi
    sleep 3
  done
  URL="http://127.0.0.1:18080"
  echo "已启动: $URL"
  if command -v open >/dev/null 2>&1; then
    open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL"
  fi
  exit 0
fi

echo "请先安装并启动 Docker Desktop，然后重新运行 ./start.sh"
echo "https://www.docker.com/products/docker-desktop/"
exit 1
