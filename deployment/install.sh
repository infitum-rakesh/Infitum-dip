#!/usr/bin/env bash
# ============================================================
#  Data Intelligence Platform — Linux Server Installer
#  Lives at the root of the DataIntelligencePlatform/ package,
#  as a sibling of backend/, frontend/, logs/, run/, uploads/.
#  Resolves everything from SCRIPT_DIR — never depends on the
#  caller's current working directory, and never hardcodes a
#  developer-specific path. Mirrors install.bat step-for-step;
#  see that file's comments for the Windows equivalent of each step.
# ============================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Portable y/n prompt check — deliberately NOT "${VAR,,}" (bash 4+ only;
# some minimal/older distros still ship bash 3.2, where that is a syntax
# error at runtime: "bad substitution").
is_yes() { case "$1" in [Yy]*) return 0 ;; *) return 1 ;; esac; }
is_no() { case "$1" in [Nn]*) return 0 ;; *) return 1 ;; esac; }

fail() {
    echo
    echo "========================================"
    echo " INSTALLATION FAILED"
    echo "========================================"
    echo " Fix the error reported above, then rerun install.sh."
    echo " It is safe to rerun — completed steps are skipped or are"
    echo " idempotent, so nothing will be duplicated."
    echo "========================================"
    exit 1
}

echo
echo "========================================"
echo " Data Intelligence Platform Installation"
echo "========================================"
echo

# ── Step 1: OS sanity check ─────────────────────────────────
echo "[1/16] Checking operating system..."
if [ -f /etc/os-release ]; then
    . /etc/os-release
    echo "  Detected: ${PRETTY_NAME:-unknown Linux}"
else
    echo "  WARNING: could not read /etc/os-release — continuing anyway (advisory check only)."
fi

# ── Step 2: Python ───────────────────────────────────────────
echo "[2/16] Checking Python installation..."
PYTHON_BIN=""
for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$candidate"
        break
    fi
done
if [ -z "$PYTHON_BIN" ]; then
    echo "  ERROR: Python 3 was not found on PATH."
    echo "  Install Python 3.11 or 3.12 (e.g. 'sudo apt-get install python3.12 python3.12-venv')."
    fail
fi
echo "  Found $("$PYTHON_BIN" --version 2>&1) at $(command -v "$PYTHON_BIN")"

# ── Step 3: Node.js / npm (only needed if frontend/dist isn't
#            already shipped — see Step 7+8 below) ─────────────
echo "[3/16] Checking Node.js / npm..."
SKIP_FRONTEND_BUILD=0
if [ -f "$SCRIPT_DIR/frontend/dist/index.html" ]; then
    echo "  frontend/dist already present (pre-built) — Node.js/npm are not required on this server."
    SKIP_FRONTEND_BUILD=1
else
    if ! command -v node >/dev/null 2>&1; then
        echo "  ERROR: Node.js was not found on PATH, and no pre-built frontend/dist was shipped with this package."
        echo "  Either install Node.js 20 LTS (e.g. via nodesource) and rerun install.sh,"
        echo "  or build the frontend on a machine that has Node.js and copy frontend/dist here first"
        echo "  (see README_DEPLOYMENT.md's \"Deploying without Node.js\" section)."
        fail
    fi
    if ! command -v npm >/dev/null 2>&1; then
        echo "  ERROR: npm was not found on PATH (usually installed alongside Node.js)."
        fail
    fi
    echo "  Found Node.js $(node --version)"
fi

# ── Step 4: required system dependencies ────────────────────
echo "[4/16] Checking required system dependencies..."
echo "  (Tesseract OCR is checked later, after Python deps are installed"
echo "   and validate_config.py can actually import pytesseract. On most"
echo "   distros: 'sudo apt-get install tesseract-ocr tesseract-ocr-eng'.)"

# ── Step 5: Python virtual environment ──────────────────────
echo "[5/16] Creating Python virtual environment..."
if [ -f "$SCRIPT_DIR/.venv/bin/python" ]; then
    echo "  .venv already exists, skipping."
else
    if ! "$PYTHON_BIN" -m venv "$SCRIPT_DIR/.venv"; then
        echo "  ERROR: failed to create the virtual environment."
        fail
    fi
fi
VENV_PY="$SCRIPT_DIR/.venv/bin/python"
VENV_PIP="$SCRIPT_DIR/.venv/bin/pip"

# ── Step 6: backend Python dependencies ──────────────────────
echo "[6/16] Installing backend Python dependencies..."
"$VENV_PY" -m pip install --upgrade pip
if ! "$VENV_PIP" install -r "$SCRIPT_DIR/backend/requirements.txt"; then
    echo "  ERROR: pip install failed. See the output above."
    fail
