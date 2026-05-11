"""Wholesale application service."""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError
from app.models.company import Company, CompanyUser
from app.models.discount_group import DiscountGroup
from app.models.user import User
from app.models.wholesale import WholesaleApplication
from app.schemas.wholesale import ApproveApplicationRequest, RejectApplicationRequest


class WholesaleService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list_applications(
        self,
        status: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[WholesaleApplication], int]:
        query = select(WholesaleApplication)
        if status:
            query = query.where(WholesaleApplication.status == status)
        query = query.order_by(WholesaleApplication.created_at.desc())

        result = await self.db.execute(query.offset((page - 1) * per_page).limit(per_page))
        applications = list(result.scalars().all())

        count_result = await self.db.execute(
            select(WholesaleApplication).where(
                WholesaleApplication.status == status if status else True
            )
        )
        total = len(list(count_result.scalars().all()))

        return applications, total

    async def get_application(self, application_id: uuid.UUID) -> WholesaleApplication:
        result = await self.db.execute(
            select(WholesaleApplication).where(WholesaleApplication.id == application_id)
        )
        application = result.scalar_one_or_none()
        if not application:
            raise NotFoundError("Application not found")
        return application

    async def approve(
        self,
        application_id: uuid.UUID,
        data: ApproveApplicationRequest,
        admin_user_id: uuid.UUID,
    ) -> Company:
        application = await self.get_application(application_id)
        if application.status != "pending":
            raise ConflictError(f"Application is already {application.status}")

        # Create company
        company = Company(
            name=application.company_name,
            tax_id=application.tax_id,
            business_type=application.business_type,
            website=application.website,
            status="active",
            pricing_tier_id=data.pricing_tier_id,
            shipping_tier_id=data.shipping_tier_id,
            admin_notes=data.admin_notes,
            tax_exempt=data.tax_exempt,
            # Copy extended registration fields from application
            phone=getattr(application, "phone", None),
            fax=getattr(application, "fax", None),
            company_email=getattr(application, "company_email", None),
            address_line1=getattr(application, "address_line1", None),
            address_line2=getattr(application, "address_line2", None),
            city=getattr(application, "city", None),
            state_province=getattr(application, "state_province", None),
            postal_code=getattr(application, "postal_code", None),
            country=getattr(application, "country", None),
            how_heard=getattr(application, "how_heard", None),
            num_employees=getattr(application, "num_employees", None),
            num_sales_reps=getattr(application, "num_sales_reps", None),
            secondary_business=getattr(application, "secondary_business", None),
            estimated_annual_volume=getattr(application, "estimated_annual_volume", None),
            ppac_number=getattr(application, "ppac_number", None),
            ppai_number=getattr(application, "ppai_number", None),
            asi_number=getattr(application, "asi_number", None),
        )
        self.db.add(company)
        await self.db.flush()

        # Assign discount group via company tags
        if data.discount_group_id:
            dg_result = await self.db.execute(
                select(DiscountGroup).where(DiscountGroup.id == data.discount_group_id)
            )
            dg = dg_result.scalar_one_or_none()
            if dg and dg.customer_tag:
                company.tags = [dg.customer_tag]

        # Find the user by email and assign to company as owner
        user_result = await self.db.execute(
            select(User).where(User.email == application.email)
        )
        user = user_result.scalar_one_or_none()
        if user:
            membership = CompanyUser(
                company_id=company.id,
                user_id=user.id,
                role="owner",
                is_active=True,
            )
            self.db.add(membership)
            # Retail users who submitted the activation form are inactive until approval
            if not user.is_active:
                user.is_active = True
                user.account_type = "wholesale"

        # Update application
        application.status = "approved"
        application.company_id = company.id
        application.reviewed_by_id = admin_user_id
        application.admin_notes = data.admin_notes
        await self.db.flush()

        # Send approval email directly (synchronous, non-fatal)
        from app.services.email_service import EmailService
        from app.core.config import settings as _settings
        email_svc = EmailService(self.db)
        try:
            email_svc.send_raw(
                to_email=application.email,
                subject="Your Wholesale Account Has Been Approved — AF Apparels",
                body_html=f"""
                <div style="font-family:sans-serif;max-width:600px;margin:0 auto">
                  <div style="background:#080808;padding:24px;text-align:center">
                    <span style="font-size:36px;font-weight:900;color:#1A5CFF">A</span>
                    <span style="font-size:36px;font-weight:900;color:#E8242A">F</span>
                    <span style="color:#fff;font-size:14px;margin-left:8px;letter-spacing:.1em">APPARELS</span>
                  </div>
                  <div style="padding:32px;background:#fff">
                    <h2 style="color:#059669">Account Approved! ✅</h2>
                    <p>Hi {application.first_name},</p>
                    <p>Great news! Your wholesale account for <b>{application.company_name}</b> has been approved.</p>
                    <p>You can now log in to the AF Apparels wholesale portal and start placing orders.</p>
                    <p style="margin:24px 0">
                      <a href="{_settings.FRONTEND_URL}/login"
                         style="background:#E8242A;color:#fff;padding:12px 28px;border-radius:6px;text-decoration:none;font-weight:700;display:inline-block">
                        Log In to Your Account →
                      </a>
                    </p>
                    <p style="color:#6b7280;font-size:13px">Questions? Call us at (214) 272-7213</p>
                    <p>— AF Apparels Team</p>
                  </div>
                </div>
                """,
            )
        except Exception:
            pass  # non-fatal — approval still goes through

        # QB customer sync (Celery — non-fatal if Celery is down)
        try:
            from app.tasks.quickbooks_tasks import sync_customer_to_qb
            sync_customer_to_qb.delay(str(company.id))
        except Exception:
            pass

        return company

    async def reject(
        self,
        application_id: uuid.UUID,
        data: RejectApplicationRequest,
        admin_user_id: uuid.UUID,
    ) -> WholesaleApplication:
        application = await self.get_application(application_id)
        if application.status != "pending":
            raise ConflictError(f"Application is already {application.status}")

        application.status = "rejected"
        application.rejection_reason = data.rejection_reason
        application.reviewed_by_id = admin_user_id
        application.admin_notes = data.admin_notes
        await self.db.flush()

        from app.tasks.email_tasks import send_wholesale_rejected_email
        send_wholesale_rejected_email.delay(str(application.id), data.rejection_reason)

        return application
