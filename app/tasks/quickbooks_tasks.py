"""QuickBooks sync Celery tasks.

T194: sync_customer_to_qb, sync_order_invoice_to_qb
Both use exponential backoff with max 5 retries.
All attempts are logged to qb_sync_log.
"""
import asyncio
import uuid

from app.core.celery import celery_app


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _log_attempt(entity_type: str, entity_id: str, status: str, error: str | None, qb_entity_id: str | None = None):
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
            log = QBSyncLog(
                entity_type=entity_type,
                entity_id=uuid.UUID(entity_id),
            )
            session.add(log)
        log.status = status
        log.attempt_count = (log.attempt_count or 0) + 1
        log.error_message = error
        if qb_entity_id:
            log.qb_entity_id = qb_entity_id
        await session.commit()


@celery_app.task(
    bind=True,
    max_retries=5,
)
def sync_customer_to_qb(self, company_id: str):
    """Sync a Company to QuickBooks as a Customer."""

    async def _fetch():
        from app.core.database import AsyncSessionLocal
        from app.models.company import Company
        from app.models.user import User
        from app.services.quickbooks_service import QuickBooksService
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Company).where(Company.id == uuid.UUID(company_id))
            )
            company = result.scalar_one_or_none()
            if not company:
                return None
            user_result = await session.execute(
                select(User)
                .where(User.company_id == uuid.UUID(company_id))
                .where(User.role == "owner")
                .limit(1)
            )
            owner = user_result.scalar_one_or_none()
            email = owner.email if owner else f"noreply+{company_id[:8]}@afapparels.com"
            name, ref = company.name, str(company.id)

        # Initialize service with live DB tokens (outside the company session)
        svc = await QuickBooksService().initialize()
        return name, email, ref, svc

    try:
        result = _run_async(_fetch())
        if not result:
            _run_async(_log_attempt("company", company_id, "failed", "Company not found"))
            return

        company_name, email, ref_id, svc = result
        qb_id = svc.create_customer(company_name, email, ref_id=ref_id)
        _run_async(_log_attempt("company", company_id, "success", None, qb_entity_id=qb_id))
        return {"status": "success", "qb_customer_id": qb_id}

    except Exception as exc:
        _run_async(_log_attempt("company", company_id, "retry", str(exc)))
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(
    bind=True,
    max_retries=5,
)
def sync_order_invoice_to_qb(self, order_id: str):
    """Sync an Order to QuickBooks as an Invoice."""

    async def _fetch():
        from app.core.database import AsyncSessionLocal
        from app.models.order import Order
        from app.models.system import QBSyncLog
        from app.services.quickbooks_service import QuickBooksService
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Order)
                .options(selectinload(Order.items), selectinload(Order.company))
                .where(Order.id == uuid.UUID(order_id))
            )
            order = result.scalar_one_or_none()
            if not order:
                return None

            log_result = await session.execute(
                select(QBSyncLog)
                .where(QBSyncLog.entity_type == "company")
                .where(QBSyncLog.entity_id == order.company_id)
                .where(QBSyncLog.status == "success")
                .order_by(QBSyncLog.created_at.desc())
                .limit(1)
            )
            log = log_result.scalar_one_or_none()
            qb_customer_id = log.qb_entity_id if log else None
            # Snapshot the fields we need before the session closes
            order_data = {
                "company_id": str(order.company_id),
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

        # Initialize service with live DB tokens (outside the order session)
        svc = await QuickBooksService().initialize()
        return order_data, qb_customer_id, svc

    try:
        result = _run_async(_fetch())
        if not result:
            _run_async(_log_attempt("order", order_id, "failed", "Order not found"))
            return

        order_data, qb_customer_id, svc = result

        if not qb_customer_id:
            sync_customer_to_qb.delay(order_data["company_id"])
            raise RuntimeError("QB customer not yet synced — will retry after company sync")

        qb_invoice_id = svc.create_invoice(
            qb_customer_id=qb_customer_id,
            order_number=order_data["order_number"],
            line_items=order_data["items"],
            total=order_data["total"],
        )

        async def _update():
            from app.core.database import AsyncSessionLocal
            from app.models.order import Order
            from sqlalchemy import select

            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Order).where(Order.id == uuid.UUID(order_id))
                )
                o = result.scalar_one_or_none()
                if o:
                    o.qb_invoice_id = qb_invoice_id
                    o.qb_sync_status = "synced"
                    await session.commit()

        _run_async(_update())
        _run_async(_log_attempt("order", order_id, "success", None, qb_entity_id=qb_invoice_id))
        return {"status": "success", "qb_invoice_id": qb_invoice_id}

    except Exception as exc:
        _run_async(_log_attempt("order", order_id, "retry", str(exc)))
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)
