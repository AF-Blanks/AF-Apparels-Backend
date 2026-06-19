# backend/app/api/v1/products.py
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.schemas.product import CategoryOut, FilterParams, ProductDetail, ProductListItem
from app.schemas.review import ProductReviewCreate
from app.services.product_service import ProductService
from app.types.api import PaginatedResponse

router = APIRouter(prefix="/products", tags=["products"])


@router.get("/categories", response_model=list[CategoryOut])
async def list_categories(db: AsyncSession = Depends(get_db)):
    svc = ProductService(db)
    return await svc.get_category_tree()


@router.get("", response_model=PaginatedResponse[ProductListItem])
async def list_products(
    request: Request,
    category: str | None = None,
    size: str | None = None,
    color: str | None = None,
    price_min: Decimal | None = None,
    price_max: Decimal | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
    gender: str | None = None,
    fabric: str | None = None,
    weight: str | None = None,
    in_stock: bool | None = None,
    product_code: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    discount_percent = getattr(request.state, "tier_discount_percent", Decimal("0"))
    discount_group_id = getattr(request.state, "discount_group_id", None)
    params = FilterParams(
        category=category,
        size=size,
        color=color,
        price_min=price_min,
        price_max=price_max,
        q=q,
        page=page,
        page_size=page_size,
        gender=gender,
        fabric=fabric,
        weight=weight,
        in_stock=in_stock,
        product_code=product_code,
    )
    is_guest = getattr(request.state, "company_id", None) is None and not getattr(request.state, "is_admin", False)
    svc = ProductService(db)
    items, total = await svc.list_with_filters_and_search(params, discount_percent, discount_group_id, is_guest)
    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        pages=(total + page_size - 1) // page_size,
    )


