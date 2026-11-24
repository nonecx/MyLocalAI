#!/usr/bin/env bash
# MyLocalAI website launcher (Linux / macOS).
#
# Usage:
#   ./start.sh                 # start on port 8000, listen on 127.0.0.1, open browser after 2s
#   ./start.sh --no-browser    # automated testing: do not open a browser
#   ./start.sh --lan           # listen on 0.0.0.0 so phones/tablets on the same Wi-Fi can open it
#                              # (combine: ./start.sh --lan --no-browser)
#
# 2026-10-22 fix G7: port and interpreter are configurable; a start.bat-only
# deployment was not portable.
# 2026-11-24 mobile quick entry: --lan opens the LAN listener so the phone can
# reach the site and "add to home screen"; default stays 127.0.0.1 (opt-in only).
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
PYTHON="${PYTHON:-}"
HOST="127.0.0.1"
LAN=0
OPEN_BROWSER=1

for arg in "$@"; do
  case "$arg" in
    --lan)        HOST="0.0.0.0"; LAN=1 ;;
    --no-browser) OPEN_BROWSER=0 ;;
    *)            echo "WARNING: unknown argument: $arg (supported: --lan / --no-browser)" >&2 ;;
  esac
done

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

echo "MyLocalAI website"
echo "  URL : http://127.0.0.1:${PORT}"
if [ "$LAN" = "1" ]; then
  echo "  LAN : phones/tablets on the same Wi-Fi can open one of:"
  # hostname -I is Linux-only; macOS has no equivalent one-liner, so try both.
  {
    hostname -I 2>/dev/null || true
    ipconfig getifaddr en0 2>/dev/null || true
  } | tr ' ' '\n' | awk 'NF && $0 ~ /^[0-9]+\./ { print "        http://" $0 ":'"${PORT}"'" }'
  echo "  note: allow the firewall prompt on first run, otherwise the phone cannot connect."
else
  echo "  LAN : local only (add --lan to let phones/tablets on the same Wi-Fi connect)"
fi

if [ "$OPEN_BROWSER" = "1" ]; then
  (
    sleep 2
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "http://127.0.0.1:${PORT}" || true
    elif command -v open >/dev/null 2>&1; then
      open "http://127.0.0.1:${PORT}" || true
    fi
  ) &
fi

exec "$PYTHON" -m uvicorn mylocalai.web.app:app --host "$HOST" --port "$PORT"