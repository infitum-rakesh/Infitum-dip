"""
TALLY-J migration — adds schedule_id/trigger columns to the existing
tally_extraction_runs and tally_reconciliation_runs tables, so a
scheduled run can be told apart from a manually-triggered one (see those
models' own docstrings). The new tally_reconciliation_schedule_configs
table needs no migration script — a brand-new table is picked up
automatically by init_db()/create_all() on next backend startup, same
convention as every other new-table addition in this codebase.

Idempotent: safe to run more than once.
"""
import sqlite3

DB_PATH = "dip.db"


def column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def add_column(cur, table, column, ddl_type):
    if column_exists(cur, table, column):
        print(f"{table}.{column} already exists, skipping.")
        return False
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
    print(f"{table}: added {column}")
    return True


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    for table in ("tally_extraction_runs", "tally_reconciliation_runs"):
        cur.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
        if cur.fetchone() is None:
            print(f"{table} does not exist yet — skipping (will be created fresh with the new columns).")
            continue
        add_column(cur, table, "schedule_id", "INTEGER REFERENCES schedules(id)")
        if add_column(cur, table, "trigger", "VARCHAR(20)"):
            cur.execute(f"UPDATE {table} SET trigger = 'Manual' WHERE trigger IS NULL")
            print(f"  backfilled {cur.rowcount} row(s) to trigger='Manual'")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
