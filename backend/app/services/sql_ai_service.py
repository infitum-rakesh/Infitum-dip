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
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import ConnectorError, ConnectorUnsupportedError
from app.models.user import User
from app.services import ai_quota_service, connection_service, sql_validator
from app.services.ai_connection_access_service import ai_connection_access_service

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


async def _get_ai_connector(db: AsyncSession, ai_connection_id: int, organization_id: int | None, acting_user: User | None = None):
    conn = await connection_service.get_ai_connection(db, ai_connection_id, organization_id)
    if conn is None:
        raise SqlAiError("AI connection not found (it may have been deleted, or belongs to a different organization).")
    if conn.category.value != "AI_PROVIDER":
        raise SqlAiError(f"Connection '{conn.name}' is a {conn.category.value} connection, not an AI Provider — pick one from the AI Agent category.")
    # Same mandatory authorization gate as every other AI call site — see
    # ai_connection_access_service.py's module docstring. The SQL Workspace
    # AI Assistant always has a real interactive user (unlike the document
    # pipeline's automated contexts), so acting_user is expected non-None here.
    access = await ai_connection_access_service.check_access(
        db, organization_id=organization_id, acting_user=acting_user, connection_id=ai_connection_id, required="use",
    )
    if not access.granted:
        raise SqlAiError(access.denial_message)
    try:
        connector = connection_service.build_connector(conn)
    except ConnectorError as exc:
        raise SqlAiError(str(exc)) from exc
    return connector, conn


async def _generate_json(
    db: AsyncSession, connector, conn, *, organization_id: int | None, user_id: int | None,
    prompt: str, system: str, max_tokens: int,
) -> dict:
    """Same one-retry recovery as extraction_service._generate_json —
    hands the model its own bad response and asks it to correct itself
    before failing outright. See module docstring for why this isn't a
    shared import across the two services. Also mirrors that module's
    quota enforcement (AIC-P7): one reservation/one request_id spans
    both real calls of this single logical attempt."""
    try:
        reservation = await ai_quota_service.check_and_reserve(
            db, organization_id=organization_id, connection=conn, user_id=user_id, prompt=prompt, max_tokens=max_tokens,
        )
    except ai_quota_service.AiQuotaExceededError as exc:
        raise SqlAiError(str(exc)) from exc

    request_id = str(uuid.uuid4())
    usage_sum = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    usage_seen = False

    def _accumulate() -> None:
        nonlocal usage_seen
        u = connector.last_usage
        if u:
            usage_seen = True
            usage_sum["input_tokens"] += u.get("input_tokens") or 0
            usage_sum["output_tokens"] += u.get("output_tokens") or 0
            usage_sum["total_tokens"] += u.get("total_tokens") or 0

    async def _commit(status: str, error_message: str | None):
        await ai_quota_service.commit_usage(
            db, reservation=reservation, organization_id=organization_id, connection_id=conn.id, user_id=user_id,
            feature="SQL_WORKSPACE_NL_TO_SQL", request_id=request_id, usage=usage_sum if usage_seen else None,
            status=status, error_message=error_message,
        )

    try:
        raw = await asyncio.to_thread(connector.generate, prompt, system=system, max_tokens=max_tokens)
    except ConnectorError as exc:
        await ai_quota_service.release(db, reservation)
        raise SqlAiError(str(exc)) from exc
    _accumulate()
    try:
        result = _parse_json(raw)
    except SqlAiError:
        result = None

    if result is not None:
        await _commit("SUCCESS", None)
        return result

    repair_prompt = (
        f"{prompt}\n\n---\nYour previous response was NOT valid JSON — it started with:\n"
        f"{(raw or '')[:300]!r}\n\nRespond again with ONLY valid JSON exactly as instructed above. "
        "No prose, no explanation, no markdown code fences — the response must start with {."
    )
    try:
        raw2 = await asyncio.to_thread(connector.generate, repair_prompt, system=system, max_tokens=max_tokens)
    except ConnectorError as exc:
        await _commit("FAILED", str(exc))
        raise SqlAiError(str(exc)) from exc
    _accumulate()
    try:
        result = _parse_json(raw2)
    except SqlAiError as exc:
        await _commit("FAILED", str(exc))
        raise
    await _commit("SUCCESS", None)
    return result


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
    question: str, schema_name: str | None, current_sql: str | None, acting_user: User | None = None,
) -> dict:
    if not (question or "").strip():
        raise SqlAiError("Ask a question first.")

    conn = await connection_service.get_connection(db, connection_id, organization_id)
    if conn is None:
        raise SqlAiError(f"Connection {connection_id} was not found.")

    schema_context = await build_schema_context(conn, schema_name)
    ai_connector, ai_conn = await _get_ai_connector(db, ai_connection_id, organization_id, acting_user)

    prompt_parts = [
        f"SQL DIALECT: {_dialect_label(conn.connection_type.value)}",
        f"SCHEMA:\n{schema_context}",
    ]
    if (current_sql or "").strip():
        prompt_parts.append(f"CURRENT QUERY IN EDITOR (the user may be asking to modify this):\n{current_sql.strip()}")
    prompt_parts.append(f"USER REQUEST: {question.strip()}")
    prompt = "\n\n".join(prompt_parts)

    acting_user_id = acting_user.id if acting_user is not None else None
    parsed = await _generate_json(
        db, ai_connector, ai_conn, organization_id=organization_id, user_id=acting_user_id,
        prompt=prompt, system=NL_TO_SQL_SYSTEM, max_tokens=1200,
    )
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
