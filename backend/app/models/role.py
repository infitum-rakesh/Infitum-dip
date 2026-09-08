"""
Custom Role models — org-scoped, fine-grained RBAC on top of the platform's
existing fixed-tier UserRole (super_admin/seller/org_admin/data_engineer/
analyst/viewer, see models/user.py). A custom Role is a named bundle of
(category, action) grants across the same page/feature categories the
sidebar already exposes.

Scope note: this module owns Role/RolePermission/RoleAuditLog and its own
CRUD end to end. It deliberately does NOT retrofit enforcement into every
other router's require_action() calls — doing so would mean editing
~8 unrelated modules' endpoints. User.custom_role_id is the additive seam
another module could consume later; see services/role_service.py.
"""
import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.connection import PlatformBase


class RoleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class PermissionAction(str, enum.Enum):
    VIEW = "VIEW"
    CREATE = "CREATE"
    EDIT = "EDIT"
    EXECUTE = "EXECUTE"
    DELETE = "DELETE"
    MANAGE = "MANAGE"
    # SQL Workspace-specific granular actions (see role_service.
    # CATEGORY_ACTIONS) — meaningless for every other category, only ever
    # offered/assignable for the "SQL Workspace" row of the permission
    # matrix. WRITE gates saving/editing a query definition (distinct from
    # DML, which gates actually RUNNING an INSERT/UPDATE/DELETE statement
    # against real data).
    WRITE = "WRITE"
    EXPORT = "EXPORT"
    AI_ASSISTANT = "AI_ASSISTANT"
    PRODUCTION_ACCESS = "PRODUCTION_ACCESS"
    DDL = "DDL"
    DML = "DML"


# Permission categories — matches the sidebar's page/feature list exactly
# (see role_service.CATEGORY_MODULE_MAP for which ones are gated behind a
# licensed module vs always available).
PERMISSION_CATEGORIES = [
    "Dashboard", "Data Catalog", "Connections", "Documents", "Document Intelligence",
    "Review Queue", "Transformation", "Validation", "Reconciliation", "Anomaly Detection", "Pipeline",
    "SQL Workspace", "Schedules", "Incidents", "Monitoring", "Lineage", "Users",
    "Roles", "Licenses", "Audit Logs", "Settings", "Invoice Intelligence", "Payment Approvals",
]


class Role(PlatformBase):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("organization_id", "name", name="uq_role_org_name"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[RoleStatus] = mapped_column(Enum(RoleStatus), default=RoleStatus.ACTIVE)

    # Seeded starter roles (see role_service.ensure_default_roles) — protected
    # from deletion but their permissions remain fully editable.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)

    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return f"<Role {self.name} ({self.status})>"


class RolePermission(PlatformBase):
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "category", "action", name="uq_role_permission"),)

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(60), nullable=False)  # one of PERMISSION_CATEGORIES
    action: Mapped[PermissionAction] = mapped_column(Enum(PermissionAction), nullable=False)

    def __repr__(self) -> str:
        return f"<RolePermission role={self.role_id} {self.category}:{self.action}>"


class RoleAuditEventType(str, enum.Enum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    PERMISSIONS_CHANGED = "PERMISSIONS_CHANGED"
    STATUS_CHANGED = "STATUS_CHANGED"
    USER_ASSIGNED = "USER_ASSIGNED"
    USER_UNASSIGNED = "USER_UNASSIGNED"
    DELETED = "DELETED"


class RoleAuditLog(PlatformBase):
    __tablename__ = "role_audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), nullable=False, index=True)

    event_type: Mapped[RoleAuditEventType] = mapped_column(Enum(RoleAuditEventType), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)

    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<RoleAuditLog role={self.role_id} {self.event_type}>"
