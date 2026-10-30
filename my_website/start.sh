#!/usr/bin/env bash
# MyLocalAI website launcher (Linux / macOS).
#
# Usage:
#   ./start.sh                 # start on port 8000 and open the browser after 2s
#   ./start.sh --no-browser    # automated testing: do not open a browser
#
# Environment overrides:
#   PORT=8010 ./start.sh       # listen on another port
#   PYTHON=/path/to/python ./start.sh
#
# 2026-10-22 fix G7: port and interpreter are configurable; a start.bat-only
# deployment was not portable.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
PYTHON="${PYTHON:-}"

if [ -z "$PYTHON" ]; then
  if [ -x "../.venv/bin/python" ]; then
    PYTHON="../.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON="python"
  else
    echo "ERROR: no Python interpreter found. Create the venv first:" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  .venv/bin/python -m pip install -r requirements.txt" >&2
    exit 1
  fi
fi

if [ "${1:-}" != "--no-browser" ]; then
  (
    sleep 2
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "http://127.0.0.1:${PORT}" || true
    fi
  ) &
fi

exec "$PYTHON" -m uvicorn app:app --host 127.0.0.1 --port "$PORT"
