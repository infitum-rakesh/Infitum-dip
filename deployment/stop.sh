#!/usr/bin/env bash
# ============================================================
#  Data Intelligence Platform — stop.sh
#  Targets the exact PID start.sh recorded — never a generic
#  process-name match that could kill an unrelated process.
#
#  Graceful shutdown order (mirrors stop.bat): this process embeds
#  the scheduler and worker pools (see README_DEPLOYMENT.md), so a
#  plain SIGTERM already runs FastAPI's lifespan shutdown, which
#  calls scheduler.shutdown(wait=False) — stopping new scheduler
#  ticks — before the process exits. We send SIGTERM first and only
#  escalate to SIGKILL if it doesn't exit in time.
# ============================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PIDFILE="$SCRIPT_DIR/run/backend.pid"

if [ ! -f "$PIDFILE" ]; then
    echo "No run/backend.pid found — nothing appears to be running (or it was started outside start.sh)."
    exit 0
fi

PID="$(cat "$PIDFILE")"
if ! kill -0 "$PID" 2>/dev/null; then
    echo "Recorded PID $PID is not running. Removing stale run/backend.pid."
    rm -f "$PIDFILE"
    exit 0
fi

echo "Stopping backend (PID $PID)..."
kill -TERM "$PID" 2>/dev/null

STOPPED=0
for _ in $(seq 1 10); do
    if ! kill -0 "$PID" 2>/dev/null; then
        STOPPED=1
        break
    fi
    sleep 1
done

if [ "$STOPPED" = "0" ]; then
    echo "Process did not exit gracefully within 10 seconds — forcing termination."
    kill -9 "$PID" 2>/dev/null
fi

rm -f "$PIDFILE"
echo "Backend stopped."
exit 0
