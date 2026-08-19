from datetime import datetime

from pydantic import BaseModel


class PermissionCell(BaseModel):
    category: str
    action: str


# ── Responses ────────────────────────────────────────────────────

class RoleOut(BaseModel):
    id: int
    name: str
    description: str | None
    status: str
    is_system: bool
    user_count: int
    permission_count: int
    created_by: int | None
    created_by_name: str | None = None
    created_at: datetime
    updated_at: datetime


class RoleListOut(BaseModel):
    items: list[RoleOut]
    total: int
    categories: list[str]           # PERMISSION_CATEGORIES, for the frontend matrix
    licensed_categories: list[str]  # subset actually assignable for this org
    category_actions: dict[str, list[str]]  # category -> its real assignable action set (role_service.CATEGORY_ACTIONS)


class RoleUserOut(BaseModel):
    id: int
    full_name: str
    email: str
    department: str | None
    status: str  # ACTIVE | INACTIVE | PENDING (reuses user_admin_service.status_of)


class RoleDetailOut(RoleOut):
    permissions: list[PermissionCell]
    users: list[RoleUserOut]
    categories: list[str]
    licensed_categories: list[str]
    category_actions: dict[str, list[str]]


class RoleAuditEventOut(BaseModel):
    id: int
    event_type: str
    message: str | None
    actor_id: int | None
    actor_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class RoleAuditListOut(BaseModel):
    role_id: int
    items: list[RoleAuditEventOut]


# ── Requests ─────────────────────────────────────────────────────

class RoleCreateRequest(BaseModel):
    name: str
    description: str | None = None
    permissions: list[PermissionCell] = []


class RoleUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    permissions: list[PermissionCell] | None = None  # None = leave unchanged; [] clears all


class AssignUserRequest(BaseModel):
    user_id: int
