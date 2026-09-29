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

import json
import re
from sqlalchemy import inspect
from app.connectors.base import ColumnInfo, ConnectorError, ConnectorValidationError
from app.loading.writers.base import BaseBulkWriter, normalize_identifier

_ORACLE_SIZE_RE = re.compile(r"\((\d+)\s*(?:BYTE|CHAR)?\)")


def _describe_oversized_column(engine, schema, table, columns, row) -> str | None:
    """Best-effort column-name attribution for a single-row Oracle insert
    failure. Oracle/oracledb's own driver error for a bind-size violation
    names a bind position (or nothing at all) — never the target column —
    so without this, both the job log and the RejectedRecord entry say
    only 'insert failed' with no hint which field was too long. Compares
    each value's real serialized length against that column's actual
    reflected Oracle type/size (VARCHAR2/CHAR/NVARCHAR2 have a real hard
    limit; CLOB/NCLOB/BLOB don't) and, when one clearly exceeds it,
    returns a message naming it to prepend to the real driver error.
    Returns None when nothing obviously exceeds its column's size (a
    constraint violation, wrong data type, etc. keep the unmodified
    driver message) — only called when a batch has already been isolated
    to a single row, so this reflection call is never on the hot path."""
    try:
        reflected = {c["name"]: str(c["type"]).upper() for c in inspect(engine).get_columns(table, schema=schema)}
    except Exception:
        return None
    culprits = []
    for col in columns:
        type_str = reflected.get(col)
        if not type_str or "CLOB" in type_str or "BLOB" in type_str:
            continue
        m = _ORACLE_SIZE_RE.search(type_str)
        if not m:
            continue
        limit = int(m.group(1))
        value = row.get(col)
        if value is None:
            continue
        length = len(str(value))
        if length > limit:
            culprits.append(f"column '{col}' value is {length:,} chars but target type is {type_str} (max {limit:,})")
    return "; ".join(culprits) if culprits else None


def _serialize_oracle_cell(v):
    if v == "" or v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def _oracle_type_for_column_info(col: ColumnInfo) -> str:
    dt = (col.data_type or "string").lower()
    length = col.max_length or 4000
    if dt in ("string", "phone", "email", "url", "picklist", "multipicklist", "encryptedstring", "combobox"):
        length = 4000
    if length > 4000:
        length = 4000

    type_map = {
        "id": "VARCHAR2(18)",
        "string": f"VARCHAR2({min(length, 4000)})",
        "textarea": "CLOB",
        "phone": f"VARCHAR2({min(length, 4000)})",
        "email": f"VARCHAR2({min(length, 4000)})",
        "url": f"VARCHAR2({min(length, 4000)})",
        "picklist": f"VARCHAR2({min(length, 4000)})",
        "multipicklist": f"VARCHAR2({min(length, 4000)})",
        "boolean": "VARCHAR2(5)",
        "int": "VARCHAR2(100)",
        "double": "VARCHAR2(100)",
        "currency": "VARCHAR2(100)",
        "percent": "VARCHAR2(100)",
        "date": "VARCHAR2(10)",
        "datetime": "VARCHAR2(30)",
        "time": "VARCHAR2(10)",
        "base64": "CLOB",
        "reference": "VARCHAR2(18)",
        "address": "CLOB",
        "location": "CLOB",
        "encryptedstring": f"VARCHAR2({min(length, 4000)})",
        "combobox": f"VARCHAR2({min(length, 4000)})",
        "anytype": "CLOB",
        "longtextarea": "CLOB",
        "clob": "CLOB",
        "nclob": "CLOB",
        "blob": "CLOB",
    }
    return type_map.get(dt, f"VARCHAR2({min(length, 4000)})")


