"""
Run every backend/scripts/migrate_*.py script's main() in one pass — the
step install.bat/install.sh and upgrade.bat/upgrade.sh both already
reference but that never actually existed in the repo.

Every migrate_*.py script here is hardcoded to `sqlite3.connect("dip.db")`
(see e.g. migrate_tally_j.py) — none of them are Postgres/MySQL-aware.
That's fine for a brand-new database: app.database.connection.init_db()
runs SQLAlchemy's create_all() on every backend startup, which builds
every table with its FULLY CURRENT schema regardless of DB engine — the
migrate_*.py scripts exist only to retrofit ALTER-COLUMN-style changes
onto a SQLite database that predates a specific feature. So:

  - DATABASE_URL is sqlite   -> actually run every migrate_*.py main()
  - DATABASE_URL is anything else (Postgres/MySQL) -> print an honest
    disclosure and exit 0. A fresh Postgres/MySQL database already gets
    the full current schema from init_db()'s create_all() on first
    startup; an EXISTING Postgres/MySQL database that predates a feature
    has no automated incremental-migration path yet. This is a real,
    disclosed gap, not silently faked success.

Each script is independently idempotent (checks column/table existence
before altering, skips cleanly if the table doesn't exist yet), so run
order does not affect correctness — sorted alphabetically for a
deterministic, reproducible run. One script failing does not abort the
rest; every script still gets a chance to run, and failures are
summarized at the end.

Usage (run from backend/, same as every other script here):
    python scripts/run_all_migrations.py
"""
import importlib.util
import os
import sys
from pathlib import Path

# Same sys.path fixup as scripts/create_seller_account.py / validate_config.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402

SCRIPTS_DIR = Path(__file__).resolve().parent


def _discover_migration_scripts() -> list[Path]:
    return sorted(
        p for p in SCRIPTS_DIR.glob("migrate_*.py")
        if p.name != Path(__file__).name
    )


def _run_one(path: Path) -> tuple[bool, str]:
    module_name = f"_run_all_migrations__{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        if not hasattr(module, "main"):
            return False, f"{path.name}: has no main() — skipped."
        module.main()
        return True, f"{path.name}: OK"
    except Exception as exc:
        return False, f"{path.name}: FAILED — {exc}"


def main() -> int:
    settings = get_settings()

    if not settings.database_url.startswith("sqlite"):
        print(
            "run_all_migrations: DATABASE_URL is not SQLite "
            f"({settings.database_url.split('://')[0]}). Every migrate_*.py script in "
            "this directory is hardcoded to sqlite3 and cannot run against this database.\n"
            "A brand-new database already gets the fully current schema from "
            "init_db()'s create_all() on first backend startup — nothing to do here.\n"
            "An EXISTING Postgres/MySQL database that predates a specific feature has "
            "no automated incremental-migration path yet; this is a known, disclosed "
            "gap, not something this script can silently paper over."
        )
        return 0

    scripts = _discover_migration_scripts()
    if not scripts:
        print("run_all_migrations: no migrate_*.py scripts found — nothing to do.")
        return 0

    print(f"run_all_migrations: running {len(scripts)} migration script(s) against {settings.database_url}\n")
    failures = []
    for path in scripts:
        print(f"--- {path.name} ---")
        ok, message = _run_one(path)
        print(message)
        if not ok:
            failures.append(message)
        print()

    if failures:
        print(f"run_all_migrations: {len(failures)} of {len(scripts)} script(s) failed:")
        for message in failures:
            print(f"  - {message}")
        return 1

    print(f"run_all_migrations: all {len(scripts)} script(s) completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
