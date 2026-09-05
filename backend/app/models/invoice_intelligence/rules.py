"""
Validation rule engine + ERP field-mapping models for Invoice
Intelligence — versioned, tenant-aware. See
services/invoice/invoice_validation_service.py (rule engine, INV6) and
invoice_erp_mapping_service.py (canonical -> ERP field mapping, INV9).

Versioning follows the same "never mutate a published version in place,
publish a new row instead" convention already used for
ColumnMappingVersion (models/column_mapping.py, CM1-CM14 project
history) — `is_current` marks the one row currently in effect per
(organization, name); every ApInvoice records which rule_version/
mapping_version it was actually evaluated against (see core.py) so a
later version bump never silently rewrites the meaning of a past decision.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import TenantBase


class ApInvoiceRuleCategory(str, enum.Enum):
    SCHEMA = "SCHEMA"
    ARITHMETIC = "ARITHMETIC"
    REFERENCE = "REFERENCE"
    PO = "PO"
    DUPLICATE = "DUPLICATE"
    POLICY = "POLICY"
    ERP = "ERP"


class ApInvoiceRuleSeverity(str, enum.Enum):
    WARNING = "WARNING"
    EXCEPTION = "EXCEPTION"  # blocks straight-through, routes to review/reject per the decision matrix


class ApInvoiceValidationRule(TenantBase):
    __tablename__ = "ap_invoice_validation_rules"
    __table_args__ = (UniqueConstraint("organization_id", "name", "version", name="uq_ap_invoice_rule_org_name_version"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(150), nullable=False)
    category: Mapped[ApInvoiceRuleCategory] = mapped_column(Enum(ApInvoiceRuleCategory), nullable=False, index=True)
    severity: Mapped[ApInvoiceRuleSeverity] = mapped_column(Enum(ApInvoiceRuleSeverity), default=ApInvoiceRuleSeverity.EXCEPTION)
    # Declarative rule configuration, shape depends on `category` — e.g.
    # ARITHMETIC: {"expression": "subtotal + tax - discount + freight == total", "tolerance": 0.01}
    # SCHEMA:     {"required_fields": ["invoice_number", "vendor_name", "total"]}
    # POLICY:     {"tax_tolerance_pct": 5, "amount_tolerance_pct": 2}
    # See invoice_validation_service.py for the interpreter per category.
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    message: Mapped[str] = mapped_column(String(500), nullable=False)  # business-language failure explanation (Technical Design §6)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    effective_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ApInvoiceValidationRule {self.name} v{self.version} ({self.category})>"


class ApInvoiceRuleResult(str, enum.Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


class ApInvoiceValidationResult(TenantBase):
    """One row per rule execution per invoice processing run — the
    evidence behind a decision, not just its outcome."""
    __tablename__ = "ap_invoice_validation_results"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("ap_invoices.id"), nullable=False, index=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("ap_invoice_validation_rules.id"), nullable=True, index=True)
    rule_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    category: Mapped[ApInvoiceRuleCategory] = mapped_column(Enum(ApInvoiceRuleCategory), nullable=False)
    result: Mapped[ApInvoiceRuleResult] = mapped_column(Enum(ApInvoiceRuleResult), nullable=False, index=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)  # {expected, actual, ...} — rule-specific

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ApInvoiceValidationResult invoice={self.invoice_id} {self.category}:{self.result}>"


class ApInvoiceErpMapping(TenantBase):
    """Canonical ApInvoice field -> ERP request field, versioned. One row
    per (organization, erp_connection, canonical_field) per version —
    header and line mappings share this table, distinguished by
    `is_line_mapping`."""
    __tablename__ = "ap_invoice_erp_mappings"
    __table_args__ = (
        UniqueConstraint("organization_id", "erp_connection_id", "canonical_field", "version", name="uq_ap_erp_mapping"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)
    erp_connection_id: Mapped[int | None] = mapped_column(nullable=True, index=True)  # Connection.id, no FK

    canonical_field: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "invoice_number", "lines[].line_total"
    erp_field: Mapped[str] = mapped_column(String(150), nullable=False)  # e.g. "InvoiceNumber", "InvoiceLines[].Amount"
    is_line_mapping: Mapped[bool] = mapped_column(Boolean, default=False)
    default_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    transform_config: Mapped[dict] = mapped_column(JSON, default=dict)  # {"type": "date_format"|"currency_code"|"lookup"|"passthrough", ...}

    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    created_by: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ApInvoiceErpMapping {self.canonical_field} -> {self.erp_field} v{self.version}>"


class ApInvoiceModelVersion(TenantBase):
    """A config-snapshot pointer, NOT a real ML model registry (this
    codebase has no training infrastructure — see
    invoice_learning_service's module docstring for the same disclosure).
    Records which OCR/extraction/prompt/schema/normalization/ruleset/
    mapping configuration was live at a point in time, so a past
    ApInvoice's rule_version/mapping_version columns can be traced back
    to a human-readable snapshot for audit/reproducibility."""
    __tablename__ = "ap_invoice_model_versions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(nullable=True, index=True)

    component: Mapped[str] = mapped_column(String(60), nullable=False)  # ocr|ai_extraction|prompt|schema|normalization|validation_ruleset|mapping|erp_config
    version_label: Mapped[str] = mapped_column(String(60), nullable=False)
    config_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<ApInvoiceModelVersion {self.component}:{self.version_label}>"
