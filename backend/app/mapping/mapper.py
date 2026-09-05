"""
mapper — validation and the preview SQL builder for a mapping definition
(spec sections 15 "Validation" column, 18 data type compatibility, 34
"Validate Mapping"/"Preview" buttons).

validate_mapping() never executes anything — it's pure Python checking
the mapping rows against the (already live-fetched) source/target column
lists: duplicate target columns (a real conflict — two rows can't both
own the same target), references to columns that don't exist (schema
drift since the mapping was last touched), and type-compatibility
warnings from datatype_mapper.

build_preview_sql() is the one place a mapping definition becomes real
SQL — see its docstring for the trust boundary (transformation
expressions are the same kind of user-authored SQL fragment Phase 2's
custom-SQL preview already allowed for this same authenticated,
permissioned user; a keyword/statement guard here is defense-in-depth,
not the primary safety mechanism).
"""
from __future__ import annotations

import re

from app.mapping.datatype_mapper import INCOMPATIBLE, WARNING, compatibility


class MapperValidationError(Exception):
    """Bad mapping definition — can't be turned into SQL or saved as-is."""


_FORBIDDEN_PATTERN = re.compile(
    r";|--|/\*|\b(DROP|DELETE|INSERT|UPDATE|ALTER|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE|CREATE)\b",
    re.IGNORECASE,
)


def validate_mapping(
    mappings: list[dict], source_columns: list[dict], target_columns: list[dict],
    *, allow_target_defaults: bool = False,
) -> dict:
    """allow_target_defaults — CM2/spec section 27: a required (NOT NULL)
    target column left unmapped is always a blocking ERROR, UNLESS the
    column has a real database default *and* the caller has explicitly
    opted into trusting it (allow_target_defaults=True) — never inferred
    silently. Column dicts should carry "nullable"/"default" when
    available (see mapping_service.py::columns_to_dicts()); columns
    missing that key are treated as nullable (no ERROR raised) rather
    than guessed as required, since guessing wrong the other way would
    block a mapping over metadata this function was never given."""
    source_names = {c["name"] for c in source_columns}
    target_names = {c["name"] for c in target_columns}
    target_types = {c["name"]: c.get("data_type", "") for c in target_columns}
    source_types = {c["name"]: c.get("data_type", "") for c in source_columns}
    target_nullable = {c["name"]: c.get("nullable", True) for c in target_columns}
    target_default = {c["name"]: c.get("default") for c in target_columns}

    issues = []
    row_status: dict[str, str] = {}

    target_usage: dict[str, list[str]] = {}
    for m in mappings:
        target_usage.setdefault(m["target_column"], []).append(m["id"])

    for m in mappings:
        row_id = m["id"]
        tgt = m["target_column"]
        severity_here = "OK"

        if len(target_usage.get(tgt, [])) > 1:
            issues.append({"row_id": row_id, "severity": "ERROR", "message": f"Target column '{tgt}' is mapped by more than one row."})
            severity_here = "CONFLICT"

        if tgt not in target_names:
            issues.append({"row_id": row_id, "severity": "ERROR", "message": f"Target column '{tgt}' no longer exists on the target object."})
            severity_here = "CONFLICT"

        for src in (m.get("source_columns") or []):
            if src not in source_names:
                issues.append({"row_id": row_id, "severity": "ERROR", "message": f"Source column '{src}' no longer exists on the source object."})
                severity_here = "CONFLICT"

        if m.get("lookup"):
            lk = m["lookup"]
            if not (lk.get("table") and lk.get("key_column") and lk.get("return_column")):
                issues.append({"row_id": row_id, "severity": "ERROR", "message": "Lookup mapping is missing table/key column/return column."})
                severity_here = "CONFLICT"
            elif not (m.get("source_columns")):
                issues.append({"row_id": row_id, "severity": "ERROR", "message": "Lookup mapping needs a source column to join on."})
                severity_here = "CONFLICT"

        if m.get("transformation") and _FORBIDDEN_PATTERN.search(m["transformation"]):
            issues.append({"row_id": row_id, "severity": "ERROR", "message": "Transformation contains a disallowed SQL keyword or statement separator."})
            severity_here = "CONFLICT"

        # CM2/spec section 30 — a business/natural key must actually come
        # from source data (renamed, transformed, or looked-up); a literal
        # constant can never distinguish one record from another, so
        # flagging one is_key is very likely a config mistake, not a real
        # SCD1/SCD2 key. WARNING, not ERROR — a constant *component* of a
        # composite key alongside real key columns is a legitimate (if
        # unusual) pattern this shouldn't hard-block.
        if m.get("is_key") and m.get("constant_value") is not None:
            issues.append({"row_id": row_id, "severity": "WARNING", "message": f"'{tgt}' is marked as a business key but is mapped from a constant value — constants can't distinguish records for change detection."})
            severity_here = "WARNING" if severity_here == "OK" else severity_here

        if severity_here != "CONFLICT" and len(m.get("source_columns") or []) == 1 and not m.get("lookup") and tgt in target_types:
            src = m["source_columns"][0]
            compat = compatibility(source_types.get(src, ""), target_types[tgt])
            if compat["status"] == INCOMPATIBLE:
                issues.append({"row_id": row_id, "severity": "ERROR", "message": f"{src} ({compat['source_family']}) -> {tgt} ({compat['target_family']}): {compat['reason']}"})
                severity_here = "CONFLICT"
            elif compat["status"] == WARNING and not m.get("transformation"):
                issues.append({"row_id": row_id, "severity": "WARNING", "message": f"{src} -> {tgt}: {compat['reason']} Consider adding a CAST/conversion expression."})
                severity_here = "WARNING" if severity_here == "OK" else severity_here

        row_status[row_id] = severity_here

    mapped_targets = {m["target_column"] for m in mappings}
    unmapped_required = []
    for t in target_columns:
        if t["name"] in mapped_targets:
            continue
        is_required = not target_nullable.get(t["name"], True)
        has_default = target_default.get(t["name"]) is not None
        if is_required and not (has_default and allow_target_defaults):
            # spec section 27 — blocking, not informational: a NOT NULL
            # target column silently left unmapped would insert NULL into
            # a column the database itself refuses to accept, failing the
            # load anyway but only after real rows have started moving.
            # Catch it at validate/publish time instead.
            issues.append({"row_id": None, "severity": "ERROR", "message": f"Target column '{t['name']}' is NOT NULL but has no source mapping and no usable default."})
            unmapped_required.append(t["name"])
        else:
            issues.append({"row_id": None, "severity": "INFO", "message": f"Target column '{t['name']}' has no mapping."})

    # unmapped_required issues carry row_id=None (they're not about any one
    # mapping row), so they can't flow through row_status — folded into the
    # overall rollup directly instead.
    if any(v == "CONFLICT" for v in row_status.values()) or unmapped_required:
        overall = "CONFLICT"
    elif any(v == "WARNING" for v in row_status.values()):
        overall = "WARNING"
    else:
        overall = "VALIDATED"

    return {"issues": issues, "row_status": row_status, "overall_status": overall}


