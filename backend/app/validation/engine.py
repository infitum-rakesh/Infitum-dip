"""
engine — turns a rule set into ONE real aggregate SQL query
(COUNT(*) plus one SUM(CASE WHEN <violation> THEN 1 ELSE 0 END) per
enabled rule, executed via the connector's own preview_sql — the same
SELECT/WITH-only, fetchmany-bounded execution path every other real query
surface in this codebase already goes through) and turns its one result
row into a per-rule pass/fail/warning verdict.

One query for every rule, not one query per rule — a rule set with 20
rules against a 100M-row table still does exactly one full scan, not 20.
"""
from __future__ import annotations

from app.validation.rules import RuleValidationError, build_unique_violation, build_violation_condition


def run_rule_set(connector, schema: str | None, object_name: str, rules: list[dict]) -> dict:
    """Returns {"total_records": int, "results": [ {rule_id, rule_name,
    rule_type, columns, severity, status, pass_count, fail_count,
    pass_rate, sql_condition, sql_joins}, ... ]}."""
    enabled = [r for r in (rules or []) if r.get("enabled", True)]
    if not enabled:
        raise RuleValidationError("No enabled rules to run.")

    quote_ident = connector.quote_identifier
    base_sql = connector.quote_object(object_name, schema)

    joins: list[str] = []
    conditions: dict[str, tuple[str, str]] = {}  # rule_id -> (condition_sql, joins_sql_for_this_rule)
    select_exprs = []
    for i, rule in enumerate(enabled):
        joins_before = len(joins)
        if rule.get("type") == "UNIQUE":
            cond = build_unique_violation(rule, quote_ident, base_sql)
        else:
            cond = build_violation_condition(rule, quote_ident, connector.connection_type, joins, i)
        # Only THIS rule's own join (if any) is needed to reproduce its
        # failing rows later — not every other rule's joins too.
        rule_joins_sql = " ".join(joins[joins_before:])
        conditions[rule["id"]] = (cond, rule_joins_sql)
        select_exprs.append(f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END) AS r_{i}")

    sql = f"SELECT COUNT(*) AS total_records, {', '.join(select_exprs)} FROM {base_sql} AS s"
    if joins:
        sql += " " + " ".join(joins)

    preview = connector.preview_sql(sql, {}, 1)
    if not preview.rows:
        raise RuleValidationError("The validation query returned no rows — the object may be empty or inaccessible.")
    row = dict(zip(preview.columns, preview.rows[0]))
    total = int(row["total_records"] or 0)

    results = []
    for i, rule in enumerate(enabled):
        fail = int(row.get(f"r_{i}") or 0)
        passed = max(0, total - fail)
        rate = round((passed / total) * 100, 2) if total else 100.0
        threshold = rule.get("threshold", 100) or 100
        severity = rule.get("severity", "error")
        if rate >= threshold:
            status = "PASSED"
        elif severity == "error":
            status = "FAILED"
        else:
            status = "WARNING"
        cond_sql, rule_joins_sql = conditions[rule["id"]]
        results.append({
            "rule_id": rule["id"], "rule_name": rule.get("name", rule["id"]), "rule_type": rule.get("type"),
            "columns": rule.get("columns") or [], "severity": severity,
            "status": status, "pass_count": passed, "fail_count": fail, "pass_rate": rate,
            "sql_condition": cond_sql, "sql_joins": rule_joins_sql,
        })
    return {"total_records": total, "results": results}


def failed_records_sql(connector, schema: str | None, object_name: str, sql_condition: str, sql_joins: str) -> str:
    base_sql = connector.quote_object(object_name, schema)
    sql = f"SELECT s.* FROM {base_sql} AS s"
    if sql_joins:
        sql += f" {sql_joins}"
    sql += f" WHERE {sql_condition}"
    return sql
