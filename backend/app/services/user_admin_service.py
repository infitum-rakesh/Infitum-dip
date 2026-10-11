"""
Users admin service — organization admins managing users belonging to
their own organization.

Invite always generates a secure temp password server-side and returns
it once in the API response regardless of email outcome — never the
sole way to get it if delivery fails or `send_invite` is off. When
`InviteUserRequest.send_invite` is True, invite_user() also attempts a
real send via services/email_service (Seller Settings → Email must be
configured) and reports delivered/detail back, same pattern
services/seller/customer_service.create_customer() already established.
Reset Password still only returns the temp password — no corresponding
reset-notification email flow exists yet.
"""
import secrets

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.organization import Organization
from app.models.user import User, UserRole
from app.models.user_activity import UserActivityLog, UserActivityType

ADMIN_ROLES = {UserRole.ORG_ADMIN, UserRole.SELLER, UserRole.SUPER_ADMIN}


class UserAdminValidationError(Exception):
    pass


def status_of(user: User) -> str:
    """
    ACTIVE / INACTIVE / PENDING for the Users admin UI. Deliberately keyed
    off last_login rather than is_verified — this codebase has no email
    verification flow, so is_verified never flips true and would make
    every user "pending" forever. "Pending Invitation" instead means
    exactly that: invited, still hasn't ever signed in.
    """
    if not user.is_active:
        return "INACTIVE"
    if user.last_login is None:
        return "PENDING"
    return "ACTIVE"


def _generate_temp_password() -> str:
    return secrets.token_urlsafe(9)  # ~12 chars, URL-safe


def _split_name(first: str | None, last: str | None, fallback: str) -> str:
    full = f"{(first or '').strip()} {(last or '').strip()}".strip()
    return full or fallback


# ── Lookups ──────────────────────────────────────────────────────

async def get_user(db: AsyncSession, user_id: int, organization_id: int | None) -> User | None:
    stmt = select(User).where(User.id == user_id)
    if organization_id is not None:
        stmt = stmt.where(User.organization_id == organization_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def list_users(
    db: AsyncSession, *, organization_id: int | None, search: str = "",
    role: str | None = None, status_filter: str | None = None,
    page: int = 1, page_size: int = 20, sort_by: str = "created_at", sort_dir: str = "desc",
) -> tuple[list[User], int]:
    stmt = select(User)
    if organization_id is not None:
        stmt = stmt.where(User.organization_id == organization_id)
    if role:
        stmt = stmt.where(User.role == role)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(User.full_name.ilike(like), User.email.ilike(like), User.department.ilike(like)))

    count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total_before_status = count_result.scalar_one()

    sort_col = getattr(User, sort_by, User.created_at)
    stmt = stmt.order_by(sort_col.desc() if sort_dir == "desc" else sort_col.asc())

    result = await db.execute(stmt)
    users = list(result.scalars().all())

    # status is computed, not a column — filter in Python.
    if status_filter:
        users = [u for u in users if status_of(u) == status_filter]
        total = len(users)
    else:
        total = total_before_status

    start = (page - 1) * page_size
    return users[start:start + page_size], total


async def get_stats(db: AsyncSession, organization_id: int | None) -> dict:
    stmt = select(User)
    if organization_id is not None:
        stmt = stmt.where(User.organization_id == organization_id)
    result = await db.execute(stmt)
    users = list(result.scalars().all())

    return {
        "total": len(users),
        "active": sum(1 for u in users if status_of(u) == "ACTIVE"),
        "inactive": sum(1 for u in users if status_of(u) == "INACTIVE"),
        "pending": sum(1 for u in users if status_of(u) == "PENDING"),
        "administrators": sum(1 for u in users if u.role in ADMIN_ROLES),
    }


# ── Invite / create ──────────────────────────────────────────────

async def invite_user(db: AsyncSession, *, organization_id: int | None, actor_id: int | None, body):
    exists = await db.execute(select(User).where(User.email == body.email.lower()))
    if exists.scalar_one_or_none():
        raise UserAdminValidationError("A user with this email already exists.")

    try:
        role = UserRole(body.role)
    except ValueError:
        raise UserAdminValidationError(f"Invalid role: {body.role}")

    org = None
    if organization_id is not None:
        org_result = await db.execute(select(Organization).where(Organization.id == organization_id))
        org = org_result.scalar_one_or_none()
        if org and org.max_users and org.max_users > 0:
            count_result = await db.execute(select(func.count()).select_from(select(User).where(User.organization_id == organization_id).subquery()))
            if count_result.scalar_one() >= org.max_users:
                raise UserAdminValidationError(f"Organization has reached its user limit ({org.max_users}).")

    temp_password = _generate_temp_password()
    user = User(
        email=body.email.lower(),
        full_name=_split_name(body.first_name, body.last_name, body.email),
        hashed_password=hash_password(temp_password),
        role=role,
        department=body.department,
        is_active=body.is_active,
        is_verified=False,
        organization_id=organization_id,
        must_change_password=True,
    )
    db.add(user)
    await db.flush()

    db.add(UserActivityLog(
        user_id=user.id, event_type=UserActivityType.INVITED,
        message=f"Invited as {role.value}" + ("" if body.send_invite else " (invitation email disabled)"),
        actor_id=actor_id,
    ))
    await db.commit()
    await db.refresh(user)

    email_result = None
    if body.send_invite:
        from app.core.config import get_settings
        from app.services import email_service
        subject, body_text = email_service.build_invite_email(
            full_name=user.full_name, email=user.email, temp_password=temp_password,
            login_url=f"{get_settings().frontend_base_url}/login",
            role_label=role.value.replace("_", " ").title(), org_name=org.name if org else None,
        )
        email_result = await email_service.send_email(db, to=user.email, subject=subject, body=body_text)

    return user, temp_password, email_result


