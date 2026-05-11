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


@router.get("/validate-activation-token")
async def validate_activation_token(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Validate a retail activation token and return pre-fill data."""
    from app.models.user import User

    result = await db.execute(
        select(User).where(User.activation_token == token)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=400, detail="INVALID_TOKEN")

    if user.activation_token_expires and user.activation_token_expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="TOKEN_EXPIRED")

    return {
        "valid": True,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
    }


@router.post("/activate-account")
async def activate_account(
    payload: ActivateAccountSchema,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Submit wholesale application form from a retail activation link.

    Sets the user's password and creates a WholesaleApplication for admin review.
    User stays inactive until admin approves.
    """
    from app.models.user import User
    from app.models.wholesale import WholesaleApplication

    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    result = await db.execute(
        select(User).where(User.activation_token == payload.token)
    )
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=400, detail="Invalid activation token")

    if user.activation_token_expires and user.activation_token_expires < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="TOKEN_EXPIRED")

    # Update user — set password and profile, but keep inactive (pending admin approval)
    user.first_name = payload.first_name
    user.last_name = payload.last_name
    user.phone = payload.phone
    user.hashed_password = hash_password(payload.password)
    user.is_active = False  # admin must approve before they can log in
    user.activation_token = None
    user.activation_token_expires = None

    # Create WholesaleApplication so it appears in the admin approval queue
    application = WholesaleApplication(
        company_name=payload.company_name,
        business_type=payload.business_type,
        website=payload.website,
        tax_id=payload.tax_id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=user.email,
        phone=payload.phone,
        company_email=payload.company_email,
        address_line1=payload.address_line1,
        address_line2=payload.address_line2,
        city=payload.city,
        state_province=payload.state_province,
        postal_code=payload.postal_code,
        country=payload.country,
        how_heard=payload.how_heard,
        secondary_business=payload.secondary_business,
        num_employees=payload.num_employees,
        num_sales_reps=payload.num_sales_reps,
        status="pending",
    )
    db.add(application)
    await db.commit()

    # Send emails (non-fatal)
    from app.services.email_service import EmailService
    email_svc = EmailService(db)
    try:
        email_svc.send_raw(
            to_email=user.email,
            subject="We Received Your Application — AF Apparels",
            body_html=email_svc._base_template(
                f'<h2 style="color:#1B3A5C;font-size:22px;font-weight:800;margin:0 0 8px">'
                f'Application Received! ✅</h2>'
                f'<p style="color:#374151;margin:0 0 16px">Hi {payload.first_name},</p>'
                f'<p style="color:#374151;margin:0 0 16px">'
                f'We received your wholesale application for <b>{payload.company_name}</b>. '
                f'Our team will review within 1–2 business days and notify you of our decision.</p>'
                f'<p style="color:#374151;margin:0">Questions? Call <b>(214) 272-7213</b></p>'
            ),
        )
    except Exception:
        pass

    if settings.ADMIN_NOTIFICATION_EMAIL:
        try:
            email_svc.send_raw(
                to_email=settings.ADMIN_NOTIFICATION_EMAIL,
                subject=f"New Wholesale Application (Retail Conversion): {payload.company_name}",
                body_html=email_svc._base_template(
                    f'<h2 style="color:#1B3A5C;font-size:20px;font-weight:800;margin:0 0 8px">'
                    f'New Application (Retail → Wholesale)</h2>'
                    f'<div style="background:#F9F8F4;border-radius:8px;padding:16px 20px;margin-bottom:20px">'
                    f'<table style="width:100%">'
                    f'<tr><td style="font-size:12px;color:#6b7280;padding:3px 0">Company</td>'
                    f'<td style="font-size:13px;font-weight:700;text-align:right">{payload.company_name}</td></tr>'
                    f'<tr><td style="font-size:12px;color:#6b7280;padding:3px 0">Name</td>'
                    f'<td style="font-size:13px;text-align:right">{payload.first_name} {payload.last_name}</td></tr>'
                    f'<tr><td style="font-size:12px;color:#6b7280;padding:3px 0">Email</td>'
                    f'<td style="font-size:13px;text-align:right">{user.email}</td></tr>'
                    f'<tr><td style="font-size:12px;color:#6b7280;padding:3px 0">Type</td>'
                    f'<td style="font-size:13px;text-align:right">{payload.business_type}</td></tr>'
                    f'</table></div>'
                    f'<p style="margin:0">'
                    f'<a href="{settings.FRONTEND_URL}/admin/customers" '
                    f'style="background:#1B3A5C;color:#fff;padding:12px 24px;border-radius:6px;'
                    f'font-weight:700;text-decoration:none;font-size:14px;display:inline-block">'
                    f'Review Application →</a></p>'
                ),
            )
        except Exception:
            pass

    return {"message": "Application submitted. We'll review within 1–2 business days."}


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
