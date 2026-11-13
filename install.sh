#!/usr/bin/env bash
# Serverda bitta buyruq bilan o'rnatish:  bash install.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo ">> Docker o'rnatilmoqda…"
  curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version >/dev/null 2>&1; then
  apt-get update && apt-get install -y docker-compose-plugin
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo ">> .env yaratildi. Kalitlarni yozing:  nano .env   keyin qayta:  bash install.sh"
  exit 0
fi

mkdir -p data
docker compose up -d --build
sleep 8
docker compose logs --tail=20
