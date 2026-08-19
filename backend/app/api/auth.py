"""Auth router — /api/auth/..."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.connection import get_db
from app.schemas.auth import (
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
    get_org_by_id,
    issue_tokens,
    refresh_access_token,
    register_user,
)
from app.core.security import decode_token
from jose import JWTError

router = APIRouter(prefix="/auth", tags=["auth"])


def _http_401(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _http_400(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    ip_address = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    try:
        user = await authenticate_user(db, body.email, body.password, ip_address=ip_address, user_agent=user_agent)
    except AuthError as exc:
        raise _http_401(str(exc))

    tokens = issue_tokens(user)
    org = await get_org_by_id(db, user.organization_id) if user.organization_id else None
    return TokenResponse(
        **tokens,
        user=UserOut.model_validate(user),
        organization=OrgOut.model_validate(org) if org else None,
    )


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)):
    try:
        user, org = await register_user(db, body)
    except AuthError as exc:
        raise _http_400(str(exc))

    tokens = issue_tokens(user)
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
async def logout():
    # Token invalidation handled client-side; server-side blacklist can be added later
    return MessageResponse(message="Logged out successfully.")


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
