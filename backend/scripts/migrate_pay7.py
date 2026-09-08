"""
Payment Invoice Approval Phase 7 migration — one small additive column.

approval_workflow_steps.assigned_at / peer_review_steps.assigned_at
(DATETIME, nullable): the real "SLA clock start" timestamp — stamped by
approver_resolution_service.resolve_and_assign_step() the moment a step
actually becomes ASSIGNED (Phase 7 extends that function to set it).
Phase 4's due_at/reminder_after_hours/escalation_after_hours/
escalated_at columns all already exist and need no migration; this is
the one genuinely missing piece sla_scheduler_service.py (Phase 7) needs
to know WHEN a step's SLA clock actually started (updated_at doesn't
work for this — it changes on any field edit, not just assignment).

Safe in-place ALTER (nullable, no constraint change). Idempotent.
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

    add_column(cur, "approval_workflow_steps", "assigned_at", "DATETIME")
    add_column(cur, "peer_review_steps", "assigned_at", "DATETIME")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
