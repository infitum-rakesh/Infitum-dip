"""
Scheduled Mapping (Document Intelligence: Dynamic JSON Extraction spec,
section 7 — "Default Mapping for Scheduled Processing").

Schema changes:
  1. document_schedule_configs.mapping_id — the EXPLICIT mapping profile
     a schedule uses (section 7.2's highest-precedence option). Nullable:
     unset means "fall through to the org's document-type default, if
     any" at resolution time. Simple ADD COLUMN — no NOT NULL relaxation
     needed, no SQLite table-rebuild required (contrast
     migrate_doc_mapping_profiles.py, which did need that technique).
  2. document_mapped_outputs gets two new nullable columns (schedule_id,
     schedule_run_id) and one NOT NULL-with-default column (trigger) —
     "every scheduled execution must record... Schedule ID, Run ID"
     (section 7.4). A manual Apply-Mapping-Profile run (Document
     Details) leaves schedule_id/schedule_run_id NULL and trigger="MANUAL".
  3. document_mapping_defaults is a brand-new table (the org's default
     mapping profile per document_type — section 7.3).

Real finding while writing this migration: document_mapped_outputs does
NOT exist yet in the live dip.db at all — the dev backend process
serving this database was never restarted since the Mapping Profiles
phase added that model (same standing "another session owns the dev
server" constraint that's blocked live browser verification all
session), so init_db()'s create_all() never ran for it. Rather than
hand-write CREATE TABLE SQL for a table I can't directly observe running
code create, this migration creates document_mapped_outputs and
document_mapping_defaults via SQLAlchemy's own Table.create(checkfirst=True)
straight from the real ORM model definitions in app/models/ — guaranteed
byte-for-byte consistent with what create_all() would produce, no risk
of hand-SQL drift (wrong JSON affinity, wrong default clause, etc.). If
document_mapped_outputs already exists in some OTHER environment without
the three new columns (not the case here), the ADD-COLUMN fallback below
still applies defensively.

Idempotent throughout.
"""
import sqlite3

from sqlalchemy import create_engine

DB_PATH = "dip.db"


def _has_table(cur, table):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def _has_column(cur, table, column):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def add_schedule_config_mapping_id(cur):
    if _has_column(cur, "document_schedule_configs", "mapping_id"):
        print("document_schedule_configs.mapping_id already exists, skipping.")
        return
    cur.execute("ALTER TABLE document_schedule_configs ADD COLUMN mapping_id INTEGER")
    print("document_schedule_configs: added mapping_id")


def create_missing_tables_via_orm_metadata():
    """Creates document_mapped_outputs / document_mapping_defaults from
    the real ORM model definitions if they don't exist yet — see module
    docstring for why this is preferable to hand-written CREATE TABLE SQL.

    Imports app.main FIRST (for side effects only) — it transitively
    imports every model module, which is what finishes SQLAlchemy's
    declarative mapper configuration (Mapped[int]-style annotations only
    resolve to a real column type once the whole registry is configured).
    Without this, DocumentMappedOutput.mapping_id resolves to NullType
    and Table.create() fails with a real, reproducible CompileError —
    confirmed by hitting it directly while writing this script."""
    import app.main  # noqa: F401
    from app.models.document_mapped_output import DocumentMappedOutput
    from app.models.document_mapping_default import DocumentMappingDefault

    engine = create_engine(f"sqlite:///{DB_PATH}")
    DocumentMappedOutput.__table__.create(engine, checkfirst=True)
    DocumentMappingDefault.__table__.create(engine, checkfirst=True)
    engine.dispose()
    print("document_mapped_outputs / document_mapping_defaults: created (if missing) from current ORM model definitions.")


def add_mapped_output_schedule_columns_if_preexisting(cur):
    """Defensive ADD-COLUMN fallback for an environment where
    document_mapped_outputs already existed (pre-dating this migration)
    without the three new columns — not the case in this dev database,
    but kept so this script is safe to run anywhere."""
    if not _has_table(cur, "document_mapped_outputs"):
        return
    if _has_column(cur, "document_mapped_outputs", "schedule_id"):
        print("document_mapped_outputs.schedule_id already exists, skipping.")
        return
    cur.execute("ALTER TABLE document_mapped_outputs ADD COLUMN schedule_id INTEGER")
    cur.execute("ALTER TABLE document_mapped_outputs ADD COLUMN schedule_run_id INTEGER")
    cur.execute("ALTER TABLE document_mapped_outputs ADD COLUMN trigger VARCHAR(10) NOT NULL DEFAULT 'MANUAL'")
    print("document_mapped_outputs: added schedule_id, schedule_run_id, trigger (default MANUAL)")


def main():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        add_schedule_config_mapping_id(cur)
        con.commit()
    finally:
        con.close()

    create_missing_tables_via_orm_metadata()

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        add_mapped_output_schedule_columns_if_preexisting(cur)
        con.commit()
    finally:
        con.close()


if __name__ == "__main__":
    main()
