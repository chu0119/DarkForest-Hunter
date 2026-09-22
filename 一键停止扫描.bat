@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: 1. Stop watch process (verify PID is actually python/run.py before killing)
if exist results\watch.pid (
    for /f %%p in (results\watch.pid) do (
        :: Verify the PID is actually a python process running run.py
        tasklist /FI "PID eq %%p" /FO CSV /NH 2>nul | findstr /i "python" >nul
        if not errorlevel 1 (
            echo [*] Stopping Watch PID=%%p
            taskkill /PID %%p /F >nul 2>&1
        ) else (
            echo [!] PID %%p is not a python process, skipping
        )
    )
    del results\watch.pid >nul 2>&1
)

:: 2. Stop mihomo
echo [*] Stopping mihomo
taskkill /F /IM mihomo.exe >nul 2>&1

:: 3. Wait for ports to release
ping -n 3 127.0.0.1 >nul

echo.
echo [OK] All processes stopped. Ports 17890+ released.
ping -n 4 127.0.0.1 >nul
