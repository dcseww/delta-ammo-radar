@echo off
rem ---------------------------------------------------------------------------
rem  Quick launcher (console mode) for df-ammo-radar.
rem  Use this if the .vbs launcher is blocked by policy/AV, or when you want to
rem  see the startup output. Everything user-facing is printed by start.ps1.
rem ---------------------------------------------------------------------------
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
if errorlevel 1 (
  echo.
  echo [FAIL] startup failed - see logs\backend.log.err
  pause
)
