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


def _retry_delay(exc: Exception, retries: int) -> int:
    """Return retry countdown in seconds.

    QB 429 (rate limit hit) gets 300 s × 2^n so we back off hard and let
    the quota window reset before hammering again.  All other errors keep
    the original 60 s × 2^n schedule.
    """
    is_rate_limited = (
        hasattr(exc, "response") and getattr(exc.response, "status_code", 0) == 429
    ) or "429" in str(exc)
    base = 300 if is_rate_limited else 60
    return base * (2 ** retries)


def _failure_status(task) -> str:
    """Return the QBSyncLog status to record for a FAILED attempt.

    Returns "failed" only on the terminal attempt (no Celery retries left),
    otherwise "retry". This lets the admin dashboard — which filters on
    status == "failed" — surface genuinely dead syncs instead of every failed
    attempt staying stuck on "retry" forever.

    Safe to call from inside a task's nested coroutine: `task` is the bound
    task instance (self), and task.request.retries / task.max_retries are
    populated for the duration of execution.
    """
    max_r = task.max_retries if task.max_retries is not None else 5
    return "failed" if task.request.retries >= max_r else "retry"


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
                # Guard: valid QB Accounting IDs are small integers (no hyphens).
                # A UUID-shaped value means QB Payments wrote the company UUID
                # here by mistake — treat it as not-yet-synced and proceed.
                _existing_qb_id = company.qb_customer_id or ""
                if _existing_qb_id and "-" not in _existing_qb_id:
                    logger.info(
                        "sync_customer_to_qb — already synced, skipping:"
                        " company=%s qb_customer_id=%s",
                        company_id, _existing_qb_id,
                    )
                    return {"status": "success", "qb_customer_id": _existing_qb_id}
                if _existing_qb_id:
                    logger.warning(
                        "sync_customer_to_qb — qb_customer_id looks like a UUID (%s),"
                        " re-syncing company=%s",
                        _existing_qb_id, company_id,
                    )

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
                phone = company.phone or None

                # Build QB billing/shipping address from company registration fields
                bill_addr: dict | None = None
                if company.address_line1:
                    _addr: dict[str, str] = {"Line1": company.address_line1}
                    if company.address_line2:
                        _addr["Line2"] = company.address_line2
                    if company.city:
                        _addr["City"] = company.city
                    if company.state_province:
                        _addr["CountrySubDivisionCode"] = company.state_province
                    if company.postal_code:
                        _addr["PostalCode"] = company.postal_code
                    _addr["Country"] = company.country or "US"
                    bill_addr = _addr

                # ── 4. QB API call (session + lock still held) ────────────────
                # create_customer already does find-or-create by DisplayName,
                # so this is idempotent even without the DB lock.
                svc = await QuickBooksService().initialize()
                qb_id = await asyncio.to_thread(
                    svc.create_customer, name, email, phone, ref_id=ref, bill_addr=bill_addr,
                )
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
            # Record "failed" on the terminal attempt (no retries left) so the
            # admin dashboard surfaces dead syncs; earlier attempts stay "retry".
            await _log_attempt("company", company_id, _failure_status(self), str(exc))
            raise  # re-raised so the outer except can trigger Celery retry

    try:
        return _run_async(_run_all())
    except Exception as exc:
        delay = _retry_delay(exc, self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=5)
