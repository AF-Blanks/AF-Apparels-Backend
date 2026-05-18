"""Shippo integration for shipping label generation and package tracking."""
import json
import logging
import os

logger = logging.getLogger(__name__)

# Maps carrier key → Shippo service-level token fragment used for rate selection
CARRIER_TOKENS = {
    "usps": "usps_priority",
    "ups": "ups_ground",
    "fedex": "fedex_ground",
}


async def create_shippo_label(order, carrier: str) -> dict:
    try:
        import shippo

        api_key = os.getenv("SHIPPO_API_KEY", "")
        if not api_key:
            return {"success": False, "error": "SHIPPO_API_KEY not set"}

        shippo.config.api_key = api_key

        # Parse shipping address from order snapshot
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

        shipment = shippo.Shipment.create(
            address_from={
                "name": "AF Apparels",
                "street1": "10719 Turbeville Rd",
                "city": "Dallas",
                "state": "TX",
                "zip": "75243",
                "country": "US",
                "phone": "2145550100",
                "email": "shipping@afapparels.com",
            },
            address_to={
                "name": to_name,
                "street1": to_street,
                "city": to_city,
                "state": to_state,
                "zip": to_zip,
                "country": to_country,
            },
            parcels=[{
                "length": "12",
                "width": "10",
                "height": "6",
                "distance_unit": "in",
                "weight": "16",
                "mass_unit": "oz",
            }],
            async_=False,
        )

        token_fragment = CARRIER_TOKENS.get(carrier, "usps_priority")

        # Find matching rate
        selected_rate = None
        for rate in (shipment.rates or []):
            token = (getattr(rate.servicelevel, "token", "") or "").lower()
            if token_fragment in token:
                selected_rate = rate
                break

        # Fallback to first available rate
        if not selected_rate and shipment.rates:
            selected_rate = shipment.rates[0]

        if not selected_rate:
            return {"success": False, "error": "No rates available for this carrier"}

        logger.info(
            "Shippo rate selected: %s %s $%s",
            selected_rate.provider,
            getattr(selected_rate.servicelevel, "name", ""),
            selected_rate.amount,
        )

        # Generate label
        transaction = shippo.Transaction.create(
            rate=selected_rate.object_id,
            label_file_type="PDF",
            async_=False,
        )

        if transaction.status == "SUCCESS":
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
    try:
        import shippo

        api_key = os.getenv("SHIPPO_API_KEY", "")
        if not api_key:
            return {"status": "UNKNOWN", "error": "SHIPPO_API_KEY not configured"}

        shippo.config.api_key = api_key

        tracking = shippo.Track.get_status(carrier.lower(), tracking_number)
        ts = getattr(tracking, "tracking_status", None)
        return {
            "status": getattr(ts, "status", "UNKNOWN") if ts else "UNKNOWN",
            "detail": getattr(ts, "status_details", "") or "",
            "eta": str(tracking.eta) if getattr(tracking, "eta", None) else None,
        }
    except Exception as exc:
        logger.error("Shippo tracking error: %s", exc)
        return {"status": "UNKNOWN", "error": str(exc)}
