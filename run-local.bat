@echo off
setlocal
cd /d "%~dp0"
if not exist ".env" (
  echo Missing .env. Copy .env.example to .env and set ADMIN_REVIEW_KEY first.
  pause
  exit /b 1
)
py -3 server.py