def sync_order_invoice_to_qb(self, order_id: str, force_payment: bool = False):
    """Sync an Order to QuickBooks as an Invoice.

    force_payment=True: create QB payment even for Net-30 orders (used by mark-paid endpoint).

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
            logger.info("QB sync starting — order_id=%s", order_id)

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

                logger.info(
                    "QB sync order found — order_number=%s total=%.2f payment_status=%s payment_method=%s company_id=%s",
                    order.order_number, float(order.total), order.payment_status,
                    order.payment_method, order.company_id,
                )

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

                line_items = [
                    {
                        "description": f"{i.product_name} ({i.sku})",
                        "quantity": i.quantity,
                        "unit_price": float(i.unit_price),
                        "amount": float(i.line_total),
                        "qb_item_id": sku_to_qb_item.get(i.sku),
                        "_sku": i.sku,  # internal field — used for inline QB sync below
                    }
                    for i in order.items
                ]

                # Collect variants not yet in QB so we can sync them inline after svc init.
                # This prevents the fallback to item "1" (Services) which misclassifies revenue.
                from app.models.inventory import InventoryRecord as _IR
                from sqlalchemy import func as _func
                _unsynced_variants: list[dict] = []
                _sku_to_product_name = {i.sku: i.product_name for i in order.items}
                for _sku_key, _qb_id in sku_to_qb_item.items():
                    if _qb_id:
                        continue
                    _pv2 = (await session.execute(
                        select(_PV).where(_PV.sku == _sku_key)
                    )).scalar_one_or_none()
                    if _pv2:
                        _stk = int((await session.execute(
                            select(_func.coalesce(_func.sum(_IR.quantity), 0))
                            .where(_IR.variant_id == _pv2.id)
                        )).scalar() or 0)
                        _unsynced_variants.append({
                            "variant_id": str(_pv2.id),
                            "sku": _sku_key,
                            "name": f"{_sku_to_product_name.get(_sku_key, 'Product')} - {_sku_key}",
                            "price": float(_pv2.retail_price),
                            "cost": float(_pv2.cost_per_item) if _pv2.cost_per_item else None,
                            "stock": _stk,
                        })
                # Add shipping as a line item; tax is handled by QB Automated Sales Tax
                shipping = float(order.shipping_cost or 0)
                if shipping > 0:
                    line_items.append({
                        "description": "Shipping & Handling",
                        "quantity": 1,
                        "unit_price": shipping,
                        "amount": shipping,
                        "qb_item_id": settings.QB_SHIPPING_ITEM_ID or None,
                    })

                # Add 3% credit card convenience fee if charged
                _conv_fee = float(getattr(order, "convenience_fee", None) or 0)
                if _conv_fee > 0:
                    _conv_item_id = settings.QB_CONVENIENCE_FEE_ITEM_ID or None
                    if not _conv_item_id:
                        logger.warning(
                            "QB invoice: convenience_fee=%.2f but QB_CONVENIENCE_FEE_ITEM_ID"
                            " not set — fee will use fallback item '1' (Services)."
                            " Create a 'CC Convenience Fee' service item in QB and set"
                            " QB_CONVENIENCE_FEE_ITEM_ID in .env",
                            _conv_fee,
                        )
                    line_items.append({
                        "description": "Credit Card Convenience Fee (3%)",
                        "quantity": 1,
                        "unit_price": _conv_fee,
                        "amount": _conv_fee,
                        "qb_item_id": _conv_item_id,
                    })
                    logger.info(
                        "QB invoice: convenience_fee=%.2f added as line item (qb_item_id=%s)",
                        _conv_fee, _conv_item_id,
                    )

                # Parse shipping address for QB AST (Automated Sales Tax)
                import json as _json
                _addr_raw: dict = {}
                try:
                    if order.shipping_address_snapshot:
                        _addr_raw = _json.loads(order.shipping_address_snapshot)
                except Exception:
                    pass
                shipping_addr: dict | None = None
                if _addr_raw:
                    shipping_addr = {
                        "Line1": _addr_raw.get("address_line1") or _addr_raw.get("line1") or _addr_raw.get("street1") or "",
                        "City": _addr_raw.get("city") or "",
                        "CountrySubDivisionCode": _addr_raw.get("state") or _addr_raw.get("state_province") or "",
                        "PostalCode": _addr_raw.get("postal_code") or _addr_raw.get("zip") or _addr_raw.get("zip_code") or "",
                        "Country": "US",
                    }

                order_data = {
                    "company_id": str(order.company_id) if order.company_id else None,
                    "order_number": order.order_number,
                    "total": float(order.total),
                    "payment_status": order.payment_status,
                    "payment_method": order.payment_method or "",
                    "created_at_date": order.created_at.strftime("%Y-%m-%d") if order.created_at else None,
                    "items": line_items,
                    "qb_invoice_id": order.qb_invoice_id,  # cached from prior successful run
                    "shipping_addr": shipping_addr,
                }

            # ── 2. Load live QB tokens ────────────────────────────────────────
            svc = await QuickBooksService().initialize()

            # ── 2.5. Inline-sync variants missing from QB before invoice ──────
            # Prevents "1" (Services) fallback which misclassifies revenue + COGS.
            if _unsynced_variants:
                logger.warning(
                    "sync_order_invoice_to_qb: %d SKU(s) not in QB — syncing inline before invoice",
                    len(_unsynced_variants),
                )
                from sqlalchemy import text as _sql_v
                for _uv in _unsynced_variants:
                    try:
                        _new_qb_id = await asyncio.to_thread(
                            svc.find_or_create_item,
                            _uv["sku"], _uv["name"], _uv["price"], _uv["cost"], _uv["stock"], "",
                        )
                        sku_to_qb_item[_uv["sku"]] = _new_qb_id
                        async with AsyncSessionLocal() as _vs:
                            await _vs.execute(
                                _sql_v("UPDATE product_variants SET qb_item_id=:qid WHERE id=CAST(:vid AS UUID)"),
                                {"qid": str(_new_qb_id), "vid": _uv["variant_id"]},
                            )
                            await _vs.commit()
                        logger.info(
                            "sync_order_invoice_to_qb: inline-synced sku=%s → qb_item_id=%s",
                            _uv["sku"], _new_qb_id,
                        )
                    except Exception as _uv_exc:
                        logger.error(
                            "sync_order_invoice_to_qb: inline-sync failed for sku=%s: %s — "
                            "invoice line will fall back to item '1'",
                            _uv["sku"], _uv_exc,
                        )
                # Patch order_data line items with newly resolved qb_item_ids
                for _li in order_data["items"]:
                    if not _li.get("qb_item_id") and _li.get("_sku"):
                        _li["qb_item_id"] = sku_to_qb_item.get(_li["_sku"])

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
                # Company exists but hasn't been synced to QB yet.
                # Only dispatch customer sync on the first attempt — subsequent retries
                # just wait for it to complete (avoids up to 5 duplicate dispatches).
                if self.request.retries == 0:
                    sync_customer_to_qb.delay(order_data["company_id"])
                raise RuntimeError("QB customer not yet synced — retrying after company sync")

            # ── 4. Create invoice (sync, run in thread) ───────────────────────
            # Fast path: if qb_invoice_id is already stored in our DB (from a prior task
            # run that succeeded at create but failed at payment), skip the invoice create
            # entirely — avoids 1 CorePlus DocNumber query on every retry.
            _existing_invoice_id = order_data.get("qb_invoice_id")
            if _existing_invoice_id:
                qb_invoice_id = _existing_invoice_id
                logger.info(
                    "sync_order_invoice_to_qb: invoice already in QB — id=%s order=%s (skipping create)",
                    qb_invoice_id, order_data["order_number"],
                )
            else:
                logger.info(
                    "QB sync creating invoice — order=%s customer=%s total=%.2f items=%d",
                    order_data["order_number"], qb_customer_id, order_data["total"], len(order_data["items"]),
                )
                qb_invoice_id = await asyncio.to_thread(
                    svc.create_invoice,
                    qb_customer_id=qb_customer_id,
                    order_number=order_data["order_number"],
                    line_items=order_data["items"],
                    total=order_data["total"],
                    shipping_addr=order_data.get("shipping_addr"),
                )
                logger.info("sync_order_invoice_to_qb success — qb_invoice_id=%s order=%s", qb_invoice_id, order_data["order_number"])

                # ── 5. Persist QB invoice ID back to the order row ────────────────
                # Use raw SQL to avoid ORM Enum commit issues with qb_sync_status
                from sqlalchemy import text as _sql_text
                async with AsyncSessionLocal() as session:
                    try:
                        await session.execute(
                            _sql_text(
                                "UPDATE orders SET qb_invoice_id=:iid, qb_sync_status='synced' WHERE id=:oid"
                            ),
                            {"iid": str(qb_invoice_id), "oid": order_id},
                        )
                        await session.commit()
                        logger.info(
                            "QB invoice ID %s saved to DB for order %s",
                            qb_invoice_id, order_data["order_number"],
                        )
                    except Exception as _save_exc:
                        await session.rollback()
                        logger.error(
                            "Failed to save qb_invoice_id to DB for order %s: %s",
                            order_data["order_number"], _save_exc, exc_info=True,
                        )
                        raise  # re-raise so task retries; create_invoice is now idempotent

            # ── 5b. If order is paid (card/ACH), record QB payment on the invoice ──
            # Applies to all non-net_30 paid orders (card, qb_payments, ach, bank_transfer).
            # payment_method="" (None column) also passes != "net_30" so older orders are covered.
            _pmt_method = order_data.get("payment_method") or ""
            _is_paid = order_data.get("payment_status") == "paid"
            _is_net30 = _pmt_method.lower() in ("net_30", "net30")
            if _is_paid and (not _is_net30 or force_payment):
                logger.info(
                    "sync_order_invoice_to_qb: recording QB payment — order=%s invoice=%s"
                    " method=%s total=%.2f",
                    order_data["order_number"], qb_invoice_id,
                    _pmt_method or "card", order_data["total"],
                )
                try:
                    payment = await asyncio.to_thread(
                        svc.create_payment_for_invoice,
                        qb_invoice_id,
                        order_data["total"],
                        _pmt_method or "card",
                        order_data.get("created_at_date"),
                        qb_customer_id,  # skip GET /invoice — saves 1 Core API call
                    )
                    logger.info(
                        "QB payment created — invoice=%s order=%s payment_id=%s",
                        qb_invoice_id,
                        order_data["order_number"],
                        payment.get("Id"),
                    )
                except Exception as _pay_exc:
                    logger.error(
                        "QB create_payment_for_invoice FAILED — order=%s invoice=%s"
                        " method=%s total=%.2f error=%s",
                        order_data["order_number"], qb_invoice_id,
                        _pmt_method, order_data["total"], _pay_exc,
                        exc_info=True,
                    )
            else:
                logger.info(
                    "sync_order_invoice_to_qb: skipping QB payment — order=%s"
                    " payment_status=%s method=%s",
                    order_data["order_number"],
                    order_data.get("payment_status"),
                    _pmt_method,
                )

            # ── 6. Log success ────────────────────────────────────────────────
            await _log_attempt("order", order_id, "success", None, qb_entity_id=qb_invoice_id)
            return {"status": "success", "qb_invoice_id": qb_invoice_id}

        except Exception as exc:
            logger.exception("sync_order_invoice_to_qb error: %s", exc)
            # Record "failed" on the terminal attempt (no retries left) so the
            # admin dashboard surfaces dead syncs; earlier attempts stay "retry".
            await _log_attempt("order", order_id, _failure_status(self), str(exc))
            raise

    try:
        return _run_async(_run_all())
    except Exception as exc:
        delay = _retry_delay(exc, self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=5)
def sync_variant_to_qb(self, variant_id: str):
    """Sync a ProductVariant to QuickBooks as an Inventory Item.

    Creates the QB item if it doesn't exist, or updates price/cost if it does.
    Writes the QB item Id back to product_variants.qb_item_id.
    """

    async def _run_all():
        from app.core.database import AsyncSessionLocal
        from app.models.product import ProductVariant, Product, ProductCategory
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
                    .options(
                        selectinload(ProductVariant.product)
                            .selectinload(Product.images),
                        selectinload(ProductVariant.product)
                            .selectinload(Product.category_links)
                            .selectinload(ProductCategory.category),
                    )
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

                product = variant.product
                product_name = product.name if product else "Product"
                sku = variant.sku
                item_name = f"{product_name} - {sku}"
                unit_price = float(variant.retail_price)
                cost = float(variant.cost_per_item) if variant.cost_per_item else None

                # Build QB item description: category name + primary image URL
                categories = product.categories if product else []
                category_name = categories[0].name if categories else ""
                primary_img = product.primary_image if product else None
                image_url = primary_img.url_medium if primary_img else ""
                desc_parts = []
                if category_name:
                    desc_parts.append(f"Category: {category_name}")
                if image_url:
                    desc_parts.append(f"Image: {image_url}")
                description = " | ".join(desc_parts)

                # ── 4. QB API call (session stays open, lock held throughout) ─
                svc = await QuickBooksService().initialize()
                qb_item_id = await asyncio.to_thread(
                    svc.find_or_create_item, sku, item_name, unit_price, cost, total_stock,
                    description,
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
        delay = _retry_delay(exc, self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=5)
def sync_variant_batch_to_qb(self, variant_ids: list):
    """Sync multiple ProductVariants to QuickBooks in a single Celery task.

    Replaces the old per-variant dispatch loop in bulk-generate and CSV-import
    endpoints. Instead of firing N tasks (each with up to 5 retries), this
    processes all variants serially inside one task — N QB API calls total,
    one retry envelope for the whole batch.

    Each variant is committed individually so a single failure does not roll
    back the rest. FOR UPDATE lock per variant prevents concurrent workers from
    creating duplicate QB items.
    """
    logger.info("sync_variant_batch_to_qb started — %d variants", len(variant_ids))

    async def _run_all():
        from app.core.database import AsyncSessionLocal
        from app.models.product import ProductVariant, Product, ProductCategory
        from app.models.inventory import InventoryRecord
        from app.services.quickbooks_service import QuickBooksService
        from sqlalchemy import select, func
        from sqlalchemy.orm import selectinload

        svc = await QuickBooksService().initialize()
        synced = 0

        async with AsyncSessionLocal() as session:
            for variant_id in variant_ids:
                try:
                    variant = (await session.execute(
                        select(ProductVariant)
                        .options(
                            selectinload(ProductVariant.product)
                                .selectinload(Product.images),
                            selectinload(ProductVariant.product)
                                .selectinload(Product.category_links)
                                .selectinload(ProductCategory.category),
                        )
                        .where(ProductVariant.id == uuid.UUID(variant_id))
                        .with_for_update()
                    )).scalar_one_or_none()

                    if not variant:
                        logger.warning("sync_variant_batch_to_qb: variant %s not found", variant_id)
                        continue

                    if variant.qb_item_id:
                        logger.info(
                            "sync_variant_batch_to_qb: variant %s already synced (qb_item_id=%s) — skipping",
                            variant_id, variant.qb_item_id,
                        )
                        continue

                    total_stock = int((await session.execute(
                        select(func.coalesce(func.sum(InventoryRecord.quantity), 0))
                        .where(InventoryRecord.variant_id == uuid.UUID(variant_id))
                    )).scalar() or 0)

                    product = variant.product
                    product_name = product.name if product else "Product"
                    sku = variant.sku
                    item_name = f"{product_name} - {sku}"
                    unit_price = float(variant.retail_price)
                    cost = float(variant.cost_per_item) if variant.cost_per_item else None

                    categories = product.categories if product else []
                    category_name = categories[0].name if categories else ""
                    primary_img = product.primary_image if product else None
                    image_url = primary_img.url_medium if primary_img else ""
                    desc_parts = []
                    if category_name:
                        desc_parts.append(f"Category: {category_name}")
                    if image_url:
                        desc_parts.append(f"Image: {image_url}")
                    description = " | ".join(desc_parts)

                    qb_item_id = await asyncio.to_thread(
                        svc.find_or_create_item, sku, item_name, unit_price, cost,
                        total_stock, description,
                    )

                    variant.qb_item_id = qb_item_id
                    await session.commit()
                    synced += 1
                    logger.info(
                        "sync_variant_batch_to_qb: synced variant=%s qb_item_id=%s",
                        variant_id, qb_item_id,
                    )

                except Exception as exc:
                    logger.warning(
                        "sync_variant_batch_to_qb: variant %s failed (skipping): %s",
                        variant_id, exc,
                    )

        logger.info("sync_variant_batch_to_qb done — %d/%d synced", synced, len(variant_ids))
        return {"status": "success", "synced": synced, "total": len(variant_ids)}

    try:
        return _run_async(_run_all())
    except Exception as exc:
        delay = _retry_delay(exc, self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=5)
def sync_inventory_to_qb(self, variant_id: str, deferred_count: int = 0):
    """Push the current total stock for a variant to QuickBooks.

    If the variant has no QB item yet, falls back to sync_variant_to_qb
    (which creates the item and sets initial QtyOnHand in one call).
    deferred_count caps re-queues at 3 to prevent infinite loops.
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
                    if deferred_count >= 3:
                        logger.warning(
                            "sync_inventory_to_qb: variant %s still not in QB after %d defers — stopping",
                            variant_id, deferred_count,
                        )
                        return {"status": "deferred_limit_reached"}
                    sync_variant_to_qb.delay(variant_id)
                    # Re-queue once variant sync completes; cap at 3 total defers
                    sync_inventory_to_qb.apply_async(
                        args=[variant_id],
                        kwargs={"deferred_count": deferred_count + 1},
                        countdown=60,
                    )
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
        delay = _retry_delay(exc, self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=3)