def validate_key_range_mapping(mappings: list[dict]) -> None:
    """KEY_RANGE Run Load mode (JJ3/JJ4 — execution/load_executor.py) reads
    rows via a plain key-range chunk fetch (connectors' read_key_range_chunk),
    not a real SQL engine — for a source like Salesforce that has no SQL
    engine to begin with. A mapping row needing one (a lookup JOIN, or a
    raw SQL transformation expression) can't be honored there. Raises a
    clear, disclosed error naming the offending column(s) rather than
    silently dropping the lookup/transformation or evaluating it wrong."""
    offenders = [m["target_column"] for m in mappings if m.get("lookup") or m.get("transformation")]
    if offenders:
        raise MapperValidationError(
            "This mapping uses a lookup or transformation on: " + ", ".join(offenders) + " — those need a "
            "real SQL engine to evaluate and aren't supported for a source that only supports key-range "
            "reads (e.g. Salesforce). Use a plain column mapping for those columns, or run this load "
            "against a SQL-capable source instead."
        )


def build_row_transform(mappings: list[dict]):
    """Builds a plain-Python per-row transform for the KEY_RANGE load path
    (execution/load_worker.py's run_key_range_partition) — no SQL engine
    involved, so this does in Python what build_preview_sql's generated
    SQL's column aliasing/COALESCE does server-side for the SQL_STREAM
    path: renames each fetched source field to its mapped target column,
    substitutes constant_value, and falls back to default_value when the
    source value is NULL. Every mapping row must already have passed
    validate_key_range_mapping() (no lookup/transformation) before this
    is used."""
    def _transform(row: dict) -> dict:
        out = {}
        for m in mappings:
            target = m["target_column"]
            if m.get("constant_value") is not None:
                out[target] = m["constant_value"]
                continue
            value = row.get(m["source_columns"][0]) if m.get("source_columns") else None
            if value is None and m.get("default_value") is not None:
                value = m["default_value"]
            out[target] = value
        return out
    return _transform


def build_preview_sql(quote_object, quote_ident, source_schema: str | None, source_object: str, mappings: list[dict]) -> str:
    """
    quote_object(object_name, schema) / quote_ident(name) — dialect-aware
    identifier quoting, supplied by the caller (mapping_service.py) via
    the connector's own _quote_object / identifier_preparer so this stays
    decoupled from any specific connector implementation.
    """
    if not mappings:
        raise MapperValidationError("No mapping rows to preview.")

    base = quote_object(source_object, source_schema)
    joins: list[str] = []
    select_exprs = []
    for i, m in enumerate(mappings):
        expr = _row_expression(m, quote_ident, joins, i)
        select_exprs.append(f'{expr} AS {quote_ident(m["target_column"])}')

    sql = f"SELECT {', '.join(select_exprs)} FROM {base} AS s"
    if joins:
        sql += " " + " ".join(joins)
    return sql


def _row_expression(m: dict, quote_ident, joins: list[str], index: int) -> str:
    lookup = m.get("lookup")
    if lookup:
        key_col = m["source_columns"][0] if m.get("source_columns") else None
        if not key_col:
            raise MapperValidationError(f"Lookup mapping for '{m['target_column']}' needs a source column to join on.")
        alias = f"lk{index}"
        joins.append(
            f'LEFT JOIN {quote_ident(lookup["table"])} AS {alias} '
            f'ON s.{quote_ident(key_col)} = {alias}.{quote_ident(lookup["key_column"])}'
        )
        expr = f'{alias}.{quote_ident(lookup["return_column"])}'
    elif m.get("constant_value") is not None:
        expr = _sql_literal(m["constant_value"])
    elif m.get("transformation"):
        if _FORBIDDEN_PATTERN.search(m["transformation"]):
            raise MapperValidationError(f"Transformation for '{m['target_column']}' contains disallowed SQL.")
        expr = m["transformation"]
    elif m.get("source_columns"):
        expr = f's.{quote_ident(m["source_columns"][0])}'
    else:
        expr = "NULL"

    if m.get("default_value") is not None:
        expr = f'COALESCE({expr}, {_sql_literal(m["default_value"])})'
    return expr


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"
