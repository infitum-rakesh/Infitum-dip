"""
Scheduled Document Intelligence Workflow — migration.

Run once, backend STOPPED, before restarting on the new models:
    cd backend && python3 scripts/migrate_document_schedules.py

1. documents — add nullable source_connection_id/source_path/
   source_schedule_id columns (safe in-place ALTER — every existing
   manual/bulk-uploaded document just gets NULLs, meaning "not
   schedule-sourced", which is exactly correct).
2. schedule_runs — add nullable document_stats (JSON, stored as TEXT in
   SQLite) and cancel_requested (safe in-place ALTER; the RunStatus enum
   gaining COMPLETED_WITH_ERRORS/CANCELLED members needs no migration —
   SQLite's `status` column is a bare VARCHAR with no CHECK constraint,
   same as every other enum column in this codebase, per the SB1
   migration's own note).
3. document_schedule_configs / document_source_registry — brand-new
   tables, created for free by init_db()'s create_all() on next backend
   startup as long as their model module is imported somewhere main.py
   reaches before init_db() runs (added to main.py's model-import block).
   Nothing to do here for these two.

Idempotent: safe to re-run (skips any column already migrated).
"""
import sqlite3

DB_PATH = "dip.db"


def column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def add_column(cur, table, column, ddl_type):
    if column_exists(cur, table, column):
        print(f"{table}.{column} already exists, skipping.")
        return
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
    print(f"{table}: added {column}")


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    add_column(cur, "documents", "source_connection_id", "INTEGER")
    add_column(cur, "documents", "source_path", "VARCHAR(1000)")
    add_column(cur, "documents", "source_schedule_id", "INTEGER")

    add_column(cur, "schedule_runs", "document_stats", "JSON")
    add_column(cur, "schedule_runs", "cancel_requested", "BOOLEAN DEFAULT 0")

    con.commit()
    con.close()
    print("Done. document_schedule_configs / document_source_registry will be "
          "created automatically by init_db() on next backend startup.")


if __name__ == "__main__":
    main()
