"""Tax calculation — disabled at checkout; tax is handled by QuickBooks Automated Sales Tax on invoice."""
import logging

logger = logging.getLogger(__name__)

# ZipTax integration disabled — tax is now calculated by QuickBooks AST on invoice creation.
# ZIPTAX_BASE_URL = "https://api.zip-tax.com/request/v40"
#
# def get_ziptax_client() -> str | None:
#     return os.getenv("ZIPTAX_API_KEY") or None


async def calculate_tax(
    to_state: str,
    to_zip: str,
    to_city: str,
    subtotal: float,
    shipping: float,
) -> dict:
    """Tax calculation disabled — QB Automated Sales Tax handles tax on invoice.

    Returns zero tax at checkout; QB invoice will have correct tax applied.
    """
    logger.info("Tax calculation skipped — QB AST handles tax on invoice (state=%s zip=%s)", to_state, to_zip)
    return {"rate": 0.0, "tax_amount": 0.0, "region": to_state.upper() if to_state else "", "source": "qb_ast"}
