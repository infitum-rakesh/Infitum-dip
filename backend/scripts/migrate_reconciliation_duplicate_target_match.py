"""
Adds the target-side duplicate-key detection column (see
models/reconciliation.py):

  reconciliation_runs: duplicate_target_count

Real bug found live: if the TARGET object has 2+ rows sharing the same
key, reconciliation/engine.py's _fetch_key_rows() used to silently
overwrite the earlier row with the later one — a source record whose id
matched that key was confidently reported as a clean MATCH_BY_ID even
though which target row it actually corresponded to was never decided.
The engine now detects this and reports it as its own
DUPLICATE_TARGET_MATCH exception type (already a defined enum value,
previously never produced). This column is nullable — every existing run
row is unaffected; only a fresh run populates it (0 included, whenever no
duplicates are found). Idempotent.
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

    add_column(cur, "reconciliation_runs", "duplicate_target_count", "INTEGER")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