fi

# ── Step 7+8: frontend dependencies + production build ──────
# Skipped entirely when frontend/dist was already shipped pre-built
# (SKIP_FRONTEND_BUILD=1, set in Step 3) — that is the normal path for
# a packaged release deployed to a server with no Node.js on it at all.
if [ "$SKIP_FRONTEND_BUILD" = "1" ]; then
    echo "[7/16] Installing frontend dependencies..."
    echo "  Skipped — using the pre-built frontend/dist shipped with this package."
    echo "[8/16] Building frontend production bundle..."
    echo "  Skipped — frontend/dist/index.html already exists."
else
    echo "[7/16] Installing frontend dependencies..."
    if [ -f "$SCRIPT_DIR/frontend/package-lock.json" ]; then
        (cd "$SCRIPT_DIR/frontend" && npm ci)
    else
        (cd "$SCRIPT_DIR/frontend" && npm install)
    fi
    if [ $? -ne 0 ]; then
        echo "  ERROR: npm install failed. See the output above."
        fail
    fi

    echo "[8/16] Building frontend production bundle..."
    if ! (cd "$SCRIPT_DIR/frontend" && npm run build); then
        echo "  ERROR: frontend build failed. See the output above."
        fail
    fi
    if [ ! -f "$SCRIPT_DIR/frontend/dist/index.html" ]; then
        echo "  ERROR: build finished but frontend/dist/index.html is missing."
        fail
    fi
    echo "  OK — frontend/dist is ready (the backend serves it directly)."
fi

# ── Step 9: required directories ─────────────────────────────
echo "[9/16] Creating required directories..."
mkdir -p "$SCRIPT_DIR/logs" "$SCRIPT_DIR/run" "$SCRIPT_DIR/uploads"
echo "  OK"

# ── Step 10: environment configuration ───────────────────────
echo "[10/16] Configuring environment..."
SKIP_ENV_WIZARD=0
if [ -f "$SCRIPT_DIR/backend/.env" ]; then
    echo "  backend/.env already exists."
    read -r -p "  Reconfigure it now? [y/N]: " RECONFIGURE
    if ! is_yes "$RECONFIGURE"; then
        SKIP_ENV_WIZARD=1
    fi
fi

if [ "$SKIP_ENV_WIZARD" = "0" ]; then
    echo
    echo "  ---- Database configuration ----"
    echo "  [1] SQLite     (default — good for evaluation / small deployments)"
    echo "  [2] PostgreSQL (recommended for production)"
    echo "  [3] MySQL"
    echo "  [4] SQL Server (NOT supported as the platform database in this"
    echo "                   build — no async driver installed. Offered only"
    echo "                   for Bring-Your-Own-Database tenant targets you"
    echo "                   configure LATER inside the app, not this installer.)"
    read -r -p "  Select database type [1]: " DBCHOICE
    DBCHOICE="${DBCHOICE:-1}"

    if [ "$DBCHOICE" = "4" ]; then
        echo "  SQL Server cannot be used as the platform database. Defaulting to SQLite."
        DBCHOICE=1
    fi

    if [ "$DBCHOICE" = "1" ]; then
        DATABASE_URL="sqlite+aiosqlite:///./dip.db"
        echo
        read -r -p "  No database details given — use SQLite (can migrate to a real DB later)? [Y/n]: " CONFIRM_SQLITE
        if is_no "$CONFIRM_SQLITE"; then
            echo "  Rerun install.sh and choose PostgreSQL or MySQL instead."
            fail
        fi
    else
        read -r -p "  Host: " DBHOST
        read -r -p "  Port: " DBPORT
        read -r -p "  Database name: " DBNAME
        read -r -p "  Username: " DBUSER
        read -r -s -p "  Password: " DBPASS
        echo
        read -r -p "  Use SSL? [y/N]: " DBSSL

        if [ "$DBCHOICE" = "2" ]; then DBDRIVER="postgresql+asyncpg"; fi
        if [ "$DBCHOICE" = "3" ]; then DBDRIVER="mysql+aiomysql"; fi
        DATABASE_URL="${DBDRIVER}://${DBUSER}:${DBPASS}@${DBHOST}:${DBPORT}/${DBNAME}"
        if is_yes "$DBSSL"; then DATABASE_URL="${DATABASE_URL}?ssl=true"; fi
    fi

    echo
    echo "  ---- Security configuration ----"
    SECRET_KEY="$("$VENV_PY" -c 'import secrets; print(secrets.token_hex(32))')"
    echo "  Generated a random SECRET_KEY."

    echo
    echo "  ---- Application configuration ----"
    read -r -p "  Public base URL this server will be reached at (e.g. https://dip.yourcompany.com) [http://localhost:8080]: " PUBLIC_URL
    PUBLIC_URL="${PUBLIC_URL:-http://localhost:8080}"
    read -r -p "  uvicorn worker processes [1 — recommended, see README_DEPLOYMENT.md]: " WORKERCOUNT
    WORKERCOUNT="${WORKERCOUNT:-1}"
    if [ "$WORKERCOUNT" != "1" ]; then
        echo "  WARNING: more than 1 worker means SQL Workspace query cancellation"
        echo "  and other in-memory task state will NOT be shared across workers."
        read -r -p "  Proceed with $WORKERCOUNT workers anyway? [y/N]: " WORKERCONFIRM
        if ! is_yes "$WORKERCONFIRM"; then WORKERCOUNT=1; fi
    fi

    cat > "$SCRIPT_DIR/backend/.env" <<EOF
