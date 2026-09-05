"""
Invoice Intelligence Phase 1 (INV1) migration — two small additive columns
on pre-existing tables; every brand-new Invoice Intelligence table
(ap_invoice_*) is picked up for free by init_db()'s create_all() once the
model modules are imported in main.py, so no ALTER is needed for those.

- documents.ai_raw_response (JSON, nullable): persists the raw AI
  extraction JSON that extraction_service already computes internally but
  currently discards. Generically useful (not invoice-only) — the Invoice
  Intelligence layer reads it to materialize canonical ApInvoice rows
  without a second AI call.
- document_schedule_configs.document_type (VARCHAR, nullable): lets a
  scheduled source pin a document type (e.g. "INVOICE") so scheduled
  intake uses the real taxonomy instead of ad-hoc detection.

Safe in-place ALTERs (all nullable, no constraint change). Idempotent.
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

    add_column(cur, "documents", "ai_raw_response", "JSON")
    add_column(cur, "document_schedule_configs", "document_type", "VARCHAR(60)")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
