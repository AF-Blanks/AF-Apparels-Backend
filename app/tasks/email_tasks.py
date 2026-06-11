"""Email Celery tasks — full implementation with 3-retry exponential backoff."""
import asyncio
import logging

from app.core.celery import celery_app
from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _run(coro):
    """Run an async coroutine from a sync Celery task."""
    return asyncio.run(coro)


def _fmt_items(items) -> list[dict]:
    """Convert order items to template-safe dicts."""
    return [
        {
            "product_name": getattr(item, "product_name", "") or "",
            "color": getattr(item, "color", "") or "",
            "size": getattr(item, "size", "") or "",
            "quantity": getattr(item, "quantity", 0),
            "unit_price": f"${float(item.unit_price):.2f}" if getattr(item, "unit_price", None) is not None else "",
            "line_total": f"${float(item.line_total):.2f}" if getattr(item, "line_total", None) is not None else "",
        }
        for item in (items or [])
    ]


# ─── Order received ──────────────────────────────────────────────────────────

def _build_order_vars(order, company_name: str, contact_name: str, order_url: str) -> dict:
    """Build full template variables dict for order_received.html."""
    import json
    items_list = _fmt_items(getattr(order, "items", []))

    shipping_addr = ""
    snap = order.shipping_address_snapshot
    if snap:
        try:
            d = json.loads(snap) if isinstance(snap, str) else snap
            parts = [
                d.get("address_line1") or "",
                d.get("address_line2") or "",
                d.get("city") or "",
                d.get("state_province") or "",
                d.get("postal_code") or "",
            ]
            shipping_addr = ", ".join(p for p in parts if p)
        except Exception:
            pass

    pm_map = {
        "card": "Credit Card",
        "ach": "ACH / Bank Transfer",
        "net30": "Net 30",
        "net60": "Net 60",
        "wire": "Wire Transfer",
        "cash": "Cash",
    }
    payment_display = pm_map.get(order.payment_method or "", order.payment_method or "")

    subtotal = float(order.subtotal or 0)
    shipping = float(order.shipping_cost or 0)
    tax = float(order.tax_amount or 0)
    fee = float(order.convenience_fee or 0)
    total = float(order.total or 0)

    return {
        "contact_name": contact_name,
        "order_number": order.order_number,
        "company_name": company_name,
        "order_date": order.created_at.strftime("%B %d, %Y"),
        "order_total": f"${total:.2f}",
        "order_url": order_url,
        "items_list": items_list,
        "subtotal": f"${subtotal:.2f}",
        "shipping_cost": f"${shipping:.2f}",
        "tax_amount": f"${tax:.2f}",
        "convenience_fee": f"${fee:.2f}" if fee else "",
        "payment_method": payment_display,
        "shipping_address": shipping_addr,
        "has_items": bool(items_list),
    }


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_order_confirmation_email(self, order_id: str) -> dict:
    """Send order received email — handles both wholesale contacts and guest orders."""
    try:
        async def _send():
            import uuid as _uuid
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload
            from app.models.order import Order, OrderItem
            from app.models.company import Company, Contact
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(
                    select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
                )).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}

                svc = EmailService(db)
                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"
                sent = 0

                if order.is_guest_order or not order.company_id:
                    # Guest order — send directly to guest email
                    to_email = order.guest_email or ""
                    if not to_email:
                        return {"status": "skipped", "reason": "no_guest_email"}
                    contact_name = (order.guest_name or "Valued Customer").split()[0]
                    vars_ = _build_order_vars(order, "", contact_name, order_url)
                    ok = svc.send_from_file(
                        template_name="order_received.html",
                        to_email=to_email,
                        subject=f"Order Received — {order.order_number} | AF Apparels",
                        variables=vars_,
                    )
                    sent = 1 if ok else 0
                else:
                    # Wholesale order — send to all notify contacts
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
                    company_name = company.name if company else ""
                    for contact in contacts:
                        vars_ = _build_order_vars(
                            order, company_name,
                            contact.first_name or "Valued Customer",
                            order_url,
                        )
                        ok = svc.send_from_file(
                            template_name="order_received.html",
                            to_email=contact.email,
                            subject=f"Order Received — {order.order_number} | AF Apparels",
                            variables=vars_,
                        )
                        if ok:
                            sent += 1

                return {"status": "sent", "sent": sent, "order_id": order_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


# ─── Admin new order alert ────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_admin_new_order_email(self, order_id: str) -> dict:
    """Notify admin of a new order via admin_new_order.html template."""
    try:
        async def _send():
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload
            from app.models.order import Order
            from app.models.company import Company
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(
                    select(Order).options(selectinload(Order.items)).where(Order.id == order_id)
                )).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}

                admin_email = getattr(settings, "ADMIN_NOTIFICATION_EMAIL", "") or getattr(settings, "ADMIN_EMAIL", "")
                if not admin_email:
                    return {"status": "skipped", "reason": "no_admin_email_configured"}

                company_name = ""
                if order.company_id:
                    company = (await db.execute(
                        select(Company).where(Company.id == order.company_id)
                    )).scalar_one_or_none()
                    company_name = company.name if company else ""

                customer_display = (
                    order.guest_name or order.guest_email or "Guest"
                    if order.is_guest_order or not order.company_id
                    else company_name
                )

                pm_map = {
                    "card": "Credit Card", "ach": "ACH / Bank Transfer",
                    "net30": "Net 30", "net60": "Net 60",
                    "wire": "Wire Transfer", "cash": "Cash",
                }
                payment_display = pm_map.get(order.payment_method or "", order.payment_method or "")
                items_list = _fmt_items(getattr(order, "items", []))
                admin_order_url = f"{settings.FRONTEND_URL}/admin/orders/{order_id}"

                svc = EmailService(db)
                ok = svc.send_from_file(
                    template_name="admin_new_order.html",
                    to_email=admin_email,
                    subject=f"New Order {order.order_number} — ${float(order.total):.2f} | AF Apparels",
                    variables={
                        "order_number": order.order_number,
                        "order_date": order.created_at.strftime("%B %d, %Y %I:%M %p"),
                        "customer_display": customer_display,
                        "customer_email": order.guest_email or "",
                        "company_name": company_name,
                        "order_total": f"${float(order.total):.2f}",
                        "subtotal": f"${float(order.subtotal or 0):.2f}",
                        "shipping_cost": f"${float(order.shipping_cost or 0):.2f}",
                        "tax_amount": f"${float(order.tax_amount or 0):.2f}",
                        "payment_method": payment_display,
                        "shipping_method": order.shipping_method or "",
                        "items_list": items_list,
                        "admin_order_url": admin_order_url,
                        "is_guest": bool(order.is_guest_order or not order.company_id),
                    },
                )
                return {"status": "sent" if ok else "failed", "order_id": order_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


# ─── Order confirmed ─────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_order_confirmed_email(self, order_id: str) -> dict:
    """Notify contacts when the order is confirmed by admin."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.order import Order
            from app.models.company import Company, Contact
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}
                company = (await db.execute(select(Company).where(Company.id == order.company_id))).scalar_one_or_none()
                contacts = (await db.execute(
                    select(Contact).where(Contact.company_id == order.company_id, Contact.notify_order_confirmation.is_(True))
                )).scalars().all()
                if not contacts:
                    return {"status": "skipped", "reason": "no_notify_contacts"}
                svc = EmailService(db)
                company_name = company.name if company else ""
                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"
                sent = 0
                for contact in contacts:
                    ok = svc.send_from_file(
                        template_name="order_confirmed.html",
                        to_email=contact.email,
                        subject=f"Order Confirmed — {order.order_number} | AF Apparels",
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


# ─── Order processing ────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_order_processing_email(self, order_id: str) -> dict:
    """Notify contacts that the order is being processed."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.order import Order
            from app.models.company import Company, Contact
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}
                company = (await db.execute(select(Company).where(Company.id == order.company_id))).scalar_one_or_none()
                contacts = (await db.execute(
                    select(Contact).where(Contact.company_id == order.company_id, Contact.notify_order_confirmation.is_(True))
                )).scalars().all()
                if not contacts:
                    return {"status": "skipped", "reason": "no_notify_contacts"}
                svc = EmailService(db)
                company_name = company.name if company else ""
                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"
                sent = 0
                for contact in contacts:
                    ok = svc.send_from_file(
                        template_name="order_processing.html",
                        to_email=contact.email,
                        subject=f"Order {order.order_number} Is Being Processed | AF Apparels",
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


# ─── Order ready ─────────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_order_ready_email(self, order_id: str) -> dict:
    """Notify contacts that the order is packed and ready."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.order import Order
            from app.models.company import Company, Contact
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}
                company = (await db.execute(select(Company).where(Company.id == order.company_id))).scalar_one_or_none()
                contacts = (await db.execute(
                    select(Contact).where(Contact.company_id == order.company_id, Contact.notify_order_confirmation.is_(True))
                )).scalars().all()
                if not contacts:
                    return {"status": "skipped", "reason": "no_notify_contacts"}
                svc = EmailService(db)
                company_name = company.name if company else ""
                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"
                sent = 0
                for contact in contacts:
                    ok = svc.send_from_file(
                        template_name="order_ready.html",
                        to_email=contact.email,
                        subject=f"Order {order.order_number} Is Ready | AF Apparels",
                        variables={
                            "contact_name": contact.first_name or "Valued Customer",
                            "order_number": order.order_number,
                            "company_name": company_name,
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


# ─── Order shipped ───────────────────────────────────────────────────────────

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
                order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}
                contacts = (await db.execute(
                    select(Contact).where(Contact.company_id == order.company_id, Contact.notify_order_shipped.is_(True))
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


# ─── Ready for pickup ────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_ready_for_pickup_email(self, order_id: str) -> dict:
    """Notify contacts that a will-call order is ready for pickup."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.order import Order
            from app.models.company import Company, Contact
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}
                company = (await db.execute(select(Company).where(Company.id == order.company_id))).scalar_one_or_none()
                contacts = (await db.execute(
                    select(Contact).where(Contact.company_id == order.company_id, Contact.notify_order_confirmation.is_(True))
                )).scalars().all()
                if not contacts:
                    return {"status": "skipped", "reason": "no_notify_contacts"}
                svc = EmailService(db)
                company_name = company.name if company else ""
                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"
                sent = 0
                for contact in contacts:
                    ok = svc.send_from_file(
                        template_name="ready_for_pickup.html",
                        to_email=contact.email,
                        subject=f"Order {order.order_number} Ready for Pickup | AF Apparels",
                        variables={
                            "contact_name": contact.first_name or "Valued Customer",
                            "order_number": order.order_number,
                            "company_name": company_name,
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


# ─── Order delivered ─────────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_order_delivered_email(self, order_id: str) -> dict:
    """Notify contacts when the order is marked as delivered."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.order import Order
            from app.models.company import Company, Contact
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}
                company = (await db.execute(select(Company).where(Company.id == order.company_id))).scalar_one_or_none()
                contacts = (await db.execute(
                    select(Contact).where(Contact.company_id == order.company_id, Contact.notify_order_confirmation.is_(True))
                )).scalars().all()
                if not contacts:
                    return {"status": "skipped", "reason": "no_notify_contacts"}
                svc = EmailService(db)
                company_name = company.name if company else ""
                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"
                sent = 0
                for contact in contacts:
                    ok = svc.send_from_file(
                        template_name="order_delivered.html",
                        to_email=contact.email,
                        subject=f"Order {order.order_number} Delivered | AF Apparels",
                        variables={
                            "contact_name": contact.first_name or "Valued Customer",
                            "order_number": order.order_number,
                            "company_name": company_name,
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


# ─── Order cancelled ─────────────────────────────────────────────────────────

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
                order = (await db.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}
                contacts = (await db.execute(
                    select(Contact).where(Contact.company_id == order.company_id, Contact.notify_order_confirmation.is_(True))
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


# ─── Invoice / Purchase order ────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_invoice_email(self, order_id: str) -> dict:
    """Send invoice notification to all contacts with notify_invoices=True."""
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
                    select(Order).where(Order.id == order_id).options(selectinload(Order.items))
                )).scalar_one_or_none()
                if not order:
                    return {"status": "skipped", "reason": "order_not_found"}
                company = (await db.execute(select(Company).where(Company.id == order.company_id))).scalar_one_or_none()
                contacts = (await db.execute(
                    select(Contact).where(Contact.company_id == order.company_id, Contact.notify_invoices.is_(True))
                )).scalars().all()
                if not contacts:
                    return {"status": "skipped", "reason": "no_notify_contacts"}
                svc = EmailService(db)
                company_name = company.name if company else ""
                order_url = f"{settings.FRONTEND_URL}/account/orders/{order_id}"
                items = _fmt_items(getattr(order, "items", []))
                sent = 0
                for contact in contacts:
                    ok = svc.send_from_file(
                        template_name="purchase_order.html",
                        to_email=contact.email,
                        subject=f"Invoice Ready — Order {order.order_number} | AF Apparels",
                        variables={
                            "contact_name": contact.first_name or "Valued Customer",
                            "company_name": company_name,
                            "order_number": order.order_number,
                            "po_number": order.po_number or order.qb_invoice_id or "",
                            "order_date": order.created_at.strftime("%B %d, %Y"),
                            "order_total": f"${float(order.total):.2f}",
                            "items": items,
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


# ─── Wholesale application received ─────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_wholesale_application_received_email(self, to_email: str, contact_name: str, company_name: str) -> dict:
    """Notify applicant that their wholesale application was received."""
    try:
        async def _send():
            from app.services.email_service import EmailService
            async with AsyncSessionLocal() as db:
                svc = EmailService(db)
                ok = svc.send_from_file(
                    template_name="wholesale_application_received.html",
                    to_email=to_email,
                    subject="AF Apparels Wholesale Application Received",
                    variables={
                        "contact_name": contact_name or "Valued Customer",
                        "company_name": company_name or "",
                    },
                )
                return {"status": "sent" if ok else "failed"}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


# ─── Wholesale approved ──────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def send_wholesale_approved_email(self, application_id: str, company_id: str) -> dict:
    """Notify applicant that their wholesale account was approved."""
    try:
        async def _send():
            from sqlalchemy import select
            from app.models.wholesale import WholesaleApplication
            from app.services.email_service import EmailService
            from app.core.config import settings
            async with AsyncSessionLocal() as db:
                app = (await db.execute(
                    select(WholesaleApplication).where(WholesaleApplication.id == application_id)
                )).scalar_one_or_none()
                if not app:
                    return {"status": "skipped", "reason": "application_not_found"}
                svc = EmailService(db)
                ok = svc.send_from_file(
                    template_name="wholesale_approved.html",
                    to_email=app.email,
                    subject="Your AF Apparels Wholesale Account is Approved!",
                    variables={
                        "contact_name": app.first_name or "Valued Customer",
                        "company_name": app.company_name or "",
                        "login_url": f"{settings.FRONTEND_URL}/login",
                    },
                )
                return {"status": "sent" if ok else "failed", "application_id": application_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


# ─── Wholesale rejected ──────────────────────────────────────────────────────

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
                ok = svc.send_from_file(
                    template_name="wholesale_rejected.html",
                    to_email=app.email,
                    subject="AF Apparels Wholesale Application Update",
                    variables={
                        "contact_name": app.first_name or "Valued Customer",
                        "company_name": app.company_name or "",
                        "reason": reason or "",
                    },
                )
                return {"status": "sent" if ok else "failed", "application_id": application_id}
        return _run(_send())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


# ─── Auth / account tasks (DB-stored templates) ───────────────────────────────

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
                invite_url = (
                    f"{settings.FRONTEND_URL}/auth/accept-invite?token={invite_token}"
                    if invite_token
                    else f"{settings.FRONTEND_URL}/auth/login"
                )
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
