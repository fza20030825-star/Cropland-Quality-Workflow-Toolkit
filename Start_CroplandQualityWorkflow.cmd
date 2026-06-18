@echo off
setlocal

cd /d "%~dp0"

set "PYTHON_EXE="

for %%P in (
    "D:\anaconda3\envs\arcgispro-py3\python.exe"
    "%LOCALAPPDATA%\Programs\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
    "%ProgramFiles%\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
    "%ProgramFiles(x86)%\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe"
) do (
    if not defined PYTHON_EXE if exist "%%~P" set "PYTHON_EXE=%%~P"
)

if not defined PYTHON_EXE (
    for /f "delims=" %%P in ('where python.exe 2^>nul') do (
        if not defined PYTHON_EXE (
            "%%~P" -c "import arcpy" >nul 2>nul
            if not errorlevel 1 set "PYTHON_EXE=%%~P"
        )
    )
)

if not defined PYTHON_EXE (
    echo Cannot find an ArcGIS Pro Python environment with arcpy.
    echo Please install ArcGIS Pro, or edit this file and set PYTHON_EXE manually.
    echo.
    pause
    exit /b 1
)

if /I "%~1"=="--print-python" (
    echo %PYTHON_EXE%
    exit /b 0
)

"%PYTHON_EXE%" "%~dp0run_workflow_ui.py"
if errorlevel 1 (
    echo.
    echo The workflow UI exited with an error.
    pause
    exit /b 1
)

endlocal
