"""Add per-document OCR language configuration storage.

Idempotent local/SQLite migration for existing dev databases. New databases
get the column from models.document.Document via create_all().
"""
import sqlite3

DB_PATH = "dip.db"


def column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    if column_exists(cur, "documents", "language_config"):
        print("documents.language_config already exists, skipping.")
    else:
        cur.execute("ALTER TABLE documents ADD COLUMN language_config JSON")
        print("documents: added language_config")
    con.commit()
    con.close()


if __name__ == "__main__":
    main()