def sync_po_receipt_to_qb(self, po_id: str, receiving_id: str):
    """Create a QuickBooks Vendor Bill when a PO receiving is recorded.

    Looks up the manufacturer name from the PO, builds line items from the
    receiving's items, then calls quickbooks_service.create_vendor_bill.
    Writes qb_bill_id back to both POReceiving and PurchaseOrder rows.
    """
    logger.info("sync_po_receipt_to_qb started — po=%s receiving=%s", po_id, receiving_id)

    async def _run_all():
        from app.core.database import AsyncSessionLocal
        from app.models.purchase_order import PurchaseOrder, POReceiving, POLineItem
        from app.models.product import ProductVariant, Product  # noqa: F401
        from app.services.quickbooks_service import QuickBooksService
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        try:
            async with AsyncSessionLocal() as session:
                po = (await session.execute(
                    select(PurchaseOrder)
                    .options(
                        selectinload(PurchaseOrder.manufacturer),
                        selectinload(PurchaseOrder.line_items)
                            .selectinload(POLineItem.variant)
                            .selectinload(ProductVariant.product),
                    )
                    .where(PurchaseOrder.id == uuid.UUID(po_id))
                )).scalar_one_or_none()

                receiving = (await session.execute(
                    select(POReceiving)
                    .options(selectinload(POReceiving.items))
                    .where(POReceiving.id == uuid.UUID(receiving_id))
                )).scalar_one_or_none()

                if not po or not receiving:
                    logger.error(
                        "sync_po_receipt_to_qb: po or receiving not found po=%s receiving=%s",
                        po_id, receiving_id,
                    )
                    return None

                if receiving.qb_bill_id:
                    logger.info(
                        "sync_po_receipt_to_qb: already synced receiving=%s bill=%s",
                        receiving_id, receiving.qb_bill_id,
                    )
                    return {"status": "success", "qb_bill_id": receiving.qb_bill_id}

                vendor_name = po.manufacturer.name if po.manufacturer else "Unknown Vendor"
                li_map = {str(li.id): li for li in po.line_items}
                bill_lines = []
                for ri in receiving.items:
                    li = li_map.get(str(ri.po_line_item_id)) if ri.po_line_item_id else None
                    variant = li.variant if li else None

                    if variant:
                        product_name = (
                            variant.product.name if variant.product else (li.new_product_name or "Item")
                        )
                        detail = "/".join(filter(None, [variant.color, variant.size]))
                        desc = f"{product_name} — {detail}" if detail else product_name
                        qb_item_id = variant.qb_item_id
                    elif li and li.new_product_name:
                        detail = "/".join(filter(None, [li.new_product_color, li.new_product_size]))
                        desc = f"{li.new_product_name} — {detail}" if detail else li.new_product_name
                        qb_item_id = None
                    else:
                        desc = f"SKU {li.new_product_sku}" if li and li.new_product_sku else "Item"
                        qb_item_id = None

                    bill_lines.append({
                        "description": desc,
                        "qty": ri.qty_received,
                        "unit_price": float(ri.unit_cost_actual),
                        "qb_item_id": qb_item_id,
                    })

                if not bill_lines:
                    logger.warning("sync_po_receipt_to_qb: no line items for receiving=%s", receiving_id)
                    return None

                svc = await QuickBooksService().initialize()
                logger.info(
                    "sync_po_receipt_to_qb v3: calling create_vendor_bill(await) "
                    "vendor=%s lines=%d",
                    vendor_name, len(bill_lines),
                )
                qb_result = await svc.create_vendor_bill(
                    vendor_name,
                    bill_lines,
                    po.po_number,
                    receiving.received_date.isoformat() if receiving.received_date else None,
                )
                # QB returns "Id" (capital-I) natively; we also inject lowercase "id" alias
                qb_bill_id = str(qb_result.get("Id") or qb_result.get("id") or "")
                if not qb_bill_id:
                    raise ValueError(f"QB create_vendor_bill returned no id: {qb_result}")
                logger.info("sync_po_receipt_to_qb QB bill created — bill_id=%s", qb_bill_id)

                receiving.qb_bill_id = qb_bill_id
                receiving.qb_synced = True
                po.qb_bill_id = qb_bill_id
                po.qb_synced = True
                await session.commit()

            return {"status": "success", "qb_bill_id": qb_bill_id}

        except Exception as exc:
            logger.exception("sync_po_receipt_to_qb error: %s", exc)
            raise

    try:
        return _run_async(_run_all())
    except Exception as exc:
        delay = _retry_delay(exc, self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


@celery_app.task(bind=True, max_retries=5)
def sync_inventory_batch_to_qb(self, variant_ids: list[str]):
    """Push current stock for multiple variants in one task.

    Called once per order instead of once per variant — replaces the old
    per-variant loop that fired N separate tasks on every checkout.
    Reduces QB API calls from (2 × N variants) to a single Celery task.
    """
    logger.info("sync_inventory_batch_to_qb started — %d variants", len(variant_ids))

    async def _run_all():
        from app.core.database import AsyncSessionLocal
        from app.models.product import ProductVariant
        from app.models.inventory import InventoryRecord
        from app.services.quickbooks_service import QuickBooksService
        from sqlalchemy import select, func

        svc = await QuickBooksService().initialize()

        async with AsyncSessionLocal() as session:
            for variant_id in variant_ids:
                try:
                    variant = (await session.execute(
                        select(ProductVariant)
                        .where(ProductVariant.id == uuid.UUID(variant_id))
                    )).scalar_one_or_none()

                    if not variant:
                        logger.warning("sync_inventory_batch_to_qb: variant %s not found", variant_id)
                        continue

                    if not variant.qb_item_id:
                        # Not yet in QB — dispatch individual sync to create the item
                        sync_variant_to_qb.delay(variant_id)
                        logger.info(
                            "sync_inventory_batch_to_qb: variant %s not in QB yet"
                            " — dispatched sync_variant_to_qb",
                            variant_id,
                        )
                        continue

                    total_stock = int((await session.execute(
                        select(func.coalesce(func.sum(InventoryRecord.quantity), 0))
                        .where(InventoryRecord.variant_id == uuid.UUID(variant_id))
                    )).scalar() or 0)

                    await asyncio.to_thread(
                        svc.update_item,
                        variant.qb_item_id,
                        float(variant.retail_price),
                        float(variant.cost_per_item) if variant.cost_per_item else None,
                        total_stock,
                    )
                    logger.info(
                        "sync_inventory_batch_to_qb: updated variant=%s qty=%d",
                        variant_id, total_stock,
                    )

                except Exception as exc:
                    logger.warning(
                        "sync_inventory_batch_to_qb: variant %s failed (skipping): %s",
                        variant_id, exc,
                    )

        return {"status": "success", "synced": len(variant_ids)}

    try:
        return _run_async(_run_all())
    except Exception as exc:
        delay = _retry_delay(exc, self.request.retries)
        raise self.retry(exc=exc, countdown=delay)


# ── Chargeback / payment-reversal detection (daily sweep) ─────────────────────

# Statuses that indicate money was reversed after an initial successful charge.
# Intuit does not publish a complete enum of QB Payments charge statuses, so this
# list is intentionally conservative — only well-understood "money went back"
# states trigger auto-suspend. Anything else is logged for manual review only.
_CHARGEBACK_BAD_STATUSES = {
    "REFUNDED", "CANCELLED", "CANCELED", "CHARGEBACK",
    "DISPUTED", "DECLINED", "REVERSED", "VOIDED",
}

# Hard caps so this sweep can NEVER turn into an API storm, regardless of how
# many paid card orders accumulate:
#   - at most this many QB Payments status checks in one run
#   - a small fixed pause between each check on top of the shared rate limiter
_CHARGEBACK_MAX_ORDERS_PER_RUN = 100
_CHARGEBACK_PAUSE_BETWEEN_CALLS_SEC = 0.5
# Card network dispute windows are ~120 days; no need to ever check older orders.
_CHARGEBACK_LOOKBACK_DAYS = 120


@celery_app.task(bind=True, max_retries=1)
def check_card_payment_chargebacks(self):
    """Daily sweep — re-checks recent *card* orders against QuickBooks Payments
    to detect chargebacks/refunds/reversals that happened after the fact.

    ACH/bank-transfer orders are intentionally excluded: they never create a
    QuickBooks Payments transaction in this system (admin verifies them
    manually against the bank statement), so there is nothing in QB to poll.

    On detecting a reversed payment: suspends the company (existing
    CompanyService.suspend) and emails the admin. Never re-processes an
    already-suspended company, and hard-caps how many QB calls it makes per
    run — see the constants above.
    """

    async def _run_all():
        from datetime import datetime, timedelta, timezone

        from app.core.database import AsyncSessionLocal
        from app.models.company import Company
        from app.models.order import Order
        from app.services.company_service import CompanyService
        from app.services.email_service import EmailService
        from app.services.qb_payments_service import QBPaymentsService
        from sqlalchemy import select

        cutoff = datetime.now(timezone.utc) - timedelta(days=_CHARGEBACK_LOOKBACK_DAYS)

        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(Order, Company)
                .join(Company, Company.id == Order.company_id)
                .where(
                    Order.payment_method.in_(["card", "credit_card", "qb_payments"]),
                    Order.qb_payment_charge_id.isnot(None),
                    Order.payment_status == "paid",
                    Order.created_at >= cutoff,
                    Company.status != "suspended",
                )
                .order_by(Order.created_at.desc())
                .limit(_CHARGEBACK_MAX_ORDERS_PER_RUN)
            )).all()

        if not rows:
            logger.info("check_card_payment_chargebacks: nothing to check")
            return {"checked": 0, "flagged": 0}

        logger.info("check_card_payment_chargebacks: checking %d order(s)", len(rows))

        qb_pay = QBPaymentsService()
        flagged = 0

        for order, company in rows:
            try:
                charge = await asyncio.to_thread(qb_pay.get_charge, order.qb_payment_charge_id)
                charge_status = str(charge.get("status", "")).upper()

                if charge_status in _CHARGEBACK_BAD_STATUSES:
                    flagged += 1
                    logger.warning(
                        "check_card_payment_chargebacks: REVERSAL DETECTED — "
                        "order=%s company=%s status=%s",
                        order.order_number, company.name, charge_status,
                    )
                    async with AsyncSessionLocal() as sess:
                        try:
                            svc = CompanyService(sess)
                            await svc.suspend(
                                company.id,
                                f"Auto-suspended: payment for order {order.order_number} "
                                f"was reversed by QuickBooks (status={charge_status}).",
                            )
                            await sess.commit()
                        except Exception:
                            await sess.rollback()
                            logger.exception(
                                "check_card_payment_chargebacks: failed to suspend company %s",
                                company.id,
                            )
                            continue

                        if settings.ADMIN_NOTIFICATION_EMAIL:
                            try:
                                EmailService(sess).send_raw(
                                    to_email=settings.ADMIN_NOTIFICATION_EMAIL,
                                    subject=f"⚠️ Payment reversed — {company.name} auto-suspended",
                                    body_html=(
                                        f"<h2 style='color:#B91C1C'>Payment Reversal Detected</h2>"
                                        f"<p>QuickBooks reports this payment is no longer valid "
                                        f"(status: <b>{charge_status}</b>).</p>"
                                        f"<table style='width:100%;font-size:14px'>"
                                        f"<tr><td>Company</td><td><b>{company.name}</b></td></tr>"
                                        f"<tr><td>Order</td><td><b>{order.order_number}</b></td></tr>"
                                        f"<tr><td>Amount</td><td><b>${float(order.total):.2f}</b></td></tr>"
                                        f"<tr><td>QB Charge ID</td><td>{order.qb_payment_charge_id}</td></tr>"
                                        f"</table>"
                                        f"<p style='margin-top:16px'>The company's account has been "
                                        f"<b>automatically suspended</b>. Review the order and, if "
                                        f"appropriate, reactivate the account from the admin panel.</p>"
                                    ),
                                )
                            except Exception:
                                logger.exception(
                                    "check_card_payment_chargebacks: admin alert email failed for order %s",
                                    order.order_number,
                                )

            except Exception as exc:
                logger.warning(
                    "check_card_payment_chargebacks: status check failed for order %s: %s",
                    order.order_number, exc,
                )

            # Explicit pacing on top of the shared rate limiter — belt and
            # suspenders against any burst, even though _rate_limiter.wait()
            # inside QBPaymentsService already throttles every call.
            await asyncio.sleep(_CHARGEBACK_PAUSE_BETWEEN_CALLS_SEC)

        logger.info(
            "check_card_payment_chargebacks: done — checked=%d flagged=%d",
            len(rows), flagged,
        )
        return {"checked": len(rows), "flagged": flagged}

    # Single attempt per scheduled run — if something goes wrong mid-sweep,
    # tomorrow's run picks up any orders that weren't reached. No aggressive
    # retry loop that could pile up extra QB calls.
    return _run_async(_run_all())


