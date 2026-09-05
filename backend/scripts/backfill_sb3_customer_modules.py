"""
SB3 backfill — Multi-Subscription Billing & Payments Platform.

Assigns every pre-SB1-migration customer_modules row (subscription_id
still NULL) to its organization's then-current Subscription — the most
recently created one, matching the "latest row is current" assumption
every pre-existing service already made before this feature. Going
forward, every new CustomerModule row is created with an explicit
subscription_id by services/seller/module_service.py /
services/seller/subscription_service.py — this script only ever needs to
run once.

Run with the backend either stopped or running (pure data backfill, no
schema change) — but for consistency with every other migration script
in this project, run it stopped:
    cd backend && python3 scripts/backfill_sb3_customer_modules.py
"""
import sqlite3

DB_PATH = "dip.db"


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("SELECT id, organization_id FROM customer_modules WHERE subscription_id IS NULL")
    orphaned = cur.fetchall()
    print(f"Found {len(orphaned)} customer_modules rows with no subscription_id.")

    # organization_id -> most-recent Subscription.id ("current" subscription,
    # same definition every pre-existing service used before this feature).
    cur.execute("""
        SELECT s.organization_id, s.id
        FROM subscriptions s
        WHERE s.id = (
            SELECT s2.id FROM subscriptions s2
            WHERE s2.organization_id = s.organization_id
            ORDER BY s2.created_at DESC, s2.id DESC
            LIMIT 1
        )
    """)
    current_sub_by_org = dict(cur.fetchall())
    print(f"Resolved current subscription for {len(current_sub_by_org)} organizations.")

    updated = 0
    unresolved = []
    for row_id, org_id in orphaned:
        sub_id = current_sub_by_org.get(org_id)
        if sub_id is None:
            unresolved.append((row_id, org_id))
            continue
        cur.execute("UPDATE customer_modules SET subscription_id = ? WHERE id = ?", (sub_id, row_id))
        updated += 1

    con.commit()
    print(f"Backfilled subscription_id on {updated} rows.")
    if unresolved:
        print(f"WARNING: {len(unresolved)} rows could not be resolved (org has zero subscriptions): {unresolved}")

    # verification
    cur.execute("SELECT count(*) FROM customer_modules WHERE subscription_id IS NULL")
    remaining = cur.fetchone()[0]
    print(f"customer_modules rows still NULL after backfill: {remaining}")

    con.close()


if __name__ == "__main__":
    main()
