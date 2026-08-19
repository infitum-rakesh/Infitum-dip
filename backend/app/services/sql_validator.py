"""
sql_validator — server-side single-statement SQL classifier for the SQL
Workspace. Used today by the AI SQL Assistant (sql_ai_service.py) to tag
every AI-proposed statement with a real command/category *before* it's
shown to the user — never trusting the model's own claim about what it
wrote. Deliberately a standalone module, not inlined into sql_ai_service,
because the upcoming role/environment-scoped SQL_WORKSPACE_DDL/DML
permission gates (see task tracker's History/Export/Security phase) will
call validate() directly against user-typed SQL too, not just
AI-generated SQL — this is the one place that logic will live.

classify_statement() looks at the real leading keyword of the statement
(after stripping comments/whitespace) — never guessed from AI metadata,
never assumed safe by default. An unrecognized leading token classifies
as UNKNOWN, not READ.
"""
from __future__ import annotations

import re

# command -> category. WITH covers CTEs, which almost always precede a
# SELECT but can legally precede INSERT/UPDATE/DELETE too (a "writable
# CTE") — classified as READ here since that's overwhelmingly the common
# case and the underlying DML keyword still appears later in the text;
# a mis-classification here only affects the advisory badge shown to the
# user, never actual execution privileges (run_sql() itself is the real
# enforcement boundary, unchanged by this module).
COMMAND_CATEGORY = {
    "SELECT": "READ", "WITH": "READ", "SHOW": "READ", "EXPLAIN": "READ",
    "DESCRIBE": "READ", "DESC": "READ", "PRAGMA": "READ",
    "INSERT": "WRITE", "UPDATE": "WRITE", "DELETE": "WRITE", "MERGE": "WRITE",
    "REPLACE": "WRITE", "UPSERT": "WRITE",
    "CREATE": "DDL", "ALTER": "DDL", "DROP": "DDL", "TRUNCATE": "DDL",
    "RENAME": "DDL",
    "GRANT": "DCL", "REVOKE": "DCL",
}

# Commands severe enough to warrant a hard warning even before any
# allow/block-list config exists (that config is the future Security
# phase's job — this is just an honest label, not an enforcement rule).
DESTRUCTIVE_COMMANDS = {"DELETE", "DROP", "TRUNCATE", "UPDATE", "ALTER"}


class SqlValidationError(Exception):
    """Raised by validate() — empty SQL, more than one statement, or a
    command excluded by an explicit allow/block list."""


def _strip_leading_noise(sql: str) -> str:
    """Strips leading whitespace and SQL comments (-- line comments and
    /* block comments */) so the real first keyword can be found even
    when a query starts with an explanatory comment — a real pattern for
    AI-generated SQL, which often opens with `-- Find top customers`."""
    s = sql
    while True:
        s = s.lstrip()
        if s.startswith("--"):
            nl = s.find("\n")
            s = s[nl + 1:] if nl != -1 else ""
            continue
        if s.startswith("/*"):
            end = s.find("*/")
            s = s[end + 2:] if end != -1 else ""
            continue
        break
    return s


def classify_statement(sql: str) -> dict:
    """Returns {"command": "SELECT", "category": "READ"}. An empty
    statement or one whose first token isn't a recognized SQL command
    classifies as {"command": "UNKNOWN", "category": "UNKNOWN"} — never
    silently treated as a safe read."""
    stripped = _strip_leading_noise(sql or "")
    match = re.match(r"[A-Za-z]+", stripped)
    command = match.group(0).upper() if match else ""
    category = COMMAND_CATEGORY.get(command, "UNKNOWN")
    return {"command": command or "UNKNOWN", "category": category}


def split_statements(sql: str) -> list[str]:
    """String-literal- and comment-aware split on top-level `;` —
    deliberately NOT a naive sql.split(';'), which would incorrectly
    break on a semicolon inside a string literal (e.g. a WHERE clause
    matching "a;b") or a comment. Returns non-empty, whitespace-trimmed
    statement texts only (a trailing `;` or trailing whitespace produces
    no extra empty entry)."""
    statements: list[str] = []
    buf: list[str] = []
    has_content = False  # True once buf holds real SQL, not just whitespace/comments
    i, n = 0, len(sql or "")
    in_single = in_double = False
    while i < n:
        c = sql[i]
        if in_single:
            buf.append(c)
            has_content = True
            if c == "'" and sql[i + 1:i + 2] == "'":  # escaped '' inside a string
                buf.append(sql[i + 1])
                i += 2
                continue
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            buf.append(c)
            has_content = True
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "-" and sql[i + 1:i + 2] == "-":
            nl = sql.find("\n", i)
            chunk = sql[i:nl] if nl != -1 else sql[i:]
            buf.append(chunk)  # comment text — deliberately NOT marked as content
            i = nl if nl != -1 else n
            continue
        if c == "/" and sql[i + 1:i + 2] == "*":
            end = sql.find("*/", i + 2)
            chunk = sql[i:end + 2] if end != -1 else sql[i:]
            buf.append(chunk)  # comment text — deliberately NOT marked as content
            i = (end + 2) if end != -1 else n
            continue
        if c == "'":
            in_single = True
            buf.append(c)
            has_content = True
            i += 1
            continue
        if c == '"':
            in_double = True
            buf.append(c)
            has_content = True
            i += 1
            continue
        if c == ";":
            text = "".join(buf).strip()
            if has_content and text:
                statements.append(text)
            buf = []
            has_content = False
            i += 1
            continue
        if not c.isspace():
            has_content = True
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if has_content and tail:
        statements.append(tail)
    return statements


def validate(
    sql: str, *, allowed_commands: set[str] | None = None, blocked_commands: set[str] | None = None,
) -> dict:
    """Raises SqlValidationError for empty SQL, more than one statement,
    or (only when the caller passes them) a command outside
    allowed_commands / inside blocked_commands. Both lists are None by
    default — no restriction — real per-role/environment enforcement is
    the upcoming Security phase's job; this just gives it one correct
    place to plug into. Returns classify_statement()'s result on success."""
    statements = split_statements(sql)
    if not statements:
        raise SqlValidationError("SQL text is empty.")
    if len(statements) > 1:
        raise SqlValidationError(f"Only one statement is allowed at a time — found {len(statements)}.")
    result = classify_statement(statements[0])
    command = result["command"]
    if blocked_commands and command in blocked_commands:
        raise SqlValidationError(f"{command} statements are not permitted here.")
    if allowed_commands and command not in allowed_commands:
        raise SqlValidationError(f"{command} statements are not permitted here.")
    return result
