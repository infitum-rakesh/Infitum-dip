"""
PAY20 — API/webhook intake source kind. Idempotent ALTER TABLE script for
the new source_api_* columns on document_schedule_configs (all nullable/
defaulted — existing FILE/DATABASE-kind rows are completely unaffected).

Run once against the SQLite DB, then restart the backend:
    cd backend && python scripts/migrate_pay20_api_intake_source.py
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
    add_column(cur, "document_schedule_configs", "source_api_endpoint", "VARCHAR(500)")
    add_column(cur, "document_schedule_configs", "source_api_method", "VARCHAR(10) DEFAULT 'GET'")
    add_column(cur, "document_schedule_configs", "source_api_query_params", "JSON")
    add_column(cur, "document_schedule_configs", "source_api_pagination", "JSON")
    add_column(cur, "document_schedule_configs", "source_api_json_path", "VARCHAR(200)")
    cur.execute("UPDATE document_schedule_configs SET source_api_method = 'GET' WHERE source_api_method IS NULL")
    con.commit()
    con.close()
    print("Done.")


if __name__ == "__main__":
    main()
