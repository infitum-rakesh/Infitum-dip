"""
Document Mapping Profiles — reuses the existing ColumnMapping /
ColumnMappingVersion versioning system (DRAFT -> VALIDATED -> PUBLISHED ->
RETIRED) for Document Intelligence extraction-to-target mapping, per the
explicit instruction: "do not create a third versioning system."

Two real schema changes needed on the EXISTING tables:
  1. source_connection_id / target_connection_id must become nullable —
     a Document Intelligence source has no Connection row at all (it's a
     document_type against the canonical/ad-hoc taxonomy), and an Invoice
     Schema target has no Connection row either (it's ApInvoice's real
     typed columns, introspected directly). SQLite has no ALTER COLUMN to
     drop a NOT NULL constraint in place, so both tables are rebuilt —
     same technique as migrate_tally_h2_nullable_invoice_id.py.
  2. Two new nullable columns on each table: source_document_type (set
     when the source is a Document Intelligence document_type instead of
     a connection) and target_format ("DB_TABLE" — the existing, default,
     unchanged behavior — or "INVOICE_SCHEMA"). ADD COLUMN is a simple,
     non-destructive SQLite operation, done in the same rebuild for
     source_connection_id but can run standalone for target_connection_id/
     target_format on a table that doesn't otherwise need rebuilding —
     kept together here for one clean pass.

A brand-new table (document_mapped_outputs — the append-only execution-
history record for "which mapping profile version produced which result
for which document") needs no migration here at all; it's picked up by
the normal init_db()/create_all() path the first time the app starts
with the new model imported in main.py.

Idempotent: every rebuild checks the live column's nullability first and
skips if already applied.
"""
import sqlite3

DB_PATH = "dip.db"


