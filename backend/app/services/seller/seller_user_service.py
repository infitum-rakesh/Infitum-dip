"""
Seller Portal -> Seller Users service (Phase 7) — manages the Seller's OWN
staff accounts (User rows with role=SELLER, organization_id=NULL), distinct
from Phase 2's Customers "Users" tab which manages a *customer's* org
users. Same underlying User/UserActivityLog models, different scope.

Invite always generates a temp password server-side and returns it once
(same as Reset Password) regardless of email outcome — never the sole
way to get it if delivery fails or no provider is configured. When
email_service IS configured (Seller Settings → Email), invite_user() also
attempts a real send and reports delivered/detail back, same pattern
services/seller/customer_service.create_customer() already established.
UserActivityLog is keyed by user_id only (not org-scoped), so
get_activity()/get_login_history()/actor_name_map()/activity_to_out()
from user_admin_service are reused directly rather than duplicated.

Every mutation also writes a SellerAuditLog row via record_seller_event
(seller_user_id=actor, customer_id=None — these events aren't about any
one customer) so Phase 8's Seller Audit Logs page has a real trail of who
granted/changed/revoked Seller Portal access.
"""
import secrets

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.models.seller_audit_log import record_seller_event
from app.models.user import User, UserRole
from app.models.user_activity import UserActivityLog, UserActivityType
from app.services import user_admin_service

SELLER_ROLES = [
    "SELLER_ADMIN", "SELLER_CUSTOMER_MANAGER", "SELLER_LICENSE_MANAGER",
    "SELLER_SUPPORT", "SELLER_VIEWER",
]
# A Seller user with seller_role=None gets full SELLER_ADMIN-equivalent
# access (core/deps.require_seller_action's "unset = full tier default"
# convention) — treat both as "an admin" for the last-admin-standing guard.
_ADMIN_EQUIVALENT = {None, "SELLER_ADMIN"}


class SellerUserServiceError(Exception):
    pass


def _generate_temp_password() -> str:
    return secrets.token_urlsafe(9)  # ~12 chars, URL-safe


def _split_name(first: str | None, last: str | None, fallback: str) -> str:
    full = f"{(first or '').strip()} {(last or '').strip()}".strip()
    return full or fallback


# ── Lookups ──────────────────────────────────────────────────────

async def get_user(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id, User.role == UserRole.SELLER))
    return result.scalar_one_or_none()


async def list_users(
    db: AsyncSession, *, search: str = "", seller_role_filter: str | None = None,
    status_filter: str | None = None, page: int = 1, page_size: int = 20,
    sort_by: str = "created_at", sort_dir: str = "desc",
) -> tuple[list[User], int]:
    stmt = select(User).where(User.role == UserRole.SELLER)
    if seller_role_filter:
        if seller_role_filter == "SELLER_ADMIN":
            stmt = stmt.where(or_(User.seller_role == "SELLER_ADMIN", User.seller_role.is_(None)))
        else:
            stmt = stmt.where(User.seller_role == seller_role_filter)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(User.full_name.ilike(like), User.email.ilike(like), User.department.ilike(like)))

    count_result = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total_before_status = count_result.scalar_one()

    sort_col = getattr(User, sort_by, User.created_at)
    stmt = stmt.order_by(sort_col.desc() if sort_dir == "desc" else sort_col.asc())
    result = await db.execute(stmt)
    users = list(result.scalars().all())

    # status is computed, not a column — filter in Python (same as user_admin_service).
    if status_filter:
        users = [u for u in users if user_admin_service.status_of(u) == status_filter]
        total = len(users)
    else:
        total = total_before_status

    start = (page - 1) * page_size
    return users[start:start + page_size], total


async def get_stats(db: AsyncSession) -> dict:
    result = await db.execute(select(User).where(User.role == UserRole.SELLER))
    users = list(result.scalars().all())
    return {
        "total": len(users),
        "active": sum(1 for u in users if user_admin_service.status_of(u) == "ACTIVE"),
        "inactive": sum(1 for u in users if user_admin_service.status_of(u) == "INACTIVE"),
        "pending": sum(1 for u in users if user_admin_service.status_of(u) == "PENDING"),
        "admins": sum(1 for u in users if u.seller_role in _ADMIN_EQUIVALENT),
    }


