"""
PostgresBulkWriter — batched multi-row INSERT via psycopg2.extras.execute_
values, obtained from the connector's own pooled engine's raw DBAPI
connection (connectors/database/base.py's engine()/pool_manager.py — same
pool every other Postgres operation uses, no separate connection path).

Deliberately NOT COPY FROM STDIN, even though COPY is the textbook
"fastest Postgres bulk load" answer. COPY's TEXT/CSV wire format needs
hand-rolled per-value serialization and NULL-vs-empty-string escaping
(get that subtly wrong — e.g. an empty string silently becoming NULL —
and it's a real data-corruption bug that's easy to miss in testing).
execute_values still gets genuine multi-row-per-round-trip performance
(one INSERT statement covers the whole batch) while reusing psycopg2's
own parameter binding — the same type-safe path every other write in
this codebase already goes through. A future pass could add a real COPY
writer if profiling shows execute_values isn't fast enough; nothing here
blocks that.
"""
from __future__ import annotations

from psycopg2.extras import execute_values

from app.connectors.base import ConnectorValidationError
from app.loading.writers.base import BaseBulkWriter


class PostgresBulkWriter(BaseBulkWriter):
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
            quoted_cols = ", ".join(self.connector.quote_identifier(c) for c in columns)
            sql = f"INSERT INTO {quoted_table} ({quoted_cols}) VALUES %s"

            if key_columns:
                missing = [k for k in key_columns if k not in columns]
                if missing:
                    raise ConnectorValidationError(f"Upsert key column(s) not in the loaded column set: {', '.join(missing)}.")
                quoted_keys = ", ".join(self.connector.quote_identifier(c) for c in key_columns)
                update_cols = [c for c in columns if c not in key_columns]
                if update_cols:
                    set_clause = ", ".join(
                        f"{self.connector.quote_identifier(c)} = EXCLUDED.{self.connector.quote_identifier(c)}"
                        for c in update_cols
                    )
                    sql += f" ON CONFLICT ({quoted_keys}) DO UPDATE SET {set_clause}"
                else:
                    sql += f" ON CONFLICT ({quoted_keys}) DO NOTHING"

            values = [[r.get(c) for c in columns] for r in rows]
            execute_values(cur, sql, values, page_size=len(values))
            raw.commit()
            return len(rows)
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()
