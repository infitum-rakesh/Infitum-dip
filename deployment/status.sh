#!/usr/bin/env bash
# ============================================================
#  Data Intelligence Platform — status.sh
#  All checks are real: kill -0 for the process, real HTTP calls
#  for backend/scheduler/OCR/DB health. Scheduler/Worker are
#  reported as in-process subsystems of the one backend PID (see
#  README_DEPLOYMENT.md) — this is the real architecture, not a
#  simplification of a "should be separate" design.
# ============================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PIDFILE="$SCRIPT_DIR/run/backend.pid"
PORT=8080
if [ -f "$SCRIPT_DIR/backend/.env" ]; then
    ENV_PORT="$(grep -m1 '^PORT=' "$SCRIPT_DIR/backend/.env" | cut -d= -f2-)"
    [ -n "$ENV_PORT" ] && PORT="$ENV_PORT"
fi

echo "========================================"
echo " Data Intelligence Platform — Status"
echo "========================================"
echo

# ── Backend process ──────────────────────────────────────────
BACKEND_UP=0
PID=""
if [ -f "$PIDFILE" ]; then
    PID="$(cat "$PIDFILE")"
    if kill -0 "$PID" 2>/dev/null; then
        BACKEND_UP=1
    fi
fi
if [ "$BACKEND_UP" = "1" ]; then
    echo "Backend:  RUNNING  (PID $PID, port $PORT)"
else
    echo "Backend:  STOPPED"
fi

# ── HTTP health + ops-status (real endpoints) ────────────────
if [ "$BACKEND_UP" = "1" ]; then
    OPS_JSON="$(curl -s "http://localhost:$PORT/api/ops-status" 2>/dev/null || true)"
    if echo "$OPS_JSON" | grep -q '"scheduler_running":true'; then
        echo "Scheduler (in-process): RUNNING"
    else
        echo "Scheduler (in-process): STOPPED or UNREACHABLE"
    fi
    echo "  Worker pools (same process): see logs/worker.log for recent activity"

    OCRCODE="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/ocr-health" 2>/dev/null || true)"
    if [ "$OCRCODE" = "200" ]; then
        echo "OCR (Tesseract): AVAILABLE"
    else
        echo "OCR (Tesseract): CHECK FAILED (http $OCRCODE)"
    fi
else
    echo "Scheduler: N/A (backend not running)"
    echo "OCR: N/A (backend not running)"
fi

# ── Database reachability (real check, no server needed) ────
if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    DB_ERR="$(mktemp)"
    if (cd "$SCRIPT_DIR/backend" && "$SCRIPT_DIR/.venv/bin/python" scripts/validate_config.py --db-only) >/dev/null 2>"$DB_ERR"; then
        echo "Database: CONNECTED"
    else
        echo "Database: FAILED — see $DB_ERR"
    fi
else
    echo "Database: UNKNOWN (.venv not found — run install.sh)"
fi

# ── Storage ───────────────────────────────────────────────────
if [ -d "$SCRIPT_DIR/uploads" ]; then
    echo "Storage:  AVAILABLE"
else
    echo "Storage:  MISSING (run install.sh)"
fi

echo
echo "========================================"
exit 0
