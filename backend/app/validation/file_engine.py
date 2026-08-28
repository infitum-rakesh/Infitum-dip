"""
file_engine — pure-Python rule evaluator for an uploaded-file-backed
ValidationRuleSet. validation/engine.py's one-aggregate-SQL-query approach
doesn't apply here — a file connector has no SQL to push a rule into — so
this streams the file once (via the same RowSource-shaped
.get_columns()/.iter_rows() interface reconciliation/engine.py already
defines, reused rather than reinvented) and evaluates every enabled rule
against each row in Python, accumulating pass/fail counts in a single
pass regardless of how many rules are enabled — same "one query/one scan
covers every rule" property engine.py has for DB objects.

Only NOT_NULL / RANGE / FORMAT / UNIQUE are supported. REF_INTEGRITY needs
a real JOIN against another object and SQL_CONDITION needs an arbitrary
boolean SQL expression — neither has a meaningful file-only equivalent, so
both are refused with a clear, disclosed error (validation_service.py
surfaces it before the run even starts) rather than silently skipped or
approximated.
"""
from __future__ import annotations

import re

from app.validation.rules import RuleValidationError

UNSUPPORTED_FILE_RULE_TYPES = {"REF_INTEGRITY", "SQL_CONDITION"}

# UNIQUE rules hold one occurrence-count per distinct key in memory for the
# whole pass — bounded by distinct key cardinality, not row count, but
# still needs a real, disclosed ceiling rather than an unbounded dict.
MAX_FILE_VALIDATION_ROWS = 500_000


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_violates(rule: dict, row: dict) -> bool:
    """UNIQUE is NOT handled here — it needs the whole pass, not a
    per-row verdict — see run_rule_set()/failed_records() below."""
    rtype = rule.get("type")
    cols = rule.get("columns") or []
    if not cols:
        raise RuleValidationError(f"Rule '{rule.get('name', rule.get('id'))}' has no column configured.")
    col = cols[0]
    value = row.get(col)

    if rtype == "NOT_NULL":
        return value is None or (isinstance(value, str) and value.strip() == "")

    if rtype == "RANGE":
        if value is None:
            return False  # NULLs exempt from range checks — same convention as validation/engine.py
        cfg = rule.get("config") or {}
        n = _num(value)
        if n is None:
            return True  # non-numeric value in a numeric range check is itself a violation
        if cfg.get("min") not in (None, "") and n < float(cfg["min"]):
            return True
        if cfg.get("max") not in (None, "") and n > float(cfg["max"]):
            return True
        return False

    if rtype == "FORMAT":
        pattern = (rule.get("config") or {}).get("pattern") or ""
        if not pattern:
            raise RuleValidationError(f"Rule '{rule.get('name', rule.get('id'))}' (Format) needs a regex pattern.")
        if value is None:
            return False
        try:
            return re.search(pattern, str(value)) is None
        except re.error as exc:
            raise RuleValidationError(f"Rule '{rule.get('name', rule.get('id'))}' has an invalid regex pattern: {exc}")

    if rtype in UNSUPPORTED_FILE_RULE_TYPES:
        raise RuleValidationError(_unsupported_message(rule))

    raise RuleValidationError(f"Unknown or unsupported rule type '{rtype}'.")


def _unsupported_message(rule: dict) -> str:
    name = rule.get("name", rule.get("id", "?"))
    rtype = rule.get("type")
    return (
        f"Rule '{name}' ({rtype}) isn't supported for uploaded files — it needs real relational SQL. "
        "Use NOT_NULL, RANGE, FORMAT, or UNIQUE for file-backed rule sets, or point this rule set at a "
        "database object instead."
    )


def assert_rules_supported(rules: list[dict]) -> None:
    """Raised up front (before streaming the file) so an unsupported rule
    fails fast with a clear message instead of erroring mid-scan."""
    for rule in rules or []:
        if not rule.get("enabled", True):
            continue
        if rule.get("type") in UNSUPPORTED_FILE_RULE_TYPES:
            raise RuleValidationError(_unsupported_message(rule))