# ── Update / role / status ───────────────────────────────────────

async def update_user(db: AsyncSession, user: User, body, actor_id: int | None) -> User:
    patch = body.model_dump(exclude_unset=True)
    changed = []

    if patch.get("first_name") is not None or patch.get("last_name") is not None:
        new_name = _split_name(patch.get("first_name"), patch.get("last_name"), user.full_name)
        if new_name != user.full_name:
            changed.append("name")
        user.full_name = new_name
    if "department" in patch and patch["department"] != user.department:
        user.department = patch["department"]
        changed.append("department")
    if "accessible_modules" in patch and patch["accessible_modules"] != user.accessible_modules:
        user.accessible_modules = patch["accessible_modules"]
        changed.append("module access")

    if changed:
        db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.UPDATED, message=f"Updated {', '.join(changed)}", actor_id=actor_id))

    await db.commit()
    await db.refresh(user)
    return user


async def disable_user(db: AsyncSession, user: User, actor_id: int | None) -> User:
    if actor_id == user.id:
        raise UserAdminValidationError("You cannot disable your own account.")
    user.is_active = False
    db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.DISABLED, message="Account disabled", actor_id=actor_id))
    await db.commit()
    await db.refresh(user)
    return user


async def enable_user(db: AsyncSession, user: User, actor_id: int | None) -> User:
    user.is_active = True
    db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.ENABLED, message="Account enabled", actor_id=actor_id))
    await db.commit()
    await db.refresh(user)
    return user


async def reset_password(db: AsyncSession, user: User, actor_id: int | None) -> tuple[User, str]:
    temp_password = _generate_temp_password()
    user.hashed_password = hash_password(temp_password)
    user.must_change_password = True
    db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.PASSWORD_RESET, message="Password reset by administrator", actor_id=actor_id))
    await db.commit()
    await db.refresh(user)
    return user, temp_password


async def _other_admin_count(db: AsyncSession, organization_id: int | None, exclude_user_id: int) -> int:
    stmt = select(func.count()).select_from(
        select(User).where(
            User.organization_id == organization_id,
            User.role == UserRole.ORG_ADMIN,
            User.id != exclude_user_id,
        ).subquery()
    )
    result = await db.execute(stmt)
    return result.scalar_one()


async def change_role(db: AsyncSession, user: User, new_role: str, actor_id: int | None) -> User:
    try:
        role = UserRole(new_role)
    except ValueError:
        raise UserAdminValidationError(f"Invalid role: {new_role}")

    if user.role == UserRole.ORG_ADMIN and role != UserRole.ORG_ADMIN:
        if await _other_admin_count(db, user.organization_id, user.id) == 0:
            raise UserAdminValidationError("Cannot change role — this is the only administrator in the organization.")

    old = user.role
    user.role = role
    db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.ROLE_CHANGED, message=f"Role changed from {old.value} to {role.value}", actor_id=actor_id))
    await db.commit()
    await db.refresh(user)
    return user


async def delete_user(db: AsyncSession, user: User, actor_id: int | None) -> None:
    if actor_id == user.id:
        raise UserAdminValidationError("You cannot delete your own account.")
    if user.role == UserRole.ORG_ADMIN:
        if await _other_admin_count(db, user.organization_id, user.id) == 0:
            raise UserAdminValidationError("Cannot delete — this is the only administrator in the organization.")

    await db.execute(UserActivityLog.__table__.delete().where(UserActivityLog.user_id == user.id))
    await db.delete(user)
    await db.commit()


# ── Activity / login history ──────────────────────────────────────

async def get_activity(db: AsyncSession, user_id: int, limit: int = 100) -> list[UserActivityLog]:
    result = await db.execute(
        select(UserActivityLog).where(UserActivityLog.user_id == user_id)
        .order_by(UserActivityLog.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


async def get_login_history(db: AsyncSession, user_id: int, limit: int = 100) -> list[UserActivityLog]:
    result = await db.execute(
        select(UserActivityLog).where(
            UserActivityLog.user_id == user_id,
            UserActivityLog.event_type.in_([UserActivityType.LOGIN, UserActivityType.LOGIN_FAILED]),
        ).order_by(UserActivityLog.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())


# ── Output shaping ───────────────────────────────────────────────

async def actor_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    result = await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))
    return {row[0]: row[1] for row in result.all()}


def user_to_out(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "department": user.department,
        "role": user.role.value if hasattr(user.role, "value") else user.role,
        "status": status_of(user),
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "accessible_modules": user.accessible_modules,
        "last_login": user.last_login,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }


def activity_to_out(event: UserActivityLog, name_map: dict[int, str]) -> dict:
    return {
        "id": event.id,
        "event_type": event.event_type.value if hasattr(event.event_type, "value") else event.event_type,
        "message": event.message,
        "actor_id": event.actor_id,
        "actor_name": name_map.get(event.actor_id),
        "created_at": event.created_at,
    }
