"""Email Celery tasks — full implementation with 3-retry exponential backoff."""
import asyncio
import logging

from app.core.celery import celery_app
from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _run(coro):
    """Run an async coroutine from a sync Celery task."""
    return asyncio.run(coro)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_order_confirmation_email(self, order_id: str) -> dict:
    """Send order confirmation to all contacts with notify_order_confirmation=True."""
    try:
        async def _send():
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload
            from app.models.order import Order
            from app.models.company import Company, Contact
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(
                    select(Order).where(Order.id == order_id)
                )).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}

                company = (await db.execute(
                    select(Company).where(Company.id == order.company_id)
                )).scalar_one_or_none()

                contacts = (await db.execute(
                    select(Contact).where(
                        Contact.company_id == order.company_id,
                        Contact.notify_order_confirmation.is_(True),
                    )
                )).scalars().all()

                if not contacts:
                    return {"status": "skipped", "reason": "no_notify_contacts"}

                svc = EmailService(db)
                company_name = company.name if company else ""
                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"
                sent = 0
                for contact in contacts:
                    ok = svc.send_from_file(
                        template_name="order_received.html",
                        to_email=contact.email,
                        subject=f"Order Received — {order.order_number} | AF Apparels",
                        variables={
                            "contact_name": contact.first_name or "Valued Customer",
                            "order_number": order.order_number,
                            "company_name": company_name,
                            "order_date": order.created_at.strftime("%B %d, %Y"),
                            "order_total": f"${float(order.total):.2f}",
                            "order_url": order_url,
                        },
                    )
                    if ok:
                        sent += 1
                return {"status": "sent", "sent": sent, "order_id": order_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_order_shipped_email(self, order_id: str, tracking_number: str = "") -> dict:
    """Send shipping notification to all contacts with notify_order_shipped=True."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.order import Order
            from app.models.company import Contact
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(
                    select(Order).where(Order.id == order_id)
                )).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}

                contacts = (await db.execute(
                    select(Contact).where(
                        Contact.company_id == order.company_id,
                        Contact.notify_order_shipped.is_(True),
                    )
                )).scalars().all()

                if not contacts:
                    return {"status": "skipped", "reason": "no_notify_contacts"}

                tracking = tracking_number or order.tracking_number or ""
                carrier = order.carrier or ""
                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"

                svc = EmailService(db)
                sent = 0
                for contact in contacts:
                    ok = svc.send_from_file(
                        template_name="order_shipped.html",
                        to_email=contact.email,
                        subject=f"Your Order {order.order_number} Has Shipped! | AF Apparels",
                        variables={
                            "contact_name": contact.first_name or "Valued Customer",
                            "order_number": order.order_number,
                            "order_total": f"${float(order.total):.2f}",
                            "tracking_number": tracking,
                            "carrier": carrier,
                            "order_url": order_url,
                        },
                    )
                    if ok:
                        sent += 1
                return {"status": "sent", "sent": sent, "order_id": order_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_wholesale_approved_email(self, application_id: str, company_id: str) -> dict:
    """Notify applicant that their wholesale account was approved."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.wholesale import WholesaleApplication
            from app.services.email_service import EmailService
            async with AsyncSessionLocal() as db:
                app = (await db.execute(
                    select(WholesaleApplication).where(WholesaleApplication.id == application_id)
                )).scalar_one_or_none()
                if not app:
                    return {"status": "skipped", "reason": "application_not_found"}
                svc = EmailService(db)
                variables = {
                    "company_name": app.company_name,
                    "contact_name": app.contact_name,
                }
                ok = await svc.send("wholesale_approved", app.contact_email, variables)
                return {"status": "sent" if ok else "failed", "application_id": application_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_wholesale_rejected_email(self, application_id: str, reason: str) -> dict:
    """Notify applicant that their application was rejected."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.wholesale import WholesaleApplication
            from app.services.email_service import EmailService
            async with AsyncSessionLocal() as db:
                app = (await db.execute(
                    select(WholesaleApplication).where(WholesaleApplication.id == application_id)
                )).scalar_one_or_none()
                if not app:
                    return {"status": "skipped", "reason": "application_not_found"}
                svc = EmailService(db)
                variables = {
                    "company_name": app.company_name,
                    "contact_name": app.contact_name,
                    "reason": reason,
                }
                ok = await svc.send("wholesale_rejected", app.contact_email, variables)
                return {"status": "sent" if ok else "failed", "application_id": application_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_password_reset_email(self, user_id: str, reset_token: str) -> dict:
    """Send password reset link."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.user import User
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
                if not user:
                    return {"status": "skipped", "reason": "user_not_found"}
                svc = EmailService(db)
                reset_url = f"{settings.FRONTEND_URL}/auth/reset-password?token={reset_token}"
                variables = {"name": user.full_name or user.email, "reset_url": reset_url}
                ok = await svc.send("password_reset", user.email, variables)
                return {"status": "sent" if ok else "failed", "user_id": user_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_email_verification(self, user_id: str, verification_token: str) -> dict:
    """Send email address verification link."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.user import User
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
                if not user:
                    return {"status": "skipped", "reason": "user_not_found"}
                svc = EmailService(db)
                verify_url = f"{settings.FRONTEND_URL}/auth/verify-email?token={verification_token}"
                variables = {"name": user.full_name or user.email, "verify_url": verify_url}
                ok = await svc.send("email_verification", user.email, variables)
                return {"status": "sent" if ok else "failed", "user_id": user_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_user_invitation_email(self, invited_user_id: str, company_id: str, invite_token: str = "") -> dict:
    """Send portal invitation to a new company user."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.user import User
            from app.models.company import Company
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                user = (await db.execute(select(User).where(User.id == invited_user_id))).scalar_one_or_none()
                company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
                if not user or not company:
                    return {"status": "skipped", "reason": "user_or_company_not_found"}
                svc = EmailService(db)
                invite_url = f"{settings.FRONTEND_URL}/auth/accept-invite?token={invite_token}" if invite_token else f"{settings.FRONTEND_URL}/auth/login"
                variables = {
                    "name": user.full_name or user.email,
                    "company_name": company.name,
                    "invite_url": invite_url,
                }
                ok = await svc.send("user_invitation", user.email, variables)
                return {"status": "sent" if ok else "failed", "user_id": invited_user_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_rma_status_email(self, rma_id: str) -> dict:
    """Send RMA status update (approved or rejected)."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.rma import RMARequest
            from app.models.user import User
            from app.services.email_service import EmailService
            async with AsyncSessionLocal() as db:
                rma = (await db.execute(select(RMARequest).where(RMARequest.id == rma_id))).scalar_one_or_none()
                if not rma:
                    return {"status": "skipped", "reason": "rma_not_found"}
                user = (await db.execute(select(User).where(User.id == rma.submitted_by_id))).scalar_one_or_none()
                if not user:
                    return {"status": "skipped", "reason": "user_not_found"}
                event = "rma_approved" if rma.status == "approved" else "rma_rejected"
                svc = EmailService(db)
                variables = {
                    "rma_number": rma.rma_number,
                    "status": rma.status,
                    "resolution_notes": rma.admin_notes or "",
                }
                ok = await svc.send(event, user.email, variables)
                return {"status": "sent" if ok else "failed", "rma_id": rma_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_invoice_email(self, order_id: str) -> dict:
    """Send invoice notification to all contacts with notify_invoices=True."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.order import Order
            from app.models.company import Contact
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(
                    select(Order).where(Order.id == order_id)
                )).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}

                contacts = (await db.execute(
                    select(Contact).where(
                        Contact.company_id == order.company_id,
                        Contact.notify_invoices.is_(True),
                    )
                )).scalars().all()

                if not contacts:
                    return {"status": "skipped", "reason": "no_notify_contacts"}

                invoice_html = f"<tr><td style='padding:4px 12px 4px 0;color:#6b7280'>Invoice #</td><td><strong>{order.qb_invoice_id}</strong></td></tr>" if order.qb_invoice_id else ""
                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"
                svc = EmailService(db)
                sent = 0
                for contact in contacts:
                    ok = svc.send_raw(
                        to_email=contact.email,
                        subject=f"Invoice Ready – Order {order.order_number}",
                        body_html=f"""
                        <h2>Invoice Ready</h2>
                        <p>Hi {contact.first_name},</p>
                        <p>An invoice is ready for order <strong>{order.order_number}</strong>.</p>
                        <table style="border-collapse:collapse;margin:16px 0">
                          <tr><td style="padding:4px 12px 4px 0;color:#6b7280">Order #</td><td><strong>{order.order_number}</strong></td></tr>
                          {invoice_html}
                          <tr><td style="padding:4px 12px 4px 0;color:#6b7280">Total</td><td><strong>${float(order.total):.2f}</strong></td></tr>
                        </table>
                        <p><a href="{order_url}" style="background:#1d4ed8;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;display:inline-block">View & Download Invoice</a></p>
                        <p style="color:#6b7280;font-size:13px">Payment terms: Net 30. Please remit payment referencing your order number.</p>
                        <p style="color:#6b7280;font-size:13px">AF Apparels Wholesale</p>
                        """,
                    )
                    if ok:
                        sent += 1
                return {"status": "sent", "sent": sent, "order_id": order_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_order_cancelled_email(self, order_id: str, reason: str = "") -> dict:
    """Notify all contacts with notify_order_confirmation=True when an order is cancelled."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.order import Order
            from app.models.company import Contact
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(
                    select(Order).where(Order.id == order_id)
                )).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}

                contacts = (await db.execute(
                    select(Contact).where(
                        Contact.company_id == order.company_id,
                        Contact.notify_order_confirmation.is_(True),
                    )
                )).scalars().all()

                if not contacts:
                    return {"status": "skipped", "reason": "no_notify_contacts"}

                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"
                svc = EmailService(db)
                sent = 0
                for contact in contacts:
                    ok = svc.send_from_file(
                        template_name="order_cancelled.html",
                        to_email=contact.email,
                        subject=f"Order {order.order_number} Cancelled | AF Apparels",
                        variables={
                            "contact_name": contact.first_name or "Valued Customer",
                            "order_number": order.order_number,
                            "order_total": f"${float(order.total):.2f}",
                            "reason": reason,
                            "order_url": order_url,
                        },
                    )
                    if ok:
                        sent += 1
                return {"status": "sent", "sent": sent, "order_id": order_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_payment_failed_email(self, order_id: str) -> dict:
    """Notify buyer of a failed payment."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.order import Order
            from app.models.user import User
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}
                user = (await db.execute(select(User).where(User.id == order.created_by_id))).scalar_one_or_none()
                if not user:
                    return {"status": "skipped", "reason": "user_not_found"}
                svc = EmailService(db)
                retry_url = f"{settings.FRONTEND_URL}/orders/{order_id}"
                variables = {
                    "order_number": order.order_number,
                    "total": str(order.total),
                    "retry_url": retry_url,
                }
                ok = await svc.send("payment_failed", user.email, variables)
                return {"status": "sent" if ok else "failed", "order_id": order_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)
