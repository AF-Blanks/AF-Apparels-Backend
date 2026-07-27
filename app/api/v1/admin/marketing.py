"""Admin email marketing — broadcast an email to active wholesale customers."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.company import Company, CompanyUser
from app.models.user import User

router = APIRouter(prefix="/admin/marketing", tags=["admin", "marketing"])


def _recipients_query():
    return (
        select(User.id, User.email, User.first_name)
        .join(CompanyUser, CompanyUser.user_id == User.id)
        .join(Company, Company.id == CompanyUser.company_id)
        .where(User.is_active.is_(True), CompanyUser.is_active.is_(True), Company.status == "active")
        .distinct()
    )


@router.get("/recipients-count")
async def get_recipients_count(db: AsyncSession = Depends(get_db)) -> dict:
    result = await db.execute(select(func.count()).select_from(_recipients_query().subquery()))
    return {"count": result.scalar_one()}


class CampaignSendRequest(BaseModel):
    subject: str = Field(..., min_length=1, max_length=255)
    body_html: str = Field(..., min_length=1)


@router.post("/send")
async def send_campaign(payload: CampaignSendRequest, db: AsyncSession = Depends(get_db)) -> dict:
    from app.tasks.email_tasks import send_marketing_email

    result = await db.execute(_recipients_query())
    recipients = result.all()
    if not recipients:
        raise HTTPException(status_code=422, detail="No active customers to send to")

    insert_result = await db.execute(
        text(
            "INSERT INTO marketing_campaigns (subject, body_html, recipient_count) "
            "VALUES (:subject, :body_html, :count) RETURNING id"
        ),
        {"subject": payload.subject, "body_html": payload.body_html, "count": len(recipients)},
    )
    campaign_id = insert_result.scalar_one()
    await db.commit()

    for user_id, email, first_name in recipients:
        send_marketing_email.delay(str(user_id), email, first_name, payload.subject, payload.body_html)

    return {"campaign_id": str(campaign_id), "recipient_count": len(recipients)}


@router.get("/campaigns")
async def list_campaigns(db: AsyncSession = Depends(get_db)) -> list[dict]:
    result = await db.execute(
        text(
            "SELECT id, subject, recipient_count, sent_at FROM marketing_campaigns "
            "ORDER BY sent_at DESC LIMIT 50"
        )
    )
    return [
        {
            "id": str(row.id),
            "subject": row.subject,
            "recipient_count": row.recipient_count,
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        }
        for row in result.all()
    ]
