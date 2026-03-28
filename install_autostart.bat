@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "TASK_NAME=3x-ui-bridge"
set "START_BAT=%SCRIPT_DIR%bootstrap.bat"

if not exist "%START_BAT%" (
  echo start.bat not found: %START_BAT%
  exit /b 1
)

schtasks /Create /F /RL HIGHEST /SC ONSTART /RU SYSTEM /DELAY 0000:30 ^
  /TN "%TASK_NAME%" ^
  /TR "\"%START_BAT%\""

if %ERRORLEVEL% EQU 0 (
  echo Task created: %TASK_NAME%
  echo It will run %START_BAT% at system startup.
  exit /b 0
)

echo Failed to create SYSTEM task, falling back to user logon task...
schtasks /Create /F /SC ONLOGON /RU "%USERNAME%" ^
  /TN "%TASK_NAME%" ^
  /TR "\"%START_BAT%\""

if %ERRORLEVEL% NEQ 0 (
  echo Failed to create task %TASK_NAME%
  exit /b %ERRORLEVEL%
)

echo Task created: %TASK_NAME%
echo It will run %START_BAT% at user logon.
