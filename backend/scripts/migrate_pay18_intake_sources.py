"""
PAY18 — non-file (DATABASE query) invoice intake sources.

Run once, backend STOPPED, before restarting on the new model columns:
    cd backend && python3 scripts/migrate_pay18_intake_sources.py

document_schedule_configs — add nullable source_kind (defaults existing
rows to "FILE", byte-identical behavior for every schedule created before
this existed), source_query, source_record_key_column.

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

    add_column(cur, "document_schedule_configs", "source_kind", "VARCHAR(20) DEFAULT 'FILE'")
    add_column(cur, "document_schedule_configs", "source_query", "TEXT")
    add_column(cur, "document_schedule_configs", "source_record_key_column", "VARCHAR(200)")

    # Backfill existing NULLs from the ALTER's own DEFAULT — SQLite applies
    # a column DEFAULT only to rows inserted AFTER the ALTER, not rows that
    # already existed, so every pre-existing schedule needs this explicit
    # pass to genuinely read "FILE" rather than NULL.
    cur.execute("UPDATE document_schedule_configs SET source_kind = 'FILE' WHERE source_kind IS NULL")

    con.commit()
    con.close()
    print("Done.")


if __name__ == "__main__":
    main()
