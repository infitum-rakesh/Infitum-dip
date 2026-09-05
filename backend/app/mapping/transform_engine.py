"""
transform_engine — a real, dialect-abstracted evaluator for a mapping
row's `transformation` SQL-expression string, applied directly to an
in-memory pandas DataFrame (CM6, spec sections 8/9/23).

Why this exists rather than re-running the mapping's SQL against a live
connection: a column_mapping_step pipeline node operates on whatever
DataFrame is already flowing through the graph at that point — which may
already have upstream Filter/Select/Calculated nodes applied, or may not
even be backed by a SQL-capable source at all (an uploaded file, a
Document Intelligence extraction). Re-querying the source database would
silently discard all of that upstream work and wouldn't work for a
non-SQL source in the first place. So the mapping's `transformation`
string — the exact same text app/mapping/mapper.py's build_preview_sql()
turns into real SQL for the standalone module's Preview/Run Load paths —
is instead evaluated here as a small, real, VECTORIZED pandas
implementation of the same expression, one logical function per
supported keyword (TRIM/UPPER/LOWER/CONCAT/CAST/COALESCE/CASE WHEN/||).
This is exactly the "abstraction, not hardcoded single-dialect SQL"
spec section 9 asks for — every one of these functions means the same
thing regardless of what database the mapping's source/target happen to
be, so there is nothing dialect-specific to abstract over in the first
place; the abstraction is "SQL expression -> real Python/pandas
operation", not "SQL expression -> per-vendor SQL text".

Deliberately NOT a general SQL parser — only the exact function
vocabulary spec section 8's own examples use is supported. An expression
outside that vocabulary raises TransformEvaluationError with the exact
unsupported fragment named, rather than silently mis-evaluating or
falling back to a no-op passthrough — matches this codebase's existing
mapper.py forbidden-keyword-guard precedent of failing loudly on
anything it can't safely handle.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd


class TransformEvaluationError(Exception):
    """A transformation expression could not be evaluated — either it
    references a column that doesn't exist, or it uses a construct this
    evaluator doesn't (yet) support."""


_CAST_RE = re.compile(r"^CAST\s*\((.+)\s+AS\s+([A-Za-z0-9_]+)\s*(?:\([^)]*\))?\)$", re.IGNORECASE | re.DOTALL)
_TRIM_RE = re.compile(r"^TRIM\s*\((.+)\)$", re.IGNORECASE | re.DOTALL)
_UPPER_RE = re.compile(r"^UPPER\s*\((.+)\)$", re.IGNORECASE | re.DOTALL)
_LOWER_RE = re.compile(r"^LOWER\s*\((.+)\)$", re.IGNORECASE | re.DOTALL)
_COALESCE_RE = re.compile(r"^COALESCE\s*\((.+)\)$", re.IGNORECASE | re.DOTALL)
_CONCAT_RE = re.compile(r"^CONCAT\s*\((.+)\)$", re.IGNORECASE | re.DOTALL)
_CASE_RE = re.compile(r"^CASE\s+WHEN\s+(.+?)\s+THEN\s+(.+?)\s+ELSE\s+(.+?)\s+END$", re.IGNORECASE | re.DOTALL)
_COMPARISON_RE = re.compile(r"^(.+?)\s*(!=|<>|>=|<=|=|>|<)\s*(.+)$", re.DOTALL)

_CAST_TYPE_MAP = {
    # Real, working coercions — every one of these is what the pipeline's
    # own Cast/Type node (_run_cast) already maps to, reused verbatim so
    # CAST(...) here means exactly the same thing it means everywhere
    # else in this app.
    "INT": "int64", "INTEGER": "int64", "BIGINT": "int64", "SMALLINT": "int64",
    "DECIMAL": "float64", "NUMERIC": "float64", "FLOAT": "float64", "DOUBLE": "float64", "REAL": "float64",
    "VARCHAR": "string", "CHAR": "string", "TEXT": "string", "STRING": "string",
    "BOOLEAN": "bool", "BOOL": "bool",
    "DATE": "datetime64[ns]", "DATETIME": "datetime64[ns]", "TIMESTAMP": "datetime64[ns]",
}


def _split_top_level(s: str, sep: str) -> list[str]:
    """Splits `s` on `sep` only at paren-depth 0 and outside quotes — the
    same "don't split inside a nested call or a string literal" rule a
    real SQL tokenizer applies. Used for both `||` concatenation and
    comma-separated function arguments."""
    parts, buf, depth, in_quote = [], "", 0, False
    i = 0
    while i < len(s):
        c = s[i]
        if c == "'" :
            in_quote = not in_quote
            buf += c
        elif not in_quote and c == "(":
            depth += 1
            buf += c
        elif not in_quote and c == ")":
            depth -= 1
            buf += c
        elif not in_quote and depth == 0 and s[i:i + len(sep)] == sep:
            parts.append(buf)
            buf = ""
            i += len(sep) - 1
        else:
            buf += c
        i += 1
    parts.append(buf)
    return [p.strip() for p in parts]


