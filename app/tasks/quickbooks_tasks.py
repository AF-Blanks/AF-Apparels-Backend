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
    """Run a coroutine in a fresh event loop. Call only ONCE per task execution.

    Disposes the shared asyncpg engine pool before closing the loop so that
    on Celery retries the new event loop gets fresh connections instead of
    hitting 'Future attached to a different loop'.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            from app.core.database import engine as _engine
            loop.run_until_complete(_engine.dispose())
        except Exception:
            pass
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
            # Single session holds FOR UPDATE from check through commit — same
            # race-free pattern as sync_variant_to_qb.
            async with AsyncSessionLocal() as session:
                # ── 1. Lock the company row ───────────────────────────────────
                company = (await session.execute(
                    select(Company)
                    .where(Company.id == uuid.UUID(company_id))
                    .with_for_update()
                )).scalar_one_or_none()

                if not company:
                    await _log_attempt("company", company_id, "failed", "Company not found")
                    return None

                # ── 2. Already synced? Return immediately ─────────────────────
                if company.qb_customer_id:
                    logger.info(
                        "sync_customer_to_qb — already synced, skipping:"
                        " company=%s qb_customer_id=%s",
                        company_id, company.qb_customer_id,
                    )
                    return {"status": "success", "qb_customer_id": company.qb_customer_id}

                # ── 3. Snapshot data (lock still held) ───────────────────────
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

                # ── 4. QB API call (session + lock still held) ────────────────
                # create_customer already does find-or-create by DisplayName,
                # so this is idempotent even without the DB lock.
                svc = await QuickBooksService().initialize()
                qb_id = await asyncio.to_thread(svc.create_customer, name, email, ref_id=ref)
                logger.info("sync_customer_to_qb QB customer ready — qb_id=%s", qb_id)

                # ── 5. Save in the SAME session and commit atomically ─────────
                company.qb_customer_id = qb_id
                await session.commit()
                logger.info(
                    "qb_customer_id saved to DB: company=%s qb_customer_id=%s",
                    company_id, qb_id,
                )

            # ── 6. Log success ────────────────────────────────────────────────
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
                    # Fast path: company.qb_customer_id (QB Accounting integer like "2").
                    # Guard: QB Payments flow may have written the company UUID here —
                    # UUIDs contain hyphens; reject them and fall back to QBSyncLog.
                    raw_qb_id = order.company.qb_customer_id if order.company else None
                    if raw_qb_id and "-" not in raw_qb_id:
                        qb_customer_id = raw_qb_id
                        logger.info(
                            "sync_order_invoice_to_qb qb_customer_id from company column: %s",
                            qb_customer_id,
                        )
                    else:
                        if raw_qb_id:
                            logger.warning(
                                "sync_order_invoice_to_qb company.qb_customer_id is a UUID (%s)"
                                " — QB Payments overwrote it; falling back to QBSyncLog",
                                raw_qb_id,
                            )
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
                        logger.info(
                            "sync_order_invoice_to_qb qb_customer_id from QBSyncLog: %s",
                            qb_customer_id,
                        )

                # Snapshot all needed fields before the session closes.
                # Also look up qb_item_id per SKU so invoices can reference QB items.
                from app.models.product import ProductVariant as _PV
                sku_to_qb_item: dict[str, str | None] = {}
                for i in order.items:
                    if i.sku and i.sku not in sku_to_qb_item:
                        pv = (await session.execute(
                            select(_PV).where(_PV.sku == i.sku)
                        )).scalar_one_or_none()
                        sku_to_qb_item[i.sku] = pv.qb_item_id if pv else None

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
                            "qb_item_id": sku_to_qb_item.get(i.sku),
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


@celery_app.task(bind=True, max_retries=5)
def sync_variant_to_qb(self, variant_id: str):
    """Sync a ProductVariant to QuickBooks as an Inventory Item.

    Creates the QB item if it doesn't exist, or updates price/cost if it does.
    Writes the QB item Id back to product_variants.qb_item_id.
    """

    async def _run_all():
        from app.core.database import AsyncSessionLocal
        from app.models.product import ProductVariant
        from app.models.inventory import InventoryRecord
        from app.services.quickbooks_service import QuickBooksService
        from sqlalchemy import select, func
        from sqlalchemy.orm import selectinload

        try:
            # Single session holds the FOR UPDATE lock from check through commit.
            # A concurrent worker blocks on SELECT FOR UPDATE until this session
            # commits (releasing the lock), then it re-reads and sees qb_item_id
            # already set — so it skips creation and returns immediately.
            async with AsyncSessionLocal() as session:
                # ── 1. Lock the row ───────────────────────────────────────────
                variant = (await session.execute(
                    select(ProductVariant)
                    .options(selectinload(ProductVariant.product))
                    .where(ProductVariant.id == uuid.UUID(variant_id))
                    .with_for_update()
                )).scalar_one_or_none()

                if not variant:
                    logger.warning("sync_variant_to_qb: variant %s not found", variant_id)
                    return None

                # ── 2. Already synced? Return immediately ─────────────────────
                if variant.qb_item_id:
                    logger.info(
                        "sync_variant_to_qb — already synced, skipping:"
                        " variant=%s qb_item_id=%s",
                        variant_id, variant.qb_item_id,
                    )
                    return {"status": "success", "qb_item_id": variant.qb_item_id}

                # ── 3. Snapshot data (session + lock still held) ──────────────
                total_stock = int((await session.execute(
                    select(func.coalesce(func.sum(InventoryRecord.quantity), 0))
                    .where(InventoryRecord.variant_id == uuid.UUID(variant_id))
                )).scalar() or 0)

                product_name = variant.product.name if variant.product else "Product"
                sku = variant.sku
                item_name = f"{product_name} - {sku}"
                unit_price = float(variant.retail_price)
                cost = float(variant.cost_per_item) if variant.cost_per_item else None

                # ── 4. QB API call (session stays open, lock held throughout) ─
                svc = await QuickBooksService().initialize()
                qb_item_id = await asyncio.to_thread(
                    svc.find_or_create_item, sku, item_name, unit_price, cost, total_stock,
                )
                logger.info(
                    "sync_variant_to_qb QB item ready — variant=%s qb_item_id=%s",
                    variant_id, qb_item_id,
                )

                # ── 5. Save in the SAME session and commit atomically ─────────
                # variant is tracked by this session (loaded above), so ORM
                # dirty-tracking will flush the change on commit.
                variant.qb_item_id = qb_item_id
                await session.commit()
                # Lock released here — concurrent worker now unblocks and sees
                # qb_item_id already set, skipping duplicate creation.
                logger.info("qb_item_id saved to DB: variant=%s qb_item_id=%s", variant_id, qb_item_id)

            return {"status": "success", "qb_item_id": qb_item_id}

        except Exception as exc:
            logger.exception("sync_variant_to_qb error: %s", exc)
            raise

    try:
        return _run_async(_run_all())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=5)
def sync_inventory_to_qb(self, variant_id: str):
    """Push the current total stock for a variant to QuickBooks.

    If the variant has no QB item yet, falls back to sync_variant_to_qb
    (which creates the item and sets initial QtyOnHand in one call).
    """

    async def _run_all():
        from app.core.database import AsyncSessionLocal
        from app.models.product import ProductVariant
        from app.models.inventory import InventoryRecord
        from app.services.quickbooks_service import QuickBooksService
        from sqlalchemy import select, func

        try:
            async with AsyncSessionLocal() as session:
                variant = (await session.execute(
                    select(ProductVariant).where(ProductVariant.id == uuid.UUID(variant_id))
                )).scalar_one_or_none()

                if not variant:
                    logger.warning("sync_inventory_to_qb: variant %s not found", variant_id)
                    return None

                if not variant.qb_item_id:
                    sync_variant_to_qb.delay(variant_id)
                    # Re-queue this inventory sync to run after variant sync completes
                    sync_inventory_to_qb.apply_async(args=[variant_id], countdown=30)
                    return {"status": "deferred", "reason": "variant not yet synced to QB"}

                total_stock = int((await session.execute(
                    select(func.coalesce(func.sum(InventoryRecord.quantity), 0))
                    .where(InventoryRecord.variant_id == uuid.UUID(variant_id))
                )).scalar() or 0)

                qb_item_id = variant.qb_item_id
                unit_price = float(variant.retail_price)
                cost = float(variant.cost_per_item) if variant.cost_per_item else None

            svc = await QuickBooksService().initialize()
            await asyncio.to_thread(svc.update_item, qb_item_id, unit_price, cost, total_stock)
            logger.info("sync_inventory_to_qb success — variant=%s qty=%d", variant_id, total_stock)
            return {"status": "success", "qty_on_hand": total_stock}

        except Exception as exc:
            logger.exception("sync_inventory_to_qb error: %s", exc)
            raise

    try:
        return _run_async(_run_all())
    except Exception as exc:
        delay = 60 * (2 ** self.request.retries)
        raise self.retry(exc=exc, countdown=delay)
