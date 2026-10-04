"""
TALLY-F migration — additive nullable review_item_id column on the
pre-existing tally_reconciliation_exceptions table (TALLY-E already
created and populated this table during its own live verification, so
this is a real ALTER, not a fresh table create_all() would pick up).

Safe in-place ALTER, no constraint change on any pre-existing column.
Idempotent.
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
    add_column(cur, "tally_reconciliation_exceptions", "review_item_id", "INTEGER")
    con.commit()
    con.close()


if __name__ == "__main__":
    main()
