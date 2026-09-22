"""Price list generation Celery tasks."""
import logging
from decimal import Decimal

from app.core.celery import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=2)
def generate_price_list_task(
    self, request_id: str, company_id: str, format: str = "pdf"
) -> dict:
    """Generate a PDF or Excel price list for a company and upload to S3.

    Steps:
    1. Load company pricing tier from DB
    2. Fetch all active products + variants
    3. Apply tier discount to each variant retail_price
    4. Render to PDF (reportlab) or Excel (openpyxl)
    5. Upload to S3; store URL in price_list_requests.file_url
    6. Update status to 'completed'
    """
    try:
        import asyncio
        from sqlalchemy import select, update
        from app.core.database import async_session_factory
        from app.models.system import PriceListRequest
        from app.models.company import Company
        from app.models.product import Product, ProductVariant
        from app.models.pricing import PricingTier
        from app.services.pricing_service import PricingService

        async def _run():
            async with async_session_factory() as db:
                # Mark as processing
                await db.execute(
                    update(PriceListRequest)
                    .where(PriceListRequest.id == request_id)
                    .values(status="processing")
                )
                await db.commit()

                # Load company + pricing tier
                result = await db.execute(
                    select(Company).where(Company.id == company_id)
                )
                company = result.scalar_one_or_none()
                if not company or not company.pricing_tier_id:
                    raise ValueError(f"Company {company_id} has no pricing tier")

                tier_result = await db.execute(
                    select(PricingTier).where(
                        PricingTier.id == company.pricing_tier_id
                    )
                )
                tier = tier_result.scalar_one()
                pricing_svc = PricingService(db)

                # Fetch active products + variants
                products_result = await db.execute(
                    select(Product).where(Product.status == "active")
                )
                products = products_result.scalars().all()

                rows = []
                for product in products:
                    variants_result = await db.execute(
                        select(ProductVariant).where(
                            ProductVariant.product_id == product.id,
                            ProductVariant.status == "active",
                        )
                    )
                    for variant in variants_result.scalars().all():
                        _md = getattr(variant, "markdown_price", None)
                        effective = (
                            Decimal(str(_md)) if _md is not None
                            else pricing_svc.calculate_effective_price(
                                variant.retail_price, tier.discount_percent
                            )
                        )
                        rows.append(
                            {
                                "product_name": product.name,
                                "sku": variant.sku,
                                "color": variant.color,
                                "size": variant.size,
                                "retail_price": variant.retail_price,
                                "tier_price": effective,
                            }
                        )

                # Generate file (format-dependent)
                if format == "excel":
                    file_bytes, mime, ext = _build_excel(rows, tier.name)
                else:
                    file_bytes, mime, ext = _build_pdf(rows, tier.name)

                # Upload to S3
                file_url = _upload_to_s3(file_bytes, f"pricelist_{request_id}.{ext}")

                # Mark completed
                await db.execute(
                    update(PriceListRequest)
                    .where(PriceListRequest.id == request_id)
                    .values(status="completed", file_url=file_url)
                )
                await db.commit()
                return {"status": "completed", "file_url": file_url}

        return asyncio.run(_run())

    except Exception as exc:
        logger.exception("Price list generation failed for request %s", request_id)
        # Mark failed in DB (best-effort, non-blocking)
        try:
            import asyncio
            from sqlalchemy import update
            from app.core.database import async_session_factory
            from app.models.system import PriceListRequest

            async def _mark_failed():
                async with async_session_factory() as db:
                    await db.execute(
                        update(PriceListRequest)
                        .where(PriceListRequest.id == request_id)
                        .values(status="failed")
                    )
                    await db.commit()

            asyncio.run(_mark_failed())
        except Exception:
            pass
        raise self.retry(exc=exc, countdown=60)


def _build_excel(rows: list[dict], tier_name: str) -> tuple[bytes, str, str]:
    """Build Excel workbook bytes from rows."""
    from openpyxl import Workbook
    from io import BytesIO

    wb = Workbook()
    ws = wb.active
    ws.title = f"Price List — {tier_name}"
    headers = ["Product", "SKU", "Color", "Size", "Retail Price", "Your Price"]
    ws.append(headers)
    for row in rows:
        ws.append([
            row["product_name"],
            row["sku"],
            row["color"] or "",
            row["size"] or "",
            float(row["retail_price"]),
            float(row["tier_price"]),
        ])
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"


def _build_pdf(rows: list[dict], tier_name: str) -> tuple[bytes, str, str]:
    """Build PDF bytes from rows using reportlab."""
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    from io import BytesIO

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = [Paragraph(f"Price List — {tier_name}", styles["Title"])]

    table_data = [["Product", "SKU", "Color", "Size", "Retail", "Your Price"]]
    for row in rows:
        table_data.append([
            row["product_name"],
            row["sku"],
            row["color"] or "",
            row["size"] or "",
            f"${row['retail_price']:.2f}",
            f"${row['tier_price']:.2f}",
        ])

    t = Table(table_data)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.grey),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
    ]))
    elements.append(t)
    doc.build(elements)
    return buf.getvalue(), "application/pdf", "pdf"


def _upload_to_s3(file_bytes: bytes, key: str) -> str:
    """Upload bytes to S3 and return a presigned URL."""
    import boto3
    from app.core.config import get_settings

    settings = get_settings()
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_REGION,
    )
    bucket = settings.S3_BUCKET_NAME
    s3_key = f"pricelists/{key}"
    s3.put_object(Bucket=bucket, Key=s3_key, Body=file_bytes)
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": s3_key},
        ExpiresIn=86400,  # 24 hours
    )
    return url
