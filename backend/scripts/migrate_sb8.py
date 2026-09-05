"""
SB8 migration — add subscriptions.pending_plan_id (downgrade-at-period-end,
spec section 37). Safe in-place ALTER (nullable, no constraint change).
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
    if column_exists(cur, "subscriptions", "pending_plan_id"):
        print("subscriptions.pending_plan_id already exists, skipping.")
    else:
        cur.execute("ALTER TABLE subscriptions ADD COLUMN pending_plan_id INTEGER REFERENCES plans(id)")
        print("subscriptions: added pending_plan_id")
    con.commit()
    con.close()


if __name__ == "__main__":
    main()