class OracleBulkWriter(BaseBulkWriter):
    def ensure_table_from_columns(
        self, schema: str | None, table: str, columns: list[ColumnInfo], *, recreate: bool = False,
        key_columns: list[str] | None = None, target_column_names: dict[str, str] | None = None,
        recreate_incompatible: bool = False,
    ) -> str:
        if not columns:
            raise ValueError(f"Cannot create table '{table}' — no source columns given.")

        table = normalize_identifier(table)
        key_set = {normalize_identifier(k) for k in (key_columns or [])}
        names = {k: normalize_identifier(v) for k, v in (target_column_names or {}).items()}
        resolved_columns = [(names.get(c.name, normalize_identifier(c.name)), c) for c in columns]

        engine = self.connector.engine(pool_size=1)
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            if schema:
                cur.execute(
                    "SELECT COUNT(*) FROM all_tables WHERE UPPER(owner) = UPPER(:1) AND UPPER(table_name) = UPPER(:2)",
                    [schema, table],
                )
            else:
                cur.execute("SELECT COUNT(*) FROM user_tables WHERE UPPER(table_name) = UPPER(:1)", [table])
            exists = bool(cur.fetchone()[0])

            if exists and not recreate:
                should_recreate = False
                if recreate_incompatible:
                    if schema:
                        cur.execute(
                            "SELECT column_name, data_type FROM all_tab_columns WHERE UPPER(owner) = UPPER(:1) AND UPPER(table_name) = UPPER(:2)",
                            [schema, table],
                        )
                    else:
                        cur.execute(
                            "SELECT column_name, data_type FROM user_tab_columns WHERE UPPER(table_name) = UPPER(:1)",
                            [table],
                        )
                    existing = {normalize_identifier(row[0]): str(row[1]).upper() for row in cur.fetchall()}
                    for target_name, col in resolved_columns:
                        existing_type = existing.get(target_name)
                        expected_type = _oracle_type_for_column_info(col).upper()
                        if not existing_type:
                            should_recreate = True
                            break
                        if expected_type not in ("CLOB", "BLOB") and existing_type in ("CLOB", "NCLOB", "BLOB"):
                            should_recreate = True
                            break
                if not should_recreate:
                    return "exists"
                recreate = True

            quoted_table = self.connector.quote_object(table, schema)
            if exists and recreate:
                cur.execute(f"DROP TABLE {quoted_table}")
                raw.commit()

            column_sql = []
            for target_name, col in resolved_columns:
                quoted_name = self.connector.quote_identifier(target_name)
                col_type = _oracle_type_for_column_info(col)
                primary = " PRIMARY KEY" if (target_name in key_set or col.primary_key) else ""
                column_sql.append(f"{quoted_name} {col_type}{primary}")
            cur.execute(f"CREATE TABLE {quoted_table} ({', '.join(column_sql)})")
            raw.commit()
            return "recreated" if exists else "created"
        except Exception:
            raw.rollback()
            raise
        finally:
            raw.close()

    def write_batch(
        self, schema: str | None, table: str, columns: list[str], rows: list[dict],
        key_columns: list[str] | None = None,
    ) -> int:
        if not rows:
            return 0

        # Normalize identifiers to lowercase matching ensure_table/ensure_table_from_columns DDL
        # and clean empty string values to None for proper SQL NULL bindings in Oracle.
        table = normalize_identifier(table)
        col_map = {c: normalize_identifier(c) for c in columns}
        columns = [col_map[c] for c in columns]
        key_columns = [col_map.get(k, normalize_identifier(k)) for k in key_columns] if key_columns else key_columns
        rows = [{col_map.get(k, normalize_identifier(k)): _serialize_oracle_cell(v) for k, v in row.items()} for row in rows]

        engine = self.connector.engine(pool_size=2)
        raw = engine.raw_connection()
        try:
            cur = raw.cursor()
            if hasattr(cur, "bindarraysize"):
                cur.bindarraysize = len(rows)
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
        except Exception as exc:
            raw.rollback()
            if len(rows) == 1:
                culprit = _describe_oversized_column(engine, schema, table, columns, rows[0])
                if culprit:
                    raise ConnectorError(f"{culprit} — {exc}") from exc
            raise
        finally:
            raw.close()
