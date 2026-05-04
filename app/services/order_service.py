# backend/app/services/order_service.py
"""OrderService — create orders with server-side validation + price snapshots."""
import logging
from decimal import Decimal
from uuid import UUID

from app.models.user import User  # ← yeh add karo
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError, InsufficientStockError
from app.models.company import Company, UserAddress
from app.models.inventory import InventoryRecord
from app.models.order import CartItem, Order, OrderItem, OrderTemplate
from app.models.product import Product, ProductVariant
from app.models.pricing import PricingTier
from app.models.shipping import ShippingTier
from app.schemas.order import AddressIn, CheckoutConfirmRequest, OrderListItem, OrderOut

logger = logging.getLogger(__name__)

_ORDER_COUNTER_KEY = "order:counter"


class OrderService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Create order (US-6)
    # ------------------------------------------------------------------

    async def create_order(
        self,
        company_id: UUID,
        user_id: UUID,
        confirm: CheckoutConfirmRequest,
        discount_percent: Decimal = Decimal("0"),
        qb_charge_id: str | None = None,
        qb_payment_status: str | None = None,
        coupon_discount_amount: Decimal = Decimal("0"),
    ) -> Order:
        settings = get_settings()

        # 1. Load cart items
        cart_result = await self.db.execute(
            select(CartItem).where(CartItem.company_id == company_id)
        )
        cart_items = cart_result.scalars().all()
        if not cart_items:
            raise ValidationError("Cart is empty")

        # 2. Load company + shipping tier
        company_result = await self.db.execute(
            select(Company).where(Company.id == company_id)
        )
        company = company_result.scalar_one_or_none()
        if not company:
            raise NotFoundError(f"Company {company_id} not found")

        # 3. Validate + snapshot each item
        order_items_data = []
        subtotal = Decimal("0")
        total_units = 0

        for cart_item in cart_items:
            variant_result = await self.db.execute(
                select(ProductVariant, Product)
                .join(Product, ProductVariant.product_id == Product.id)
                .where(ProductVariant.id == cart_item.variant_id)
            )
            row = variant_result.first()
            if not row:
                raise NotFoundError(f"Variant {cart_item.variant_id} not found")
            variant, product = row

            # Stock check — only enforce when inventory records exist.
            # COALESCE returns 0 when no records found; treat 0 as unlimited.
            stock_result = await self.db.execute(
                select(func.coalesce(func.sum(InventoryRecord.quantity), 0)).where(
                    InventoryRecord.variant_id == variant.id
                )
            )
            available = stock_result.scalar_one()
            if available > 0 and available < cart_item.quantity:
                raise InsufficientStockError(
                    f"Insufficient stock for {variant.sku}: {available} available"
                )
            # available == 0 means no inventory records → unlimited stock, skip check

            # Price snapshot
            from app.services.pricing_service import PricingService
            pricing_svc = PricingService(self.db)
            unit_price = pricing_svc.calculate_effective_price(
                variant.retail_price, discount_percent
            )
            line_total = unit_price * cart_item.quantity
            subtotal += line_total
            total_units += cart_item.quantity

            order_items_data.append({
                "variant_id": variant.id,
                "product_name": product.name,
                "sku": variant.sku,
                "color": variant.color,
                "size": variant.size,
                "quantity": cart_item.quantity,
                "unit_price": unit_price,
                "line_total": line_total,
            })

        # 5. Calculate shipping
        shipping_method = confirm.shipping_method or "standard"
        shipping_cost = Decimal("0")

        if shipping_method == "will_call":
            shipping_cost = Decimal("0.00")
        else:
            from app.models.discount_group import DiscountGroup as _DiscountGroup
            from app.services.shipping_service import ShippingService
            from sqlalchemy.orm import selectinload

            def _override_val(c: Company) -> Decimal | None:
                val = c.shipping_override_amount
                if val is None:
                    return None
                d = Decimal(str(val))
                return d if d > Decimal("0") else None

            shipping_svc = ShippingService(self.db)
            _dg_applied = False

            if company.tags:
                dg_result = await self.db.execute(
                    select(_DiscountGroup)
                    .where(
                        _DiscountGroup.customer_tag.in_(company.tags),
                        _DiscountGroup.status == "enabled",
                    )
                    .limit(1)
                )
                dg = dg_result.scalar_one_or_none()
                if dg and dg.shipping_type != "store_default":
                    _dg_applied = True
                    shipping_cost = shipping_svc.calculate_dg_shipping_cost(
                        total_units,
                        dg.shipping_type,
                        dg.shipping_amount,
                        dg.shipping_calc_type,
                        dg.shipping_brackets_json,
                        _override_val(company),
                        order_subtotal=subtotal,
                    )

            if not _dg_applied and company.shipping_tier_id:
                shipping_tier_result = await self.db.execute(
                    select(ShippingTier)
                    .options(selectinload(ShippingTier.brackets))
                    .where(ShippingTier.id == company.shipping_tier_id)
                )
                shipping_tier = shipping_tier_result.scalar_one_or_none()
                if shipping_tier:
                    shipping_cost = shipping_svc.calculate_shipping_cost(
                        total_units, shipping_tier,
                        _override_val(company),
                        order_subtotal=subtotal,
                    )

            if shipping_method == "expedited":
                shipping_cost += Decimal("45.00")

        tax_amount_val = Decimal(str(confirm.tax_amount or 0))
        total = subtotal + shipping_cost + tax_amount_val - coupon_discount_amount

        # 6. Resolve shipping address
        shipping_address = await self._resolve_address(confirm, company_id)

        # 7. Generate order number
        order_number = await self._generate_order_number()

        # 8. Create Order record
        import json as _json
        # Determine payment_status: map QB status strings to our enum values
        # QB returns "CAPTURED" but our enum only accepts: unpaid|pending|paid|refunded|failed
        _qb_status = qb_payment_status or ""
        _payment_status = "paid" if _qb_status == "CAPTURED" else "pending"

        order = Order(
            company_id=company_id,
            placed_by_id=user_id,
            order_number=order_number,
            status="pending",
            payment_status=_payment_status,
            po_number=confirm.po_number,
            notes=confirm.order_notes,
            stripe_payment_intent_id=confirm.payment_intent_id,
            qb_payment_charge_id=qb_charge_id,
            qb_payment_status=qb_payment_status,
            subtotal=subtotal,
            shipping_cost=shipping_cost,
            tax_amount=tax_amount_val,
            total=total,
            shipping_method=shipping_method,
            shipping_address_id=confirm.address_id if confirm.address_id else None,
            shipping_address_snapshot=_json.dumps(shipping_address) if shipping_address else None,
        )
        self.db.add(order)
        await self.db.flush()

        # Save tax_rate / tax_region via raw SQL — columns may not exist in older deployments.
        # This is a best-effort update; failure does not block order creation.
        _tax_rate_val = confirm.tax_rate
        _tax_region_val = getattr(confirm, "tax_region", None)
        if _tax_rate_val is not None or _tax_region_val is not None:
            try:
                from sqlalchemy import text as _text
                await self.db.execute(
                    _text(
                        "UPDATE orders SET tax_rate = :tr, tax_region = :trg WHERE id = :oid"
                    ),
                    {"tr": _tax_rate_val, "trg": _tax_region_val, "oid": str(order.id)},
                )
            except Exception as _tax_exc:
                logger.warning("Could not save tax_rate/tax_region on order %s (columns may be missing): %s", order.id, _tax_exc)

        # 9. Create OrderItem records
        for item_data in order_items_data:
            order_item = OrderItem(
                order_id=order.id,
                **item_data,
            )
            self.db.add(order_item)

        # 9.5. Deduct inventory for each ordered variant using explicit SQL UPDATE
        from sqlalchemy import update as _update
        for item_data in order_items_data:
            variant_id = item_data["variant_id"]
            qty_to_deduct = int(item_data["quantity"])

            inv_result = await self.db.execute(
                select(InventoryRecord)
                .where(InventoryRecord.variant_id == variant_id)
                .order_by(InventoryRecord.quantity.desc())
            )
            inv_records = inv_result.scalars().all()

            for record in inv_records:
                if qty_to_deduct <= 0:
                    break
                current_qty = int(record.quantity)
                deduct = min(current_qty, qty_to_deduct)
                if deduct > 0:
                    await self.db.execute(
                        _update(InventoryRecord)
                        .where(InventoryRecord.id == record.id)
                        .values(quantity=current_qty - deduct)
                    )
                    qty_to_deduct -= deduct

        # 10. Clear cart
        from sqlalchemy import delete
        await self.db.execute(
            delete(CartItem).where(CartItem.company_id == company_id)
        )

        await self.db.flush()

        # Reload order with items eager-loaded (async ORM cannot lazy-load during response serialization)
        from sqlalchemy.orm import selectinload
        result = await self.db.execute(
            select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
        )
        order = result.scalar_one()

        # Step 11 — ✅ Direct email, no Celery
        try:
            from app.services.email_service import EmailService
            email_svc = EmailService(self.db)
            
            # Customer ko confirmation
            customer_result = await self.db.execute(
                select(User).where(User.id == user_id)
            )
            customer = customer_result.scalar_one_or_none()
            
            if customer:
                await email_svc.send(
                    trigger_event="order_confirmation",
                    to_email=customer.email,
                    variables={
                        "first_name": customer.first_name or "there",
                        "order_number": order.order_number,
                        "order_total": f"${float(order.total):.2f}",
                        "order_url": f"{settings.FRONTEND_URL}/account/orders/{order.id}",
                        "items": [
                            {
                                "product_name": i.product_name,
                                "sku": i.sku,
                                "color": i.color or "",
                                "size": i.size or "",
                                "quantity": i.quantity,
                                "unit_price": f"${float(i.unit_price):.2f}",
                                "line_total": f"${float(i.line_total):.2f}",
                            }
                            for i in order.items
                        ],
                    },
                )
        
            # Admin ko notification
            if settings.ADMIN_NOTIFICATION_EMAIL:
                email_svc.send_raw(
                    to_email=settings.ADMIN_NOTIFICATION_EMAIL,
                    subject=f"New Order — {order.order_number} (${float(order.total):.2f})",
                    body_html=f"""
                        <h2>New Order Received</h2>
                        <p><b>Order:</b> {order.order_number}</p>
                        <p><b>Total:</b> ${float(order.total):.2f}</p>
                        <p><b>Items:</b> {len(order.items)}</p>
                        <a href="{settings.FRONTEND_URL}/admin/orders/{order.id}">View Order →</a>
                    """,
                )
        except Exception as e:
            logger.warning("Order confirmation email failed: %s", e)

        # 12. Auto-create statement charge transaction
        try:
            from app.models.statement import StatementTransaction
            txn = StatementTransaction(
                company_id=company_id,
                transaction_date=order.created_at.strftime("%Y-%m-%d"),
                description=f"Order #{order.order_number}",
                transaction_type="charge",
                amount=float(order.total),
                reference_number=order.order_number,
                order_id=order.id,
            )
            self.db.add(txn)
            await self.db.flush()
        except Exception:
            pass

        return order

    # ------------------------------------------------------------------
    # Get / list orders
    # ------------------------------------------------------------------

    async def get_order(self, order_id: UUID, company_id: UUID) -> Order:
        from sqlalchemy.orm import selectinload

        result = await self.db.execute(
            select(Order)
            .options(selectinload(Order.items))
            .where(Order.id == order_id, Order.company_id == company_id)
        )
        order = result.scalar_one_or_none()
        if not order:
            raise NotFoundError(f"Order {order_id} not found")
        return order

    async def list_orders_for_company(
        self,
        company_id: UUID,
        page: int = 1,
        page_size: int = 20,
        q: str | None = None,
        status: str | None = None,
    ) -> tuple[list[Order], int]:
        from sqlalchemy.orm import selectinload

        base = select(Order).where(Order.company_id == company_id)
        if q:
            base = base.where(
                (Order.order_number.ilike(f"%{q}%")) | (Order.po_number.ilike(f"%{q}%"))
            )
        if status:
            base = base.where(Order.status == status)

        count_result = await self.db.execute(
            select(func.count()).select_from(base.subquery())
        )
        total = count_result.scalar_one()

        result = await self.db.execute(
            base
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        orders = result.scalars().all()
        return list(orders), total

    # ------------------------------------------------------------------
    # Reorder (T150 — Phase 15)
    # ------------------------------------------------------------------

    async def reorder(
        self, order_id: UUID, company_id: UUID, discount_percent: Decimal = Decimal("0")
    ) -> list[dict]:
        """Copy a past order's items into the cart with current pricing + stock check."""
        order = await self.get_order(order_id, company_id)
        from app.services.pricing_service import PricingService
        pricing_svc = PricingService(self.db)

        added = []
        skipped = []

        for order_item in order.items:
            stock_result = await self.db.execute(
                select(func.coalesce(func.sum(InventoryRecord.quantity), 0)).where(
                    InventoryRecord.variant_id == order_item.variant_id
                )
            )
            available = stock_result.scalar_one()

            if available < order_item.quantity:
                skipped.append({"sku": order_item.sku, "reason": "insufficient_stock"})
                continue

            variant_result = await self.db.execute(
                select(ProductVariant).where(ProductVariant.id == order_item.variant_id)
            )
            variant = variant_result.scalar_one_or_none()
            if not variant or variant.status != "active":
                skipped.append({"sku": order_item.sku, "reason": "discontinued"})
                continue

            effective_price = pricing_svc.calculate_effective_price(
                variant.retail_price, discount_percent
            )

            cart_item = CartItem(
                company_id=company_id,
                variant_id=order_item.variant_id,
                quantity=order_item.quantity,
                unit_price=effective_price,
            )
            self.db.add(cart_item)
            added.append({"sku": order_item.sku})

        await self.db.flush()
        return {"added": added, "skipped": skipped}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _resolve_address(self, confirm: CheckoutConfirmRequest, company_id: UUID) -> dict:
        if confirm.address_id:
            result = await self.db.execute(
                select(UserAddress).where(
                    UserAddress.id == confirm.address_id,
                    UserAddress.company_id == company_id,
                )
            )
            addr = result.scalar_one_or_none()
            if addr:
                return {
                    "line1": addr.address_line1,
                    "line2": addr.address_line2,
                    "city": addr.city,
                    "state": addr.state,
                    "postal_code": addr.postal_code,
                    "country": addr.country,
                }
        if confirm.shipping_address:
            return confirm.shipping_address.model_dump()
        return {}

    async def _generate_order_number(self) -> str:
        from app.core.redis import redis_increment
        try:
            counter = await redis_increment(_ORDER_COUNTER_KEY)
        except Exception:
            import random
            counter = random.randint(10000, 99999)
        return f"AF-{counter:06d}"
