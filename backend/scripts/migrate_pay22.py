"""
Payment Invoice Approval Phase 22 migration.

The user's own explicit ask: create_workflow() now persists a real
ApprovalWorkflow row for EVERY invoice that reaches the decision engine
while PAYMENT_APPROVAL is active — not just the ones an ApprovalRule
actually claims. Before this phase, "no rule matched" (or "a rule matched
but needed neither approval nor peer review") returned None and nothing
was ever persisted.

This requires two real schema changes to approval_workflows:
  1. A new terminal status value, AUTO_APPROVED, added to the
     ApprovalWorkflowStatus enum. SQLAlchemy's Enum column type sizes its
     VARCHAR to the longest member length at CREATE TABLE time — the
     existing `status VARCHAR(11)` (sized for "IN_PROGRESS") is too
     narrow for "AUTO_APPROVED" (13 chars) and must be recreated wider.
  2. rule_id/rule_name/rule_version made NULLABLE — an AUTO_APPROVED row
     for an invoice no rule matched at all has no rule to reference (see
     the model's own comment on ApprovalWorkflow.rule_id). SQLite has no
     ALTER COLUMN to relax a NOT NULL constraint in place.

Same safe pattern as migrate_pay4.py: this table was provably empty in
this deployment's dip.db at the time of this fix (verified via sqlite3
directly — 0 rows in approval_workflows, and its children
approval_workflow_steps/peer_review_steps are 0 rows too), so the fix is
to assert it's still empty, then DROP it so init_db()'s create_all()
recreates it fresh from the updated model (wider status column, nullable
rule_* columns). Refuses to drop if it finds ANY row, so this never
silently discards real data if run again later after real workflows
exist. Idempotent: a no-op if the table is already gone (recreated) and
init_db() hasn't run yet, or already recreated.

approval_workflow_audit_log is NOT touched — its `event_type` column is a
plain VARCHAR(20) sized independently of the enum's longest current
member (see workflow.py's own field comment), and the new AUTO_APPROVED
audit event value (13 chars) fits it without any change.
"""
import sqlite3

DB_PATH = "dip.db"

TABLES_TO_RECREATE = ["approval_workflows"]


def table_exists(cur, table):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def row_count(cur, table):
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    for table in TABLES_TO_RECREATE:
        if not table_exists(cur, table):
            print(f"{table} does not exist (already migrated, or init_db() hasn't run yet) — skipping.")
            continue
        count = row_count(cur, table)
        if count > 0:
            print(f"REFUSING to drop {table} — it has {count} real row(s). "
                  f"This script only recreates a table it can prove is still empty.")
            continue
        cur.execute(f"DROP TABLE {table}")
        print(f"{table}: dropped (was empty) — init_db() will recreate it with the wider status column "
              f"and nullable rule_id/rule_name/rule_version on next backend start.")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
