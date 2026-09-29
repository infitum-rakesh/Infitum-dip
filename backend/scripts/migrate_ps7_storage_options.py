"""
PS7 migration — add processed_data_storage_configs.storage_type,
load_mode, file_format, pending_reset (Part 5 of the Document Data
Storage Configuration spec: Load Modes + File Format options).

processed_data_storage_configs already exists with live rows (from the
original PS1-PS6 phase), so these new columns don't materialize via
create_all() like a brand-new table would — same in-place ALTER pattern
as migrate_sb8.py/migrate_doc_language_config_20260913.py. Defaults
match the SQLAlchemy model's column defaults so existing rows behave
exactly as before this migration (DATABASE/CREATE_IF_NOT_EXISTS/JSONL,
pending_reset off).

Idempotent.
"""
import sqlite3

DB_PATH = "dip.db"


def column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    table = "processed_data_storage_configs"

    additions = [
        ("storage_type", "TEXT NOT NULL DEFAULT 'DATABASE'"),
        ("load_mode", "TEXT NOT NULL DEFAULT 'CREATE_IF_NOT_EXISTS'"),
        ("file_format", "TEXT NOT NULL DEFAULT 'JSONL'"),
        ("pending_reset", "BOOLEAN NOT NULL DEFAULT 0"),
    ]
    for column, ddl in additions:
        if column_exists(cur, table, column):
            print(f"{table}.{column} already exists, skipping.")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            print(f"{table}: added {column}")
    con.commit()
    con.close()


if __name__ == "__main__":
    main()
