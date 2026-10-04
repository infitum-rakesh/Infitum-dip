"""
TALLY-C migration — additive nullable GST/TDS columns on the pre-existing
ap_invoices/ap_invoice_lines tables, plus ap_invoices.voucher_type
(NOT NULL, defaulted 'PURCHASE' for every existing row).

Every brand-new TALLY-C table (tally_canonical_vouchers/parties/ledgers)
is picked up for free by init_db()'s create_all() once the model module
is imported in main.py — no ALTER needed for those.

Safe in-place ALTERs, no constraint change on any pre-existing column.
Idempotent.
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

    for col, ddl in [
        ("payment_reference", "VARCHAR(120)"),
        ("vendor_gstin", "VARCHAR(20)"),
        ("vendor_pan", "VARCHAR(15)"),
        ("buyer_gstin", "VARCHAR(20)"),
        ("place_of_supply", "VARCHAR(80)"),
        ("tds_section", "VARCHAR(20)"),
        ("tds_rate", "VARCHAR(20)"),
        ("tds_amount", "FLOAT"),
        ("cgst_amount", "FLOAT"),
        ("sgst_amount", "FLOAT"),
        ("igst_amount", "FLOAT"),
        ("cess_amount", "FLOAT"),
        ("voucher_type", "VARCHAR(20) NOT NULL DEFAULT 'PURCHASE'"),
    ]:
        add_column(cur, "ap_invoices", col, ddl)

    for col, ddl in [
        ("hsn_sac_code", "VARCHAR(20)"),
        ("uom", "VARCHAR(20)"),
        ("cgst_rate", "VARCHAR(20)"),
        ("cgst_amount", "FLOAT"),
        ("sgst_rate", "VARCHAR(20)"),
        ("sgst_amount", "FLOAT"),
        ("igst_rate", "VARCHAR(20)"),
        ("igst_amount", "FLOAT"),
    ]:
        add_column(cur, "ap_invoice_lines", col, ddl)

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
