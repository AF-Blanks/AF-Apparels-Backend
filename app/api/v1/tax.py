"""POST /api/v1/tax/calculate — ZipTax-backed tax calculation."""
import logging
import os
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tax")


class TaxCalculateRequest(BaseModel):
    subtotal: float
    zip_code: str
    state: str
    discount: float = 0.0


@router.post("/calculate")
async def calculate_tax(
    body: TaxCalculateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    state = body.state.upper()
    taxable_subtotal = max(0.0, body.subtotal - body.discount)

    logger.info(
        "Tax calculate: raw_state=%r state=%s zip=%r subtotal=%.2f discount=%.2f taxable=%.2f",
        body.state, state, body.zip_code, body.subtotal, body.discount, taxable_subtotal,
    )
    logger.info(
        "Tax endpoint received: zip_code=%r type=%s state=%r",
        body.zip_code, type(body.zip_code).__name__, body.state,
    )

    # Strip and clean zip before calling ZipTax (rCode 108 = invalid format)
    clean_zip = str(body.zip_code).strip().zfill(5) if body.zip_code else ""
    logger.info("Clean zip: %r length=%d", clean_zip, len(clean_zip))

    # Tax-exempt companies pay no tax
    company_id = getattr(request.state, "company_id", None)
    if company_id:
        from app.models.company import Company
        company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
        if company and company.tax_exempt:
            logger.info("Tax: company %s is tax-exempt → returning 0", company_id)
            return {"tax_rate": 0.0, "tax_amount": 0.0, "region": state, "taxable": False, "source": "exempt"}

    # ZipTax: use when API key is configured and we have address data
    from app.services.tax_service import get_ziptax_client
    api_key_present = get_ziptax_client() is not None
    logger.info("Tax: zip_code=%r taxable_subtotal=%.2f ziptax_key_present=%s", body.zip_code, taxable_subtotal, api_key_present)

    if clean_zip and taxable_subtotal > 0:
        if api_key_present:
            from app.services.tax_service import calculate_tax as ziptax_calc
            result = await ziptax_calc(
                to_state=state,
                to_zip=clean_zip,
                to_city="",
                subtotal=taxable_subtotal,
                shipping=0,
            )
            logger.info("ZipTax service returned: %s", result)
            if result.get("source") == "ziptax":
                logger.info(
                    "ZipTax success: %s %s → rate=%.4f%% amount=$%.2f",
                    state, clean_zip, result["rate"], result["tax_amount"],
                )
                return {
                    "tax_rate": result["rate"],
                    "tax_amount": result["tax_amount"],
                    "region": result["region"],
                    "taxable": result["tax_amount"] > 0,
                    "source": "ziptax",
                }
            logger.warning("ZipTax did not return source=ziptax — result: %s", result)
        else:
            logger.warning("ZIPTAX_API_KEY not set in environment — skipping ZipTax")
    else:
        logger.info("Tax: skipping ZipTax — clean_zip=%r empty or taxable_subtotal=0", clean_zip)

    # Fallback: manual tax_rates table
    from app.api.v1.admin.taxes import TaxRate
    r = (await db.execute(
        select(TaxRate).where(TaxRate.region == state, TaxRate.is_enabled == True)  # noqa: E712
    )).scalar_one_or_none()

    if r:
        rate = float(r.rate)
        tax_amount = round(taxable_subtotal * rate / 100, 2)
        logger.info("Tax: manual table match for %s → rate=%.4f%% amount=$%.2f", state, rate, tax_amount)
        return {
            "tax_rate": rate,
            "tax_amount": tax_amount,
            "region": r.region,
            "taxable": tax_amount > 0,
            "source": "manual",
        }

    logger.info("Tax: no manual rate found for %s → returning 0", state)
    return {"tax_rate": 0.0, "tax_amount": 0.0, "region": state, "taxable": False, "source": "none"}


@router.get("/test-ziptax")
async def test_ziptax(zip_code: str = "75215"):
    """Debug endpoint — tests ZipTax API directly. Remove after debugging."""
    import httpx
    from app.services.tax_service import ZIPTAX_BASE_URL

    api_key = os.getenv("ZIPTAX_API_KEY", "")
    key_status = f"{api_key[:8]}..." if len(api_key) >= 8 else ("SET_BUT_SHORT" if api_key else "NOT_SET")

    logger.info("test-ziptax: key_status=%s zip=%s", key_status, zip_code)

    if not api_key:
        return {"error": "ZIPTAX_API_KEY is not set", "key_status": key_status}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                ZIPTAX_BASE_URL,
                params={"key": api_key, "postalcode": zip_code},
            )
        return {
            "key_status": key_status,
            "zip_code": zip_code,
            "http_status": resp.status_code,
            "response": resp.json(),
        }
    except Exception as exc:
        logger.error("test-ziptax error: %s", exc)
        return {"key_status": key_status, "zip_code": zip_code, "error": str(exc)}
