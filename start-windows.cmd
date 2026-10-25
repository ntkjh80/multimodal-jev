@echo off
REM 공백이 있는 경로도 지원합니다.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Python 3.9 or later is required.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" run.py --open
if errorlevel 1 pause
