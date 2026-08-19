"""
SqlServerBulkWriter — hand-rolled multi-row INSERT/MERGE, chunked at
SQL Server's hard 1000-row-per-VALUES-clause limit (a real T-SQL syntax
limit, not a tunable). Not pymssql's executemany(): verified directly
against a live SQL Server container that, unlike pymysql, pymssql's
executemany() has no multi-row-batching optimization — it would issue
one round trip per row, which isn't a genuine bulk write. Building the
multi-row VALUES list ourselves (still using pymssql's %s placeholder
substitution for every value — never string-formatted literals) is what
actually gets one statement per batch of up to 1000 rows.

No native SQL Server BULK INSERT / bcp here: both require the source
file to be readable by the SQL Server engine's own host filesystem
(or an accessible network share), which this backend — talking to an
arbitrary remote SQL Server over the wire — has no way to guarantee.
Disclosed, not faked.
"""
from __future__ import annotations

from app.connectors.base import ConnectorValidationError
from app.loading.writers.base import BaseBulkWriter

_MAX_ROWS_PER_STATEMENT = 1000  # SQL Server's hard limit for a VALUES row constructor


def _chunks(rows: list[dict], size: int):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


class SqlServerBulkWriter(BaseBulkWriter):
    def write_batch(
        self, schema: str | None, table: str, columns: list[str], rows: list[dict],
        key_columns: list[str] | None = None,
    ) -> int:
        if not rows:
            return 0
        if key_columns:
            missing = [k for k in key_columns if k not in columns]
            if missing:
                raise ConnectorValidationError(f"Upsert key column(s) not in the loaded column set: {', '.join(missing)}.")

        engine = self.connector.engine(pool_size=2)
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            quoted_table = self.connector.quote_object(table, schema)
            quoted_cols = [self.connector.quote_identifier(c) for c in columns]
            written = 0
            for chunk in _chunks(rows, _MAX_ROWS_PER_STATEMENT):
                if key_columns:
                    self._merge_chunk(cur, quoted_table, columns, quoted_cols, chunk, key_columns)
                else:
                    self._insert_chunk(cur, quoted_table, quoted_cols, chunk, columns)
                written += len(chunk)
            raw.commit()
            return written
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()

    def _insert_chunk(self, cur, quoted_table, quoted_cols, chunk, columns):
        row_placeholder = "(" + ", ".join(["%s"] * len(columns)) + ")"
        values_clause = ", ".join([row_placeholder] * len(chunk))
        sql = f"INSERT INTO {quoted_table} ({', '.join(quoted_cols)}) VALUES {values_clause}"
        params = tuple(v for r in chunk for v in (r.get(c) for c in columns))
        cur.execute(sql, params)

    def _merge_chunk(self, cur, quoted_table, columns, quoted_cols, chunk, key_columns):
        row_placeholder = "(" + ", ".join(["%s"] * len(columns)) + ")"
        values_clause = ", ".join([row_placeholder] * len(chunk))
        src_cols = [self.connector.quote_identifier(c) for c in columns]
        on_clause = " AND ".join(f"t.{self.connector.quote_identifier(k)} = s.{self.connector.quote_identifier(k)}" for k in key_columns)
        update_cols = [c for c in columns if c not in key_columns]
        update_clause = ", ".join(
            f"t.{self.connector.quote_identifier(c)} = s.{self.connector.quote_identifier(c)}" for c in update_cols
        )
        when_matched = f"WHEN MATCHED THEN UPDATE SET {update_clause}\n" if update_cols else ""
        sql = (
            f"MERGE INTO {quoted_table} AS t\n"
            f"USING (VALUES {values_clause}) AS s({', '.join(src_cols)})\n"
            f"ON {on_clause}\n"
            f"{when_matched}"
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(quoted_cols)}) VALUES ({', '.join('s.' + c for c in src_cols)});"
        )
        params = tuple(v for r in chunk for v in (r.get(c) for c in columns))
        cur.execute(sql, params)
