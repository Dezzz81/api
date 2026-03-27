@echo off
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not exist "%SCRIPT_DIR%logs" mkdir "%SCRIPT_DIR%logs"
set "LOG=%SCRIPT_DIR%logs\bootstrap.log"

echo [%date% %time%] Bootstrap start >> "%LOG%"

if not exist "%SCRIPT_DIR%.env" (
  echo [%date% %time%] .env not found, skipping auto-setup >> "%LOG%"
)

if not exist "%SCRIPT_DIR%venv\Scripts\activate.bat" (
  echo [%date% %time%] Creating venv >> "%LOG%"
  python -m venv "%SCRIPT_DIR%venv" >> "%LOG%" 2>&1
)

if exist "%SCRIPT_DIR%venv\Scripts\activate.bat" (
  call "%SCRIPT_DIR%venv\Scripts\activate.bat"
)

if exist "%SCRIPT_DIR%requirements.txt" (
  echo [%date% %time%] Installing requirements >> "%LOG%"
  python -m pip install -r "%SCRIPT_DIR%requirements.txt" >> "%LOG%" 2>&1
)

if exist "%SCRIPT_DIR%docker-compose.yml" (
  where docker >nul 2>&1
  if %ERRORLEVEL% EQU 0 (
    echo [%date% %time%] Starting postgres via docker compose >> "%LOG%"
    docker compose -f "%SCRIPT_DIR%docker-compose.yml" up -d >> "%LOG%" 2>&1
  ) else (
    where docker-compose >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
      echo [%date% %time%] Starting postgres via docker-compose >> "%LOG%"
      docker-compose -f "%SCRIPT_DIR%docker-compose.yml" up -d >> "%LOG%" 2>&1
    ) else (
      echo [%date% %time%] Docker not found, skipping DB startup >> "%LOG%"
    )
  )
)

echo [%date% %time%] Waiting for DB >> "%LOG%"
timeout /t 10 /nobreak >> "%LOG%"

echo [%date% %time%] Starting API >> "%LOG%"
call "%SCRIPT_DIR%start.bat"
