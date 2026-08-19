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


def validate_mapping(mappings: list[dict], source_columns: list[dict], target_columns: list[dict]) -> dict:
    source_names = {c["name"] for c in source_columns}
    target_names = {c["name"] for c in target_columns}
    target_types = {c["name"]: c.get("data_type", "") for c in target_columns}
    source_types = {c["name"]: c.get("data_type", "") for c in source_columns}

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
    for t in target_columns:
        if t["name"] not in mapped_targets:
            issues.append({"row_id": None, "severity": "INFO", "message": f"Target column '{t['name']}' has no mapping."})

    if any(v == "CONFLICT" for v in row_status.values()):
        overall = "CONFLICT"
    elif any(v == "WARNING" for v in row_status.values()):
        overall = "WARNING"
    else:
        overall = "VALIDATED"

    return {"issues": issues, "row_status": row_status, "overall_status": overall}


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
