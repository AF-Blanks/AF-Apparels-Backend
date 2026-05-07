"""Admin — system settings, email templates, and audit log."""
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.auth_middleware import require_admin
from app.models.system import AuditLog
from app.schemas.email import EmailTemplateOut, EmailTemplateUpdate, PreviewRequest, PreviewResponse
from app.services.email_service import EmailService

router = APIRouter(prefix="/admin")


@router.get("/email-templates", response_model=list[EmailTemplateOut])
async def list_email_templates(db: AsyncSession = Depends(get_db)):
    svc = EmailService(db)
    return await svc.list_templates()


@router.get("/email-templates/{template_id}", response_model=EmailTemplateOut)
async def get_email_template(template_id: UUID, db: AsyncSession = Depends(get_db)):
    svc = EmailService(db)
    return await svc.get_template_by_id(template_id)


@router.patch("/email-templates/{template_id}", response_model=EmailTemplateOut)
async def update_email_template(
    template_id: UUID,
    payload: EmailTemplateUpdate,
    db: AsyncSession = Depends(get_db),
):
    svc = EmailService(db)
    tpl = await svc.get_template_by_id(template_id)
    update_fields = payload.model_dump(exclude_unset=True)
    for field, value in update_fields.items():
        setattr(tpl, field, value)
    await db.flush()
    await db.refresh(tpl)
    await db.commit()
    return tpl


@router.post("/email-templates/{template_id}/preview", response_model=PreviewResponse)
async def preview_email_template(
    template_id: UUID,
    data: PreviewRequest,
    db: AsyncSession = Depends(get_db),
):
    svc = EmailService(db)
    tpl = await svc.get_template_by_id(template_id)
    return PreviewResponse(
        subject=svc.render_template(tpl.subject, data.variables),
        body_html=svc.render_template(tpl.body_html, data.variables),
        body_text=svc.render_template(tpl.body_text, data.variables) if tpl.body_text else None,
    )


@router.post("/email-templates/{template_id}/test", status_code=status.HTTP_202_ACCEPTED)
async def send_test_email(
    template_id: UUID,
    data: PreviewRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Send a test render of the template to EMAIL_FROM_ADDRESS."""
    from app.core.config import settings
    svc = EmailService(db)
    tpl = await svc.get_template_by_id(template_id)

    def _send():
        import asyncio
        async def _async():
            async with __import__("app.core.database", fromlist=["AsyncSessionLocal"]).AsyncSessionLocal() as _db:
                _svc = EmailService(_db)
                await _svc.send(tpl.trigger_event, settings.EMAIL_FROM_ADDRESS, data.variables)
        asyncio.get_event_loop().run_until_complete(_async())

    background_tasks.add_task(_send)
    return {"message": f"Test email queued to {settings.EMAIL_FROM_ADDRESS}"}


# ── T206: Admin system settings ───────────────────────────────────────────────

@router.get("/settings")
async def get_platform_settings(
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return all platform settings as a key-value dict."""
    from app.models.system import Settings as PlatformSettings
    rows = (await db.execute(select(PlatformSettings))).scalars().all()
    return {row.key: row.value for row in rows}


@router.patch("/settings")
async def update_platform_settings(
    payload: dict,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Upsert platform settings. Invalidates Redis cache key 'platform_settings'."""
    from app.core.redis import redis_delete
    from app.models.system import Settings as PlatformSettings

    ALLOWED_KEYS = {
        "mov", "moq", "guest_pricing_mode", "tax_rate",
        "low_stock_threshold", "notification_email", "standard_shipping",
    }

    updated = {}
    for key, value in payload.items():
        if key not in ALLOWED_KEYS:
            continue
        result = await db.execute(
            select(PlatformSettings).where(PlatformSettings.key == key)
        )
        setting = result.scalar_one_or_none()
        if setting:
            setting.value = str(value)
        else:
            db.add(PlatformSettings(key=key, value=str(value)))
        updated[key] = str(value)

    await db.commit()
    await redis_delete("platform_settings")
    return {"updated": updated}


# ── T198: Audit Log ───────────────────────────────────────────────────────────

@router.get("/audit-log")
async def list_audit_log(
    admin_user_id: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, description="ISO date YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="ISO date YYYY-MM-DD"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Paginated audit log filterable by user, entity type/id, date range."""
    from datetime import datetime

    q = select(AuditLog).order_by(AuditLog.created_at.desc())

    if admin_user_id:
        from app.core.database import AsyncSessionLocal
        import uuid as _uuid
        try:
            q = q.where(AuditLog.admin_user_id == _uuid.UUID(admin_user_id))
        except ValueError:
            pass

    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    if entity_id:
        q = q.where(AuditLog.entity_id == entity_id)
    if date_from:
        try:
            q = q.where(AuditLog.created_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            q = q.where(AuditLog.created_at <= datetime.fromisoformat(date_to + "T23:59:59"))
        except ValueError:
            pass

    # Count
    from sqlalchemy import func, select as sa_select
    count_q = sa_select(func.count()).select_from(q.subquery())
    total = (await db.execute(count_q)).scalar_one() or 0

    offset = (page - 1) * page_size
    rows = (await db.execute(q.offset(offset).limit(page_size))).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": str(row.id),
                "admin_user_id": str(row.admin_user_id) if row.admin_user_id else None,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "old_values": row.old_values,
                "new_values": row.new_values,
                "ip_address": row.ip_address,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }
