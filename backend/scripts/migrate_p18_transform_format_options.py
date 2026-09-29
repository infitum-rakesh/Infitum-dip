"""
P18 migration — add transformation_jobs.format_options (Part 8 of the
spec: configurable CSV delimiter/quotechar/encoding, Excel sheet name,
XML root/record element names for the Data Format Transformation
Engine's target-file output).

transformation_jobs already exists (Phase 4/TE4), so this new column
doesn't materialize via create_all() like a brand-new table would — same
in-place ALTER pattern as migrate_ps7_storage_options.py/
migrate_p17_webhook_options.py. NULL means every current default
(comma-delimited CSV, "Result" XLSX sheet, <rows>/<row> XML tags) —
identical behavior to every row created before this column existed.

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
    table = "transformation_jobs"

    if column_exists(cur, table, "format_options"):
        print(f"{table}.format_options already exists, skipping.")
    else:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN format_options JSON")
        print(f"{table}: added format_options")
    con.commit()
    con.close()


if __name__ == "__main__":
    main()
