"""
SqlWorkspaceQuery — a saved query (Phase 9: SQL Workspace).

Just the query text tied to a connection, same "flexible, minimal"
shape as every other saved-definition model in this codebase
(ValidationRuleSet, ReconciliationJob, ColumnMapping) — see
models/validation.py for the fuller rationale. Running a query is
stateless (POST /sql-workspace/connections/{id}/run returns results
directly in the response, same as Preview elsewhere) — no run history
is persisted here; a saved row is only ever the query TEXT itself.

is_shared (History/Export/Security phase) makes a query visible in the
org-wide "Shared" tab for every teammate, not just its owner — the
"My" tab is just created_by == caller, always true regardless of this
flag. False by default: saving a query is private until the owner
explicitly shares it. Only the owner (or an elevated role — see
api/sql_workspace.py's _ELEVATED_ROLES) may edit/delete/reshare a row;
see sql_workspace_service._ensure_owner_or_elevated.
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import Base


class SqlWorkspaceQuery(Base):
    __tablename__ = "sql_workspace_queries"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id"), nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String(200), nullable=True)  # informational only — not enforced against sql_text
    sql_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<SqlWorkspaceQuery {self.name} (connection={self.connection_id})>"


class SqlWorkspaceQueryFavorite(Base):
    """Per-user favorite marker on a saved query — a many-to-many join,
    not a boolean on SqlWorkspaceQuery, because favoriting is inherently
    per-viewer: two teammates can each favorite (or not) the same shared
    query independently. A user can only favorite a query they can
    actually see (their own, or one with is_shared=True) — enforced in
    sql_workspace_service.toggle_favorite(), not here."""
    __tablename__ = "sql_workspace_query_favorites"
    __table_args__ = (UniqueConstraint("user_id", "query_id", name="uq_sql_workspace_query_favorite"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    query_id: Mapped[int] = mapped_column(ForeignKey("sql_workspace_queries.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
