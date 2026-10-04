#!/usr/bin/env bash
# ============================================================
#  Data Intelligence Platform — upgrade.sh
#  Never overwrites backend/.env or any production data.
#  Expects the new package's backend/app, backend/scripts, and
#  frontend/dist to be copied over this installation's equivalents
#  (via rsync --delete, mirroring robocopy /MIR's exact-mirror
#  semantics), leaving .env/dip.db/uploads/logs untouched — this
#  matches how the deployment package is redistributed (see
#  README_DEPLOYMENT.md "Upgrade").
# ============================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

fail() {
    exit 1
}

echo "========================================"
echo " Data Intelligence Platform — Upgrade"
echo "========================================"
echo

if [ $# -lt 1 ]; then
    echo "Usage: upgrade.sh <path-to-extracted-new-package>"
    echo "  e.g. upgrade.sh /opt/downloads/DataIntelligencePlatform-2.1"
    fail
fi
NEWPKG="$1"
if [ ! -d "$NEWPKG/backend/app" ]; then
    echo "ERROR: $NEWPKG/backend/app not found — is this a valid package folder?"
    fail
fi
if ! command -v rsync >/dev/null 2>&1; then
    echo "ERROR: rsync is required for an exact-mirror file update but was not found on PATH."
    echo "Install it (e.g. 'sudo apt-get install rsync') and rerun upgrade.sh."
    fail
fi

# ── Step 1: backup configuration ─────────────────────────────
echo "[1/8] Backing up configuration..."
mkdir -p "$SCRIPT_DIR/backup"
TS="$(date +%Y%m%d_%H%M%S)"
cp "$SCRIPT_DIR/backend/.env" "$SCRIPT_DIR/backup/.env.$TS.bak"
echo "  Saved backup/.env.$TS.bak"

# ── Step 2: backup/verify database ───────────────────────────
echo "[2/8] Backing up database (if SQLite)..."
if [ -f "$SCRIPT_DIR/backend/dip.db" ]; then
    cp "$SCRIPT_DIR/backend/dip.db" "$SCRIPT_DIR/backup/dip.db.$TS.bak"
    echo "  Saved backup/dip.db.$TS.bak"
else
    echo "  No local dip.db (likely using an external Postgres/MySQL database — back that up separately)."
fi

# ── Step 3: stop application ──────────────────────────────────
echo "[3/8] Stopping application..."
if [ -f "$SCRIPT_DIR/run/backend.pid" ]; then
    PID="$(cat "$SCRIPT_DIR/run/backend.pid")"
    kill -TERM "$PID" 2>/dev/null
    sleep 3
    kill -9 "$PID" 2>/dev/null
    rm -f "$SCRIPT_DIR/run/backend.pid"
fi
echo "  Stopped."

# ── Step 4: update application files ─────────────────────────
echo "[4/8] Updating application files..."
rsync -a --delete "$NEWPKG/backend/app/" "$SCRIPT_DIR/backend/app/"
rsync -a --delete "$NEWPKG/backend/scripts/" "$SCRIPT_DIR/backend/scripts/"
if [ -d "$NEWPKG/frontend/dist" ]; then
    rsync -a --delete "$NEWPKG/frontend/dist/" "$SCRIPT_DIR/frontend/dist/"
fi
if [ -f "$NEWPKG/backend/requirements.txt" ]; then
    cp "$NEWPKG/backend/requirements.txt" "$SCRIPT_DIR/backend/requirements.txt"
fi
echo "  Updated backend/app, backend/scripts, frontend/dist, requirements.txt."
echo "  backend/.env, dip.db, uploads/, and logs/ were left untouched."

# ── Step 5: install dependency changes ───────────────────────
echo "[5/8] Installing dependency changes..."
if ! "$SCRIPT_DIR/.venv/bin/pip" install -r "$SCRIPT_DIR/backend/requirements.txt"; then
    echo "  ERROR: pip install failed. See the output above."
    fail
fi

# ── Step 6: run migrations ────────────────────────────────────
echo "[6/8] Running database migrations..."
if ! (cd "$SCRIPT_DIR/backend" && "$SCRIPT_DIR/.venv/bin/python" scripts/run_all_migrations.py); then
    echo "  ERROR: migration run failed. Your pre-upgrade backups are in backup/$TS*."
    echo "  See the output above, fix the issue, and rerun upgrade.sh."
    fail
fi

# ── Step 7: start application ──────────────────────────────────
echo "[7/8] Starting application..."
"$SCRIPT_DIR/start.sh"

# ── Step 8: health check ──────────────────────────────────────
echo "[8/8] Health check..."
HEALTH_OK=0
for _ in $(seq 1 15); do
    HCODE="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/api/health 2>/dev/null || true)"
    if [ "$HCODE" = "200" ]; then
        HEALTH_OK=1
        break
    fi
    sleep 2
done

if [ "$HEALTH_OK" = "0" ]; then
    echo "  ERROR: backend did not report healthy after upgrade. Check logs/backend.log."
    echo "  Backups are available in backup/$TS* if you need to roll back."
    fail
fi

echo
echo "========================================"
echo " Upgrade completed successfully."
echo "========================================"
exit 0
