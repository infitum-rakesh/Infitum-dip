"""
Custom Role service — Administration -> Roles.

Role permissions can never exceed the modules licensed to the organization
(see CATEGORY_MODULE_MAP + clamp_permissions below): any (category, action)
grant for a category backed by a licensable module the org doesn't have is
silently dropped on save, exactly like a user's accessible_modules already
can't exceed org.licensed_modules elsewhere in this codebase.

Seeded starter roles (ensure_default_roles) are plain data, not hardcoded
branches in a page component — the frontend renders whatever the API
returns and never special-cases a role by name.
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.role import (
    PERMISSION_CATEGORIES,
    PermissionAction,
    Role,
    RoleAuditEventType,
    RoleAuditLog,
    RolePermission,
    RoleStatus,
)
from app.models.user import User, UserRole

# category -> licensing module key, or None when the category is foundational
# (always available regardless of licensed_modules) — mirrors how each page
# was actually gated when it was built (see e.g. Sidebar.jsx `module:` keys).
CATEGORY_MODULE_MAP: dict[str, str | None] = {
    "Dashboard": None,
    "Data Catalog": "DATA_CATALOG",
    "Connections": None,
    "Documents": "DOCUMENT_INTELLIGENCE",
    "Document Intelligence": "DOCUMENT_INTELLIGENCE",
    "Review Queue": "DOCUMENT_INTELLIGENCE",
    "Transformation": "TRANSFORMATION",
    "Validation": "VALIDATION",
    "Reconciliation": "RECONCILIATION",
    "Anomaly Detection": "ANOMALY_DETECTION",
    "Pipeline": None,
    "SQL Workspace": "SQL_WORKSPACE",
    "Schedules": None,
    "Incidents": None,
    "Monitoring": "OBSERVABILITY",
    "Lineage": "LINEAGE",
    "Users": None,
    "Roles": None,
    "Licenses": None,
    "Audit Logs": None,
    "Settings": None,
    "Invoice Intelligence": "INVOICE_INTELLIGENCE",
    "Payment Approvals": "PAYMENT_APPROVAL",
    "Bulk Object Migration": "BULK_MIGRATION",
}

# Every category offers the same generic 6-action set EXCEPT "SQL
# Workspace", which offers its own granular action set instead (see
# models/role.py's PermissionAction docstring) — WRITE/EXPORT/AI_ASSISTANT/
# PRODUCTION_ACCESS/DDL/DML are meaningless for "Dashboard" or "Users",
# so they're only ever offered/assignable for that one category row. Used
# both to render the right toggle set per category (GET /roles/... ships
# this to the frontend) and to reject a (category, action) combination
# that doesn't apply, in clamp_permissions below.
_GENERIC_ACTIONS = ["VIEW", "CREATE", "EDIT", "EXECUTE", "DELETE", "MANAGE"]
SQL_WORKSPACE_CATEGORY = "SQL Workspace"
_SQL_WORKSPACE_ACTIONS = ["VIEW", "EXECUTE", "WRITE", "DELETE", "EXPORT", "AI_ASSISTANT", "DML", "DDL", "PRODUCTION_ACCESS"]
CATEGORY_ACTIONS: dict[str, list[str]] = {c: _GENERIC_ACTIONS for c in PERMISSION_CATEGORIES}
CATEGORY_ACTIONS[SQL_WORKSPACE_CATEGORY] = _SQL_WORKSPACE_ACTIONS

# Seeded once per organization the first time it opens Roles with none yet
# (see ensure_default_roles) — a reasonable starting point, fully editable
# afterwards. Not a fixed enum: just initial row data.
_DEFAULT_ROLE_GRANTS: dict[str, dict[str, list[str]]] = {
    "Organization Admin": {
        **{c: ["VIEW", "CREATE", "EDIT", "EXECUTE", "DELETE", "MANAGE"] for c in PERMISSION_CATEGORIES if c != SQL_WORKSPACE_CATEGORY},
        SQL_WORKSPACE_CATEGORY: list(_SQL_WORKSPACE_ACTIONS),  # full access, incl. DDL + PRODUCTION_ACCESS
    },
    "Data Engineer": {
        "Dashboard": ["VIEW"], "Data Catalog": ["VIEW", "CREATE", "EDIT"], "Connections": ["VIEW", "CREATE", "EDIT"],
        "Documents": ["VIEW", "CREATE", "EDIT", "EXECUTE"], "Document Intelligence": ["VIEW", "EXECUTE"],
        "Review Queue": ["VIEW", "EDIT"], "Transformation": ["VIEW", "CREATE", "EDIT", "EXECUTE"],
        "Validation": ["VIEW", "CREATE", "EDIT", "EXECUTE"], "Reconciliation": ["VIEW", "CREATE", "EDIT", "EXECUTE"],
        "Pipeline": ["VIEW", "CREATE", "EDIT", "EXECUTE"],
        # Read/write against dev & staging connections, no schema changes
        # and no production access by default — an admin can grant those
        # explicitly per engineer via this same matrix.
        "SQL Workspace": ["VIEW", "EXECUTE", "WRITE", "DELETE", "EXPORT", "AI_ASSISTANT", "DML"],
        "Schedules": ["VIEW", "CREATE", "EDIT", "EXECUTE"], "Incidents": ["VIEW", "CREATE", "EDIT"],
    },
    "Data Analyst": {
        "Dashboard": ["VIEW"], "Data Catalog": ["VIEW"], "Documents": ["VIEW"], "Document Intelligence": ["VIEW"],
        "Transformation": ["VIEW"], "Validation": ["VIEW"], "Reconciliation": ["VIEW"],
        # Read-only: can run SELECTs, export results, ask the AI Assistant —
        # can't save/delete queries or run any write statement.
        "SQL Workspace": ["VIEW", "EXECUTE", "EXPORT", "AI_ASSISTANT"], "Incidents": ["VIEW"],
    },
    "Data Steward": {
        "Dashboard": ["VIEW"], "Data Catalog": ["VIEW", "EDIT", "MANAGE"], "Documents": ["VIEW", "EDIT"],
        "Document Intelligence": ["VIEW"], "Review Queue": ["VIEW", "EDIT", "MANAGE"],
        "Validation": ["VIEW", "CREATE", "EDIT", "MANAGE"], "Reconciliation": ["VIEW", "EDIT"],
        "Incidents": ["VIEW", "CREATE", "EDIT"],
    },
    "Reviewer": {
        "Dashboard": ["VIEW"], "Documents": ["VIEW"], "Document Intelligence": ["VIEW"],
        "Review Queue": ["VIEW", "EDIT", "EXECUTE"], "Incidents": ["VIEW"],
    },
    "Developer": {
        "Dashboard": ["VIEW"], "Data Catalog": ["VIEW"], "Connections": ["VIEW", "CREATE", "EDIT"],
        "Transformation": ["VIEW", "CREATE", "EDIT", "EXECUTE", "DELETE"],
        "Pipeline": ["VIEW", "CREATE", "EDIT", "EXECUTE", "DELETE"],
        # Full DDL on dev/staging connections (schema iteration is core to
        # this role) but production access is still an explicit, separate grant.
        "SQL Workspace": ["VIEW", "EXECUTE", "WRITE", "DELETE", "EXPORT", "AI_ASSISTANT", "DML", "DDL"],
        "Schedules": ["VIEW", "CREATE", "EDIT", "EXECUTE"],
    },
    "Viewer": {
        "Dashboard": ["VIEW"], "Data Catalog": ["VIEW"], "Documents": ["VIEW"], "Document Intelligence": ["VIEW"],
        "Transformation": ["VIEW"], "Validation": ["VIEW"], "Reconciliation": ["VIEW"], "Incidents": ["VIEW"],
    },
}

# Fallback SQL Workspace permission set for a user with NO custom role
# assigned — mirrors core.deps.ROLE_ACTIONS' "fixed tier default" pattern,
# just expressed in the granular action vocabulary. SUPER_ADMIN is handled
# as an unconditional bypass in effective_sql_permissions() below (same
# convention require_action() already uses), not listed here.
SQL_WORKSPACE_TIER_DEFAULTS: dict[UserRole, set[str]] = {
    UserRole.SELLER:        set(_SQL_WORKSPACE_ACTIONS),
    UserRole.ORG_ADMIN:     set(_SQL_WORKSPACE_ACTIONS),
    UserRole.DATA_ENGINEER: {"VIEW", "EXECUTE", "WRITE", "DELETE", "EXPORT", "AI_ASSISTANT", "DML"},
    UserRole.ANALYST:       {"VIEW", "EXECUTE", "EXPORT", "AI_ASSISTANT"},
    UserRole.VIEWER:        {"VIEW"},
}


class RoleValidationError(Exception):
    pass


def licensed_categories(org: Organization | None) -> list[str]:
    licensed = set(org.licensed_modules or []) if org else set()
    return [c for c in PERMISSION_CATEGORIES if CATEGORY_MODULE_MAP.get(c) is None or CATEGORY_MODULE_MAP[c] in licensed]


def clamp_permissions(permissions: list, org: Organization | None) -> tuple[list, list]:
    """Returns (allowed, dropped) — dropped cells reference either an
    unlicensed category, or a (category, action) combination that category
    doesn't actually offer (e.g. "MANAGE" for "SQL Workspace", which only
    ever offers its own granular action set — see CATEGORY_ACTIONS)."""
    allowed_categories = set(licensed_categories(org))
    allowed, dropped = [], []
    for p in permissions:
        valid_category = p.category in allowed_categories
        valid_action = p.action in CATEGORY_ACTIONS.get(p.category, _GENERIC_ACTIONS)
        (allowed if (valid_category and valid_action) else dropped).append(p)
    return allowed, dropped


async def effective_sql_permissions(db: AsyncSession, user: User) -> set[str]:
    """Real per-user SQL Workspace permission set. SUPER_ADMIN always gets
    everything (same unconditional bypass core.deps.require_action gives
    it). A user with a custom_role_id gets EXACTLY what that role's "SQL
    Workspace" grants say — even an empty set, if the role genuinely
    doesn't grant any; that's the point of a fine-grained system, not a
    bug to work around. Everyone else falls back to
    SQL_WORKSPACE_TIER_DEFAULTS for their fixed UserRole tier."""
    if user.role == UserRole.SUPER_ADMIN:
        return set(_SQL_WORKSPACE_ACTIONS)
    if user.custom_role_id:
        result = await db.execute(
            select(RolePermission.action).where(
                RolePermission.role_id == user.custom_role_id,
                RolePermission.category == SQL_WORKSPACE_CATEGORY,
            )
        )
        return {(a.value if hasattr(a, "value") else a) for (a,) in result.all()}
    return set(SQL_WORKSPACE_TIER_DEFAULTS.get(user.role, set()))


# ── Seeding ──────────────────────────────────────────────────────

async def ensure_default_roles(db: AsyncSession, organization_id: int | None, org: Organization | None) -> None:
    stmt = select(func.count()).select_from(select(Role).where(Role.organization_id == organization_id).subquery())
    result = await db.execute(stmt)
    if result.scalar_one() > 0:
        return

    for name, grants in _DEFAULT_ROLE_GRANTS.items():
        role = Role(organization_id=organization_id, name=name, description=f"{name} — starter role", is_system=True, status=RoleStatus.ACTIVE)
        db.add(role)
        await db.flush()
        cells = [PermissionCellLike(category=c, action=a) for c, actions in grants.items() for a in actions]
        allowed, _dropped = clamp_permissions(cells, org)
        for cell in allowed:
            db.add(RolePermission(role_id=role.id, category=cell.category, action=PermissionAction(cell.action)))
        db.add(RoleAuditLog(role_id=role.id, event_type=RoleAuditEventType.CREATED, message="Seeded starter role"))
    await db.commit()


class PermissionCellLike:
    """Plain (category, action) pair — avoids importing the pydantic schema into the service layer."""
    def __init__(self, category: str, action: str):
        self.category = category
        self.action = action


# ── Lookups ──────────────────────────────────────────────────────

async def get_role(db: AsyncSession, role_id: int, organization_id: int | None) -> Role | None:
    stmt = select(Role).where(Role.id == role_id)
    if organization_id is not None:
        stmt = stmt.where(Role.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_roles(db: AsyncSession, organization_id: int | None) -> list[Role]:
    stmt = select(Role).order_by(Role.created_at.asc())
    if organization_id is not None:
        stmt = stmt.where(Role.organization_id == organization_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_permissions(db: AsyncSession, role_id: int) -> list[RolePermission]:
    result = await db.execute(select(RolePermission).where(RolePermission.role_id == role_id))
    return list(result.scalars().all())


async def get_users(db: AsyncSession, role_id: int) -> list[User]:
    result = await db.execute(select(User).where(User.custom_role_id == role_id))
    return list(result.scalars().all())


async def user_count(db: AsyncSession, role_id: int) -> int:
    result = await db.execute(select(func.count()).select_from(select(User).where(User.custom_role_id == role_id).subquery()))
    return result.scalar_one()


async def permission_count(db: AsyncSession, role_id: int) -> int:
    result = await db.execute(select(func.count()).select_from(select(RolePermission).where(RolePermission.role_id == role_id).subquery()))
    return result.scalar_one()


# ── Create / update / delete ────────────────────────────────────

async def create_role(db: AsyncSession, *, organization_id: int | None, actor_id: int | None, org: Organization | None, body) -> Role:
    exists = await db.execute(select(Role).where(Role.organization_id == organization_id, Role.name == body.name))
    if exists.scalar_one_or_none():
        raise RoleValidationError(f"A role named '{body.name}' already exists.")

    role = Role(organization_id=organization_id, name=body.name, description=body.description, is_system=False, created_by=actor_id)
    db.add(role)
    await db.flush()

    allowed, dropped = clamp_permissions(body.permissions, org)
    for cell in allowed:
        db.add(RolePermission(role_id=role.id, category=cell.category, action=PermissionAction(cell.action)))

    msg = f"Role created with {len(allowed)} permission grant(s)"
    if dropped:
        msg += f"; {len(dropped)} grant(s) dropped (category not licensed for this organization)"
    db.add(RoleAuditLog(role_id=role.id, event_type=RoleAuditEventType.CREATED, message=msg, actor_id=actor_id))

    await db.commit()
    await db.refresh(role)
    return role


async def update_role(db: AsyncSession, role: Role, *, org: Organization | None, body, actor_id: int | None) -> Role:
    patch = body.model_dump(exclude_unset=True)
    changed = []

    if "name" in patch and patch["name"] and patch["name"] != role.name:
        dup = await db.execute(select(Role).where(Role.organization_id == role.organization_id, Role.name == patch["name"], Role.id != role.id))
        if dup.scalar_one_or_none():
            raise RoleValidationError(f"A role named '{patch['name']}' already exists.")
        role.name = patch["name"]
        changed.append("name")
    if "description" in patch and patch["description"] != role.description:
        role.description = patch["description"]
        changed.append("description")
    if "status" in patch and patch["status"]:
        try:
            new_status = RoleStatus(patch["status"])
        except ValueError:
            raise RoleValidationError(f"Invalid status: {patch['status']}")
        if new_status != role.status:
            role.status = new_status
            db.add(RoleAuditLog(role_id=role.id, event_type=RoleAuditEventType.STATUS_CHANGED, message=f"Status changed to {new_status.value}", actor_id=actor_id))

    if changed:
        db.add(RoleAuditLog(role_id=role.id, event_type=RoleAuditEventType.UPDATED, message=f"Updated {', '.join(changed)}", actor_id=actor_id))

    if body.permissions is not None:
        await db.execute(RolePermission.__table__.delete().where(RolePermission.role_id == role.id))
        allowed, dropped = clamp_permissions(body.permissions, org)
        for cell in allowed:
            db.add(RolePermission(role_id=role.id, category=cell.category, action=PermissionAction(cell.action)))
        msg = f"Permissions updated — {len(allowed)} grant(s)"
        if dropped:
            msg += f"; {len(dropped)} grant(s) dropped (category not licensed for this organization)"
        db.add(RoleAuditLog(role_id=role.id, event_type=RoleAuditEventType.PERMISSIONS_CHANGED, message=msg, actor_id=actor_id))

    await db.commit()
    await db.refresh(role)
    return role


async def delete_role(db: AsyncSession, role: Role) -> None:
    if role.is_system:
        raise RoleValidationError("This is a starter role and cannot be deleted. You can still edit its permissions or disable it.")
    if await user_count(db, role.id) > 0:
        raise RoleValidationError("Cannot delete a role that still has users assigned to it. Unassign them first.")

    await db.execute(RolePermission.__table__.delete().where(RolePermission.role_id == role.id))
    await db.execute(RoleAuditLog.__table__.delete().where(RoleAuditLog.role_id == role.id))
    await db.delete(role)
    await db.commit()


# ── User assignment ──────────────────────────────────────────────

async def assign_user(db: AsyncSession, role: Role, user: User, actor_id: int | None) -> None:
    user.custom_role_id = role.id
    db.add(RoleAuditLog(role_id=role.id, event_type=RoleAuditEventType.USER_ASSIGNED, message=f"{user.full_name} assigned to this role", actor_id=actor_id))
    await db.commit()


async def unassign_user(db: AsyncSession, role: Role, user: User, actor_id: int | None) -> None:
    user.custom_role_id = None
    db.add(RoleAuditLog(role_id=role.id, event_type=RoleAuditEventType.USER_UNASSIGNED, message=f"{user.full_name} removed from this role", actor_id=actor_id))
    await db.commit()


# ── Audit ────────────────────────────────────────────────────────

async def get_audit_log(db: AsyncSession, role_id: int) -> list[RoleAuditLog]:
    result = await db.execute(select(RoleAuditLog).where(RoleAuditLog.role_id == role_id).order_by(RoleAuditLog.created_at.desc()))
    return list(result.scalars().all())


# ── Output shaping ───────────────────────────────────────────────

async def actor_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    result = await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row[0]: row[1] for row in result.all()}


async def role_to_out(db: AsyncSession, role: Role, name_map: dict[int, str]) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "description": role.description,
        "status": role.status.value if hasattr(role.status, "value") else role.status,
        "is_system": role.is_system,
        "user_count": await user_count(db, role.id),
        "permission_count": await permission_count(db, role.id),
        "created_by": role.created_by,
        "created_by_name": name_map.get(role.created_by),
        "created_at": role.created_at,
        "updated_at": role.updated_at,
    }


def audit_to_out(event: RoleAuditLog, name_map: dict[int, str]) -> dict:
    return {
        "id": event.id,
        "event_type": event.event_type.value if hasattr(event.event_type, "value") else event.event_type,
        "message": event.message,
        "actor_id": event.actor_id,
        "actor_name": name_map.get(event.actor_id),
        "created_at": event.created_at,
    }
