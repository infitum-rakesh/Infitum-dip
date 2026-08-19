#!/usr/bin/env bash
# Data Intelligence Platform — Seller Portal launcher (macOS)
# Double-click this file in Finder to start both servers (if not already
# running) and open the Seller screen directly in your browser.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  ============================================="
echo "   Data Intelligence Platform — Seller Portal"
echo "  ============================================="
echo ""

# ── Python (prefer the project .venv, matching start.sh) ─────────
if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
else
    echo "[ERROR] Python 3 is required but was not found on PATH."
    echo "Create a venv with: python3 -m venv .venv"
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
fi
echo "[INFO] Using Python: $PYTHON_BIN"

# ── Backend on port 8080 (matches frontend/vite.config.js's proxy target) ──
if lsof -nP -iTCP:8080 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[Backend] Already running on port 8080 — reusing it."
else
    echo "[Backend] Starting FastAPI on port 8080..."
    (
        cd "$SCRIPT_DIR/backend"
        "$PYTHON_BIN" -m uvicorn app.main:app --reload --port 8080 > /tmp/dip_seller_backend.log 2>&1 &
    )
    sleep 2
fi

# ── Frontend on port 5173 ─────────────────────────────────────────
if lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1; then
    echo "[Frontend] Already running on port 5173 — reusing it."
else
    echo "[Frontend] Starting Vite dev server on port 5173..."
    (
        cd "$SCRIPT_DIR/frontend"
        npm run dev > /tmp/dip_seller_frontend.log 2>&1 &
    )
    echo "[Frontend] Waiting for it to come up..."
    for i in $(seq 1 20); do
        if lsof -nP -iTCP:5173 -sTCP:LISTEN >/dev/null 2>&1; then break; fi
        sleep 0.5
    done
fi

echo ""
echo "  Backend:       http://localhost:8080"
echo "  API Docs:      http://localhost:8080/api/docs"
echo "  Frontend:      http://localhost:5173"
echo "  Seller Portal: http://localhost:5173/app/seller"
echo ""
echo "  Log in with a Seller-role account, e.g.:"
echo "    seller@platform-admin.com / Seller@1234!"
echo ""

# ── Open the Seller screen directly in the default browser ────────
open "http://localhost:5173/app/seller"

echo "  Opened the Seller screen in your browser."
echo "  You can close this window — the servers keep running in the"
echo "  background (check /tmp/dip_seller_backend.log and"
echo "  /tmp/dip_seller_frontend.log if something looks wrong)."
echo ""
read -n 1 -s -r -p "Press any key to close this window..."
echo ""
