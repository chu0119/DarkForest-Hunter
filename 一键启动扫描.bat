@echo off
chcp 65001 >nul
title DarkForest Watch
cd /d "%~dp0"

echo ============================================
echo   DarkForest Hunter - watch mode
echo   Close this window or press Ctrl+C to stop
echo ============================================
echo.

python -u run.py watch

echo.
echo [Watch exited]
pause
