#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p "$SCRIPT_DIR/logs"

if [[ -f "$SCRIPT_DIR/venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/venv/bin/activate"
fi

HOST="${API_HOST:-0.0.0.0}"
PORT="${API_PORT:-8000}"

exec python -m uvicorn app:app --host "$HOST" --port "$PORT" --log-level info --proxy-headers >> "$SCRIPT_DIR/logs/uvicorn.log" 2>&1
