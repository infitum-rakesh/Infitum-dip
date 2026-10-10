"""
AI Connection Subscription & Organisation-Level Access Management (Phase 2).

Three things, in order:

  1. In-place ALTERs on two EXISTING tables with live rows: connections.
     is_public (default 0 — matches the model's Python-side default, so
     every existing connection stays exactly as private as it already
     effectively was) and customer_modules' five new AI-subscription limit
     columns (all nullable, no default — None/unlimited for every
     existing row, meaningless for every module except AI_CONNECTION).

  2. CREATE TABLE IF NOT EXISTS for the four brand-new tables (models/
     ai_connection.py). This deliberately does NOT defer to init_db()'s
     create_all() the way most new-table migrations in this codebase do
     (see e.g. migrate_document_schedules.py's "will be created
     automatically... on next backend startup") — that convention only
     works when a new table starts empty. This one doesn't: step 3 below
     needs to INSERT into ai_connection_permissions in the SAME pass,
     before the backend has ever started with the new model code (this
     script runs as part of install.bat/upgrade.bat's migration step,
     strictly BEFORE the app (re)starts) — so the table has to exist
     first. The CREATE TABLE statements mirror models/ai_connection.py's
     columns/constraints exactly; once the backend does start, SQLAlchemy
     sees the tables already match and create_all() is a no-op for them.

  3. Backfill: every EXISTING ai_provider/anthropic/ollama Connection
     (category AI_PROVIDER) had NO access-control concept before this
     feature — any authenticated user in-scope could use it. Per the
     explicit compatibility decision for this rollout, access now
     requires an explicit grant, and the one group backfilled with a real
     grant (so they are not locked out of connections they already relied
     on) is existing ORG_ADMIN-tier users — for an org-owned connection,
     every ORG_ADMIN in that same org; for a platform-shared connection
     (organization_id IS NULL), every ORG_ADMIN across every org, scoped
     to their own org_id on the grant row (see AIConnectionPermission's
     own docstring for why organization_id on the grant is always the
     GRANTEE's org, not necessarily the connection's). Every other
     existing role (DATA_ENGINEER/ANALYST/VIEWER) gets no backfilled
     grant — a real, intentional access change, not an oversight; they
     need an explicit grant from their Customer Admin going forward.

Idempotent: ALTERs check column_exists first; CREATE TABLE uses IF NOT
EXISTS; the backfill INSERTs use INSERT OR IGNORE against the real
UniqueConstraint, so rerunning this script after new users/connections
have been added only grants the new ones, never duplicates existing rows.
"""
import sqlite3

DB_PATH = "dip.db"


def column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def table_exists(cur, table):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _alter_connections(cur):
    if not table_exists(cur, "connections"):
        print("connections table does not exist yet — will be created fresh by init_db() with is_public included.")
        return
    if column_exists(cur, "connections", "is_public"):
        print("connections.is_public already exists, skipping.")
    else:
        cur.execute("ALTER TABLE connections ADD COLUMN is_public BOOLEAN DEFAULT 0")
        print("connections: added is_public")


def _alter_customer_modules(cur):
    if not table_exists(cur, "customer_modules"):
        print("customer_modules table does not exist yet — will be created fresh by init_db() with AI columns included.")
        return
    additions = [
        ("max_tokens", "INTEGER"),
        ("token_period", "TEXT"),
        ("ai_provider_bundle", "JSON"),
        ("ai_rate_limit", "INTEGER"),
        ("ai_concurrency_limit", "INTEGER"),
    ]
    for column, ddl in additions:
        if column_exists(cur, "customer_modules", column):
            print(f"customer_modules.{column} already exists, skipping.")
        else:
            cur.execute(f"ALTER TABLE customer_modules ADD COLUMN {column} {ddl}")
            print(f"customer_modules: added {column}")


