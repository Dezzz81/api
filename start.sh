#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p "$SCRIPT_DIR/logs"

# Try to ensure DB is running before API starts (autostart path)
if [[ -f "$SCRIPT_DIR/docker-compose.yml" ]]; then
  if command -v docker >/dev/null 2>&1; then
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d || docker-compose -f "$SCRIPT_DIR/docker-compose.yml" up -d || true
  elif command -v systemctl >/dev/null 2>&1; then
    systemctl start postgresql >/dev/null 2>&1 || true
  elif command -v service >/dev/null 2>&1; then
    service postgresql start >/dev/null 2>&1 || true
  fi
fi

sleep 3

if [[ -f "$SCRIPT_DIR/venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/venv/bin/activate"
fi

HOST="${API_HOST:-0.0.0.0}"
PORT="${API_PORT:-8000}"

exec python -m uvicorn app:app --host "$HOST" --port "$PORT" --log-level info --proxy-headers >> "$SCRIPT_DIR/logs/uvicorn.log" 2>&1
