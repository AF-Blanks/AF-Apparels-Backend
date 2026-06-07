"""QuickBooks sync Celery tasks.

T194: sync_customer_to_qb, sync_order_invoice_to_qb
Both use exponential backoff with max 5 retries.
All attempts are logged to qb_sync_log.

Each task runs ALL async work (DB fetch, QB service init, logging) inside a
single _run_async() call so every coroutine shares one event loop — this
prevents asyncpg "Future attached to a different loop" errors that occur when
multiple _run_async() calls create separate loops while asyncpg's pool holds
connections bound to an earlier loop.
"""
import asyncio
import logging
import uuid

from app.core.celery import celery_app
from app.core.config import settings

logger = logging.getLogger(__name__)
logger.info("quickbooks_tasks loaded — broker=%s", settings.CELERY_BROKER_URL)


def _run_async(coro):
    """Run a coroutine in a fresh event loop. Call only ONCE per task execution."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _log_attempt(
    entity_type: str,
    entity_id: str,
    status: str,
    error: str | None,
    qb_entity_id: str | None = None,
) -> None:
    """Upsert a QBSyncLog row. Always opens its own fresh session."""
    from app.core.database import AsyncSessionLocal
    from app.models.system import QBSyncLog
    from sqlalchemy import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(QBSyncLog)
            .where(QBSyncLog.entity_type == entity_type)
            .where(QBSyncLog.entity_id == uuid.UUID(entity_id))
            .order_by(QBSyncLog.created_at.desc())
            .limit(1)
        )
        log = result.scalar_one_or_none()
        if log is None:
            log = QBSyncLog(entity_type=entity_type, entity_id=uuid.UUID(entity_id))
            session.add(log)
        log.status = status
        log.attempt_count = (log.attempt_count or 0) + 1
        log.error_message = error
        if qb_entity_id:
            log.qb_entity_id = qb_entity_id
        await session.commit()


@celery_app.task(bind=True, max_retries=5)
def sync_customer_to_qb(self, company_id: str):
    """Sync a Company to QuickBooks as a Customer."""
    logger.info("sync_customer_to_qb started — company_id=%s", company_id)

    async def _run_all():
        from app.core.database import AsyncSessionLocal
        from app.models.company import Company, CompanyUser
        from app.services.quickbooks_service import QuickBooksService
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        try:
            # ── 1. Fetch company + owner email ────────────────────────────────
            async with AsyncSessionLocal() as session:
                company = (await session.execute(
                    select(Company).where(Company.id == uuid.UUID(company_id))
                )).scalar_one_or_none()

                if not company:
                    await _log_attempt("company", company_id, "failed", "Company not found")
                    return None

                # User model has no company_id — look up owner via CompanyUser
                cu = (await session.execute(
                    select(CompanyUser)
                    .options(selectinload(CompanyUser.user))
                    .where(
                        CompanyUser.company_id == uuid.UUID(company_id),
                        CompanyUser.role == "owner",
                    )
                    .limit(1)
                )).scalar_one_or_none()

                email = (
                    cu.user.email
                    if (cu and cu.user)
                    else f"noreply+{company_id[:8]}@afapparels.com"
                )
                name, ref = company.name, str(company.id)

            # ── 2. Load live QB tokens from app_settings ──────────────────────
            svc = await QuickBooksService().initialize()

            # ── 3. Call QB API (sync, run in thread so it doesn't block loop) ─
            qb_id = await asyncio.to_thread(svc.create_customer, name, email, ref_id=ref)
            logger.info("sync_customer_to_qb success — qb_id=%s", qb_id)

            # ── 4. Persist QB customer ID back to company row ─────────────────
            from app.models.company import Company
            async with AsyncSessionLocal() as session:
                company = (await session.execute(
                    select(Company).where(Company.id == uuid.UUID(company_id))
                )).scalar_one_or_none()
                if company:
                    company.qb_customer_id = qb_id
                    await session.commit()

            # ── 5. Log success ────────────────────────────────────────────────
            await _log_attempt("company", company_id, "success", None, qb_entity_id=qb_id)
            return {"status": "success", "qb_customer_id": qb_id}

        except Exception as exc:
            logger.exception("sync_customer_to_qb error: %s", exc)
            await _log_attempt("company", company_id, "retry", str(exc))
            raise  # re-raised so the outer except can trigger Celery retry

    try:
        return _run_async(_run_all())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=5)
def sync_order_invoice_to_qb(self, order_id: str):
    """Sync an Order to QuickBooks as an Invoice.

    Handles three customer types:
    - True guest (company_id is NULL): create QB customer on-the-fly from guest fields.
    - Retail/wholesale with company: use company.qb_customer_id, fall back to QBSyncLog.
    - Company not yet in QB: dispatch sync_customer_to_qb and retry.
    """

    async def _run_all():
        from app.core.database import AsyncSessionLocal
        from app.models.order import Order
        from app.models.system import QBSyncLog
        from app.services.quickbooks_service import QuickBooksService
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        try:
            # ── 1. Fetch order and resolve QB customer identity ───────────────
            qb_customer_id: str | None = None
            is_guest_no_company = False
            guest_display_name: str | None = None
            guest_email_addr: str | None = None

            async with AsyncSessionLocal() as session:
                order = (await session.execute(
                    select(Order)
                    .options(selectinload(Order.items), selectinload(Order.company))
                    .where(Order.id == uuid.UUID(order_id))
                )).scalar_one_or_none()

                if not order:
                    await _log_attempt("order", order_id, "failed", "Order not found")
                    return None

                if order.company_id is None:
                    # True guest — no Company row; will create QB customer on-the-fly
                    is_guest_no_company = True
                    guest_display_name = order.guest_name or f"Guest {order.order_number}"
                    guest_email_addr = (
                        order.guest_email or f"guest+{order_id[:8]}@afapparels.com"
                    )
                else:
                    # Wholesale or retail-with-company order
                    # Check company.qb_customer_id column first (fastest path)
                    if order.company and order.company.qb_customer_id:
                        qb_customer_id = order.company.qb_customer_id
                    else:
                        # Fall back to QBSyncLog for a prior successful sync
                        log = (await session.execute(
                            select(QBSyncLog)
                            .where(QBSyncLog.entity_type == "company")
                            .where(QBSyncLog.entity_id == order.company_id)
                            .where(QBSyncLog.status == "success")
                            .order_by(QBSyncLog.created_at.desc())
                            .limit(1)
                        )).scalar_one_or_none()
                        qb_customer_id = log.qb_entity_id if log else None

                # Snapshot all needed fields before the session closes
                order_data = {
                    "company_id": str(order.company_id) if order.company_id else None,
                    "order_number": order.order_number,
                    "total": float(order.total),
                    "items": [
                        {
                            "description": f"{i.product_name} ({i.sku})",
                            "quantity": i.quantity,
                            "unit_price": float(i.unit_price),
                            "amount": float(i.line_total),
                        }
                        for i in order.items
                    ],
                }

            # ── 2. Load live QB tokens ────────────────────────────────────────
            svc = await QuickBooksService().initialize()

            # ── 3. Resolve QB customer ────────────────────────────────────────
            if is_guest_no_company:
                # Create (or find by DisplayName) a QB customer from guest fields
                qb_customer_id = await asyncio.to_thread(
                    svc.create_customer, guest_display_name, guest_email_addr
                )
                logger.info(
                    "sync_order_invoice_to_qb guest customer resolved — qb_id=%s",
                    qb_customer_id,
                )
            elif not qb_customer_id:
                # Company exists but hasn't been synced to QB yet — dispatch and retry
                sync_customer_to_qb.delay(order_data["company_id"])
                raise RuntimeError("QB customer not yet synced — retrying after company sync")

            # ── 4. Create invoice (sync, run in thread) ───────────────────────
            qb_invoice_id = await asyncio.to_thread(
                svc.create_invoice,
                qb_customer_id=qb_customer_id,
                order_number=order_data["order_number"],
                line_items=order_data["items"],
                total=order_data["total"],
            )
            logger.info("sync_order_invoice_to_qb success — qb_invoice_id=%s", qb_invoice_id)

            # ── 5. Persist QB invoice ID back to the order row ────────────────
            async with AsyncSessionLocal() as session:
                o = (await session.execute(
                    select(Order).where(Order.id == uuid.UUID(order_id))
                )).scalar_one_or_none()
                if o:
                    o.qb_invoice_id = qb_invoice_id
                    o.qb_sync_status = "synced"
                    await session.commit()

            # ── 6. Log success ────────────────────────────────────────────────
            await _log_attempt("order", order_id, "success", None, qb_entity_id=qb_invoice_id)
            return {"status": "success", "qb_invoice_id": qb_invoice_id}

        except Exception as exc:
            logger.exception("sync_order_invoice_to_qb error: %s", exc)
            await _log_attempt("order", order_id, "retry", str(exc))
            raise

    try:
        return _run_async(_run_all())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)