def _create_new_tables(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_connection_permissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            connection_id INTEGER NOT NULL REFERENCES connections(id),
            organization_id INTEGER NOT NULL,
            grantee_type VARCHAR(10) NOT NULL,
            grantee_id INTEGER NOT NULL,
            can_view BOOLEAN DEFAULT 1,
            can_use BOOLEAN DEFAULT 1,
            granted_by INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_ai_connection_permission_grantee UNIQUE (connection_id, grantee_type, grantee_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_connection_permissions_connection_id ON ai_connection_permissions(connection_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_connection_permissions_organization_id ON ai_connection_permissions(organization_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_connection_permissions_grantee_id ON ai_connection_permissions(grantee_id)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_connection_defaults (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            connection_id INTEGER NOT NULL REFERENCES connections(id),
            created_by INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_ai_connection_default_org_user UNIQUE (organization_id, user_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_connection_defaults_organization_id ON ai_connection_defaults(organization_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_connection_defaults_user_id ON ai_connection_defaults(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_connection_defaults_connection_id ON ai_connection_defaults(connection_id)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_token_usage_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER NOT NULL,
            user_id INTEGER,
            connection_id INTEGER NOT NULL REFERENCES connections(id),
            customer_module_id INTEGER,
            feature VARCHAR(60) NOT NULL,
            request_id VARCHAR(100) NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            total_tokens INTEGER,
            usage_source VARCHAR(20) NOT NULL,
            status VARCHAR(20) DEFAULT 'SUCCESS',
            error_message VARCHAR(500),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_ai_token_usage_event_request UNIQUE (connection_id, request_id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_token_usage_events_organization_id ON ai_token_usage_events(organization_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_token_usage_events_user_id ON ai_token_usage_events(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_token_usage_events_connection_id ON ai_token_usage_events(connection_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_token_usage_events_customer_module_id ON ai_token_usage_events(customer_module_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_token_usage_events_feature ON ai_token_usage_events(feature)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_token_usage_events_usage_source ON ai_token_usage_events(usage_source)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_token_usage_events_created_at ON ai_token_usage_events(created_at)")

    cur.execute("""
        CREATE TABLE IF NOT EXISTS ai_quota_reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            organization_id INTEGER NOT NULL,
            connection_id INTEGER NOT NULL REFERENCES connections(id),
            user_id INTEGER,
            customer_module_id INTEGER,
            reserved_tokens INTEGER NOT NULL,
            status VARCHAR(20) DEFAULT 'RESERVED',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            resolved_at DATETIME
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_quota_reservations_organization_id ON ai_quota_reservations(organization_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_quota_reservations_connection_id ON ai_quota_reservations(connection_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_quota_reservations_user_id ON ai_quota_reservations(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_quota_reservations_customer_module_id ON ai_quota_reservations(customer_module_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_quota_reservations_status ON ai_quota_reservations(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_ai_quota_reservations_created_at ON ai_quota_reservations(created_at)")
    print("ai_connection_permissions / ai_connection_defaults / ai_token_usage_events / ai_quota_reservations ready.")


def _backfill_org_admin_grants(cur):
    if not table_exists(cur, "users") or not table_exists(cur, "connections"):
        print("users/connections tables do not exist yet — nothing to backfill.")
        return

    # Stored value is the Enum MEMBER NAME ("ORG_ADMIN"), not UserRole.
    # ORG_ADMIN's lowercase .value ("org_admin") — confirmed live against
    # the real dev DB: SQLAlchemy's Enum column defaults to storing the
    # Python enum's member name, not its .value, unless the column was
    # explicitly configured with values_callable to do otherwise (this
    # one wasn't).
    cur.execute("SELECT id, organization_id FROM users WHERE role = 'ORG_ADMIN' AND is_active = 1")
    org_admins = cur.fetchall()
    if not org_admins:
        print("No ORG_ADMIN users found — nothing to backfill.")
        return

    cur.execute("SELECT id, organization_id FROM connections WHERE category = 'AI_PROVIDER' AND is_active = 1")
    ai_connections = cur.fetchall()
    if not ai_connections:
        print("No existing AI_PROVIDER connections found — nothing to backfill.")
        return

    granted = 0
    for user_id, user_org_id in org_admins:
        if user_org_id is None:
            continue  # an ORG_ADMIN with no organization is not a real tenant admin
        for connection_id, connection_org_id in ai_connections:
            # Backfill exactly the access this admin already effectively had:
            # their own org's connection, or a platform-shared one (NULL org).
            if connection_org_id is not None and connection_org_id != user_org_id:
                continue
            cur.execute(
                """
                INSERT OR IGNORE INTO ai_connection_permissions
                    (connection_id, organization_id, grantee_type, grantee_id, can_view, can_use, granted_by)
                VALUES (?, ?, 'USER', ?, 1, 1, NULL)
                """,
                (connection_id, user_org_id, user_id),
            )
            if cur.rowcount:
                granted += 1
    print(f"Backfilled {granted} ORG_ADMIN access grant(s) on {len(ai_connections)} existing AI connection(s) across {len(org_admins)} admin(s).")


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    _alter_connections(cur)
    _alter_customer_modules(cur)
    _create_new_tables(cur)
    _backfill_org_admin_grants(cur)

    con.commit()
    con.close()


if __name__ == "__main__":
    main()
