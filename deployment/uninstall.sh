#!/usr/bin/env bash
# ============================================================
#  Data Intelligence Platform — uninstall.sh
#  Default is to KEEP data (dip.db / uploads / logs) — deleting
#  a production database/document store from an uninstaller by
#  default is exactly the kind of silent destructive action this
#  project avoids. Deletion only happens if explicitly confirmed.
# ============================================================
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Portable y/n prompt check — deliberately NOT "${VAR,,}" (bash 4+ only;
# some minimal/older distros still ship bash 3.2, where that is a syntax
# error at runtime: "bad substitution").
is_yes() { case "$1" in [Yy]*) return 0 ;; *) return 1 ;; esac; }
is_no() { case "$1" in [Nn]*) return 0 ;; *) return 1 ;; esac; }

echo "========================================"
echo " Data Intelligence Platform — Uninstall"
echo "========================================"
echo

if [ -f "$SCRIPT_DIR/run/backend.pid" ]; then
    PID="$(cat "$SCRIPT_DIR/run/backend.pid")"
    kill -TERM "$PID" 2>/dev/null
    sleep 1
    kill -9 "$PID" 2>/dev/null
    rm -f "$SCRIPT_DIR/run/backend.pid"
    echo "Stopped running backend."
fi

echo
read -r -p "Delete the database, uploaded documents, and logs too? [y/N] (default keeps them): " DELDATA
if is_yes "$DELDATA"; then
    read -r -p "Type YES to permanently delete backend/dip.db, uploads/, and logs/: " CONFIRM
    if [ "$CONFIRM" = "YES" ]; then
        rm -f "$SCRIPT_DIR/backend/dip.db"
        rm -rf "$SCRIPT_DIR/uploads"
        rm -rf "$SCRIPT_DIR/logs"
        echo "Data deleted."
    else
        echo "Confirmation did not match \"YES\" — data was NOT deleted."
    fi
else
    echo "Keeping database, uploads, and logs. Back them up separately if you"
    echo "intend to reinstall on a different machine — see README_DEPLOYMENT.md"
    echo "\"Backup\" section."
fi

echo
read -r -p "Remove the Python virtual environment (.venv)? [Y/n]: " DELVENV
if ! is_no "$DELVENV"; then
    rm -rf "$SCRIPT_DIR/.venv"
    echo "Removed .venv."
fi

rm -rf "$SCRIPT_DIR/run"

echo
echo "Uninstall complete. The application folder itself was not deleted —"
echo "remove it manually once you've confirmed any data you wanted is backed up."
exit 0
