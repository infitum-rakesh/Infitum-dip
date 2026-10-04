#!/usr/bin/env bash
# ============================================================
#  Data Intelligence Platform — start.sh
#  Resolves everything from SCRIPT_DIR — never depends on the
#  caller's current working directory, never hardcodes a
#  developer-specific path.
#
#  One process serves the API, the built frontend, the embedded
#  scheduler, and the embedded async worker pools — see
#  README_DEPLOYMENT.md's "Architecture" section for why this
#  app doesn't run separate Backend/Worker/Scheduler processes.
# ============================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PIDFILE="$SCRIPT_DIR/run/backend.pid"
PORT=8080
WORKERCOUNT=1
if [ -f "$SCRIPT_DIR/backend/.env" ]; then
    ENV_PORT="$(grep -m1 '^PORT=' "$SCRIPT_DIR/backend/.env" | cut -d= -f2-)"
    [ -n "$ENV_PORT" ] && PORT="$ENV_PORT"
    ENV_WORKERS="$(grep -m1 '^WORKER_COUNT=' "$SCRIPT_DIR/backend/.env" | cut -d= -f2-)"
    [ -n "$ENV_WORKERS" ] && WORKERCOUNT="$ENV_WORKERS"
fi

# ── Already running? ──────────────────────────────────────────
if [ -f "$PIDFILE" ]; then
    OLDPID="$(cat "$PIDFILE")"
    if kill -0 "$OLDPID" 2>/dev/null; then
        echo "Backend is already running (PID $OLDPID). Use stop.sh first if you want to restart it."
        exit 0
    else
        rm -f "$PIDFILE"
    fi
fi

if [ ! -f "$SCRIPT_DIR/.venv/bin/uvicorn" ]; then
    echo "ERROR: .venv not found or incomplete. Run install.sh first."
    exit 1
fi

mkdir -p "$SCRIPT_DIR/logs" "$SCRIPT_DIR/run"

echo "Starting Data Intelligence Platform backend on port $PORT (workers=$WORKERCOUNT)..."

# Launch detached (nohup + disown) so it survives this shell exiting;
# app.main's own RotatingFileHandler already writes logs/backend.log,
# these redirects are a safety net for anything printed before that
# handler is attached (startup import errors, etc).
(
    cd "$SCRIPT_DIR/backend" || exit 1
    nohup "$SCRIPT_DIR/.venv/bin/uvicorn" app.main:app --host 0.0.0.0 --port "$PORT" --workers "$WORKERCOUNT" \
        >> "$SCRIPT_DIR/logs/uvicorn.out.log" 2>> "$SCRIPT_DIR/logs/uvicorn.err.log" &
    echo $! > "$PIDFILE"
)
disown 2>/dev/null || true

NEWPID="$(cat "$PIDFILE" 2>/dev/null || true)"
if [ -z "$NEWPID" ] || ! kill -0 "$NEWPID" 2>/dev/null; then
    echo "ERROR: failed to start the backend process."
    rm -f "$PIDFILE"
    exit 1
fi
echo "Backend started (PID $NEWPID)."
echo "  Application: http://localhost:$PORT"
echo "  Health:      http://localhost:$PORT/api/health"
echo "  Logs:        logs/backend.log (+ logs/scheduler.log / logs/worker.log)"
exit 0
