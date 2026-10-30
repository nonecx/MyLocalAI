@echo off
title MyLocalAI Speech Analysis Website
cd /d "%~dp0"

rem Port can be overridden:  set PORT=8010 before launching
if "%PORT%"=="" set PORT=8000

echo ==================================================
echo   MyLocalAI - Speech Analysis Website
echo   URL    : http://127.0.0.1:%PORT%
echo   Browser will open automatically in 2 seconds.
echo   Close this window to STOP the server.
echo ==================================================
echo.

rem "--no-browser" is only for automated testing: skip auto-opening the browser.
if /i not "%~1"=="--no-browser" (
    start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:%PORT%"
)

rem Prefer the project venv interpreter; fall back to whatever python is on PATH.
set "PYRUN="
if exist "..\.venv\Scripts\python.exe" set "PYRUN=..\.venv\Scripts\python.exe"
if not defined PYRUN (
    where python >nul 2>nul && set "PYRUN=python"
)
if not defined PYRUN (
    where py >nul 2>nul && set "PYRUN=py"
    if defined PYRUN set "PYEXTRA=-3"
)
if not defined PYRUN (
    echo ERROR: no Python interpreter found. Create the venv first:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

"%PYRUN%" %PYEXTRA% -m uvicorn app:app --host 127.0.0.1 --port %PORT%

echo.
echo Server stopped.
pause
