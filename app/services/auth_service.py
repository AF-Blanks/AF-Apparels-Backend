# backend/app/services/auth_service.py
"""Authentication service."""
import secrets
import uuid
from datetime import UTC, datetime, timedelta
import logging
logger = logging.getLogger(__name__)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import get_settings, settings

from app.core.exceptions import (
    AccountNotActivatedError,
    AccountSuspendedError,
    ConflictError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from app.core.redis import redis_delete, redis_get, redis_set
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.company import Company, CompanyUser
from app.models.user import User
from app.schemas.auth import LoginResponse, RegisterWholesaleRequest, TokenRefreshResponse
from app.models.wholesale import WholesaleApplication

REFRESH_COOKIE_NAME = "refresh_token"
REFRESH_TOKEN_EXPIRE_DAYS = 7


def _build_access_token_claims(user: User, membership: CompanyUser | None) -> dict:
    """Build extra JWT claims from user + company membership."""
    claims: dict = {
        "is_admin": user.is_admin,
        "account_type": getattr(user, "account_type", "wholesale"),
    }
    if membership:
        claims["company_id"] = str(membership.company_id)
        claims["company_role"] = membership.role
        if membership.company and membership.company.pricing_tier_id:
            claims["pricing_tier_id"] = str(membership.company.pricing_tier_id)
    return claims


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def login(self, email: str, password: str) -> LoginResponse:
        result = await self.db.execute(select(User).where(User.email == email.lower()))
        user = result.scalar_one_or_none()

        # Retail user who hasn't activated yet — surface specific error before password check
        if user and getattr(user, "account_type", "wholesale") == "retail" and not user.is_active:
            raise AccountNotActivatedError()

        if not user or not verify_password(password, user.hashed_password or ""):
            raise UnauthorizedError("Invalid email or password")

        if not user.is_active:
            raise UnauthorizedError("Your account is inactive")

        # Check company status for non-admin users
        membership: CompanyUser | None = None
        is_retail = getattr(user, "account_type", "wholesale") == "retail"
        if not user.is_admin and not is_retail:
            mem_result = await self.db.execute(
                select(CompanyUser)
                .where(CompanyUser.user_id == user.id, CompanyUser.is_active == True)
                .limit(1)
            )
            membership = mem_result.scalar_one_or_none()
            if membership:
                await self.db.refresh(membership, ["company"])
                company: Company = membership.company
                if company.status == "suspended":
                    raise AccountSuspendedError()
            else:
                # No company membership — check if they have a wholesale application
                app_result = await self.db.execute(
                    select(WholesaleApplication)
                    .where(WholesaleApplication.email == user.email)
                    .order_by(WholesaleApplication.created_at.desc())
                    .limit(1)
                )
                application = app_result.scalar_one_or_none()
                if application and application.status == "pending":
                    raise UnauthorizedError(
                        "Your wholesale application is pending review. "
                        "You will receive an email once your account is approved."
                    )
                elif application and application.status == "rejected":
                    raise UnauthorizedError(
                        "Your wholesale application was not approved. "
                        "Please contact us at (214) 272-7213 for more information."
                    )

        # Update last_login
        user.last_login = datetime.now(UTC)
        await self.db.flush()

        extra_claims = _build_access_token_claims(user, membership)
        access_token = create_access_token(str(user.id), extra_claims=extra_claims)
        refresh_token = create_refresh_token(str(user.id))

        # Store refresh token in Redis (7-day TTL)
        await redis_set(
            f"refresh:{user.id}",
            refresh_token,
            expire=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        )

        return LoginResponse(access_token=access_token, token_type="bearer"), refresh_token

    async def logout(self, user_id: str, access_jti: str) -> None:
        """Blacklist the access token JTI and delete the refresh token."""
        await redis_set(f"blacklist:{access_jti}", "1", expire=15 * 60)
        await redis_delete(f"refresh:{user_id}")

    async def refresh_tokens(self, refresh_token: str) -> TokenRefreshResponse:
        """Validate refresh token, issue new access token."""
        from jose import JWTError

        try:
            payload = decode_token(refresh_token)
        except JWTError:
            raise UnauthorizedError("Invalid or expired refresh token")

        if payload.get("type") != "refresh":
            raise UnauthorizedError("Wrong token type")

        user_id = payload.get("sub")
        stored = await redis_get(f"refresh:{user_id}")
        if stored != refresh_token:
            raise UnauthorizedError("Refresh token has been rotated or revoked")

        result = await self.db.execute(select(User).where(User.id == uuid.UUID(user_id)))
        user = result.scalar_one_or_none()
        if not user or not user.is_active:
            raise UnauthorizedError("User not found or inactive")

        mem_result = await self.db.execute(
            select(CompanyUser)
            .where(CompanyUser.user_id == user.id, CompanyUser.is_active == True)
            .limit(1)
        )
        membership = mem_result.scalar_one_or_none()
        if membership:
            await self.db.refresh(membership, ["company"])

        extra_claims = _build_access_token_claims(user, membership)
        new_access_token = create_access_token(user_id, extra_claims=extra_claims)
        new_refresh_token = create_refresh_token(user_id)

        # Rotate refresh token (one-time use)
        await redis_set(
            f"refresh:{user_id}",
            new_refresh_token,
            expire=REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        )

        return TokenRefreshResponse(access_token=new_access_token), new_refresh_token

    async def register_wholesale(self, data: RegisterWholesaleRequest) -> WholesaleApplication:
        """Create a user account and wholesale application with 'pending' status."""
        # Check for duplicate email
        existing = await self.db.execute(select(User).where(User.email == data.email.lower()))
        if existing.scalar_one_or_none():
            raise ConflictError("An account with this email already exists")

        user = User(
            email=data.email.lower(),
            hashed_password=hash_password(data.password),
            first_name=data.first_name,
            last_name=data.last_name,
            phone=data.phone,
            is_admin=False,
            is_active=True,
            email_verified=False,
        )
        self.db.add(user)
        await self.db.flush()

        application = WholesaleApplication(
            company_name=data.company_name,
            tax_id=data.tax_id,
            business_type=data.business_type,
            website=data.website,
            expected_monthly_volume=data.expected_monthly_volume,
            first_name=data.first_name,
            last_name=data.last_name,
            email=data.email.lower(),
            phone=data.phone,
            status="pending",
            # Extended registration fields
            company_email=getattr(data, "company_email", None),
            address_line1=getattr(data, "address_line1", None),
            address_line2=getattr(data, "address_line2", None),
            city=getattr(data, "city", None),
            state_province=getattr(data, "state_province", None),
            postal_code=getattr(data, "postal_code", None),
            country=getattr(data, "country", None),
            how_heard=getattr(data, "how_heard", None),
            num_employees=getattr(data, "num_employees", None),
            num_sales_reps=getattr(data, "num_sales_reps", None),
            secondary_business=getattr(data, "secondary_business", None),
            estimated_annual_volume=getattr(data, "estimated_annual_volume", None),
            ppac_number=getattr(data, "ppac_number", None),
            ppai_number=getattr(data, "ppai_number", None),
            asi_number=getattr(data, "asi_number", None),
            fax=getattr(data, "fax", None),
        )
        self.db.add(application)
        await self.db.flush()

        # ✅ Yeh lagao
        from app.services.email_service import EmailService
        email_svc = EmailService(self.db)
        try:
            email_svc.send_raw(
                to_email=application.email,
                subject="We Received Your Wholesale Application — AF Apparels",
                body_html=f"""
                    <div style="font-family:sans-serif;max-width:600px;margin:0 auto">
                    <div style="background:#080808;padding:24px;text-align:center">
                        <span style="font-size:36px;font-weight:900;color:#1A5CFF">A</span>
                        <span style="font-size:36px;font-weight:900;color:#E8242A">F</span>
                        <span style="color:#fff;font-size:14px;margin-left:8px;letter-spacing:.1em">APPARELS</span>
                    </div>
                    <div style="padding:32px;background:#fff">
                        <h2>Application Received! ✅</h2>
                        <p>Hi {data.first_name},</p>
                        <p>We received your wholesale application for <b>{data.company_name}</b>.</p>
                        <p>Our team will review within <b>1-2 business days</b> and notify you of our decision.</p>
                        <p>Questions? Call <b>(214) 272-7213</b></p>
                        <p>— AF Apparels Team</p>
                    </div>
                    </div>
                """,
            )
        except Exception:
            pass

        # ✅ Admin notification
        if settings.ADMIN_NOTIFICATION_EMAIL:
            try:
                email_svc.send_raw(
                    to_email=settings.ADMIN_NOTIFICATION_EMAIL,
                    subject=f"New Wholesale Application — {data.company_name}",
                    body_html=f"""
                        <h2>New Wholesale Application</h2>
                        <p><b>Company:</b> {data.company_name}</p>
                        <p><b>Name:</b> {data.first_name} {data.last_name}</p>
                        <p><b>Email:</b> {data.email}</p>
                        <p><b>Phone:</b> {data.phone}</p>
                        <p><b>Business Type:</b> {data.business_type}</p>
                        <a href="{settings.FRONTEND_URL}/admin/customers">Review Application →</a>
                    """,
                )
            except Exception:
                pass  # non-fatal

        return application

    async def send_password_reset(self, email: str) -> None:
        result = await self.db.execute(select(User).where(User.email == email.lower()))
        user = result.scalar_one_or_none()
        if not user:
            return

        token = secrets.token_urlsafe(32)
        user.password_reset_token = token
        user.password_reset_expires = datetime.now(UTC) + timedelta(hours=1)
        await self.db.flush()

        reset_url = f"{settings.FRONTEND_URL}/reset-password?token={token}"

        # ✅ Direct call — no Celery
        from app.services.email_service import EmailService
        email_svc = EmailService(self.db)
        try:
            await email_svc.send(
                trigger_event="password_reset",
                to_email=user.email,
                variables={
                    "first_name": user.first_name or "there",
                    "reset_url": reset_url,
                    "expiry_hours": 1,
                },
            )
        except Exception:
            logger.warning("Password reset email failed for %s", email)

    async def reset_password(self, token: str, new_password: str) -> None:
        result = await self.db.execute(
            select(User).where(User.password_reset_token == token)
        )
        user = result.scalar_one_or_none()
        if not user or not user.password_reset_expires:
            raise ValidationError("Invalid or expired reset token")
        if user.password_reset_expires < datetime.now(UTC):
            raise ValidationError("Reset token has expired")

        user.hashed_password = hash_password(new_password)
        user.password_reset_token = None
        user.password_reset_expires = None
