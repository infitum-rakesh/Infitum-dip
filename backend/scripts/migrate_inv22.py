"""
INV22 — Data Lineage: invoice field-level lineage migration.

Run once, backend STOPPED, before restarting on the new column:
    cd backend && python3 scripts/migrate_inv22.py

documents gains one new nullable column:
  - extraction_model (VARCHAR(200), nullable) — the real AI Provider
    connection's model name (e.g. "gpt-4o-mini") when a genuine AI call
    ran, the honest literal "CUSTOM_ENGINE" when the deterministic
    rule-based fallback ran instead, or NULL when genuinely nothing ran
    (no OCR text, or every AI connection failed) or for documents
    processed before this column existed. Closes the "model/version"
    evidence gap (spec section 119) and gives Lineage's AI_MODEL node a
    real value to show instead of leaving it blank.

Idempotent: safe to re-run (skips the column if already migrated).
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

    add_column(cur, "documents", "extraction_model", "VARCHAR(200)")

    con.commit()
    con.close()
    print("Done.")


if __name__ == "__main__":
    main()
