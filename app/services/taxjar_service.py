"""TaxJar integration for real-time sales tax calculation."""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)


def get_taxjar_client():
    api_key = os.getenv("TAXJAR_API_KEY")
    if not api_key:
        return None
    import taxjar
    return taxjar.Client(api_key=api_key, options={"api_url": "https://api.taxjar.com"})


async def calculate_tax(
    to_state: str,
    to_zip: str,
    to_city: str,
    subtotal: float,
    shipping: float,
) -> dict:
    """Return { rate (%), tax_amount ($), region, source } from TaxJar.

    rate is expressed as a percentage (e.g. 8.25 for 8.25%).
    Falls back to { rate: 0, tax_amount: 0, source: "fallback" } on any error.
    """
    try:
        client = get_taxjar_client()
        if not client:
            return {"rate": 0.0, "tax_amount": 0.0, "region": to_state.upper(), "source": "manual"}

        def _sync_call():
            return client.tax_for_order({
                "from_country": "US",
                "from_zip": "75201",
                "from_state": "TX",
                "from_city": "Dallas",
                "to_country": "US",
                "to_zip": to_zip,
                "to_state": to_state.upper(),
                "to_city": to_city,
                "amount": subtotal,
                "shipping": shipping,
            })

        tax = await asyncio.to_thread(_sync_call)

        rate = round(float(tax.rate) * 100, 4) if subtotal > 0 else 0.0
        return {
            "rate": rate,
            "tax_amount": round(float(tax.amount_to_collect), 2),
            "region": to_state.upper(),
            "source": "taxjar",
        }
    except Exception as exc:
        logger.warning("TaxJar calculation failed for %s %s: %s", to_state, to_zip, exc)
        return {"rate": 0.0, "tax_amount": 0.0, "region": to_state.upper(), "source": "fallback", "error": str(exc)}
