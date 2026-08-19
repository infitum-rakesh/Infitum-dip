"""
rules — turns one validation rule row (see models/validation.py for the
row shape) into a real boolean SQL fragment that is TRUE exactly when a
row VIOLATES the rule, evaluated against the base object aliased 's'.

Same trust boundary as mapping/mapper.py's build_preview_sql(): FORMAT and
SQL_CONDITION carry user-authored SQL fragments (a regex pattern / a
boolean expression) from this same authenticated, permissioned user who
could already run arbitrary SELECT via Preview — the forbidden-keyword
guard here is defense-in-depth, not the primary safety mechanism.

REF_INTEGRITY only supports a reference table on the SAME connection as
the object being validated — a real, disclosed scope limit. Checking a
foreign key against a table on a DIFFERENT connection would need pulling
that connection's whole key set into memory (the same bounded-set
technique reconciliation/engine.py uses for cross-connection key
comparison) rather than a single JOIN; not implemented here.
"""
from __future__ import annotations

import re

_FORBIDDEN_PATTERN = re.compile(
    r";|--|/\*|\b(DROP|DELETE|INSERT|UPDATE|ALTER|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE|CREATE)\b",
    re.IGNORECASE,
)

# Native regex match operator per dialect — SQL Server has none built in
# (would need CLR or a LIKE-based approximation that silently mishandles
# most real regex patterns), so FORMAT rules are refused there rather
# than faked with a wrong operator.
_REGEX_OPERATOR = {"POSTGRESQL": "~", "MYSQL": "REGEXP"}

RULE_TYPES = {"NOT_NULL", "RANGE", "FORMAT", "UNIQUE", "REF_INTEGRITY", "SQL_CONDITION"}


class RuleValidationError(Exception):
    """Bad rule definition — can't be turned into SQL."""


def _num_literal(value) -> str:
    try:
        return str(float(value)) if not float(value).is_integer() else str(int(float(value)))
    except (TypeError, ValueError):
        raise RuleValidationError(f"'{value}' is not a valid number.")


def _str_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _first_column(rule: dict) -> str:
    cols = rule.get("columns") or []
    if not cols:
        raise RuleValidationError(f"Rule '{rule.get('name', rule.get('id'))}' has no column configured.")
    return cols[0]


def build_violation_condition(rule: dict, quote_ident, connection_type: str, joins: list[str], index: int) -> str:
    """Returns (condition_sql). May append a LEFT JOIN to `joins`
    (REF_INTEGRITY only) — same technique mapping/mapper.py's lookup
    joins use. UNIQUE is NOT handled here — see build_unique_violation()."""
    rtype = rule.get("type")
    name = rule.get("name", rule.get("id", "?"))

    if rtype == "NOT_NULL":
        col = f"s.{quote_ident(_first_column(rule))}"
        return f"{col} IS NULL"

    if rtype == "RANGE":
        col = f"s.{quote_ident(_first_column(rule))}"
        cfg = rule.get("config") or {}
        clauses = []
        if cfg.get("min") not in (None, ""):
            clauses.append(f"{col} < {_num_literal(cfg['min'])}")
        if cfg.get("max") not in (None, ""):
            clauses.append(f"{col} > {_num_literal(cfg['max'])}")
        if not clauses:
            raise RuleValidationError(f"Rule '{name}' (Range) needs a min and/or max value.")
        # NULLs are exempt from range checks on their own — pair with a
        # NOT_NULL rule on the same column if nulls should also fail.
        return f"({col} IS NOT NULL AND ({' OR '.join(clauses)}))"

    if rtype == "FORMAT":
        op = _REGEX_OPERATOR.get(connection_type)
        if op is None:
            raise RuleValidationError(f"Format/Regex rules aren't supported against {connection_type} — no portable native regex operator.")
        col = f"s.{quote_ident(_first_column(rule))}"
        pattern = (rule.get("config") or {}).get("pattern") or ""
        if not pattern:
            raise RuleValidationError(f"Rule '{name}' (Format) needs a regex pattern.")
        return f"({col} IS NOT NULL AND NOT ({col} {op} {_str_literal(pattern)}))"

    if rtype == "REF_INTEGRITY":
        col = f"s.{quote_ident(_first_column(rule))}"
        cfg = rule.get("config") or {}
        ref_object, ref_column = cfg.get("ref_object"), cfg.get("ref_column")
        if not (ref_object and ref_column):
            raise RuleValidationError(f"Rule '{name}' (Referential Integrity) needs a reference table and column.")
        alias = f"ref{index}"
        ref_schema = cfg.get("ref_schema")
        ref_sql = f"{quote_ident(ref_schema)}.{quote_ident(ref_object)}" if ref_schema else quote_ident(ref_object)
        joins.append(f"LEFT JOIN {ref_sql} AS {alias} ON {col} = {alias}.{quote_ident(ref_column)}")
        return f"({col} IS NOT NULL AND {alias}.{quote_ident(ref_column)} IS NULL)"

    if rtype == "SQL_CONDITION":
        expr = ((rule.get("config") or {}).get("sql") or "").strip()
        if not expr:
            raise RuleValidationError(f"Rule '{name}' (SQL Condition) needs a boolean expression that valid rows must satisfy.")
        if _FORBIDDEN_PATTERN.search(expr):
            raise RuleValidationError(f"Rule '{name}' contains disallowed SQL.")
        # A row VIOLATES the rule when the expression is false OR unknown
        # (SQL's three-valued logic) — an indeterminate result never
        # silently counts as a pass.
        return f"NOT COALESCE(({expr}), FALSE)"

    raise RuleValidationError(f"Unknown or unsupported rule type '{rtype}'.")


def build_unique_violation(rule: dict, quote_ident, base_object_sql: str) -> str:
    """UNIQUE (optionally composite-key) — needs a correlated subquery
    re-referencing the same object, not a simple per-row boolean, so it's
    built separately from build_violation_condition(). NULL key columns
    are exempt (documented convention: NULLs never count as duplicates of
    each other here) — pair with NOT_NULL if that's not the intent."""
    cols = rule.get("columns") or []
    if not cols:
        raise RuleValidationError(f"Rule '{rule.get('name', rule.get('id'))}' (Uniqueness) needs at least one column.")
    alias = "dup"
    match_clause = " AND ".join(f"{alias}.{quote_ident(c)} = s.{quote_ident(c)}" for c in cols)
    not_null_guard = " AND ".join(f"s.{quote_ident(c)} IS NOT NULL" for c in cols)
    return f"({not_null_guard} AND (SELECT COUNT(*) FROM {base_object_sql} AS {alias} WHERE {match_clause}) > 1)"
