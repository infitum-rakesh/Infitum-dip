"""
Adds ID->NAME fallback matching columns (see models/reconciliation.py):

  reconciliation_jobs:       fallback_match_columns, name_normalization
  reconciliation_runs:       matched_by_id_count, matched_by_name_count, ambiguous_count
  reconciliation_exceptions: match_method, source_id, source_name, target_id, target_name

Real bug found live: the reconciliation engine did exact-key-only
matching — a source record whose id changed after a migration (but whose
name still matched) was reported MISSING even though it genuinely exists
in the target. All new columns are nullable/JSON-default-empty, so every
existing job/run/exception row is completely unaffected and every
existing job keeps its exact current (ID-only) behavior unless it
explicitly sets fallback_match_columns. Idempotent.
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

    add_column(cur, "reconciliation_jobs", "fallback_match_columns", "JSON")
    add_column(cur, "reconciliation_jobs", "name_normalization", "JSON")

    add_column(cur, "reconciliation_runs", "matched_by_id_count", "INTEGER")
    add_column(cur, "reconciliation_runs", "matched_by_name_count", "INTEGER")
    add_column(cur, "reconciliation_runs", "ambiguous_count", "INTEGER")

    add_column(cur, "reconciliation_exceptions", "match_method", "VARCHAR(20)")
    add_column(cur, "reconciliation_exceptions", "source_id", "TEXT")
    add_column(cur, "reconciliation_exceptions", "source_name", "TEXT")
    add_column(cur, "reconciliation_exceptions", "target_id", "TEXT")
    add_column(cur, "reconciliation_exceptions", "target_name", "TEXT")

    # Backfill existing rows' new JSON columns to their model-level
    # defaults (list/dict) rather than leaving them SQL NULL — matches
    # ORM-level `default=list`/`default=dict` for rows written going
    # forward; existing rows read back as [] / {} either way once the ORM
    # column type coerces None, but this keeps the raw DB state consistent
    # for anything that queries this table directly (e.g. sqlite3 CLI).
    cur.execute("UPDATE reconciliation_jobs SET fallback_match_columns = '[]' WHERE fallback_match_columns IS NULL")
    cur.execute("UPDATE reconciliation_jobs SET name_normalization = '{}' WHERE name_normalization IS NULL")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
