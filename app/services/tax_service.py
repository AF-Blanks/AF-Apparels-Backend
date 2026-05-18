"""ZipTax integration for real-time sales tax calculation."""
import logging
import os
import httpx

logger = logging.getLogger(__name__)

ZIPTAX_BASE_URL = "https://api.zip-tax.com/request/v40"


def get_ziptax_client() -> str | None:
    """Return the ZipTax API key if configured, else None."""
    return os.getenv("ZIPTAX_API_KEY") or None


async def calculate_tax(
    to_state: str,
    to_zip: str,
    to_city: str,
    subtotal: float,
    shipping: float,
) -> dict:
    """Return { rate (%), tax_amount ($), region, source } from ZipTax.

    rate is expressed as a percentage (e.g. 8.25 for 8.25%).
    Falls back to { rate: 0, tax_amount: 0, source: "fallback" } on any error.
    """
    api_key = get_ziptax_client()
    if not api_key:
        logger.warning("ZIPTAX_API_KEY is not set — skipping ZipTax, returning 0 tax")
        return {"rate": 0.0, "tax_amount": 0.0, "region": to_state.upper(), "source": "manual"}

    logger.info("ZipTax request: state=%s zip=%s subtotal=%.2f api_key_prefix=%s",
                to_state, to_zip, subtotal, api_key[:8])

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                ZIPTAX_BASE_URL,
                params={"key": api_key, "postalcode": to_zip},
            )
            logger.info("ZipTax HTTP status: %s", response.status_code)
            response.raise_for_status()
            data = response.json()
            logger.info("ZipTax raw response: %s", data)

        results = data.get("results", [])
        if not results:
            logger.warning("ZipTax returned empty results for zip=%s state=%s — data=%s", to_zip, to_state, data)
            return {"rate": 0.0, "tax_amount": 0.0, "region": to_state.upper(), "source": "fallback"}

        result = results[0]
        tax_rate_decimal = float(result.get("taxSales", 0.0))
        rate = round(tax_rate_decimal * 100, 4)
        tax_amount = round(subtotal * tax_rate_decimal, 2)

        logger.info("ZipTax result: taxSales=%s rate=%.4f%% tax_amount=$%.2f", tax_rate_decimal, rate, tax_amount)

        return {
            "rate": rate,
            "tax_amount": tax_amount,
            "region": to_state.upper(),
            "source": "ziptax",
        }
    except Exception as exc:
        logger.warning("ZipTax calculation failed for %s %s: %s", to_state, to_zip, exc)
        return {"rate": 0.0, "tax_amount": 0.0, "region": to_state.upper(), "source": "fallback", "error": str(exc)}
