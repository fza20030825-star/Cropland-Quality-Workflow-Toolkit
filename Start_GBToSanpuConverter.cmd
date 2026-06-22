@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%Start_GBToSanpuConverter.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo Launcher exited with error code %EXIT_CODE%.
    pause
)

exit /b %EXIT_CODE%
