@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if exist "%SCRIPT_DIR%venv\Scripts\activate.bat" (
  call "%SCRIPT_DIR%venv\Scripts\activate.bat"
)

set "HOST=0.0.0.0"
if not "%API_HOST%"=="" set "HOST=%API_HOST%"

set "PORT=8000"
if not "%API_PORT%"=="" set "PORT=%API_PORT%"

if not exist "%SCRIPT_DIR%logs" mkdir "%SCRIPT_DIR%logs"

python -m uvicorn app:app --host %HOST% --port %PORT% --log-level info --proxy-headers >> "%SCRIPT_DIR%logs\uvicorn.log" 2>&1