def _col_info(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    for row in cur.fetchall():
        if row[1] == column:
            return row  # (cid, name, type, notnull, dflt_value, pk)
    return None


def _is_nullable(cur, table, column):
    info = _col_info(cur, table, column)
    if info is None:
        raise RuntimeError(f"{table} has no {column} column")
    return info[3] == 0  # notnull flag: 0 = nullable


def rebuild_column_mappings(cur):
    if _is_nullable(cur, "column_mappings", "source_connection_id"):
        print("column_mappings.source_connection_id already nullable, skipping.")
        return
    cur.execute("""
        CREATE TABLE column_mappings_new (
            id INTEGER NOT NULL PRIMARY KEY,
            organization_id INTEGER,
            name VARCHAR(200) NOT NULL,
            source_connection_id INTEGER,
            source_schema VARCHAR(200),
            source_object VARCHAR(200),
            source_document_type VARCHAR(50),
            target_connection_id INTEGER,
            target_schema VARCHAR(200),
            target_object VARCHAR(200),
            target_format VARCHAR(30),
            mappings JSON NOT NULL,
            status VARCHAR(20) NOT NULL,
            created_by INTEGER,
            updated_by INTEGER,
            created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
            updated_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
            latest_version INTEGER,
            current_published_version INTEGER,
            FOREIGN KEY(organization_id) REFERENCES organizations (id),
            FOREIGN KEY(source_connection_id) REFERENCES connections (id),
            FOREIGN KEY(target_connection_id) REFERENCES connections (id),
            FOREIGN KEY(created_by) REFERENCES users (id),
            FOREIGN KEY(updated_by) REFERENCES users (id)
        )
    """)
    cur.execute("""
        INSERT INTO column_mappings_new (
            id, organization_id, name, source_connection_id, source_schema, source_object,
            target_connection_id, target_schema, target_object, mappings, status,
            created_by, updated_by, created_at, updated_at, latest_version, current_published_version
        )
        SELECT id, organization_id, name, source_connection_id, source_schema, source_object,
               target_connection_id, target_schema, target_object, mappings, status,
               created_by, updated_by, created_at, updated_at, latest_version, current_published_version
        FROM column_mappings
    """)
    # New columns default to NULL -> target_format NULL means "treat as
    # DB_TABLE" at the application layer (see models/column_mapping.py) —
    # every pre-existing row is a real connection-to-connection mapping,
    # unaffected by this migration beyond the constraint relaxation.
    cur.execute("DROP TABLE column_mappings")
    cur.execute("ALTER TABLE column_mappings_new RENAME TO column_mappings")
    cur.execute("CREATE INDEX ix_column_mappings_id ON column_mappings (id)")
    cur.execute("CREATE INDEX ix_column_mappings_organization_id ON column_mappings (organization_id)")
    print("column_mappings: rebuilt with source_connection_id/target_connection_id nullable + source_document_type/target_format added")


def rebuild_column_mapping_versions(cur):
    if _is_nullable(cur, "column_mapping_versions", "source_connection_id"):
        print("column_mapping_versions.source_connection_id already nullable, skipping.")
        return
    cur.execute("""
        CREATE TABLE column_mapping_versions_new (
            id INTEGER NOT NULL PRIMARY KEY,
            mapping_id INTEGER NOT NULL,
            version_number INTEGER NOT NULL,
            status VARCHAR(20) NOT NULL,
            mappings JSON NOT NULL,
            source_connection_id INTEGER,
            source_schema VARCHAR(200),
            source_object VARCHAR(200),
            source_document_type VARCHAR(50),
            target_connection_id INTEGER,
            target_schema VARCHAR(200),
            target_object VARCHAR(200),
            target_format VARCHAR(30),
            unmapped_column_policy VARCHAR(20) NOT NULL,
            batch_size INTEGER,
            on_conversion_failure VARCHAR(20) NOT NULL,
            change_description TEXT,
            validation_result JSON,
            source_column_count INTEGER,
            target_column_count INTEGER,
            created_by INTEGER,
            created_at DATETIME DEFAULT (CURRENT_TIMESTAMP) NOT NULL,
            CONSTRAINT uq_column_mapping_version UNIQUE (mapping_id, version_number),
            FOREIGN KEY(mapping_id) REFERENCES column_mappings (id),
            FOREIGN KEY(source_connection_id) REFERENCES connections (id),
            FOREIGN KEY(target_connection_id) REFERENCES connections (id)
        )
    """)
    cur.execute("""
        INSERT INTO column_mapping_versions_new (
            id, mapping_id, version_number, status, mappings,
            source_connection_id, source_schema, source_object,
            target_connection_id, target_schema, target_object,
            unmapped_column_policy, batch_size, on_conversion_failure,
            change_description, validation_result, source_column_count, target_column_count,
            created_by, created_at
        )
        SELECT id, mapping_id, version_number, status, mappings,
               source_connection_id, source_schema, source_object,
               target_connection_id, target_schema, target_object,
               unmapped_column_policy, batch_size, on_conversion_failure,
               change_description, validation_result, source_column_count, target_column_count,
               created_by, created_at
        FROM column_mapping_versions
    """)
    cur.execute("DROP TABLE column_mapping_versions")
    cur.execute("ALTER TABLE column_mapping_versions_new RENAME TO column_mapping_versions")
    cur.execute("CREATE INDEX ix_column_mapping_versions_status ON column_mapping_versions (status)")
    cur.execute("CREATE INDEX ix_column_mapping_versions_mapping_id ON column_mapping_versions (mapping_id)")
    cur.execute("CREATE INDEX ix_column_mapping_versions_id ON column_mapping_versions (id)")
    print("column_mapping_versions: rebuilt with source_connection_id/target_connection_id nullable + source_document_type/target_format added")


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("PRAGMA foreign_keys = OFF")
    try:
        rebuild_column_mappings(cur)
        rebuild_column_mapping_versions(cur)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        cur.execute("PRAGMA foreign_keys = ON")
        con.close()


if __name__ == "__main__":
    main()
