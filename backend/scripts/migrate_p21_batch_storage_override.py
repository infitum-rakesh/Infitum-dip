"""
P21 migration — add document_batches.storage_type / storage_target_connection_id
/ storage_target_schema / storage_target_table / storage_load_mode /
storage_file_format (Part 21 of the spec: "DATA STORAGE FOR ZIP PROCESSING" —
per-batch override of the org-wide Processed Data Storage destination).

document_batches already exists with live rows (from the ZB1-ZB12 ZIP Batch
Upload feature), so these new columns don't materialize via create_all()
like a brand-new table would — same in-place ALTER pattern as
migrate_ps7_storage_options.py. All six columns are nullable with NO default
(NULL means "no override — inherit the org-wide config exactly as before
this migration"), so every existing DocumentBatch row is completely
unaffected.

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
    table = "document_batches"

    additions = [
        ("storage_type", "TEXT"),
        ("storage_target_connection_id", "INTEGER"),
        ("storage_target_schema", "TEXT"),
        ("storage_target_table", "TEXT"),
        ("storage_load_mode", "TEXT"),
        ("storage_file_format", "TEXT"),
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
