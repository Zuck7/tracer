#!/usr/bin/env bash
# Start everything for the demo: 3 mocks + the live log, reachable from a phone on the same Wi-Fi.
#   scripts/run_demo.sh
# Ctrl+C stops all of it. Start the agent from the live log's "start agent" button.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

"$PY" -m uvicorn mocks.warehouse:app --port 8001 --log-level warning &
"$PY" -m uvicorn mocks.orders:app    --port 8002 --log-level warning &
"$PY" -m uvicorn mocks.carrier:app   --port 8003 --log-level warning &
# 0.0.0.0 so other devices on the network (a judge's phone) can open the live log.
# The mocks stay on localhost: the live log proxies chaos to them.
"$PY" -m uvicorn web.app:app --host 0.0.0.0 --port 8000 --log-level warning &

trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM
IP="$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo localhost)"
DB="$(grep -E '^TRACER_DB=' .env 2>/dev/null | cut -d= -f2)"
echo "mocks:     warehouse :8001  orders :8002  carrier+notify :8003"
echo "live log:  http://localhost:8000"
echo "phone:     http://${IP}:8000   (same Wi-Fi)"
echo "database:  ${DB:-tracer}"
wait
