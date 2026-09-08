"""
PAY21 — ERP intake source kind. Idempotent ALTER TABLE script for the one
new source_erp_query column on document_schedule_configs (nullable —
existing FILE/DATABASE/API-kind rows are completely unaffected).

Run once against the SQLite DB, then restart the backend:
    cd backend && python scripts/migrate_pay21_erp_intake_source.py
"""
import sqlite3

DB_PATH = "dip.db"


def column_exists(cur, table, column) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def add_column(cur, table, column, ddl_type):
    if not column_exists(cur, table, column):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
        print(f"Added {table}.{column}")
    else:
        print(f"{table}.{column} already exists — skipping")


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    add_column(cur, "document_schedule_configs", "source_erp_query", "TEXT")
    con.commit()
    con.close()
    print("Done.")


if __name__ == "__main__":
    main()
