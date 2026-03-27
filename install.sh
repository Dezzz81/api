#!/usr/bin/env bash
set -euo pipefail

AUTOSTART=0
if [[ "${1:-}" == "--autostart" ]]; then
  AUTOSTART=1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f ".env" ]]; then
  cp ".env.example" ".env"
  echo "Created .env from .env.example. Please edit it before production use."
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install it first."
  exit 1
fi

if [[ ! -d "venv" ]]; then
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source "venv/bin/activate"
python -m pip install -r "requirements.txt"

if [[ -f "docker-compose.yml" ]]; then
  if command -v docker >/dev/null 2>&1; then
    docker compose -f "docker-compose.yml" up -d || docker-compose -f "docker-compose.yml" up -d || true
  fi
fi

if [[ "$AUTOSTART" -eq 1 ]]; then
  SERVICE_FILE="/etc/systemd/system/script_api.service"
  if [[ $EUID -ne 0 ]]; then
    echo "Autostart requires root. Re-run with sudo."
    exit 1
  fi

  cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=3x-ui API Bridge
After=network.target

[Service]
Type=simple
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=$SCRIPT_DIR/.env
ExecStart=$SCRIPT_DIR/venv/bin/python -m uvicorn app:app --host \${API_HOST:-0.0.0.0} --port \${API_PORT:-8000} --log-level info --proxy-headers
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable script_api.service
  systemctl restart script_api.service
  echo "Autostart enabled via systemd."
fi

echo "Install completed."
