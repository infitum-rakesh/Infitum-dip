"""
OracleBulkWriter — batched array-DML via python-oracledb's executemany(),
obtained from the connector's own pooled engine's raw DBAPI connection
(connectors/database/base.py's engine()/pool_manager.py — same pool every
other Oracle operation uses, no separate connection path).

executemany() with a list-of-lists batch IS Oracle's real bulk-DML
primitive from a Python client — the oracledb driver sends the whole
batch as one array-bind round trip, not one INSERT per row. This is the
same "genuine multi-row-per-round-trip" performance rationale
postgres.py's execute_values() uses, and (like that writer) deliberately
NOT SQL*Loader, which needs an external flat file + a separate OS utility
this codebase has no process-shelling path for.

Upsert is a real, vendor-native MERGE — Oracle has no ON CONFLICT/ON
DUPLICATE KEY syntax, MERGE is the documented mechanism. The USING
(SELECT :1 AS col1, :2 AS col2, ... FROM dual) shape lets the whole batch
still go through executemany() (verified live: one MERGE statement,
positional binds, batched via executemany — not a fetch-then-compare
emulation, not one MERGE per row issued in a loop).

Positional binds (:1, :2, ...) are used throughout rather than named
binds — the column list is already the single source of truth for bind
order on both the values-tuple side and the SQL text side, so there's no
separate name-matching step that could drift.
"""
from __future__ import annotations

from app.connectors.base import ConnectorValidationError
from app.loading.writers.base import BaseBulkWriter


class OracleBulkWriter(BaseBulkWriter):
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
            quoted_table = self.connector.quote_object(table, schema)
            quoted_cols = [self.connector.quote_identifier(c) for c in columns]

            if key_columns:
                missing = [k for k in key_columns if k not in columns]
                if missing:
                    raise ConnectorValidationError(f"Upsert key column(s) not in the loaded column set: {', '.join(missing)}.")
                key_idx = {c: i for i, c in enumerate(columns) if c in key_columns}
                update_cols = [c for c in columns if c not in key_columns]

                using_select = ", ".join(f":{i + 1} AS {quoted_cols[i]}" for i in range(len(columns)))
                on_clause = " AND ".join(f"t.{quoted_cols[i]} = s.{quoted_cols[i]}" for i in key_idx.values())
                sql = f"MERGE INTO {quoted_table} t USING (SELECT {using_select} FROM dual) s ON ({on_clause})"
                if update_cols:
                    set_clause = ", ".join(f"t.{self.connector.quote_identifier(c)} = s.{self.connector.quote_identifier(c)}" for c in update_cols)
                    sql += f" WHEN MATCHED THEN UPDATE SET {set_clause}"
                insert_cols = ", ".join(quoted_cols)
                insert_vals = ", ".join(f"s.{c}" for c in quoted_cols)
                sql += f" WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals})"
            else:
                placeholders = ", ".join(f":{i + 1}" for i in range(len(columns)))
                sql = f"INSERT INTO {quoted_table} ({', '.join(quoted_cols)}) VALUES ({placeholders})"

            values = [[r.get(c) for c in columns] for r in rows]
            cur.executemany(sql, values)
            raw.commit()
            return len(rows)
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
