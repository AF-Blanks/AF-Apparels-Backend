# backend/app/api/v1/admin/users.py
"""Admin user management — list, create, update, delete, reset password."""
import secrets
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.security import hash_password
from app.models.company import CompanyUser, Company
from app.models.user import User

router = APIRouter(prefix="/admin/users", tags=["admin", "users"])


def _user_to_dict(user: User) -> dict:
    if user.is_admin:
        role = "admin"
    elif getattr(user, "is_staff", False):
        role = "staff"
    elif user.company_memberships:
        role = "customer"   # a shop account, not something assignable here
    else:
        role = "staff"

    company_name: str | None = None
    if user.company_memberships:
        first = user.company_memberships[0]
        if first.company:
            company_name = first.company.name

    return {
        "id": str(user.id),
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "full_name": f"{user.first_name} {user.last_name}".strip(),
        "role": role,
        "is_admin": user.is_admin,
        "is_staff": bool(getattr(user, "is_staff", False)),
        "is_active": user.is_active,
        "email_verified": user.email_verified,
        "company_name": company_name,
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "created_at": user.created_at.isoformat() if hasattr(user, "created_at") and user.created_at else None,
    }


def _base_query():
    return select(User).options(
        selectinload(User.company_memberships).selectinload(CompanyUser.company)
    )


@router.get("")
async def list_users(
    q: str | None = None,
    role: str | None = None,
    status: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    query = _base_query()

    if q:
        query = query.where(
            or_(
                User.first_name.ilike(f"%{q}%"),
                User.last_name.ilike(f"%{q}%"),
                User.email.ilike(f"%{q}%"),
            )
        )

    if role == "admin":
        query = query.where(User.is_admin.is_(True))
    elif role in ("staff", "customer"):
        query = query.where(User.is_admin.is_(False))

    if status == "active":
        query = query.where(User.is_active.is_(True))
    elif status == "inactive":
        query = query.where(User.is_active.is_(False))

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    query = query.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    users = result.scalars().all()

    return {"items": [_user_to_dict(u) for u in users], "total": total}


@router.post("", status_code=201)
async def create_user(
    payload: dict,
    db: AsyncSession = Depends(get_db),
):
    email: str = (payload.get("email") or "").strip().lower()
    first_name: str = (payload.get("first_name") or "").strip()
    last_name: str = (payload.get("last_name") or "").strip()
    role: str = payload.get("role", "staff")
    send_welcome: bool = bool(payload.get("send_welcome_email", False))

    if not email or not first_name:
        raise HTTPException(status_code=422, detail="email and first_name are required")
    # Only these two are assignable here — "customer" accounts are created by the
    # customer themselves (or via the customers screen), not from user management.
    if role not in ("admin", "staff"):
        raise HTTPException(status_code=422, detail="role must be 'admin' or 'staff'")

    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    raw_password: str = payload.get("password") or secrets.token_urlsafe(12)

    user = User(
        email=email,
        first_name=first_name,
        last_name=last_name,
        hashed_password=hash_password(raw_password),
        is_admin=(role == "admin"),
        is_staff=(role == "staff"),
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    if send_welcome:
        try:
            from app.services.email_service import EmailService
            from app.core.config import settings as _settings
            svc = EmailService(db)
            svc.send_raw(
                to_email=email,
                subject="Welcome to AF Apparels",
                body_html=f"""
                <div style="font-family:sans-serif;max-width:560px;margin:0 auto">
                  <div style="background:#080808;padding:24px;text-align:center">
                    <span style="font-size:36px;font-weight:900;color:#1A5CFF">A</span>
                    <span style="font-size:36px;font-weight:900;color:#E8242A">F</span>
                    <span style="color:#fff;font-size:14px;margin-left:8px;letter-spacing:.1em">APPARELS</span>
                  </div>
                  <div style="padding:32px;background:#fff">
                    <h2 style="color:#2A2830;margin:0 0 16px">Welcome, {first_name}!</h2>
                    <p style="color:#374151;font-size:14px;line-height:1.7">Your admin account has been created.</p>
                    <p style="color:#374151;font-size:14px"><b>Email:</b> {email}<br>
                    <b>Temporary password:</b> {raw_password}</p>
                    <p style="color:#374151;font-size:14px">Please log in and change your password.</p>
                    <p style="color:#7A7880;font-size:12px;margin-top:24px">AF Apparels Wholesale</p>
                  </div>
                </div>
                """,
            )
        except Exception:
            pass  # non-fatal

    # Reload with relationships
    result = await db.execute(_base_query().where(User.id == user.id))
    user = result.scalar_one()
    return _user_to_dict(user)


@router.patch("/{user_id}")
async def update_user(
    user_id: UUID,
    payload: dict,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        _base_query().where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if "first_name" in payload and payload["first_name"]:
        user.first_name = payload["first_name"].strip()
    if "last_name" in payload:
        user.last_name = (payload["last_name"] or "").strip()
    if "email" in payload and payload["email"]:
        new_email = payload["email"].strip().lower()
        if new_email != user.email:
            conflict = await db.execute(select(User).where(User.email == new_email))
            if conflict.scalar_one_or_none():
                raise HTTPException(status_code=409, detail="Email already in use")
            user.email = new_email
    if "role" in payload:
        new_role = payload["role"]
        if new_role not in ("admin", "staff"):
            raise HTTPException(status_code=422, detail="role must be 'admin' or 'staff'")
        # Exactly one of the two — never both.
        user.is_admin = (new_role == "admin")
        user.is_staff = (new_role == "staff")
    if "is_active" in payload:
        user.is_active = bool(payload["is_active"])

    await db.commit()

    result = await db.execute(_base_query().where(User.id == user.id))
    user = result.scalar_one()
    return _user_to_dict(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user)
    await db.commit()


@router.post("/{user_id}/reset-password", status_code=204)
async def reset_user_password(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    from app.services.auth_service import AuthService
    svc = AuthService(db)
    await svc.send_password_reset(user.email)
