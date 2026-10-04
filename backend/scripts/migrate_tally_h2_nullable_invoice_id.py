"""
TALLY-H fix — migrate_tally_h.py's own docstring claimed SQLite "does not
actually enforce NOT NULL" on an ALTER-added column's pre-existing
sibling constraint, and that relaxing approval_workflows.invoice_id /
approval_workflow_audit_log.invoice_id to nullable was therefore a
no-op-safe skip. That reasoning was wrong: SQLite enforces NOT NULL on
every INSERT regardless of how loosely it treats column *types*, and
real live verification of TALLY-H's submit-for-approval endpoint hit
exactly this — `sqlite3.IntegrityError: NOT NULL constraint failed:
approval_workflows.invoice_id` the moment a TALLY_VOUCHER workflow
(invoice_id=NULL by design) was actually created.

SQLite has no ALTER COLUMN to drop a NOT NULL constraint in place, so
this rebuilds both tables: create a new table with the same columns but
invoice_id nullable, copy every row across, drop the old table, rename
the new one into place, and recreate the original indexes. Wrapped in a
single transaction so a crash mid-migration leaves the original table
untouched rather than half-renamed.

Idempotent: skipped entirely if invoice_id is already nullable.
"""
import sqlite3

DB_PATH = "dip.db"


def invoice_id_is_nullable(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    for row in cur.fetchall():
        if row[1] == "invoice_id":
            return row[3] == 0  # notnull flag: 0 = nullable
    raise RuntimeError(f"{table} has no invoice_id column")


def rebuild_approval_workflows(cur):
    if invoice_id_is_nullable(cur, "approval_workflows"):
        print("approval_workflows.invoice_id already nullable, skipping.")
        return
    cur.execute("""
        CREATE TABLE approval_workflows_new (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER,
            invoice_id INTEGER,
            rule_id INTEGER,
            rule_name VARCHAR(150),
            rule_version INTEGER,
            status VARCHAR(13) NOT NULL,
            approval_required BOOLEAN NOT NULL,
            approval_mode VARCHAR(10) NOT NULL,
            approval_count_required INTEGER NOT NULL,
            approval_count_completed INTEGER NOT NULL,
            current_approval_step_number INTEGER,
            peer_review_required BOOLEAN NOT NULL,
            peer_review_mode VARCHAR(10) NOT NULL,
            peer_review_count_required INTEGER NOT NULL,
            peer_review_count_completed INTEGER NOT NULL,
            current_peer_review_step_number INTEGER,
            context_snapshot JSON,
            evaluation_trace JSON,
            decision_reason TEXT,
            created_by INTEGER,
            started_at DATETIME,
            completed_at DATETIME,
            created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
            updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
            entity_type VARCHAR(20),
            entity_id INTEGER,
            FOREIGN KEY(invoice_id) REFERENCES ap_invoices (id),
            FOREIGN KEY(rule_id) REFERENCES approval_rules (id)
        )
    """)
    cur.execute("""
        INSERT INTO approval_workflows_new
        SELECT id, organization_id, invoice_id, rule_id, rule_name, rule_version, status,
               approval_required, approval_mode, approval_count_required, approval_count_completed,
               current_approval_step_number, peer_review_required, peer_review_mode,
               peer_review_count_required, peer_review_count_completed, current_peer_review_step_number,
               context_snapshot, evaluation_trace, decision_reason, created_by, started_at, completed_at,
               created_at, updated_at, entity_type, entity_id
        FROM approval_workflows
    """)
    cur.execute("DROP TABLE approval_workflows")
    cur.execute("ALTER TABLE approval_workflows_new RENAME TO approval_workflows")
    cur.execute("CREATE INDEX ix_approval_workflows_id ON approval_workflows (id)")
    cur.execute("CREATE INDEX ix_approval_workflows_organization_id ON approval_workflows (organization_id)")
    cur.execute("CREATE INDEX ix_approval_workflows_invoice_id ON approval_workflows (invoice_id)")
    cur.execute("CREATE INDEX ix_approval_workflows_rule_id ON approval_workflows (rule_id)")
    cur.execute("CREATE INDEX ix_approval_workflows_status ON approval_workflows (status)")
    cur.execute("CREATE INDEX ix_approval_workflows_created_by ON approval_workflows (created_by)")
    print("approval_workflows: rebuilt with invoice_id nullable")


def rebuild_approval_workflow_audit_log(cur):
    if invoice_id_is_nullable(cur, "approval_workflow_audit_log"):
        print("approval_workflow_audit_log.invoice_id already nullable, skipping.")
        return
    cur.execute("""
        CREATE TABLE approval_workflow_audit_log_new (
            id INTEGER NOT NULL PRIMARY KEY,
            organization_id INTEGER,
            workflow_id INTEGER NOT NULL,
            invoice_id INTEGER,
            event_type VARCHAR(20) NOT NULL,
            message TEXT,
            details JSON,
            actor_id INTEGER,
            created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
            entity_type VARCHAR(20),
            entity_id INTEGER,
            FOREIGN KEY(workflow_id) REFERENCES approval_workflows (id),
            FOREIGN KEY(invoice_id) REFERENCES ap_invoices (id)
        )
    """)
    cur.execute("""
        INSERT INTO approval_workflow_audit_log_new
        SELECT id, organization_id, workflow_id, invoice_id, event_type, message, details,
               actor_id, created_at, entity_type, entity_id
        FROM approval_workflow_audit_log
    """)
    cur.execute("DROP TABLE approval_workflow_audit_log")
    cur.execute("ALTER TABLE approval_workflow_audit_log_new RENAME TO approval_workflow_audit_log")
    cur.execute("CREATE INDEX ix_approval_workflow_audit_log_id ON approval_workflow_audit_log (id)")
    cur.execute("CREATE INDEX ix_approval_workflow_audit_log_organization_id ON approval_workflow_audit_log (organization_id)")
    cur.execute("CREATE INDEX ix_approval_workflow_audit_log_workflow_id ON approval_workflow_audit_log (workflow_id)")
    cur.execute("CREATE INDEX ix_approval_workflow_audit_log_invoice_id ON approval_workflow_audit_log (invoice_id)")
    cur.execute("CREATE INDEX ix_approval_workflow_audit_log_actor_id ON approval_workflow_audit_log (actor_id)")
    print("approval_workflow_audit_log: rebuilt with invoice_id nullable")


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("PRAGMA foreign_keys = OFF")
    try:
        rebuild_approval_workflows(cur)
        rebuild_approval_workflow_audit_log(cur)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        cur.execute("PRAGMA foreign_keys = ON")
        con.close()


if __name__ == "__main__":
    main()
