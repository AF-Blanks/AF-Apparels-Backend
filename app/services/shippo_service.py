import os
import json
import logging
import shippo
from shippo.models import components

logger = logging.getLogger(__name__)

WAREHOUSE_ADDRESS = {
    "name": "AF Apparels",
    "street1": "10719 Turbeville Rd",
    "city": "Dallas",
    "state": "TX",
    "zip": "75243",
    "country": "US",
    "phone": "2145550100",
    "email": "shipping@afapparels.com",
}

# Maps carrier key → Shippo service-level token fragment used for rate selection
CARRIER_TOKENS = {
    "usps": "usps_priority",
    "ups": "ups_ground",
    "fedex": "fedex_ground",
}


def get_client():
    api_key = os.getenv("SHIPPO_API_KEY", "")
    if not api_key:
        raise ValueError("SHIPPO_API_KEY not set")
    return shippo.Shippo(api_key_header=api_key)


async def create_label(order_id: str, to_address: dict, carrier_token: str) -> dict:
    try:
        client = get_client()

        # For UPS, validate that city/state/zip match before creating shipment
        if "ups" in carrier_token.lower():
            try:
                validated_addr = client.addresses.create(
                    components.AddressCreateRequest(
                        name=to_address.get("name", "Customer"),
                        street1=to_address.get("street1", ""),
                        city=to_address.get("city", ""),
                        state=to_address.get("state", ""),
                        zip=to_address.get("zip", ""),
                        country=to_address.get("country", "US"),
                        validate=True,
                    )
                )
                vr = getattr(validated_addr, "validation_results", None)
                if vr and not getattr(vr, "is_valid", True):
                    return {
                        "success": False,
                        "error": "Please verify the shipping address - city, state and ZIP code must match",
                    }
            except Exception as val_exc:
                logger.warning("UPS address validation error: %s", val_exc)

        shipment = client.shipments.create(
            components.ShipmentCreateRequest(
                address_from=components.AddressCreateRequest(
                    name=WAREHOUSE_ADDRESS["name"],
                    street1=WAREHOUSE_ADDRESS["street1"],
                    city=WAREHOUSE_ADDRESS["city"],
                    state=WAREHOUSE_ADDRESS["state"],
                    zip=WAREHOUSE_ADDRESS["zip"],
                    country=WAREHOUSE_ADDRESS["country"],
                ),
                address_to=components.AddressCreateRequest(
                    name=to_address.get("name", ""),
                    street1=to_address.get("street1", ""),
                    city=to_address.get("city", ""),
                    state=to_address.get("state", ""),
                    zip=to_address.get("zip", ""),
                    country=to_address.get("country", "US"),
                ),
                parcels=[components.ParcelCreateRequest(
                    length="12",
                    width="10",
                    height="6",
                    distance_unit=components.DistanceUnitEnum.IN,
                    weight="16",
                    mass_unit=components.WeightUnitEnum.OZ,
                )],
                async_=False,
            )
        )

        # Find matching rate
        selected_rate = None
        for rate in (shipment.rates or []):
            token = rate.servicelevel.token.lower() if rate.servicelevel else ""
            if carrier_token.lower() in token:
                selected_rate = rate
                break

        if not selected_rate and shipment.rates:
            selected_rate = shipment.rates[0]

        if not selected_rate:
            return {"success": False, "error": "No rates available"}

        # Generate label
        transaction = client.transactions.create(
            components.TransactionCreateRequest(
                rate=selected_rate.object_id,
                label_file_type=components.LabelFileTypeEnum.PDF,
                async_=False,
            )
        )

        if transaction.status == components.TransactionStatusEnum.SUCCESS:
            return {
                "success": True,
                "tracking_number": transaction.tracking_number,
                "tracking_url": transaction.tracking_url_provider,
                "label_url": transaction.label_url,
                "carrier": selected_rate.provider,
                "service": selected_rate.servicelevel.name,
                "rate": float(selected_rate.amount),
            }
        else:
            if transaction.messages:
                error_text = " | ".join([m.text for m in transaction.messages if hasattr(m, "text")])
                return {"success": False, "error": error_text}
            return {"success": False, "error": "Label creation failed"}

    except Exception as e:
        logger.error(f"Shippo label error: {e}")
        return {"success": False, "error": str(e)}


async def create_shippo_label(order, carrier: str) -> dict:
    """Wrapper used by admin/orders.py — extracts address from order snapshot."""
    try:
        addr = json.loads(order.shipping_address_snapshot or "{}")
    except Exception:
        addr = {}

    to_address = {
        "name": addr.get("full_name") or addr.get("label") or "Customer",
        "street1": addr.get("address_line1") or addr.get("line1") or "",
        "city": addr.get("city") or "",
        "state": addr.get("state") or "",
        "zip": addr.get("postal_code") or addr.get("zip_code") or "",
        "country": addr.get("country") or "US",
    }

    if not all([to_address["street1"], to_address["city"], to_address["state"], to_address["zip"]]):
        return {"success": False, "error": "Incomplete shipping address on order"}

    carrier_token = CARRIER_TOKENS.get(carrier, "usps_priority")
    return await create_label(str(order.id), to_address, carrier_token)


async def track_package(tracking_number: str, carrier: str) -> dict:
    try:
        client = get_client()
        tracking = client.tracking_status.get(
            carrier=carrier.lower(),
            tracking_number=tracking_number,
        )
        return {
            "status": tracking.tracking_status.status.value if tracking.tracking_status else "UNKNOWN",
            "detail": tracking.tracking_status.status_details if tracking.tracking_status else "",
            "eta": str(tracking.eta) if tracking.eta else None,
        }
    except Exception as e:
        logger.error(f"Shippo tracking error: {e}")
        return {"status": "UNKNOWN", "error": str(e)}
