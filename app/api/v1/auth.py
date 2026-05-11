# backend/app/api/v1/auth.py
"""Auth API router."""
import secrets
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings

from app.core.database import get_db
from app.core.security import get_token_jti, hash_password, create_access_token
from app.schemas.auth import (
    ActivateAccountSchema,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    LoginResponse,
    RegisterWholesaleRequest,
    ResendActivationSchema,
    ResetPasswordRequest,
    TokenRefreshResponse,
)
from app.services.auth_service import AuthService
from app.schemas.wholesale import WholesaleApplicationOut

router = APIRouter()

REFRESH_COOKIE_NAME = "refresh_token"
REFRESH_COOKIE_MAX_AGE = 7 * 24 * 60 * 60  # 7 days


@router.post("/register-wholesale", response_model=WholesaleApplicationOut, status_code=201)
async def register_wholesale(
    data: RegisterWholesaleRequest,
    db: AsyncSession = Depends(get_db),
) -> WholesaleApplicationOut:
    """Submit a wholesale registration application."""
    service = AuthService(db)
    application = await service.register_wholesale(data)
    return WholesaleApplicationOut.model_validate(application)


@router.post("/login", response_model=LoginResponse)
async def login(
    data: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """Authenticate and return an access token. Sets httpOnly refresh cookie."""
    service = AuthService(db)
    login_response, refresh_token = await service.login(data.email, data.password)

    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=REFRESH_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,  # type: ignore[arg-type]
        path="/api/v1/refresh",
        domain=settings.COOKIE_DOMAIN,
    )
    return login_response


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Blacklist access token and clear refresh cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
        jti = get_token_jti(token)
        user_id = getattr(request.state, "user_id", None)
        if user_id and jti:
            service = AuthService(db)
            await service.logout(user_id, jti)

    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path="/api/v1/refresh",
        secure=settings.COOKIE_SECURE,
    )

@router.post("/refresh", response_model=TokenRefreshResponse)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenRefreshResponse:
    """Issue a new access token using the httpOnly refresh cookie."""
    from app.core.exceptions import UnauthorizedError

    refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_token:
        raise UnauthorizedError("Refresh token not found")

    service = AuthService(db)
    token_response, new_refresh_token = await service.refresh_tokens(refresh_token)

    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=new_refresh_token,
        max_age=REFRESH_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,  # type: ignore[arg-type]
        path="/api/v1/refresh",
        domain=settings.COOKIE_DOMAIN,
    )
    return token_response


@router.post("/forgot-password", status_code=204)
async def forgot_password(
    data: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Send password reset email (always returns 204 to prevent enumeration)."""
    service = AuthService(db)
    await service.send_password_reset(data.email)


@router.post("/reset-password", status_code=204)
async def reset_password(
    data: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    service = AuthService(db)
    await service.reset_password(data.token, data.new_password)


@router.post("/activate-account")
async def activate_account(
    payload: ActivateAccountSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set password and activate a retail account using an activation token."""
    from app.models.user import User

    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    result = await db.execute(
        select(User).where(User.activation_token == payload.token)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid activation token")

    if user.activation_token_expires and user.activation_token_expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Activation token expired. Request a new one.")

    user.hashed_password = hash_password(payload.password)
    user.is_active = True
    user.activation_token = None
    user.activation_token_expires = None
    await db.commit()
    await db.refresh(user)

    # Return JWT so the user is immediately logged in
    access_token = create_access_token(
        str(user.id),
        extra_claims={"is_admin": False, "account_type": "retail"},
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": str(user.id),
            "email": user.email,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "is_admin": False,
            "account_type": "retail",
        },
    }


@router.post("/resend-activation")
async def resend_activation(
    payload: ResendActivationSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Re-send the activation email for a retail account."""
    from app.models.user import User
    from app.services.email_service import EmailService

    result = await db.execute(
        select(User).where(User.email == payload.email.lower())
    )
    user = result.scalar_one_or_none()

    # Generic response to avoid email enumeration
    generic = {"message": "If this email has a pending account, an activation link has been sent."}

    if not user or getattr(user, "account_type", "wholesale") != "retail":
        return generic

    if user.is_active:
        return {"message": "Account already active. Please log in."}

    token = secrets.token_urlsafe(32)
    user.activation_token = token
    user.activation_token_expires = datetime.now(timezone.utc) + timedelta(days=7)
    await db.commit()

    email_svc = EmailService(db)
    try:
        email_svc.send_retail_account_activation(
            customer_email=user.email,
            first_name=user.first_name,
            activation_url=f"{settings.FRONTEND_URL}/activate-account?token={token}",
            order_number=None,
        )
    except Exception:
        pass  # non-fatal

    return generic
