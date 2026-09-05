"""
SB1 migration — Multi-Subscription Billing & Payments Platform.

Run once, backend STOPPED, before restarting on the new models:
    cd backend && python3 scripts/migrate_sb1.py

1. subscriptions — add nullable date columns (safe in-place ALTER; new
   SubscriptionStatus enum values fit fine too — inspected live and the
   existing `status` column is a bare VARCHAR with no CHECK constraint,
   and SQLite never enforces VARCHAR(n) length anyway).
2. sellers — add nullable sales_* columns (safe in-place ALTER).
3. customer_modules — recreate with a `subscription_id` FK and the
   uniqueness constraint moved from (organization_id, module) to
   (subscription_id, module). SQLite can't ALTER a constraint in place,
   so this does the standard create-copy-drop-rename dance.
   subscription_id is left NULL here for every existing row — SB3's
   backfill script assigns each row to its org's current subscription.

Idempotent: safe to re-run (skips any column/table already migrated).
"""
import sqlite3

DB_PATH = "dip.db"


def column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    sub_columns = [
        ("trial_start_date", "DATETIME"),
        ("trial_end_date", "DATETIME"),
        ("billing_start_date", "DATETIME"),
        ("current_period_start", "DATETIME"),
        ("current_period_end", "DATETIME"),
        ("next_billing_date", "DATETIME"),
        ("cancelled_at", "DATETIME"),
    ]
    for col, coltype in sub_columns:
        if not column_exists(cur, "subscriptions", col):
            cur.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} {coltype}")
            print(f"subscriptions: added {col}")
        else:
            print(f"subscriptions: {col} already exists, skipping")

    seller_columns = [
        ("sales_email", "VARCHAR(255)"),
        ("sales_phone", "VARCHAR(50)"),
        ("sales_url", "VARCHAR(500)"),
        ("sales_contact_name", "VARCHAR(255)"),
    ]
    for col, coltype in seller_columns:
        if not column_exists(cur, "sellers", col):
            cur.execute(f"ALTER TABLE sellers ADD COLUMN {col} {coltype}")
            print(f"sellers: added {col}")
        else:
            print(f"sellers: {col} already exists, skipping")

    if column_exists(cur, "customer_modules", "subscription_id"):
        print("customer_modules: subscription_id already exists, skipping recreate")
    else:
        cur.execute("SELECT count(*) FROM customer_modules")
        before_count = cur.fetchone()[0]

        cur.execute("""
            CREATE TABLE customer_modules_new (
                id INTEGER NOT NULL,
                organization_id INTEGER NOT NULL,
                subscription_id INTEGER,
                module VARCHAR(60) NOT NULL,
                enabled BOOLEAN NOT NULL,
                status VARCHAR(20) DEFAULT 'ACTIVE',
                license_type VARCHAR(30) NOT NULL,
                max_users INTEGER,
                monthly_credits INTEGER,
                max_documents INTEGER,
                max_records INTEGER,
                storage_limit_gb FLOAT,
                start_date DATETIME,
                expiry_date DATETIME,
                created_by INTEGER,
                created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_customer_module_subscription UNIQUE (subscription_id, module),
                FOREIGN KEY(organization_id) REFERENCES organizations (id),
                FOREIGN KEY(subscription_id) REFERENCES subscriptions (id),
                FOREIGN KEY(created_by) REFERENCES users (id)
            )
        """)
        cur.execute("""
            INSERT INTO customer_modules_new
                (id, organization_id, subscription_id, module, enabled, status, license_type,
                 max_users, monthly_credits, max_documents, max_records, storage_limit_gb,
                 start_date, expiry_date, created_by, created_at, updated_at)
            SELECT id, organization_id, NULL, module, enabled, status, license_type,
                   max_users, monthly_credits, max_documents, max_records, storage_limit_gb,
                   start_date, expiry_date, created_by, created_at, updated_at
            FROM customer_modules
        """)
        cur.execute("DROP TABLE customer_modules")
        cur.execute("ALTER TABLE customer_modules_new RENAME TO customer_modules")
        cur.execute("CREATE INDEX ix_customer_modules_organization_id ON customer_modules (organization_id)")
        cur.execute("CREATE INDEX ix_customer_modules_subscription_id ON customer_modules (subscription_id)")
        cur.execute("CREATE INDEX ix_customer_modules_module ON customer_modules (module)")
        cur.execute("CREATE INDEX ix_customer_modules_id ON customer_modules (id)")

        cur.execute("SELECT count(*) FROM customer_modules")
        after_count = cur.fetchone()[0]
        assert before_count == after_count, f"row count mismatch: {before_count} -> {after_count}"
        print(f"customer_modules: recreated with subscription_id + new unique constraint ({after_count} rows preserved)")

    con.commit()
    con.close()
    print("SB1 migration complete.")


if __name__ == "__main__":
    main()