def _as_series(value, df: pd.DataFrame) -> pd.Series:
    """Broadcasts a scalar (literal string/number/None) to a full Series
    when needed, or returns a real Series unchanged."""
    if isinstance(value, pd.Series):
        return value
    return pd.Series([value] * len(df), index=df.index)


def _resolve_operand(df: pd.DataFrame, token: str) -> pd.Series | str | float | int | None:
    """A leaf operand: a quoted string literal, a numeric literal, NULL,
    a real column reference, or a nested function call (resolved via
    evaluate_expression recursively)."""
    token = token.strip()
    if not token:
        raise TransformEvaluationError("Empty operand in transformation expression.")
    if token.upper() == "NULL":
        return None
    if token.startswith("'") and token.endswith("'") and len(token) >= 2:
        return token[1:-1]
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    if "(" in token:
        return evaluate_expression(df, token)
    if token in df.columns:
        return df[token]
    raise TransformEvaluationError(f"Unknown column or literal '{token}' referenced in transformation expression.")


def _evaluate_condition(df: pd.DataFrame, cond: str) -> pd.Series:
    m = _COMPARISON_RE.match(cond.strip())
    if not m:
        raise TransformEvaluationError(f"Unsupported CASE WHEN condition: '{cond}' (only simple =, !=, >, <, >=, <= comparisons are supported).")
    left_raw, op, right_raw = m.groups()
    left = _as_series(_resolve_operand(df, left_raw), df)
    right = _resolve_operand(df, right_raw)
    right_series = _as_series(right, df) if not isinstance(right, pd.Series) else right
    if op == "=":
        return left == right_series
    if op in ("!=", "<>"):
        return left != right_series
    if op == ">":
        return left > right_series
    if op == "<":
        return left < right_series
    if op == ">=":
        return left >= right_series
    return left <= right_series  # "<="


def evaluate_expression(df: pd.DataFrame, expr: str) -> pd.Series:
    """Real, vectorized evaluation of a mapping row's `transformation`
    string against `df`. Returns a pandas Series aligned to df.index.
    Raises TransformEvaluationError for anything outside the supported
    vocabulary (see module docstring) — never guesses."""
    expr = expr.strip()

    concat_parts = _split_top_level(expr, "||")
    if len(concat_parts) > 1:
        parts = [_as_series(_resolve_operand(df, p), df).astype("string").fillna("") for p in concat_parts]
        result = parts[0]
        for p in parts[1:]:
            result = result + p
        return result

    m = _CASE_RE.match(expr)
    if m:
        cond_raw, then_raw, else_raw = m.groups()
        cond = _evaluate_condition(df, cond_raw)
        then_val = _resolve_operand(df, then_raw)
        else_val = _resolve_operand(df, else_raw)
        return pd.Series(np.where(cond, _as_series(then_val, df), _as_series(else_val, df)), index=df.index)

    m = _CAST_RE.match(expr)
    if m:
        inner, sql_type = m.groups()
        series = _as_series(_resolve_operand(df, inner), df)
        dtype = _CAST_TYPE_MAP.get(sql_type.strip().upper())
        if dtype is None:
            raise TransformEvaluationError(f"Unsupported CAST target type '{sql_type}'.")
        try:
            if dtype == "datetime64[ns]":
                return pd.to_datetime(series, errors="coerce")
            return series.astype(dtype)
        except (ValueError, TypeError) as exc:
            raise TransformEvaluationError(f"CAST to {sql_type} failed: {exc}") from exc

    m = _TRIM_RE.match(expr)
    if m:
        series = _as_series(_resolve_operand(df, m.group(1)), df)
        return series.astype("string").str.strip()

    m = _UPPER_RE.match(expr)
    if m:
        series = _as_series(_resolve_operand(df, m.group(1)), df)
        return series.astype("string").str.upper()

    m = _LOWER_RE.match(expr)
    if m:
        series = _as_series(_resolve_operand(df, m.group(1)), df)
        return series.astype("string").str.lower()

    m = _COALESCE_RE.match(expr)
    if m:
        args = _split_top_level(m.group(1), ",")
        result = _as_series(_resolve_operand(df, args[0]), df)
        for arg in args[1:]:
            fallback = _as_series(_resolve_operand(df, arg), df)
            result = result.where(result.notna(), fallback)
        return result

    m = _CONCAT_RE.match(expr)
    if m:
        args = _split_top_level(m.group(1), ",")
        parts = [_as_series(_resolve_operand(df, a), df).astype("string").fillna("") for a in args]
        result = parts[0]
        for p in parts[1:]:
            result = result + p
        return result

    # No wrapping function at all — a bare column reference or literal
    # used as the whole expression (e.g. a mapping row that sets
    # `transformation` to just a column name).
    resolved = _resolve_operand(df, expr)
    return _as_series(resolved, df)