def run_rule_set(source, rules: list[dict]) -> dict:
    """`source` is a RowSource (reconciliation/engine.py) — a real
    FileRowSource wrapping the uploaded file's own LocalFileConnector.
    Returns the same shape validation/engine.py's run_rule_set() does, so
    validation_service.py can persist either result identically."""
    enabled = [r for r in (rules or []) if r.get("enabled", True)]
    if not enabled:
        raise RuleValidationError("No enabled rules to run.")
    assert_rules_supported(enabled)

    all_columns = source.get_columns()
    needed_columns = [c for c in all_columns if c in {col for r in enabled for col in (r.get("columns") or [])}]
    unique_rule_ids = {r["id"] for r in enabled if r.get("type") == "UNIQUE"}
    unique_counts: dict[str, dict[tuple, int]] = {rid: {} for rid in unique_rule_ids}

    total = 0
    fail_counts = {r["id"]: 0 for r in enabled}
    for chunk in source.iter_rows(needed_columns or all_columns):
        for row in chunk:
            total += 1
            if total > MAX_FILE_VALIDATION_ROWS:
                raise RuleValidationError(
                    f"This file has more than {MAX_FILE_VALIDATION_ROWS:,} rows — file-backed validation "
                    "isn't supported at this scale yet."
                )
            for rule in enabled:
                if rule["id"] in unique_rule_ids:
                    cols = rule.get("columns") or []
                    key = tuple(row.get(c) for c in cols)
                    if any(v is None for v in key):
                        continue  # NULL key columns exempt — same convention as validation/engine.py
                    counts = unique_counts[rule["id"]]
                    counts[key] = counts.get(key, 0) + 1
                    continue
                if _row_violates(rule, row):
                    fail_counts[rule["id"]] += 1

    for rid in unique_rule_ids:
        fail_counts[rid] = sum(c for c in unique_counts[rid].values() if c > 1)

    results = []
    for rule in enabled:
        fail = fail_counts[rule["id"]]
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
        results.append({
            "rule_id": rule["id"], "rule_name": rule.get("name", rule["id"]), "rule_type": rule.get("type"),
            "columns": rule.get("columns") or [], "severity": severity,
            "status": status, "pass_count": passed, "fail_count": fail, "pass_rate": rate,
            # No SQL was ever run — kept empty (not omitted) so this result
            # row has the exact same shape as a DB-backed one.
            "sql_condition": "", "sql_joins": "",
        })
    return {"total_records": total, "results": results}


def failed_records(source, rule: dict, limit: int) -> tuple[list[str], list[list]]:
    """Streams the file again (bounded by the same cap run_rule_set()
    enforces) and returns up to `limit` example rows violating `rule`.
    UNIQUE needs two passes — one to find which keys actually repeat, one
    to collect example rows for those keys — everything else needs one."""
    all_columns = source.get_columns()
    rtype = rule.get("type")

    if rtype in UNSUPPORTED_FILE_RULE_TYPES:
        raise RuleValidationError(_unsupported_message(rule))

    if rtype == "UNIQUE":
        cols = rule.get("columns") or []
        counts: dict[tuple, int] = {}
        for chunk in source.iter_rows(cols):
            for row in chunk:
                key = tuple(row.get(c) for c in cols)
                if any(v is None for v in key):
                    continue
                counts[key] = counts.get(key, 0) + 1
        dup_keys = {k for k, c in counts.items() if c > 1}
        out_rows: list[list] = []
        for chunk in source.iter_rows(all_columns):
            for row in chunk:
                key = tuple(row.get(c) for c in cols)
                if key in dup_keys:
                    out_rows.append([row.get(c) for c in all_columns])
                    if len(out_rows) >= limit:
                        return all_columns, out_rows
        return all_columns, out_rows

    out_rows = []
    for chunk in source.iter_rows(all_columns):
        for row in chunk:
            if _row_violates(rule, row):
                out_rows.append([row.get(c) for c in all_columns])
                if len(out_rows) >= limit:
                    return all_columns, out_rows
    return all_columns, out_rows
