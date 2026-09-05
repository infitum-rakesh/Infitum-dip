"""
Companies feature migration — adds sellers.digital_signature and
organizations.company_id/address_line/city/branch/postal_code. Safe
in-place ALTERs (all nullable, no constraint change); companies itself is
a brand-new table so it's picked up for free by init_db()'s create_all()
and needs no ALTER here. Idempotent.
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

    add_column(cur, "sellers", "digital_signature", "TEXT")

    add_column(cur, "organizations", "address_line", "VARCHAR(255)")
    add_column(cur, "organizations", "city", "VARCHAR(120)")
    add_column(cur, "organizations", "branch", "VARCHAR(120)")
    add_column(cur, "organizations", "postal_code", "VARCHAR(30)")
    add_column(cur, "organizations", "company_id", "INTEGER")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
