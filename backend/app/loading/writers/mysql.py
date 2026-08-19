"""
MySqlBulkWriter — batched multi-row INSERT via pymysql's own executemany
optimization. Verified directly against a live MySQL 8 container (see
Phase 6 completion notes): pymysql's Cursor.executemany() detects a
single-VALUES-tuple INSERT and rewrites the whole batch into ONE
"INSERT INTO t (...) VALUES (...),(...),..." statement — including when
an "ON DUPLICATE KEY UPDATE ..." clause follows the VALUES list — so this
is a genuine one-round-trip bulk write per batch, not pymysql silently
looping single-row inserts underneath.
"""
from __future__ import annotations

from app.connectors.base import ConnectorValidationError
from app.loading.writers.base import BaseBulkWriter


class MySqlBulkWriter(BaseBulkWriter):
    def write_batch(
        self, schema: str | None, table: str, columns: list[str], rows: list[dict],
        key_columns: list[str] | None = None,
    ) -> int:
        if not rows:
            return 0
        engine = self.connector.engine(pool_size=2)
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            # MySQL has no cross-database-qualified "schema.table" concept
            # the way Postgres/SQL Server do — `schema` here is unused
            # (MySQL's connector config picks the database at connect
            # time), matching how connectors/database/mysql.py already
            # treats schema as a no-op for this dialect.
            quoted_table = self.connector.quote_object(table, None)
            quoted_cols = ", ".join(self.connector.quote_identifier(c) for c in columns)
            placeholders = ", ".join(["%s"] * len(columns))
            sql = f"INSERT INTO {quoted_table} ({quoted_cols}) VALUES ({placeholders})"

            if key_columns:
                missing = [k for k in key_columns if k not in columns]
                if missing:
                    raise ConnectorValidationError(f"Upsert key column(s) not in the loaded column set: {', '.join(missing)}.")
                update_cols = [c for c in columns if c not in key_columns]
                if update_cols:
                    set_clause = ", ".join(
                        f"{self.connector.quote_identifier(c)} = VALUES({self.connector.quote_identifier(c)})"
                        for c in update_cols
                    )
                    sql += f" ON DUPLICATE KEY UPDATE {set_clause}"
                else:
                    # Every loaded column is a key column — nothing to
                    # update on conflict; IGNORE keeps a re-run idempotent
                    # instead of raising a duplicate-key error.
                    sql = sql.replace("INSERT INTO", "INSERT IGNORE INTO", 1)

            params = [tuple(r.get(c) for c in columns) for r in rows]
            cur.executemany(sql, params)
            raw.commit()
            return len(rows)
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