async def _other_admin_count(db: AsyncSession, exclude_user_id: int) -> int:
    result = await db.execute(select(func.count()).select_from(
        select(User).where(
            User.role == UserRole.SELLER, User.id != exclude_user_id,
            or_(User.seller_role == "SELLER_ADMIN", User.seller_role.is_(None)),
        ).subquery()
    ))
    return result.scalar_one()


# ── Invite / create ──────────────────────────────────────────────

async def invite_user(db: AsyncSession, *, actor: User, body, ip_address: str | None = None):
    exists = await db.execute(select(User).where(User.email == body.email.lower()))
    if exists.scalar_one_or_none():
        raise SellerUserServiceError("A user with this email already exists.")
    if body.seller_role and body.seller_role not in SELLER_ROLES:
        raise SellerUserServiceError(f"Invalid seller role: {body.seller_role}")

    temp_password = _generate_temp_password()
    user = User(
        email=body.email.lower(),
        full_name=_split_name(body.first_name, body.last_name, body.email),
        hashed_password=hash_password(temp_password),
        role=UserRole.SELLER,
        seller_role=body.seller_role,
        department=body.department,
        is_active=body.is_active,
        is_verified=False,
        organization_id=None,
        must_change_password=True,
    )
    db.add(user)
    await db.flush()

    db.add(UserActivityLog(
        user_id=user.id, event_type=UserActivityType.INVITED,
        message=f"Invited to Seller Portal as {body.seller_role or 'Seller Admin (default)'}", actor_id=actor.id,
    ))
    await db.commit()
    await db.refresh(user)

    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Seller User Created",
        resource=user.full_name, resource_id=str(user.id), ip_address=ip_address,
        after_value={"email": user.email, "seller_role": user.seller_role},
    )

    from app.core.config import get_settings
    from app.services import email_service
    subject, body_text = email_service.build_invite_email(
        full_name=user.full_name, email=user.email, temp_password=temp_password,
        login_url=f"{get_settings().frontend_base_url}/login",
        role_label=(body.seller_role or "SELLER_ADMIN").replace("_", " ").title(),
    )
    email_result = await email_service.send_email(db, to=user.email, subject=subject, body=body_text)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Seller User Invite Email",
        resource=user.email, resource_id=str(user.id), ip_address=ip_address,
        details=email_result.detail, after_value={"delivered": email_result.delivered},
    )

    return user, temp_password, email_result


# ── Update / role / status ───────────────────────────────────────

async def update_user(db: AsyncSession, user: User, body, actor: User, ip_address: str | None = None) -> User:
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

    if changed:
        db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.UPDATED, message=f"Updated {', '.join(changed)}", actor_id=actor.id))
        await db.commit()
        await db.refresh(user)
        await record_seller_event(
            db, seller_user_id=actor.id, customer_id=None, action="Seller User Updated",
            resource=user.full_name, resource_id=str(user.id), ip_address=ip_address,
            after_value={"changed": changed},
        )
    return user


async def change_seller_role(db: AsyncSession, user: User, new_role: str | None, actor: User, ip_address: str | None = None) -> User:
    if new_role and new_role not in SELLER_ROLES:
        raise SellerUserServiceError(f"Invalid seller role: {new_role}")
    if user.seller_role in _ADMIN_EQUIVALENT and new_role not in _ADMIN_EQUIVALENT:
        if await _other_admin_count(db, user.id) == 0:
            raise SellerUserServiceError("Cannot change role — this is the only Seller Admin.")

    old = user.seller_role
    user.seller_role = new_role
    db.add(UserActivityLog(
        user_id=user.id, event_type=UserActivityType.ROLE_CHANGED,
        message=f"Seller role changed from {old or 'SELLER_ADMIN (default)'} to {new_role or 'SELLER_ADMIN (default)'}",
        actor_id=actor.id,
    ))
    await db.commit()
    await db.refresh(user)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Seller Role Changed",
        resource=user.full_name, resource_id=str(user.id), ip_address=ip_address,
        before_value={"seller_role": old}, after_value={"seller_role": new_role},
    )
    return user


