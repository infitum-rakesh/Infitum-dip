"""
sql_ai_service — Natural Language → SQL for the SQL Workspace AI
Assistant panel. Calls whichever AI Provider connection (OpenAI/
Anthropic/Ollama — connectors/ai/*, all real HTTP calls, same connectors
Document Intelligence's AI Extraction already uses) the caller selects,
given REAL schema context introspected from the target database
connection (connection_service.list_objects/get_columns — the same
introspection DatabaseExplorer.jsx already displays). A different
question, schema, or model genuinely produces a different prompt and a
different real response — nothing here is canned.

The AI NEVER executes SQL. generate_sql() only returns proposed SQL text
for the caller to review and explicitly Insert/Run themselves — sql_
validator.classify_statement() tags the result with its real command/
category so the frontend can warn before a destructive statement is run.

Response parsing (_parse_json) is defensive for the same reason as
extraction_service.py's: LLMs don't always obey "respond with ONLY
JSON". One corrective retry is attempted before giving up — same proven
pattern, this module keeps its own copy rather than sharing a private
helper across unrelated services.
"""
from __future__ import annotations

import asyncio
import json
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, ConnectorUnsupportedError
from app.services import connection_service, sql_validator

MAX_TABLES_IN_CONTEXT = 25       # bounds prompt size when a schema is very wide
MAX_COLUMNS_PER_TABLE = 40
SCHEMA_OBJECT_SEARCH_LIMIT = 200  # how many objects list_objects() is asked to return before bounding


class SqlAiError(Exception):
    """Bad request — AI connection not found/not an AI Provider, target
    connection not found, or the model's response couldn't be salvaged
    into usable SQL even after the corrective retry."""


# ── JSON response parsing (defensive — see module docstring) ───────

def _parse_json(text: str):
    text = (text or "").strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    candidates = [text]
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        candidates.append(brace_match.group(0))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise SqlAiError(f"The AI model's response wasn't valid JSON: {text[:400]!r}")


async def _get_ai_connector(db: AsyncSession, ai_connection_id: int, organization_id: int | None):
    conn = await connection_service.get_connection(db, ai_connection_id, organization_id)
    if conn is None:
        raise SqlAiError("AI connection not found (it may have been deleted, or belongs to a different organization).")
    if conn.category.value != "AI_PROVIDER":
        raise SqlAiError(f"Connection '{conn.name}' is a {conn.category.value} connection, not an AI Provider — pick one from the AI Agent category.")
    try:
        return connection_service.build_connector(conn)
    except ConnectorError as exc:
        raise SqlAiError(str(exc)) from exc


async def _generate_json(connector, prompt: str, system: str, max_tokens: int) -> dict:
    """Same one-retry recovery as extraction_service._generate_json —
    hands the model its own bad response and asks it to correct itself
    before failing outright. See module docstring for why this isn't a
    shared import across the two services."""
    try:
        raw = await asyncio.to_thread(connector.generate, prompt, system=system, max_tokens=max_tokens)
    except ConnectorError as exc:
        raise SqlAiError(str(exc)) from exc
    try:
        return _parse_json(raw)
    except SqlAiError:
        pass

    repair_prompt = (
        f"{prompt}\n\n---\nYour previous response was NOT valid JSON — it started with:\n"
        f"{(raw or '')[:300]!r}\n\nRespond again with ONLY valid JSON exactly as instructed above. "
        "No prose, no explanation, no markdown code fences — the response must start with {."
    )
    try:
        raw2 = await asyncio.to_thread(connector.generate, repair_prompt, system=system, max_tokens=max_tokens)
    except ConnectorError as exc:
        raise SqlAiError(str(exc)) from exc
    return _parse_json(raw2)


# ── Real schema-context building ────────────────────────────────────

