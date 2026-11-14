#!/usr/bin/env bash
# Start the three mocks. Ctrl+C stops all of them.
#   scripts/run_mocks.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"

"$PY" -m uvicorn mocks.warehouse:app --port 8001 --log-level warning &
"$PY" -m uvicorn mocks.orders:app    --port 8002 --log-level warning &
"$PY" -m uvicorn mocks.carrier:app   --port 8003 --log-level warning &

trap 'kill $(jobs -p) 2>/dev/null' EXIT INT TERM
echo "mocks up: warehouse :8001  orders :8002  carrier+notify :8003"
wait
