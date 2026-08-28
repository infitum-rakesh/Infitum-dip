"""
QueryExecutionLog — a real, append-only audit record of every SQL
Workspace execution attempt (run_sql calls only — Explain and the AI
Assistant's generate-sql don't touch a connection's real data, so they
aren't logged here). One row per attempt, success or failure: this is
the audit trail the History/Export/Security phase's "audit logging of
every execution" requirement points at, and the data source for the
Query History panel. Never edited after insert.

sql_text is capped (SQL_TEXT_CAP) rather than truncated silently in the
UI — a very long generated/pasted statement still gets a genuinely
useful audit record, just not an unbounded one row's worth of text.
error_message goes through the same connector-level secret-scrubbing
every other persisted error message in this codebase uses (see
sql_workspace_service.run_sql()) before it ever reaches this table.
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase

SQL_TEXT_CAP = 4000


class QueryExecutionLog(TenantBase):
    __tablename__ = "query_execution_logs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    statement_command: Mapped[str] = mapped_column(String(30), nullable=False)   # SELECT, INSERT, DELETE, DROP, UNKNOWN, ...
    statement_category: Mapped[str] = mapped_column(String(20), nullable=False)  # READ, WRITE, DDL, DCL, UNKNOWN
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="MANUAL")  # MANUAL | AI_GENERATED

    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # SUCCESS | ERROR
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # rows returned (SELECT) or rows affected (write)
    duration_ms: Mapped[float | None] = mapped_column(nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)  # scrubbed — see module docstring

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    def __repr__(self) -> str:
        return f"<QueryExecutionLog {self.statement_command} conn={self.connection_id} status={self.status}>"
