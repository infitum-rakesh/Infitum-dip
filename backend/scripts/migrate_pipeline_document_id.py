"""
Adds pipelines.document_id (Open Pipeline bug fix — see models/pipeline.py).
Links a pipeline back to the document it was opened from, so re-clicking
"Open Pipeline" for the same document loads the existing pipeline instead
of creating a duplicate every time. Safe in-place ALTER (nullable, no
constraint change since SQLite can't add an FK to an existing table
in-place — enforced at the ORM layer instead). Idempotent.
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

    add_column(cur, "pipelines", "document_id", "INTEGER")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_pipelines_document_id ON pipelines (document_id)")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
