"""
P17 migration — add document_webhook_configs.http_method,
oauth_token_url, oauth_client_id, encrypted_oauth_client_secret,
oauth_scope, custom_headers, field_mapping, timeout_seconds (Part 7 of
the "push document data through external APIs" spec: OAuth2 auth +
configurable method/headers/mapping/timeout).

document_webhook_configs already exists with live rows (from the
original Phase 6 build), so these new columns don't materialize via
create_all() like a brand-new table would — same in-place ALTER pattern
as migrate_sb8.py/migrate_ps7_storage_options.py. Defaults match the
SQLAlchemy model's column defaults so existing rows keep behaving
exactly as before this migration (POST, no OAuth2, no custom headers/
mapping, 15s timeout).

Idempotent.
"""
import sqlite3

DB_PATH = "dip.db"


def column_exists(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    table = "document_webhook_configs"

    additions = [
        ("http_method", "TEXT NOT NULL DEFAULT 'POST'"),
        ("oauth_token_url", "TEXT"),
        ("oauth_client_id", "TEXT"),
        ("encrypted_oauth_client_secret", "TEXT"),
        ("oauth_scope", "TEXT"),
        ("custom_headers", "JSON"),
        ("field_mapping", "JSON"),
        ("timeout_seconds", "FLOAT NOT NULL DEFAULT 15.0"),
    ]
    for column, ddl in additions:
        if column_exists(cur, table, column):
            print(f"{table}.{column} already exists, skipping.")
        else:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            print(f"{table}: added {column}")
    con.commit()
    con.close()


if __name__ == "__main__":
    main()