async def disable_user(db: AsyncSession, user: User, actor: User, ip_address: str | None = None) -> User:
    if actor.id == user.id:
        raise SellerUserServiceError("You cannot disable your own account.")
    if user.seller_role in _ADMIN_EQUIVALENT and await _other_admin_count(db, user.id) == 0:
        raise SellerUserServiceError("Cannot disable — this is the only Seller Admin.")

    user.is_active = False
    db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.DISABLED, message="Seller Portal access disabled", actor_id=actor.id))
    await db.commit()
    await db.refresh(user)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Seller User Disabled",
        resource=user.full_name, resource_id=str(user.id), ip_address=ip_address,
    )
    return user


async def enable_user(db: AsyncSession, user: User, actor: User, ip_address: str | None = None) -> User:
    user.is_active = True
    db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.ENABLED, message="Seller Portal access enabled", actor_id=actor.id))
    await db.commit()
    await db.refresh(user)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Seller User Enabled",
        resource=user.full_name, resource_id=str(user.id), ip_address=ip_address,
    )
    return user


async def reset_password(db: AsyncSession, user: User, actor: User, ip_address: str | None = None):
    temp_password = _generate_temp_password()
    user.hashed_password = hash_password(temp_password)
    user.must_change_password = True
    db.add(UserActivityLog(user_id=user.id, event_type=UserActivityType.PASSWORD_RESET, message="Password reset by administrator", actor_id=actor.id))
    await db.commit()
    await db.refresh(user)
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Seller User Password Reset",
        resource=user.full_name, resource_id=str(user.id), ip_address=ip_address,
    )

    from app.core.config import get_settings
    from app.services import email_service
    subject, body_text = email_service.build_password_reset_email(
        full_name=user.full_name, email=user.email, temp_password=temp_password,
        login_url=f"{get_settings().frontend_base_url}/login",
    )
    email_result = await email_service.send_email(db, to=user.email, subject=subject, body=body_text)

    return user, temp_password, email_result


async def delete_user(db: AsyncSession, user: User, actor: User, ip_address: str | None = None) -> None:
    if actor.id == user.id:
        raise SellerUserServiceError("You cannot delete your own account.")
    if user.seller_role in _ADMIN_EQUIVALENT and await _other_admin_count(db, user.id) == 0:
        raise SellerUserServiceError("Cannot delete — this is the only Seller Admin.")

    name, uid = user.full_name, user.id
    await db.execute(UserActivityLog.__table__.delete().where(UserActivityLog.user_id == user.id))
    await db.delete(user)
    await db.commit()
    await record_seller_event(
        db, seller_user_id=actor.id, customer_id=None, action="Seller User Deleted",
        resource=name, resource_id=str(uid), ip_address=ip_address,
    )


# ── Activity / login history — reuse user_admin_service directly ────
# UserActivityLog is keyed by user_id only, not org-scoped, so these work
# unmodified for Seller staff.

async def get_activity(db: AsyncSession, user_id: int, limit: int = 100) -> list[UserActivityLog]:
    return await user_admin_service.get_activity(db, user_id, limit)


async def get_login_history(db: AsyncSession, user_id: int, limit: int = 100) -> list[UserActivityLog]:
    return await user_admin_service.get_login_history(db, user_id, limit)


async def actor_name_map(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    return await user_admin_service.actor_name_map(db, ids)


def activity_to_out(event: UserActivityLog, name_map: dict[int, str]) -> dict:
    return user_admin_service.activity_to_out(event, name_map)


# ── Output shaping ───────────────────────────────────────────────

def user_to_out(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "department": user.department,
        "seller_role": user.seller_role,
        "status": user_admin_service.status_of(user),
        "is_active": user.is_active,
        "last_login": user.last_login,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
    }
