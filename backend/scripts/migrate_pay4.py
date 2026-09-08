"""
Payment Invoice Approval Phase 4 migration.

Every brand-new Phase 4 table (approval_workflows, approval_workflow_steps,
peer_review_steps, approval_workflow_audit_log) is picked up for free by
init_db()'s create_all() once the model module is imported in main.py — no
ALTER needed for those.

The one real fix this script performs: Phase 2's approval_rules and
approval_rule_steps tables were created WITHOUT the sqlite_autoincrement
table option. Phase 4 adds real FK children pointing at both
(approval_workflows.rule_id, approval_workflow_steps/peer_review_steps
.rule_step_id) — exactly the "table that accumulates FK children" shape
that has already caused a real SQLite id-reuse collision twice in this
project (organizations, pipelines; see ApInvoice/Pipeline's
__table_args__ comments). Both model classes were updated
(models/approval/rules.py) to add sqlite_autoincrement — but that table
option only takes effect at CREATE TABLE time, so it doesn't retroactively
apply to a table that already exists.

Since both tables were provably empty at the time of this fix (Phase 2
was verified against real DB rows in `dip.db` and the test rows were
deleted; no create/publish API exists yet for a user to have added a real
rule), the safe fix is: assert they're still empty, then DROP them so
init_db()'s create_all() recreates them fresh with the corrected table
option baked in. This script refuses to drop either table if it finds
ANY row in it — a real safety check, not an assumption, so this never
silently discards real data if it's run again later after rules exist.
Idempotent: running it again after the tables have already been dropped
(and not yet recreated) is a no-op.
"""
import sqlite3

DB_PATH = "dip.db"

# Drop child before parent — approval_rule_steps has no FK children of
# its own left dangling by this (ApprovalWorkflowStep/PeerReviewStep
# don't exist as rows yet either, Phase 4 is schema-only).
TABLES_TO_RECREATE = ["approval_rule_steps", "approval_rules"]


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
        print(f"{table}: dropped (was empty) — init_db() will recreate it with sqlite_autoincrement on next backend start.")

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
