@echo off
setlocal

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'powershell.exe' -WorkingDirectory '%~dp0' -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File ""%~dp0start-agent.ps1""' -WindowStyle Hidden"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Failed to start agent in background. Exit code %EXIT_CODE%.
    pause
    exit /b %EXIT_CODE%
)

echo Agent started in background.
echo You can close this window. The agent will keep running.
timeout /t 2 >nul
exit /b 0