@router.get("/{slug}", response_model=ProductDetail)
async def get_product(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    discount_percent = getattr(request.state, "tier_discount_percent", Decimal("0"))
    discount_group_id = getattr(request.state, "discount_group_id", None)
    is_guest = getattr(request.state, "company_id", None) is None and not getattr(request.state, "is_admin", False)
    svc = ProductService(db)
    return await svc.get_by_slug_with_variants(slug, discount_percent, discount_group_id, is_guest)


# ── T201: Asset download endpoints ────────────────────────────────────────────

@router.get("/{product_id}/download-images")
async def download_product_images(
    product_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Stream a ZIP of all product images (large size) from S3."""
    import io
    import zipfile

    import boto3
    from fastapi.responses import StreamingResponse

    from app.core.config import settings
    from app.models.product import Product, ProductImage

    result = await db.execute(
        select(Product)
        .options(selectinload(Product.images))
        .where(Product.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    if not product.images:
        raise HTTPException(status_code=404, detail="No images available for this product")

    def _generate_zip():
        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION,
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, img in enumerate(product.images):
                # Extract S3 key from URL
                url = img.url_large
                if url.startswith("https://"):
                    key = url.split(".amazonaws.com/", 1)[-1]
                else:
                    key = url.lstrip("/")
                try:
                    obj = s3.get_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
                    img_bytes = obj["Body"].read()
                    ext = key.rsplit(".", 1)[-1] if "." in key else "jpg"
                    zf.writestr(f"image_{i + 1:02d}.{ext}", img_bytes)
                except Exception:
                    pass
        buf.seek(0)
        return buf.read()

    zip_bytes = _generate_zip()
    safe_name = product.slug.replace("/", "_")

    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-images.zip"'},
    )


@router.get("/{product_id}/images/{image_id}/download")
async def download_single_image(
    product_id: uuid.UUID,
    image_id: uuid.UUID,
    filename: str = Query("image.jpg"),
    db: AsyncSession = Depends(get_db),
):
    """Stream a single product image from S3 as a file download."""
    import boto3
    from fastapi.responses import StreamingResponse

    from app.core.config import settings
    from app.models.product import ProductImage

    result = await db.execute(
        select(ProductImage).where(
            ProductImage.id == image_id,
            ProductImage.product_id == product_id,
        )
    )
    image = result.scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    url = image.url_large or image.url_medium
    if not url:
        raise HTTPException(status_code=404, detail="Image URL not available")

    # Extract S3 key from URL
    if url.startswith("https://") and ".amazonaws.com/" in url:
        key = url.split(".amazonaws.com/", 1)[-1]
    else:
        key = url.lstrip("/")

    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_S3_REGION,
    )
    try:
        obj = s3.get_object(Bucket=settings.AWS_S3_BUCKET, Key=key)
        img_bytes = obj["Body"].read()
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to fetch image from storage")

    ext = key.rsplit(".", 1)[-1].lower() if "." in key else "jpg"
    mime = "image/webp" if ext == "webp" else f"image/{ext}" if ext in ("png", "gif") else "image/jpeg"
    safe_name = filename.replace('"', "")

    return StreamingResponse(
        iter([img_bytes]),
        media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.get("/{product_id}/download-flyer")
async def download_product_flyer(
    product_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Redirect to S3 pre-signed URL for the product flyer PDF."""
    import boto3
    from app.core.config import settings
    from app.models.product import Product, ProductAsset

    result = await db.execute(
        select(Product)
        .options(selectinload(Product.assets))
        .where(Product.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    flyer = next(
        (a for a in product.assets if a.asset_type == "flyer"),
        None,
    )
    if not flyer:
        raise HTTPException(status_code=404, detail="No flyer available for this product")

    # If already a full HTTPS URL (e.g. direct S3 link), redirect to it immediately
    if flyer.url.startswith("https://"):
        return RedirectResponse(url=flyer.url)

    # For bare S3 keys, generate a presigned URL if credentials are available
    if settings.AWS_ACCESS_KEY_ID:
        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION,
        )
        key = flyer.url.lstrip("/")
        presigned = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.AWS_S3_BUCKET, "Key": key},
            ExpiresIn=300,
        )
        return RedirectResponse(url=presigned)

    return RedirectResponse(url=flyer.url)


@router.post("/{product_id}/email-flyer")
async def email_product_flyer(
    product_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Send a product flyer email to specified recipients (public, protected by reCAPTCHA)."""
    from app.models.product import Product
    from app.core.config import settings as _settings
    from app.services.email_service import EmailService

    payload = await request.json()
    from_email: str = payload.get("from_email", "").strip()
    to_raw: str = payload.get("to", "").strip()
    cc_raw: str = payload.get("cc", "").strip()
    subject: str = payload.get("subject", "").strip()
    message: str = payload.get("message", "").strip()
    recaptcha_token = payload.get("recaptcha_token")

    to_emails = [e.strip() for e in to_raw.split(",") if e.strip()]
    cc_emails = [e.strip() for e in cc_raw.split(",") if e.strip()]

    if not to_emails:
        raise HTTPException(status_code=422, detail="At least one recipient (To) is required")
    if not subject:
        raise HTTPException(status_code=422, detail="Subject is required")

    # Verify reCAPTCHA
    if _settings.RECAPTCHA_SECRET_KEY:
        if not recaptcha_token:
            raise HTTPException(status_code=422, detail="reCAPTCHA verification required")
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://www.google.com/recaptcha/api/siteverify",
                data={"secret": _settings.RECAPTCHA_SECRET_KEY, "response": recaptcha_token},
            )
            if not resp.json().get("success"):
                raise HTTPException(status_code=422, detail="reCAPTCHA verification failed")

    result = await db.execute(
        select(Product).options(selectinload(Product.assets)).where(Product.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    flyer = next((a for a in product.assets if a.asset_type == "flyer"), None)
    if not flyer:
        raise HTTPException(status_code=404, detail="No flyer available for this product")

    reply_to_line = f'<p style="font-size:12px;color:#7A7880">Reply to: {from_email}</p>' if from_email else ""
    message_block = f'<p style="margin:0 0 20px;color:#374151;font-size:14px;line-height:1.7;white-space:pre-line">{message}</p>' if message else ""

    body_html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto">
      <div style="background:#080808;padding:24px;text-align:center">
        <span style="font-size:36px;font-weight:900;color:#1A5CFF">A</span>
        <span style="font-size:36px;font-weight:900;color:#E8242A">F</span>
        <span style="color:#fff;font-size:14px;margin-left:8px;letter-spacing:.1em">APPARELS</span>
      </div>
      <div style="padding:32px;background:#fff">
        <h2 style="font-family:sans-serif;color:#2A2830;margin:0 0 8px">Product Flyer — {product.name}</h2>
        {reply_to_line}
        <hr style="border:none;border-top:1px solid #E2E0DA;margin:16px 0">
        {message_block}
        <p style="margin:0 0 20px;color:#374151;font-size:14px">
          Please find the product flyer for <strong>{product.name}</strong> below:
        </p>
        <p style="margin:24px 0">
          <a href="{flyer.url}"
             style="background:#1A5CFF;color:#fff;padding:12px 28px;border-radius:6px;text-decoration:none;font-weight:700;display:inline-block">
            View / Download Flyer (PDF)
          </a>
        </p>
        <p style="color:#7A7880;font-size:12px;margin:24px 0 0">AF Apparels Wholesale · af-apparel.com</p>
      </div>
    </div>
    """

    svc = EmailService(db)
    for recipient in to_emails:
        svc.send_raw(
            to_email=recipient,
            subject=subject,
            body_html=body_html,
            cc=cc_emails if cc_emails else None,
            reply_to=from_email if from_email else None,
        )

    return {"message": f"Flyer sent to {len(to_emails)} recipient(s)"}


# ── Product Reviews ────────────────────────────────────────────────────────────

@router.get("/{product_id}/reviews")
async def list_product_reviews(
    product_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    from app.models.product import ProductReview
    from sqlalchemy import func

    result = await db.execute(
        select(ProductReview)
        .where(ProductReview.product_id == product_id, ProductReview.is_approved == True)  # noqa: E712
        .order_by(ProductReview.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    reviews = result.scalars().all()

    count_result = await db.execute(
        select(func.count(ProductReview.id))
        .where(ProductReview.product_id == product_id, ProductReview.is_approved == True)  # noqa: E712
    )
    total = count_result.scalar_one()

    avg_result = await db.execute(
        select(func.avg(ProductReview.rating))
        .where(ProductReview.product_id == product_id, ProductReview.is_approved == True)  # noqa: E712
    )
    avg_rating = float(avg_result.scalar_one() or 0)

    from app.schemas.review import ProductReviewOut, ReviewsResponse
    return ReviewsResponse(
        reviews=[ProductReviewOut.model_validate(r) for r in reviews],
        total=total,
        avg_rating=round(avg_rating, 1),
    )


@router.post("/{product_id}/reviews", status_code=201)
async def create_product_review(
    product_id: uuid.UUID,
    payload: ProductReviewCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from app.models.product import Product, ProductReview
    from app.schemas.review import ProductReviewOut

    product_result = await db.execute(select(Product).where(Product.id == product_id))
    if not product_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Product not found")

    user_id = getattr(request.state, "user_id", None)

    review = ProductReview(
        product_id=product_id,
        user_id=uuid.UUID(user_id) if user_id else None,
        rating=payload.rating,
        title=payload.title,
        body=payload.body,
        reviewer_name=payload.reviewer_name,
        reviewer_company=payload.reviewer_company,
        is_verified=user_id is not None,
        is_approved=True,
        image_url=payload.image_url,
    )
    db.add(review)
    await db.commit()
    await db.refresh(review)
    return ProductReviewOut.model_validate(review)


# ── T202: Bulk asset download ─────────────────────────────────────────────────

@router.post("/bulk-download")
async def bulk_asset_download(
    payload: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Accept a list of product_ids, queue ZIP generation task, return task_id."""
    import uuid as _uuid

    product_ids: list[str] = payload.get("product_ids", [])
    if not product_ids:
        raise HTTPException(status_code=400, detail="No product IDs provided")
    if len(product_ids) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 products per bulk download")

    task_id = str(_uuid.uuid4())

    from app.tasks.inventory_tasks import generate_bulk_asset_zip
    generate_bulk_asset_zip.delay(product_ids, task_id)

    return {"task_id": task_id, "status": "queued", "product_count": len(product_ids)}
