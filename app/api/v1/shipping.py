"""Public shipping endpoints — live Shippo rates and shipping type lookup."""
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/shipping", tags=["shipping"])

GRAMS_PER_OZ = 28.3495
DEFAULT_WEIGHT_GRAMS = 150  # per unit when variant has no weight set


class CartItemInput(BaseModel):
    variant_id: str
    quantity: int = 1


class LiveRatesRequest(BaseModel):
    to_zip: str
    to_state: str
    to_city: str = ""
    to_street1: str = ""
    to_name: str = "Customer"
    weight_oz: float = 16.0
    cart_items: list[CartItemInput] = []


@router.post("/live-rates")
async def get_live_rates(payload: LiveRatesRequest, db: AsyncSession = Depends(get_db)):
    """Return real-time carrier rates from Shippo.

    Calculates shipment weight from cart_items (variant weight_grams × quantity).
    Falls back to payload.weight_oz when no cart_items provided.
    """
    from app.services import shippo_service
    from shippo.models import components
    from shippo.models.components import DistanceUnitEnum, WeightUnitEnum
    from app.models.product import ProductVariant

    to_zip = payload.to_zip.strip()
    to_state = payload.to_state.strip()
    to_city = payload.to_city.strip() or "Unknown"
    to_street1 = payload.to_street1.strip() or "123 Main St"
    to_name = payload.to_name.strip() or "Customer"

    if not to_zip or not to_state:
        return {"rates": [], "error": "ZIP code and state are required"}

    # Calculate weight from cart items when provided
    if payload.cart_items:
        total_grams = 0.0
        for item in payload.cart_items:
            variant = (await db.execute(
                select(ProductVariant).where(ProductVariant.id == item.variant_id)
            )).scalar_one_or_none()
            weight_g = float(variant.weight_grams) if (variant and variant.weight_grams) else DEFAULT_WEIGHT_GRAMS
            total_grams += weight_g * item.quantity
        weight_oz = max(total_grams / GRAMS_PER_OZ, 1.0)
    else:
        weight_oz = payload.weight_oz

    try:
        client = shippo_service.get_client()
        wh = shippo_service.WAREHOUSE_ADDRESS

        shipment = client.shipments.create(
            components.ShipmentCreateRequest(
                address_from=components.AddressCreateRequest(
                    name=wh["name"],
                    street1=wh["street1"],
                    city=wh["city"],
                    state=wh["state"],
                    zip=wh["zip"],
                    country=wh["country"],
                    phone=wh["phone"],
                    email=wh["email"],
                ),
                address_to=components.AddressCreateRequest(
                    name=to_name,
                    street1=to_street1,
                    city=to_city,
                    state=to_state,
                    zip=to_zip,
                    country="US",
                ),
                parcels=[components.ParcelCreateRequest(
                    length="12",
                    width="10",
                    height="6",
                    distance_unit=DistanceUnitEnum.IN,
                    weight=str(round(weight_oz, 2)),
                    mass_unit=WeightUnitEnum.OZ,
                )],
                async_=False,
            )
        )

        rates = []
        for rate in (shipment.rates or []):
            try:
                rates.append({
                    "rate_id": rate.object_id,
                    "carrier": rate.provider or "Unknown",
                    "service": rate.servicelevel.name if rate.servicelevel else "Standard",
                    "service_token": rate.servicelevel.token if rate.servicelevel else "",
                    "cost": float(rate.amount),
                    "currency": rate.currency or "USD",
                    "days": rate.estimated_days,
                })
            except Exception:
                continue

        rates.sort(key=lambda r: r["cost"])
        return {"rates": rates}

    except Exception as exc:
        logger.warning("Shippo live rates error: %s", exc)
        return {"rates": [], "error": str(exc)}


@router.get("/shipping-type")
async def get_shipping_type(request: Request):
    """Return the shipping type applicable to the current session.

    - Users with a discount group: returns that group's shipping_type
    - All others: reads standard_shipping_method from the settings table
    """
    from app.core.database import AsyncSessionLocal
    from app.models.discount_group import DiscountGroup
    from app.models.system import Settings as PlatformSettings

    group_id = getattr(request.state, "discount_group_id", None)
    if group_id:
        async with AsyncSessionLocal() as session:
            g = (await session.execute(
                select(DiscountGroup).where(DiscountGroup.id == str(group_id))
            )).scalar_one_or_none()
            if g:
                return {
                    "shipping_type": g.shipping_type,
                    "shipping_amount": float(g.shipping_amount) if g.shipping_amount else 0,
                }

    async with AsyncSessionLocal() as session:
        setting = (await session.execute(
            select(PlatformSettings).where(PlatformSettings.key == "standard_shipping_method")
        )).scalar_one_or_none()
        if setting:
            return {"shipping_type": setting.value, "shipping_amount": 0}

    return {"shipping_type": "flat", "shipping_amount": 0}
