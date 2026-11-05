#!/usr/bin/env bash
# Route Studio - start backend and frontend (macOS / Linux / Git Bash).
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$(command -v python3 || command -v python)"
[ -z "$PY" ] && { echo "Python 3.10+ is required"; exit 1; }
command -v npm >/dev/null || { echo "Node.js 20.19+ is required"; exit 1; }

"$PY" -m pip install -q -r "$ROOT/backend/requirements.txt" 2>/dev/null \
  || "$PY" -m pip install -q --user -r "$ROOT/backend/requirements.txt"
[ -d "$ROOT/frontend/node_modules" ] || (cd "$ROOT/frontend" && npm install --no-audit --no-fund)

(cd "$ROOT/backend" && "$PY" run.py) &
BACK=$!
trap 'kill $BACK 2>/dev/null' EXIT
echo "Backend API: http://127.0.0.1:8060   Frontend: http://localhost:5173"
cd "$ROOT/frontend" && npm run dev
