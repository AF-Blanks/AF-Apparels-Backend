"""CartService — manages CartItem records and validates MOQ/MOV requirements."""
import logging
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationError
from app.models.order import CartItem, OrderTemplate
from app.models.product import Product, ProductImage, ProductVariant
from app.models.inventory import InventoryRecord
from app.schemas.cart import CartItemOut, CartResponse, CartValidation, MatrixAddRequest


class CartService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Read cart
    # ------------------------------------------------------------------

    async def get_cart_with_pricing(
        self,
        company_id: UUID,
        discount_percent: Decimal = Decimal("0"),
        group_id: str | None = None,
    ) -> CartResponse:
        items = await self._load_cart_items(company_id, discount_percent, group_id)
        subtotal = sum(i.line_total for i in items)
        total_units = sum(i.quantity for i in items)
        validation = await self._validate(items, total_units, subtotal, company_id)
        return CartResponse(
            items=items,
            subtotal=subtotal,
            item_count=len(items),
            total_units=total_units,
            validation=validation,
            discount_percent=discount_percent,
        )

    # ------------------------------------------------------------------
    # Add matrix items (US-3)
    # ------------------------------------------------------------------

    async def add_matrix_items(
        self,
        company_id: UUID,
        payload: MatrixAddRequest,
        discount_percent: Decimal = Decimal("0"),
        group_id: str | None = None,
    ) -> CartResponse:
        from app.services.pricing_service import PricingService

        pricing_svc = PricingService(self.db)

        for add_item in payload.items:
            variant_result = await self.db.execute(
                select(ProductVariant).where(ProductVariant.id == add_item.variant_id)
            )
            variant = variant_result.scalar_one_or_none()
            if not variant:
                raise NotFoundError(f"Variant {add_item.variant_id} not found")

            effective_price = await self._effective_price(
                variant, discount_percent, group_id
            )

            # Upsert: add to existing quantity if item already in cart
            existing_result = await self.db.execute(
                select(CartItem).where(
                    CartItem.company_id == company_id,
                    CartItem.variant_id == add_item.variant_id,
                )
            )
            existing = existing_result.scalar_one_or_none()

            if existing:
                existing.quantity += add_item.quantity
                existing.unit_price = effective_price
            else:
                cart_item = CartItem(
                    company_id=company_id,
                    variant_id=add_item.variant_id,
                    quantity=add_item.quantity,
                    unit_price=effective_price,
                )
                self.db.add(cart_item)

        await self.db.flush()
        return await self.get_cart_with_pricing(company_id, discount_percent, group_id)

    # ------------------------------------------------------------------
    # Update item quantity
    # ------------------------------------------------------------------

    async def update_item_quantity(
        self,
        company_id: UUID,
        item_id: UUID,
        quantity: int,
        discount_percent: Decimal = Decimal("0"),
        group_id: str | None = None,
    ) -> CartResponse:
        result = await self.db.execute(
            select(CartItem).where(
                CartItem.id == item_id, CartItem.company_id == company_id
            )
        )
        item = result.scalar_one_or_none()
        if not item:
            raise NotFoundError(f"Cart item {item_id} not found")

        item.quantity = quantity
        await self.db.flush()
        return await self.get_cart_with_pricing(company_id, discount_percent, group_id)

    # ------------------------------------------------------------------
    # Remove item
    # ------------------------------------------------------------------

    async def remove_item(
        self,
        company_id: UUID,
        item_id: UUID,
        discount_percent: Decimal = Decimal("0"),
        group_id: str | None = None,
    ) -> CartResponse:
        await self.db.execute(
            delete(CartItem).where(
                CartItem.id == item_id, CartItem.company_id == company_id
            )
        )
        await self.db.flush()
        return await self.get_cart_with_pricing(company_id, discount_percent, group_id)

    # ------------------------------------------------------------------
    # Clear cart
    # ------------------------------------------------------------------

    async def clear_cart(self, company_id: UUID) -> None:
        await self.db.execute(
            delete(CartItem).where(CartItem.company_id == company_id)
        )
        await self.db.flush()

    # ------------------------------------------------------------------
    # Validate cart (MOQ + MOV) (T081 — Phase 8)
    # ------------------------------------------------------------------

    async def validate_cart(self, company_id: UUID) -> CartValidation:
        items = await self._load_cart_items(company_id)
        total_units = sum(i.quantity for i in items)
        subtotal = sum(i.line_total for i in items)
        return await self._validate(items, total_units, subtotal, company_id)

    # ------------------------------------------------------------------
    # Save as template (T082 — Phase 8)
    # ------------------------------------------------------------------

    async def save_as_template(
        self, company_id: UUID, user_id: UUID, name: str
    ) -> OrderTemplate:
        items = await self._load_cart_items(company_id)
        if not items:
            raise ValidationError("Cart is empty")

        template_items = [
            {"variant_id": str(i.variant_id), "quantity": i.quantity}
            for i in items
        ]
        import json

        template = OrderTemplate(
            company_id=company_id,
            created_by_id=user_id,
            name=name,
            items=json.dumps(template_items),
        )
        self.db.add(template)
        await self.db.flush()
        await self.db.refresh(template)
        return template

    # ------------------------------------------------------------------
    # Quick order: validate SKU list (T139 — Phase 14)
    # ------------------------------------------------------------------

    async def validate_sku_list(self, skus: list[dict]) -> dict:
        valid, invalid, insufficient = [], [], []

        for entry in skus:
            sku = entry["sku"]
            qty = entry["quantity"]

            result = await self.db.execute(
                select(ProductVariant).where(ProductVariant.sku == sku)
            )
            variant = result.scalar_one_or_none()

            if not variant:
                invalid.append({"sku": sku, "quantity": qty, "status": "not_found"})
                continue

            stock_result = await self.db.execute(
                select(func.coalesce(func.sum(InventoryRecord.quantity), 0)).where(
                    InventoryRecord.variant_id == variant.id
                )
            )
            stock = stock_result.scalar_one()

            if stock < qty:
                insufficient.append({
                    "sku": sku,
                    "quantity": qty,
                    "status": "insufficient_stock",
                    "available_quantity": stock,
                    "variant_id": str(variant.id),
                })
            else:
                valid.append({
                    "sku": sku,
                    "quantity": qty,
                    "status": "valid",
                    "variant_id": str(variant.id),
                })

        return {"valid": valid, "invalid": invalid, "insufficient_stock": insufficient}

    async def bulk_add_validated_items(
        self,
        company_id: UUID,
        valid_items: list[dict],
        discount_percent: Decimal = Decimal("0"),
        group_id: str | None = None,
    ) -> int:
        """Upsert all valid items into cart. Returns count of items added."""
        added = 0
        for item in valid_items:
            variant_id = UUID(item["variant_id"]) if isinstance(item["variant_id"], str) else item["variant_id"]
            qty = item["quantity"]

            variant_result = await self.db.execute(
                select(ProductVariant).where(ProductVariant.id == variant_id)
            )
            variant = variant_result.scalar_one_or_none()
            effective_price = (
                await self._effective_price(variant, discount_percent, group_id)
                if variant else Decimal("0")
            )

            existing = (await self.db.execute(
                select(CartItem).where(
                    CartItem.company_id == company_id,
                    CartItem.variant_id == variant_id,
                )
            )).scalar_one_or_none()
            if existing:
                existing.quantity += qty
                existing.unit_price = effective_price
            else:
                self.db.add(CartItem(
                    company_id=company_id,
                    variant_id=variant_id,
                    quantity=qty,
                    unit_price=effective_price,
                ))
            added += 1
        await self.db.flush()
        return added

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _effective_price(
        self,
        variant: "ProductVariant",
        discount_percent: Decimal,
        group_id: str | None,
    ) -> Decimal:
        """Return price: VariantLevelPricingOverride > product-level VariantPricingOverride > tier discount."""
        from decimal import ROUND_HALF_UP
        if group_id:
            from app.models.discount_group import VariantLevelPricingOverride, VariantPricingOverride

            # Step 1: per-variant override (highest priority)
            vlp_result = await self.db.execute(
                select(VariantLevelPricingOverride).where(
                    VariantLevelPricingOverride.variant_id == str(variant.id),
                    VariantLevelPricingOverride.group_id == group_id,
                )
            )
            vlp = vlp_result.scalar_one_or_none()
            if vlp and vlp.price is not None:
                return Decimal(str(vlp.price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            # Step 2: product-level override (tier_id stores group_id per existing convention)
            ov_result = await self.db.execute(
                select(VariantPricingOverride).where(
                    VariantPricingOverride.product_id == str(variant.product_id),
                    VariantPricingOverride.tier_id == group_id,
                )
            )
            ov = ov_result.scalar_one_or_none()
            if ov is not None and ov.price is not None:
                return Decimal(str(ov.price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if ov is not None and ov.discount_percent is not None:
                multiplier = Decimal("1") - (Decimal(str(ov.discount_percent)) / Decimal("100"))
                return (variant.retail_price * multiplier).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        from app.services.pricing_service import PricingService
        return PricingService(self.db).calculate_effective_price(variant.retail_price, discount_percent)

    async def _load_cart_items(
        self,
        company_id: UUID,
        discount_percent: Decimal = Decimal("0"),
        group_id: str | None = None,
    ) -> list[CartItemOut]:
        result = await self.db.execute(
            select(CartItem).where(CartItem.company_id == company_id)
        )
        raw_items = result.scalars().all()

        items: list[CartItemOut] = []
        for item in raw_items:
            # Load variant + product info
            variant_result = await self.db.execute(
                select(ProductVariant, Product)
                .join(Product, ProductVariant.product_id == Product.id)
                .where(ProductVariant.id == item.variant_id)
            )
            row = variant_result.first()
            if not row:
                continue
            variant, product = row

            # Stock
            stock_result = await self.db.execute(
                select(func.coalesce(func.sum(InventoryRecord.quantity), 0)).where(
                    InventoryRecord.variant_id == variant.id
                )
            )
            stock = stock_result.scalar_one()

            # Re-apply pricing so cart always reflects current tier/override.
            effective_price = await self._effective_price(variant, discount_percent, group_id)
            line_total = effective_price * item.quantity
            moq_satisfied = item.quantity >= product.moq

            # Primary product image
            img_result = await self.db.execute(
                select(ProductImage)
                .where(ProductImage.product_id == product.id)
                .order_by(ProductImage.sort_order)
                .limit(1)
            )
            primary_img = img_result.scalar_one_or_none()
            image_url = None
            if primary_img:
                image_url = (
                    getattr(primary_img, "url_medium_webp", None)
                    or getattr(primary_img, "url_medium", None)
                    or getattr(primary_img, "url_thumbnail", None)
                )

            items.append(
                CartItemOut(
                    id=item.id,
                    variant_id=item.variant_id,
                    product_id=product.id,
                    product_name=product.name,
                    product_slug=product.slug,
                    product_image_url=image_url,
                    sku=variant.sku,
                    color=variant.color,
                    size=variant.size,
                    quantity=item.quantity,
                    retail_price=variant.retail_price,
                    unit_price=effective_price,
                    line_total=line_total,
                    moq=product.moq,
                    moq_satisfied=moq_satisfied,
                    stock_quantity=stock,
                )
            )
        return items

    async def _validate(
        self,
        items: list[CartItemOut],
        total_units: int,
        subtotal: Decimal,
        company_id: UUID,
    ) -> CartValidation:
        settings = get_settings()

        # MOQ violations
        moq_violations = [
            {
                "variant_id": str(i.variant_id),
                "sku": i.sku,
                "required": i.moq,
                "current": i.quantity,
            }
            for i in items
            if not i.moq_satisfied
        ]

        # MOV check
        mov_required = Decimal(str(getattr(settings, "MOV_AMOUNT", "0")))
        mov_violation = subtotal < mov_required

        # Shipping estimate
        estimated_shipping = Decimal("0")
        has_shipping_tier = False
        if items:
            try:
                from app.models.company import Company
                from app.models.discount_group import DiscountGroup
                from app.models.shipping import ShippingTier
                from app.services.shipping_service import ShippingService
                from sqlalchemy.orm import selectinload

                # Load company with shipping_tier + its brackets in one round-trip
                company_result = await self.db.execute(
                    select(Company)
                    .options(
                        selectinload(Company.shipping_tier).selectinload(ShippingTier.brackets)
                    )
                    .where(Company.id == company_id)
                )
                company = company_result.scalar_one_or_none()

                # Override helper: sanitize company override amount
                def _override(c: Company | None) -> Decimal | None:
                    val = c.shipping_override_amount if c else None
                    if val is None:
                        return None
                    d = Decimal(str(val))
                    return d if d > Decimal("0") else None

                svc = ShippingService(self.db)

                # DiscountGroup shipping takes priority over ShippingTier
                discount_group = None
                if company and company.tags:
                    dg_result = await self.db.execute(
                        select(DiscountGroup)
                        .where(
                            DiscountGroup.customer_tag.in_(company.tags),
                            DiscountGroup.status == "enabled",
                        )
                        .limit(1)
                    )
                    discount_group = dg_result.scalar_one_or_none()

                if discount_group and discount_group.shipping_type != "store_default":
                    has_shipping_tier = True
                    estimated_shipping = svc.calculate_dg_shipping_cost(
                        total_units,
                        discount_group.shipping_type,
                        discount_group.shipping_amount,
                        discount_group.shipping_calc_type,
                        discount_group.shipping_brackets_json,
                        _override(company),
                        order_subtotal=subtotal,
                    )
                else:
                    if company and company.shipping_tier_id and not company.shipping_tier:
                        # selectinload didn't populate the relationship (edge-case) — fall back
                        tier_result = await self.db.execute(
                            select(ShippingTier)
                            .options(selectinload(ShippingTier.brackets))
                            .where(ShippingTier.id == company.shipping_tier_id)
                        )
                        shipping_tier = tier_result.scalar_one_or_none()
                    else:
                        shipping_tier = company.shipping_tier if company else None

                    if shipping_tier:
                        has_shipping_tier = True
                        estimated_shipping = svc.calculate_shipping_cost(
                            total_units,
                            shipping_tier,
                            _override(company),
                            order_subtotal=subtotal,
                        )
                    else:
                        # Fallback: use standard_shipping platform setting for untiered users
                        try:
                            import json as _json
                            from app.models.system import Settings as _PlatformSettings
                            std_row = (await self.db.execute(
                                select(_PlatformSettings).where(_PlatformSettings.key == "standard_shipping")
                            )).scalar_one_or_none()
                            if std_row and std_row.value:
                                cfg = _json.loads(std_row.value)
                                if cfg.get("shipping_type") == "flat_rate" and cfg.get("brackets"):
                                    brackets_json = _json.dumps(cfg["brackets"])
                                    estimated_shipping = svc.calculate_dg_shipping_cost(
                                        total_units,
                                        "flat_rate",
                                        cfg.get("shipping_amount", 0),
                                        cfg.get("calc_type", "order_value"),
                                        brackets_json,
                                        _override(company),
                                        order_subtotal=subtotal,
                                    )
                                    has_shipping_tier = True
                                elif cfg.get("shipping_type") == "store_default":
                                    amt = Decimal(str(cfg.get("shipping_amount", 0)))
                                    if _override(company) is not None:
                                        estimated_shipping = _override(company)
                                    else:
                                        estimated_shipping = amt
                                    has_shipping_tier = True
                        except Exception:
                            pass
            except Exception as exc:
                logger.error("cart shipping estimate failed for company %s: %s", company_id, exc, exc_info=True)

        return CartValidation(
            is_valid=True,
            moq_violations=moq_violations,
            mov_violation=mov_violation,
            mov_required=mov_required,
            mov_current=subtotal,
            estimated_shipping=estimated_shipping,
            has_shipping_tier=has_shipping_tier,
        )
