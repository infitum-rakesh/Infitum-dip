"""
Adds users.must_change_password (Phase #381 — see models/user.py). Found
missing live from this exact dip.db on 2026-09-12: the column is mapped in
the ORM model and read/written by user_admin_service.reset_password() /
seller_user_service.reset_password() / auth_service, but was never
actually ALTERed into this database file, so any reset_password() call
against it would fail with "no such column: must_change_password" on
commit. Safe in-place ALTER (nullable-equivalent via DEFAULT), no
constraint change. Idempotent.
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

    add_column(cur, "users", "must_change_password", "BOOLEAN DEFAULT 0")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
