"""
Backfill script: rewrites existing ApprovalWorkflowAuditLog messages that
were generated before the "user #{id} -> real name" fix in
approver_resolution_service.py (see that module's own ResolutionOutcome.
assigned_user_name comment for the live bug this fixed for NEW rows).

Real bug found live (user-reported, twice): the fix only changes what
NEW audit rows say — rows already written before the backend restart
keep their original "assigned to user 3" text forever, since audit log
messages are a plain stored string, not re-rendered from live data.
The user's screenshot after the code fix still showed the old text on
an old row, correctly, but they want it corrected anyway.

Safe to run repeatedly (idempotent): the regex only matches "user {id}"
(a literal, no name in it), so a row already rewritten to a real name
here or by the live code path no longer matches and is left alone.
Every match is a real user id that must resolve to a real User row —
if it doesn't (deleted user), that specific row is skipped and logged,
never rewritten to a guess.
"""
import re
import sqlite3

DB_PATH = "dip.db"

# Matches both "assigned to user 3" (STEP_ASSIGNED/PEER_REVIEW_ASSIGNED,
# approver_resolution_service.py) and "previously assigned to user 3"
# (ESCALATED notification body text lives in notification_outbox rows,
# not audit log, so out of scope here — this table only ever got the
# first form, confirmed via direct query before writing this script).
PATTERN = re.compile(r"\buser (\d+)\b")


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("SELECT id, full_name FROM users")
    names = {row[0]: row[1] for row in cur.fetchall()}

    cur.execute("SELECT id, message FROM approval_workflow_audit_log WHERE message LIKE '%assigned to user %'")
    rows = cur.fetchall()

    updated, skipped = 0, 0
    for row_id, message in rows:
        def _sub(m):
            uid = int(m.group(1))
            name = names.get(uid)
            return name if name else m.group(0)  # leave "user {id}" as-is if the user is gone

        new_message = PATTERN.sub(_sub, message)
        if new_message != message:
            cur.execute("UPDATE approval_workflow_audit_log SET message = ? WHERE id = ?", (new_message, row_id))
            print(f"#{row_id}: {message!r} -> {new_message!r}")
            updated += 1
        else:
            skipped += 1

    con.commit()
    con.close()
    print(f"\n{updated} row(s) updated, {skipped} row(s) left unchanged (already fixed, or referenced user no longer exists).")


if __name__ == "__main__":
    main()
