"""Auth router — /api/auth/..."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    OrgOut,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserOut,
)
from app.services.auth_service import (
    AuthError,
    authenticate_user,
    change_password,
    get_org_by_id,
    issue_tokens,
    refresh_access_token,
    register_user,
)
from app.core.deps import get_current_user
from app.models.user import User
from app.services import session_service
from app.services.session_service import SeatLimitError
from app.core.security import decode_token
from jose import JWTError

router = APIRouter(prefix="/auth", tags=["auth"])

# Same tokenUrl as core/deps.oauth2_scheme; kept separate here so this
# module doesn't have to import the full deps.get_current_user chain just
# to read an optional bearer token for /logout.
_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def _http_401(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _http_400(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _http_403(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    try:
        user = await authenticate_user(db, body.email, body.password, ip_address=ip_address, user_agent=user_agent)
    except AuthError as exc:
        raise _http_401(str(exc))

    try:
        tokens = await issue_tokens(db, user, ip_address=ip_address, user_agent=user_agent)
    except SeatLimitError as exc:
        raise _http_403(str(exc))
    org = await get_org_by_id(db, user.organization_id) if user.organization_id else None
    return TokenResponse(
        **tokens,
        user=UserOut.model_validate(user),
        organization=OrgOut.model_validate(org) if org else None,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        user, org = await register_user(db, body)
    except AuthError as exc:
        raise _http_400(str(exc))

    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    # Brand-new org/user — never exceeds a fresh org's max_users, but goes
    # through the same real session-creation path as every other login.
    tokens = await issue_tokens(db, user, ip_address=ip_address, user_agent=user_agent)
    return TokenResponse(
        **tokens,
        user=UserOut.model_validate(user),
        organization=OrgOut.model_validate(org),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    try:
        tokens = await refresh_access_token(db, body.refresh_token)
    except AuthError as exc:
        raise _http_401(str(exc))

    # Re-load user for the response
    payload = decode_token(tokens["access_token"])
    from app.services.auth_service import get_user_by_id, get_org_by_id
    user = await get_user_by_id(db, int(payload["sub"]))
    org = await get_org_by_id(db, user.organization_id) if user and user.organization_id else None
    return TokenResponse(
        **tokens,
        user=UserOut.model_validate(user),
        organization=OrgOut.model_validate(org) if org else None,
    )


@router.post("/forgot-password", response_model=MessageResponse)
async def forgot_password(body: ForgotPasswordRequest):
    # In production: generate a reset token, send email
    # Returning success regardless to avoid email enumeration
    return MessageResponse(message="If that email exists, a reset link has been sent.")


@router.post("/logout", response_model=MessageResponse)
async def logout(token: str | None = Depends(_oauth2_scheme), db: AsyncSession = Depends(get_db)):
    """
    Real server-side logout: revokes the UserSession tied to this access
    token's `sid` claim, which immediately frees the seat it was holding
    (see services/session_service.revoke_session). Tolerant of missing/
    stale/pre-TT tokens (no sid claim, already revoked, already expired) —
    logout always reports success since the client is discarding its
    tokens either way; there's nothing the caller needs to retry.
    """
    if token:
        try:
            payload = decode_token(token)
            sid = payload.get("sid")
            if sid:
                await session_service.revoke_session(db, sid)
        except JWTError:
            pass
    return MessageResponse(message="Logged out successfully.")


@router.post("/change-password", response_model=UserOut)
async def change_password_route(
    body: ChangePasswordRequest, request: Request,
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    """Self-service password change — the one path that clears
    must_change_password. Reachable regardless of that flag's state (it
    only depends on get_current_user, not on the flag being clear
    already), since a forced-reset account has to be able to call this
    before anything else. See auth_service.change_password for the
    current-password-required-unless-flagged rule."""
    ip_address = request.client.host if request.client else None
    try:
        updated = await change_password(
            db, user, current_password=body.current_password, new_password=body.new_password, ip_address=ip_address,
        )
    except AuthError as exc:
        raise _http_400(str(exc))
    return UserOut.model_validate(updated)


@router.get("/me", response_model=UserOut)
async def me(token: str, db: AsyncSession = Depends(get_db)):
    """Quick current-user check (real apps use Depends(get_current_user))."""
    try:
        payload = decode_token(token)
    except JWTError:
        raise _http_401("Invalid token")
    from app.services.auth_service import get_user_by_id
    user = await get_user_by_id(db, int(payload["sub"]))
    if not user:
        raise _http_401("User not found")
    return UserOut.model_validate(user)
