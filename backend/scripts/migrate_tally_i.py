"""
TALLY-I migration — adds the write-back outcome columns to the existing
tally_voucher_previews table (write_status/tally_voucher_guid/
tally_master_id/tally_voucher_number/written_at). The two new tables
(tally_voucher_writeback_transactions, tally_voucher_writeback_verifications)
need no migration script — brand-new tables are picked up automatically
by init_db()/create_all() on next backend startup, same convention as
every other new-table addition in this codebase.

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

    if add_column(cur, "tally_voucher_previews", "write_status", "VARCHAR(20)"):
        cur.execute("UPDATE tally_voucher_previews SET write_status = 'NOT_WRITTEN' WHERE write_status IS NULL")
        print(f"  backfilled {cur.rowcount} row(s) to write_status='NOT_WRITTEN'")
    add_column(cur, "tally_voucher_previews", "tally_voucher_guid", "VARCHAR(100)")
    add_column(cur, "tally_voucher_previews", "tally_master_id", "VARCHAR(60)")
    add_column(cur, "tally_voucher_previews", "tally_voucher_number", "VARCHAR(100)")
    add_column(cur, "tally_voucher_previews", "written_at", "DATETIME")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
