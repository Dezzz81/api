@echo off
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not exist "%SCRIPT_DIR%logs" mkdir "%SCRIPT_DIR%logs"

if not exist "%SCRIPT_DIR%.env" (
  echo Creating .env with local defaults...
  (
    echo PANEL_SCHEME=https
    echo PANEL_HOST=panel.example.com
    echo PANEL_PORT=2053
    echo PANEL_BASE_PATH=/randompath
    echo PANEL_USERNAME=admin
    echo PANEL_PASSWORD=change_me
    echo PANEL_2FA=
    echo PANEL_VERIFY_TLS=true
    echo.
    echo INBOUND_ID=1
    echo API_TOKEN=change_me
    echo.
    echo VLESS_HOST=vpn.example.com
    echo DEFAULT_FLOW=xtls-rprx-vision
    echo DEFAULT_FINGERPRINT=random
    echo DEFAULT_ALPN=
    echo REQUEST_TIMEOUT=10
    echo.
    echo POSTGRES_DB=script_api
    echo POSTGRES_USER=script_api
    echo POSTGRES_PASSWORD=change_me
    echo DATABASE_URL=postgresql+asyncpg://script_api:change_me@127.0.0.1:5432/script_api
    echo ADMIN_USER=admin
    echo ADMIN_PASS=change_me
    echo.
    echo PANEL_SERVERS_JSON=
  ) > "%SCRIPT_DIR%.env"
)

set "DISABLE_DB=1"

if not exist "%SCRIPT_DIR%venv\Scripts\activate.bat" (
  echo Creating venv...
  python -m venv "%SCRIPT_DIR%venv"
)

if exist "%SCRIPT_DIR%venv\Scripts\activate.bat" (
  call "%SCRIPT_DIR%venv\Scripts\activate.bat"
)

if exist "%SCRIPT_DIR%requirements.txt" (
  echo Installing requirements...
  python -m pip install -r "%SCRIPT_DIR%requirements.txt"
)

set "USE_SQLITE=0"
set "DB_READY=0"

echo DB disabled for local run (DISABLE_DB=1)

echo Starting API on http://127.0.0.1:8000 ...
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --reload
