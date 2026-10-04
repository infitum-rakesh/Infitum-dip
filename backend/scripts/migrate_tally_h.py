"""
TALLY-H migration — generalizes the Payment Approval engine's workflow-
instance tables to an entity_type+entity_id pattern (so Tally voucher
write-back authorization can reuse it without forking a second copy),
and adds the write-back approval gate's own denormalized outcome to
tally_voucher_previews.

approval_rules: adds entity_type (backfilled 'INVOICE' for every
existing row — identical to their pre-migration behavior, since
resolve_candidate_rules() only just started filtering on this column).

approval_workflows / approval_workflow_audit_log: adds entity_type
('INVOICE', backfilled) and entity_id (backfilled = invoice_id for
every existing row).

invoice_id itself was left NOT NULL here on the mistaken assumption
that SQLite wouldn't enforce it on ORM inserts anyway — that was wrong
(SQLite enforces NOT NULL strictly regardless of its loose column
*typing*), and it broke the very first live TALLY_VOUCHER workflow
creation (invoice_id=NULL by design) with a real IntegrityError. See
migrate_tally_h2_nullable_invoice_id.py, which rebuilds both tables
(SQLite has no ALTER COLUMN to drop NOT NULL in place) to actually make
invoice_id nullable. Run that script too, in order, after this one.

tally_voucher_previews: adds approval_status (backfilled
'NOT_SUBMITTED') and approval_workflow_id (NULL).

Safe in-place ALTERs, no destructive change to any pre-existing column.
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
        return False
    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")
    print(f"{table}: added {column}")
    return True


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    if add_column(cur, "approval_rules", "entity_type", "VARCHAR(20)"):
        cur.execute("UPDATE approval_rules SET entity_type = 'INVOICE' WHERE entity_type IS NULL")
        print(f"  backfilled {cur.rowcount} row(s) to entity_type='INVOICE'")

    if add_column(cur, "approval_workflows", "entity_type", "VARCHAR(20)"):
        cur.execute("UPDATE approval_workflows SET entity_type = 'INVOICE' WHERE entity_type IS NULL")
        print(f"  backfilled {cur.rowcount} row(s) to entity_type='INVOICE'")
    if add_column(cur, "approval_workflows", "entity_id", "INTEGER"):
        cur.execute("UPDATE approval_workflows SET entity_id = invoice_id WHERE entity_id IS NULL")
        print(f"  backfilled {cur.rowcount} row(s) with entity_id = invoice_id")

    if add_column(cur, "approval_workflow_audit_log", "entity_type", "VARCHAR(20)"):
        cur.execute("UPDATE approval_workflow_audit_log SET entity_type = 'INVOICE' WHERE entity_type IS NULL")
        print(f"  backfilled {cur.rowcount} row(s) to entity_type='INVOICE'")
    if add_column(cur, "approval_workflow_audit_log", "entity_id", "INTEGER"):
        cur.execute("UPDATE approval_workflow_audit_log SET entity_id = invoice_id WHERE entity_id IS NULL")
        print(f"  backfilled {cur.rowcount} row(s) with entity_id = invoice_id")

    if add_column(cur, "tally_voucher_previews", "approval_status", "VARCHAR(20)"):
        cur.execute("UPDATE tally_voucher_previews SET approval_status = 'NOT_SUBMITTED' WHERE approval_status IS NULL")
        print(f"  backfilled {cur.rowcount} row(s) to approval_status='NOT_SUBMITTED'")
    add_column(cur, "tally_voucher_previews", "approval_workflow_id", "INTEGER")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