async def build_schema_context(conn, schema_name: str | None) -> str:
    """Compact `table_name(col TYPE [PK] [FK->other.col], ...)` text block
    built from REAL introspection of the target connection — not a guess,
    not a cached/stale copy. Bounded to MAX_TABLES_IN_CONTEXT tables and
    MAX_COLUMNS_PER_TABLE columns per table to keep the prompt a sane
    size against a very wide production schema; a truncation note is
    appended (never silently dropped) so the model — and the user
    reading the explanation — knows the context isn't exhaustive."""
    try:
        objects = await connection_service.list_objects(conn, schema_name, None)
    except ConnectorUnsupportedError as exc:
        raise SqlAiError(str(exc)) from exc
    objects = list(objects)[:SCHEMA_OBJECT_SEARCH_LIMIT]
    truncated_tables = len(objects) > MAX_TABLES_IN_CONTEXT
    objects = objects[:MAX_TABLES_IN_CONTEXT]

    lines = []
    for obj in objects:
        try:
            columns, _ = await connection_service.get_columns(conn, obj.name, obj.schema, False)
        except ConnectorError:
            continue  # a single unreadable object shouldn't fail the whole context
        cols = list(columns)
        truncated_cols = len(cols) > MAX_COLUMNS_PER_TABLE
        cols = cols[:MAX_COLUMNS_PER_TABLE]
        col_parts = []
        for c in cols:
            tags = []
            if c.primary_key:
                tags.append("PK")
            if c.foreign_key:
                fk = c.foreign_key
                fk_table = f"{fk['schema']}.{fk['table']}" if fk.get("schema") else fk["table"]
                tags.append(f"FK->{fk_table}.{fk['column']}")
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            col_parts.append(f"{c.name} {c.data_type}{tag_str}")
        if truncated_cols:
            col_parts.append(f"...{len(columns) - MAX_COLUMNS_PER_TABLE} more columns not shown")
        qualified = f"{obj.schema}.{obj.name}" if obj.schema else obj.name
        lines.append(f"{qualified}({', '.join(col_parts)})")

    if not lines:
        raise SqlAiError("This connection has no readable tables/views to build SQL against.")
    if truncated_tables:
        lines.append(f"... schema has more tables not shown (only the first {MAX_TABLES_IN_CONTEXT} are listed above)")
    return "\n".join(lines)


# ── NL → SQL ─────────────────────────────────────────────────────────

def _dialect_label(connection_type: str) -> str:
    return connection_type.replace("_", " ").title()


NL_TO_SQL_SYSTEM = (
    "You are a SQL assistant embedded in a database IDE. Given a real database schema and a "
    "user's natural-language request, write ONE SQL statement that answers it, written for the "
    "exact SQL dialect named in the prompt. Use ONLY the tables and columns listed in the schema — "
    "never invent a table or column name that isn't shown. If the request is genuinely ambiguous "
    "or needs a table/column that isn't in the schema, still return your best-effort SQL but say so "
    'plainly in "warnings".\n\n'
    "Respond with ONLY a JSON object, no prose, no markdown fences, exactly these keys: "
    '{"sql": "the single SQL statement, no trailing semicolon needed", "explanation": "one or two '
    'plain-English sentences on what the query does", "warnings": ["short strings — empty array if '
    'none; use this for ambiguity, assumptions made, or if the request implies a destructive '
    'INSERT/UPDATE/DELETE/DDL statement"]}'
)


async def generate_sql(
    db: AsyncSession, *, organization_id: int | None, connection_id: int, ai_connection_id: int,
    question: str, schema_name: str | None, current_sql: str | None,
) -> dict:
    if not (question or "").strip():
        raise SqlAiError("Ask a question first.")

    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise SqlAiError(f"Connection {connection_id} was not found.")

    schema_context = await build_schema_context(conn, schema_name)
    ai_connector = await _get_ai_connector(db, ai_connection_id, organization_id)

    prompt_parts = [
        f"SQL DIALECT: {_dialect_label(conn.connection_type.value)}",
        f"SCHEMA:\n{schema_context}",
    ]
    if (current_sql or "").strip():
        prompt_parts.append(f"CURRENT QUERY IN EDITOR (the user may be asking to modify this):\n{current_sql.strip()}")
    prompt_parts.append(f"USER REQUEST: {question.strip()}")
    prompt = "\n\n".join(prompt_parts)

    parsed = await _generate_json(ai_connector, prompt, NL_TO_SQL_SYSTEM, 1200)
    if not isinstance(parsed, dict) or not parsed.get("sql"):
        raise SqlAiError("The AI model didn't return a usable SQL statement.")

    sql_text = str(parsed["sql"]).strip().rstrip(";").strip()
    explanation = str(parsed.get("explanation") or "")
    warnings = [str(w) for w in (parsed.get("warnings") or []) if str(w).strip()]

    try:
        classification = sql_validator.validate(sql_text)
    except sql_validator.SqlValidationError as exc:
        raise SqlAiError(f"The AI model's response wasn't safe to use: {exc}") from exc

    if classification["command"] in sql_validator.DESTRUCTIVE_COMMANDS:
        warnings.append(f"This is a {classification['command']} statement — it will modify or remove real data if you run it. Review carefully before executing.")

    model_name = ai_connector.config.get("model") or ai_connector.connection_type
    return {
        "sql": sql_text,
        "explanation": explanation,
        "warnings": warnings,
        "statement_command": classification["command"],
        "statement_category": classification["category"],
        "model": str(model_name),
    }
