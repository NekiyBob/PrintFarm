@echo off
setlocal

cd /d "%~dp0"

net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'powershell.exe' -ArgumentList '-ExecutionPolicy Bypass -File ""%~dp0install-agent-service.ps1"" -Action Install' -Verb RunAs -Wait"
    exit /b
)

powershell -ExecutionPolicy Bypass -File ".\install-agent-service.ps1" -Action Install
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Command failed with exit code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
