"""Public shipping endpoints — live Shippo rates and shipping type lookup."""
import json
import logging

from fastapi import APIRouter, Request
from sqlalchemy import select, text

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/shipping", tags=["shipping"])


@router.post("/live-rates")
async def get_live_rates(payload: dict):
    """Return real-time carrier rates from Shippo.

    Body: { to_zip, to_state, to_city?, to_street1?, to_name?, weight_oz? }
    """
    from app.services import shippo_service
    from shippo.models import components
    from shippo.models.components import DistanceUnitEnum, WeightUnitEnum

    to_zip = (payload.get("to_zip") or "").strip()
    to_state = (payload.get("to_state") or "").strip()
    to_city = (payload.get("to_city") or "Unknown").strip()
    to_street1 = (payload.get("to_street1") or "123 Main St").strip()
    to_name = (payload.get("to_name") or "Customer").strip()
    weight_oz = float(payload.get("weight_oz") or 16.0)

    if not to_zip or not to_state:
        return {"rates": [], "error": "ZIP code and state are required"}

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
    - All others: returns the standard_shipping setting's shipping_type
    """
    from app.core.database import AsyncSessionLocal
    from app.models.discount_group import DiscountGroup

    group_id = getattr(request.state, "discount_group_id", None)
    if group_id:
        async with AsyncSessionLocal() as db:
            g = (await db.execute(
                select(DiscountGroup).where(DiscountGroup.id == str(group_id))
            )).scalar_one_or_none()
            if g:
                return {
                    "shipping_type": g.shipping_type,
                    "shipping_amount": float(g.shipping_amount) if g.shipping_amount else 0,
                }

    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            text("SELECT value FROM settings WHERE key = 'standard_shipping'")
        )).fetchone()
        if row:
            try:
                cfg = json.loads(row[0])
                return {
                    "shipping_type": cfg.get("shipping_type", "store_default"),
                    "shipping_amount": float(cfg.get("shipping_amount") or 0),
                }
            except Exception:
                pass

    return {"shipping_type": "store_default", "shipping_amount": 0}
