#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "[ERROR] Python 3 is required but was not found on PATH."
    echo "Please install Python 3 and create a venv with: python3 -m venv .venv"
    exit 1
fi

echo ""
echo "  ============================================="
echo "   Data Intelligence Platform"
echo "   Starting backend and frontend servers..."
echo "  ============================================="
echo ""

echo "[INFO] Using Python: $PYTHON_BIN"

# Ensure .venv exists and install backend dependencies each launch to avoid stale/missing packages.
if [ ! -d "$SCRIPT_DIR/.venv" ]; then
    echo "[INFO] .venv not found. Creating virtual environment..."
    "$PYTHON_BIN" -m venv "$SCRIPT_DIR/.venv"
fi

if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    . "$SCRIPT_DIR/.venv/bin/activate"
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
    echo "[INFO] Activated Python virtual environment: $SCRIPT_DIR/.venv"

    BACKEND_REQ="$SCRIPT_DIR/backend/requirements.txt"
    ROOT_REQ="$SCRIPT_DIR/requirements.txt"
    if [ -f "$BACKEND_REQ" ]; then
        echo "[INFO] Ensuring backend dependencies are installed..."
        "$SCRIPT_DIR/.venv/bin/pip" install -r "$BACKEND_REQ" >/dev/null 2>&1 || {
            echo "[WARN] backend dependencies install encountered issues; retrying with output..."
            "$SCRIPT_DIR/.venv/bin/pip" install -r "$BACKEND_REQ"
        }
    elif [ -f "$ROOT_REQ" ]; then
        echo "[INFO] Ensuring root dependencies are installed..."
        "$SCRIPT_DIR/.venv/bin/pip" install -r "$ROOT_REQ" >/dev/null 2>&1 || {
            echo "[WARN] root dependencies install encountered issues; retrying with output..."
            "$SCRIPT_DIR/.venv/bin/pip" install -r "$ROOT_REQ"
        }
    fi
else
    echo "[WARN] Failed to activate .venv; continuing with system Python: $PYTHON_BIN"
fi

if [ -d "$SCRIPT_DIR/backend" ]; then
    echo "[Backend] Starting FastAPI on port 8080..."
    (
        cd "$SCRIPT_DIR/backend"
        "$PYTHON_BIN" -m uvicorn app.main:app --reload --port 8080
    ) &
else
    echo "[ERROR] backend folder not found: $SCRIPT_DIR/backend"
    exit 1
fi

if [ -d "$SCRIPT_DIR/frontend" ]; then
    echo "[Frontend] Starting Vite dev server on port 5173..."
    (
        cd "$SCRIPT_DIR/frontend"
        if [ ! -d node_modules ] || [ ! -d node_modules/@rollup ] || [ ! -f node_modules/@rollup/rollup-darwin-arm64/package.json ]; then
            echo "[WARN] Frontend dependencies are missing or stale. Reinstalling npm packages..."
            rm -rf node_modules package-lock.json
            npm install
        fi
        npm run dev
    ) &
else
    echo "[ERROR] frontend folder not found: $SCRIPT_DIR/frontend"
    exit 1
fi

echo ""
echo "Backend: http://localhost:8080"
echo "Frontend: http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop both servers."
echo ""

wait
