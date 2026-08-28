"""
Validation — real data-quality rule checking (Phase 7: Staging,
Validation & Reconciliation).

ValidationRuleSet is a saved definition — one connection + schema/object
(the same ObjectRef shape mapping.py already uses) plus the rule rows
themselves as a JSON blob, same "flexible, per-row-shaped structure"
reasoning as ColumnMapping.mappings (models/column_mapping.py). Live
column metadata is never cached here either.

ValidationRun/ValidationRuleResult record what a real run actually found:
one aggregate SQL query (see validation/engine.py) computes every
enabled rule's pass/fail count against the live object in a single round
trip — COUNT(*) plus one SUM(CASE WHEN <violation> THEN 1 ELSE 0 END) per
rule. ValidationRuleResult.sql_condition stores the EXACT violation
condition that ran, so "show me the failing rows" (get_failed_records)
reproduces precisely what this run evaluated, not whatever the rule set
says now if it's since been edited.

One rule row (an element of ValidationRuleSet.rules) has this shape:
{
  "id": "rule_xxx",
  "name": "INVOICE_ID_NOT_NULL",
  "type": "NOT_NULL",              # NOT_NULL | RANGE | FORMAT | UNIQUE | REF_INTEGRITY | SQL_CONDITION
  "columns": ["invoice_number"],   # UNIQUE may list >1 (composite key); others use columns[0]
  "severity": "error",             # error | warning | info
  "enabled": true,
  "threshold": 100,                # required pass rate % for this rule to count as PASSED rather than FAILED
  "config": {},                    # type-specific: {min,max} for RANGE, {pattern} for FORMAT,
                                    # {refConnectionId,refSchema,refObject,refColumn} for REF_INTEGRITY,
                                    # {sql} for SQL_CONDITION (a boolean expression; violations = NOT (sql))
}
"""
import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ValidationRunStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class RuleStatus(str, enum.Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    WARNING = "WARNING"


class ValidationRuleSet(TenantBase):
    __tablename__ = "validation_rule_sets"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # connection_id=0 is the same real, established sentinel reconciliation
    # and custom_engine.py use — "not a real Connection row, see
    # uploaded_file_id instead." Kept NOT NULL rather than loosening the
    # column (would need a SQLite table rebuild, not just an ALTER TABLE
    # ADD COLUMN) — 0 is never a real Connection.id.
    #
    # Deliberately a bare int, not ForeignKey("connections.id") — see
    # reconciliation.py's ReconciliationJob.source_connection_id for why:
    # a real DB-level FK can't represent this column's own "0 means no
    # row" convention, and actively rejects it on Postgres/MySQL. Found
    # live during Bring-Your-Own-Database Phase 6 verification.
    connection_id: Mapped[int] = mapped_column(nullable=False)
    schema_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    object_name: Mapped[str] = mapped_column(String(200), nullable=False)
    uploaded_file_id: Mapped[int | None] = mapped_column(ForeignKey("uploaded_dataset_files.id"), nullable=True)

    rules: Mapped[list] = mapped_column(JSON, default=list)  # list[dict] — see module docstring for row shape

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    updated_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<ValidationRuleSet {self.name} ({self.object_name})>"


class ValidationRun(TenantBase):
    __tablename__ = "validation_runs"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    rule_set_id: Mapped[int] = mapped_column(ForeignKey("validation_rule_sets.id"), nullable=False, index=True)

    status: Mapped[ValidationRunStatus] = mapped_column(Enum(ValidationRunStatus), default=ValidationRunStatus.PENDING)
    total_records: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ValidationRun {self.id} rule_set={self.rule_set_id} status={self.status}>"


class ValidationRuleResult(TenantBase):
    __tablename__ = "validation_rule_results"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("validation_runs.id"), nullable=False, index=True)

    rule_id: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(200), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(30), nullable=False)
    columns: Mapped[list] = mapped_column(JSON, default=list)
    severity: Mapped[str] = mapped_column(String(10), default="error")

    status: Mapped[RuleStatus] = mapped_column(Enum(RuleStatus), default=RuleStatus.PASSED)
    pass_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    pass_rate: Mapped[float] = mapped_column(Float, default=100.0)

    # The exact boolean SQL fragment (and, for REF_INTEGRITY, its JOIN
    # clause) this run evaluated (TRUE = violation) — reused verbatim by
    # get_failed_records() so "show failing rows" always matches what
    # this specific run actually found, even if the rule set has since
    # been edited.
    sql_condition: Mapped[str] = mapped_column(Text, nullable=False)
    sql_joins: Mapped[str] = mapped_column(Text, default="")

    def __repr__(self) -> str:
        return f"<ValidationRuleResult {self.rule_name} run={self.run_id} status={self.status}>"
