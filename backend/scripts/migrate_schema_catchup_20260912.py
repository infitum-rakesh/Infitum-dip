"""
Catch-up migration — this dip.db had drifted behind the model definitions
by several already-completed phases (found live, 2026-09-12, while
debugging "dashboard not loading" / access errors: a full model-vs-live-
schema diff turned up these 4 remaining missing columns after the earlier
must_change_password/SB1/companies/etc. catch-up round). Safe in-place
ALTERs (all nullable, no constraint change). Idempotent.
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

    add_column(cur, "schedules", "anomaly_job_id", "INTEGER")
    add_column(cur, "column_mappings", "latest_version", "INTEGER")
    add_column(cur, "column_mappings", "current_published_version", "INTEGER")
    add_column(cur, "target_load_runs", "mapping_version", "INTEGER")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
