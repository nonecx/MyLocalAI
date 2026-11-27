@echo off
rem ============================================================
rem One-shot CI (M4): compileall src -> unittest discover -> mock gates.
rem Any step that exits non-zero fails the whole run. Usage (repo root):
rem     scripts\ci.cmd
rem NOTE: this file is intentionally pure ASCII + CRLF - Chinese cmd.exe
rem       parses batch files in the ANSI/OEM codepage (same convention as
rem       my_website/start.bat, checked by mock gate 3).
rem All mock_run.py modes hit a local mock upstream; no API quota is spent.
rem ============================================================
setlocal
cd /d "%~dp0.."
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [CI] venv interpreter not found: %PY% - create .venv first, see README
    exit /b 2
)

echo [CI 1/3] compileall src ...
"%PY%" -X utf8 -m compileall -q src
if errorlevel 1 (
    echo [CI 1/3] FAIL - compileall src
    exit /b 1
)
echo [CI 1/3] OK - compileall src

echo [CI 2/3] unittest discover -s tests -p "test_*.py" ...
"%PY%" -X utf8 -m unittest discover -s tests -p "test_*.py"
if errorlevel 1 (
    echo [CI 2/3] FAIL - unittest discover
    exit /b 1
)
echo [CI 2/3] OK - unittest discover

echo [CI 3/3] mock_run.py gates (28 checks, mock upstream) ...
if not exist ".tmp\verify-2026-10-22\mock_run.py" (
    echo [CI 3/3] SKIP - local harness missing: .tmp\verify-2026-10-22\mock_run.py
    echo [CI] RESULT: GREEN - compileall + unittest; mock gates skipped; harness not in repo
    exit /b 0
)
"%PY%" -X utf8 .tmp\verify-2026-10-22\mock_run.py
if errorlevel 1 (
    echo [CI 3/3] FAIL - mock_run.py gates
    exit /b 1
)
echo [CI 3/3] OK - mock_run.py gates

echo [CI] RESULT: ALL GREEN (compileall + unittest + mock gates)
exit /b 0