APP_ENV=production
HOST=0.0.0.0
PORT=8080
DATABASE_URL=${DATABASE_URL}
SECRET_KEY=${SECRET_KEY}
FRONTEND_BASE_URL=${PUBLIC_URL}
BACKEND_PUBLIC_BASE_URL=${PUBLIC_URL}
ALLOWED_ORIGINS=${PUBLIC_URL}
WORKER_COUNT=${WORKERCOUNT}
UPLOAD_STORAGE_PATH=./uploads
MAX_UPLOAD_SIZE_MB=25
LOG_MAX_BYTES=10000000
LOG_BACKUP_COUNT=5
EOF
    echo "  Wrote backend/.env"
fi

# ── Step 11: validate configuration ──────────────────────────
echo "[11/16] Validating configuration..."
if ! (cd "$SCRIPT_DIR/backend" && "$VENV_PY" scripts/validate_config.py); then
    echo "  ERROR: configuration is invalid. Fix the FAILED item(s) above and rerun install.sh."
    fail
fi

# ── Step 12: run database migrations / create tables ────────
echo "[12/16] Running database migrations..."
if ! (cd "$SCRIPT_DIR/backend" && "$VENV_PY" scripts/run_all_migrations.py); then
    echo "  ERROR: migration run failed. See the output above."
    echo "  Fix the issue and rerun install.sh — it is safe to rerun"
    echo "  (every migration step is idempotent)."
    fail
fi

# ── Step 14: create initial Seller Admin account ─────────────
echo "[14/16] Creating initial Seller Admin account..."
echo "  (A default seller login already exists automatically —"
echo "   seller@platform-admin.com — see README_DEPLOYMENT.md."
echo "   This step lets you set your OWN email/password instead.)"
read -r -p "  Create/reset a Seller Admin with your own credentials now? [Y/n]: " MAKE_SELLER
if ! is_no "$MAKE_SELLER"; then
    if ! (cd "$SCRIPT_DIR/backend" && "$VENV_PY" scripts/create_seller_account.py); then
        echo "  WARNING: Seller Admin creation did not complete. You can rerun"
        echo "  this later with: .venv/bin/python backend/scripts/create_seller_account.py"
    fi
else
    echo "  Skipped — the auto-seeded default Seller Admin account will be used."
fi

# ── Step 15: health checks ────────────────────────────────────
echo "[15/16] Performing health checks..."
"$SCRIPT_DIR/start.sh"
echo "  Waiting for the backend to come up..."
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
    echo "  ERROR: backend did not report healthy within 30 seconds."
    echo "  Check logs/backend.log for details."
    fail
fi
echo "  OK — backend is healthy."

# ── Step 16: installation summary ─────────────────────────────
echo "[16/16] Installation summary"
echo
echo "========================================"
echo " Installation completed successfully."
echo "========================================"
echo
echo " Application: http://localhost:8080"
echo " Backend:     http://localhost:8080/api"
echo " Health:      http://localhost:8080/api/health"
echo " Database:    CONNECTED"
echo " Storage:     READY"
echo " Configuration: backend/.env"
echo
echo " Next steps:"
echo "  1. Application is already running (started during health check)."
echo "  2. Open http://localhost:8080 in a browser."
echo "  3. Log in as Seller Admin."
echo "  4. Configure connections."
echo "  5. Configure pipelines."
echo "  6. Upload documents."
echo "  7. Start processing."
echo
echo " Use start.sh / stop.sh / status.sh to manage the service."
exit 0
