@echo off
title MyLocalAI Speech Analysis Website
cd /d "%~dp0"

rem Port can be overridden:  set PORT=8010 before launching
if "%PORT%"=="" set PORT=8000

rem ------------------------------------------------------------------
rem Options (order does not matter, both can be given together):
rem   --lan          listen on 0.0.0.0 (default: 127.0.0.1 only) so phones
rem                  and tablets on the same Wi-Fi can open the page and
rem                  use "Add to Home Screen" from the header button
rem   --no-browser   automated testing: do not auto-open the browser
rem This file must stay pure ASCII: a non-ASCII byte here breaks cmd on
rem a non-UTF8 code page (guarded by tests/test_p1p2_fixes_2026_10_23.py).
rem ------------------------------------------------------------------
set "HOST=127.0.0.1"
set "LAN=0"
set "NOBROWSER=0"
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="--lan" (
    set "HOST=0.0.0.0"
    set "LAN=1"
) else if /i "%~1"=="--no-browser" (
    set "NOBROWSER=1"
) else (
    echo WARNING: unknown argument "%~1" ^(supported: --lan / --no-browser^)
)
shift
goto parse_args
:args_done

echo ==================================================
echo   MyLocalAI - Speech Analysis Website
echo   URL    : http://127.0.0.1:%PORT%
if "%LAN%"=="1" (
    echo   LAN    : phones / tablets on the same Wi-Fi can open one of:
    for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4"') do (
        for /f "tokens=* delims= " %%B in ("%%A") do echo            http://%%B:%PORT%
    )
    echo   note   : allow the Windows firewall prompt on first run,
    echo            otherwise the phone cannot connect even on the same Wi-Fi.
) else (
    echo   LAN    : local only ^(add --lan to let phones / tablets connect^)
)
echo   Browser will open automatically in 2 seconds.
echo   Close this window to STOP the server.
echo ==================================================
echo.

rem "--no-browser" is only for automated testing: skip auto-opening the browser.
if "%NOBROWSER%"=="0" (
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

"%PYRUN%" %PYEXTRA% -m uvicorn mylocalai.web.app:app --host %HOST% --port %PORT%

echo.
echo Server stopped.
pause