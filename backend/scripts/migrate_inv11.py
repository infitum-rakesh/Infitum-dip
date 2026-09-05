"""
INV11 — Scheduler integration (invoice-typed schedules) migration.

Run once, backend STOPPED, before restarting on the new columns:
    cd backend && python3 scripts/migrate_inv11.py

document_schedule_configs gains two new nullable/defaulted columns:
  - invoice_auto_post (BOOLEAN DEFAULT 0) — auto-post a STRAIGHT_THROUGH
    invoice decision straight to the ERP with no human step.
  - invoice_erp_connection_id (INTEGER, nullable) — which ERP connection
    to post through when invoice_auto_post is enabled. No FK, same
    loose-coupling convention as source_connection_id on this table.

document_type already exists on this table (added ahead of this phase,
during INV3) — nothing to do for it here.

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

    add_column(cur, "document_schedule_configs", "invoice_auto_post", "BOOLEAN DEFAULT 0")
    add_column(cur, "document_schedule_configs", "invoice_erp_connection_id", "INTEGER")

    con.commit()
    con.close()
    print("Done.")


if __name__ == "__main__":
    main()
