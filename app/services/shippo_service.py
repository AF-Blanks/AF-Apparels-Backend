"""Shippo integration for shipping label generation and package tracking."""
import json
import logging
import os

logger = logging.getLogger(__name__)

WAREHOUSE_ADDRESS = {
    "name": "AF Apparels",
    "street1": "10719 Turbeville Rd",
    "city": "Dallas",
    "state": "TX",
    "zip": "75243",
    "country": "US",
}

# Maps carrier key → Shippo service-level token fragment used for rate selection
CARRIER_TOKENS = {
    "usps": "usps_priority",
    "ups": "ups_ground",
    "fedex": "fedex_ground",
}


def get_shippo_client():
    import shippo as _shippo
    key = os.getenv("SHIPPO_API_KEY", "")
    if not key:
        logger.warning("SHIPPO_API_KEY is not set")
        return None
    return _shippo.Shippo(api_key_header=key)


async def create_shippo_label(order, carrier: str) -> dict:
    """
    Generate a Shippo shipping label for an order.

    Args:
        order: Order ORM instance — shipping address read from shipping_address_snapshot.
        carrier: "usps" | "ups" | "fedex"

    Returns:
        {success, tracking_number, tracking_url, label_url, carrier, service, rate}
        or {success: False, error: str}
    """
    client = get_shippo_client()
    if not client:
        return {"success": False, "error": "SHIPPO_API_KEY not configured"}

    # Parse shipping address from the order snapshot
    try:
        addr = json.loads(order.shipping_address_snapshot or "{}")
    except Exception:
        addr = {}

    to_name = addr.get("full_name") or addr.get("label") or "Customer"
    to_street = addr.get("address_line1") or addr.get("line1") or ""
    to_city = addr.get("city") or ""
    to_state = addr.get("state") or ""
    to_zip = addr.get("postal_code") or addr.get("zip_code") or ""
    to_country = addr.get("country") or "US"

    if not all([to_street, to_city, to_state, to_zip]):
        return {"success": False, "error": "Incomplete shipping address on order"}

    token_fragment = CARRIER_TOKENS.get(carrier, "usps_priority")

    try:
        from shippo.models import components as _comp

        shipment = client.shipments.create(
            data=_comp.ShipmentCreateRequest(
                address_from=_comp.AddressCreateRequest(
                    name=WAREHOUSE_ADDRESS["name"],
                    street1=WAREHOUSE_ADDRESS["street1"],
                    city=WAREHOUSE_ADDRESS["city"],
                    state=WAREHOUSE_ADDRESS["state"],
                    zip=WAREHOUSE_ADDRESS["zip"],
                    country=WAREHOUSE_ADDRESS["country"],
                ),
                address_to=_comp.AddressCreateRequest(
                    name=to_name,
                    street1=to_street,
                    city=to_city,
                    state=to_state,
                    zip=to_zip,
                    country=to_country,
                ),
                parcels=[_comp.ParcelCreateRequest(
                    length="12",
                    width="10",
                    height="6",
                    distance_unit=_comp.DistanceUnitEnum.IN,
                    weight="16",
                    mass_unit=_comp.WeightUnitEnum.OZ,
                )],
                async_=False,
            )
        )

        # Choose rate matching the requested carrier token
        selected_rate = None
        for rate in (shipment.rates or []):
            token = (getattr(rate.servicelevel, "token", "") or "").lower()
            if token_fragment in token:
                selected_rate = rate
                break
        if not selected_rate and shipment.rates:
            selected_rate = shipment.rates[0]

        if not selected_rate:
            return {"success": False, "error": "No shipping rates available from Shippo"}

        logger.info(
            "Shippo rate selected: %s %s $%s for %s %s",
            selected_rate.provider,
            getattr(selected_rate.servicelevel, "name", ""),
            selected_rate.amount,
            to_zip,
            to_state,
        )

        # Purchase the label
        transaction = client.transactions.create(
            data=_comp.TransactionCreateRequest(
                rate=selected_rate.object_id,
                label_file_type=_comp.LabelFileTypeEnum.PDF,
                async_=False,
            )
        )

        status_val = getattr(
            getattr(transaction, "status", None), "value",
            str(getattr(transaction, "status", ""))
        ).upper()

        if status_val == "SUCCESS":
            return {
                "success": True,
                "tracking_number": transaction.tracking_number,
                "tracking_url": transaction.tracking_url_provider,
                "label_url": transaction.label_url,
                "carrier": selected_rate.provider,
                "service": getattr(selected_rate.servicelevel, "name", ""),
                "rate": float(selected_rate.amount),
            }

        messages = getattr(transaction, "messages", None)
        return {"success": False, "error": str(messages) if messages else "Label creation failed"}

    except Exception as exc:
        logger.error("Shippo label error for order %s: %s", getattr(order, "order_number", "?"), exc)
        return {"success": False, "error": str(exc)}


async def track_shippo_package(tracking_number: str, carrier: str) -> dict:
    """Fetch live tracking status from Shippo."""
    client = get_shippo_client()
    if not client:
        return {"status": "UNKNOWN", "error": "SHIPPO_API_KEY not configured"}
    try:
        tracking = client.tracking_status.get(
            carrier=carrier.lower(),
            tracking_number=tracking_number,
        )
        ts = getattr(tracking, "tracking_status", None)
        return {
            "status": getattr(getattr(ts, "status", None), "value", "UNKNOWN") if ts else "UNKNOWN",
            "detail": getattr(ts, "status_details", "") or "",
            "eta": str(tracking.eta) if getattr(tracking, "eta", None) else None,
        }
    except Exception as exc:
        logger.error("Shippo tracking error: %s", exc)
        return {"status": "UNKNOWN", "error": str(exc)}
