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
    is_bestseller: bool | None = None,
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
        is_bestseller=is_bestseller,
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
    color: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Stream a ZIP of product images (large size) from S3.

    When ?color= is passed, only that colour's images are zipped (matched by
    ProductImage.alt_text, the same field the gallery groups by). This powers
    the per-colour "Download All" button — one reliable ZIP instead of many
    browser-blocked individual downloads.
    """
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

    # Optional per-colour filter — matches the gallery's grouping (by alt_text).
    images = list(product.images)
    if color:
        _c = color.strip().lower()
        filtered = [im for im in images if (im.alt_text or "").strip().lower() == _c]
        if filtered:
            images = filtered
        # if nothing matched (alt_text not set for this colour), fall back to all

    def _generate_zip():
        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION,
        )
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, img in enumerate(images):
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
    if color:
        safe_color = "".join(ch for ch in color if ch.isalnum() or ch in "-_").strip("-_") or "colour"
        zip_name = f"{safe_name}-{safe_color}-images.zip"
    else:
        zip_name = f"{safe_name}-images.zip"

    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@router.get("/{product_id}/download-image/{image_id}")
async def download_single_product_image(
    product_id: uuid.UUID,
    image_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Stream ONE product image as an attachment so the browser force-downloads it
    (instead of opening the image in a new tab, which is what a direct cross-origin
    link does)."""
    import boto3
    from fastapi.responses import StreamingResponse

    from app.core.config import settings
    from app.models.product import Product, ProductImage

    img = (await db.execute(
        select(ProductImage).where(ProductImage.id == image_id, ProductImage.product_id == product_id)
    )).scalar_one_or_none()
    if not img:
        raise HTTPException(status_code=404, detail="Image not found")

    url = img.url_large or getattr(img, "url_medium", None) or ""
    if not url:
        raise HTTPException(status_code=404, detail="Image has no downloadable file")
    key = url.split(".amazonaws.com/", 1)[-1] if url.startswith("https://") else url.lstrip("/")

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
        raise HTTPException(status_code=502, detail="Could not fetch image from storage")

    slug = (await db.execute(select(Product.slug).where(Product.id == product_id))).scalar_one_or_none() or "product"
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else "jpg"
    color = (img.alt_text or "image").strip()
    safe_color = "".join(ch for ch in color if ch.isalnum() or ch in "-_").strip("-_") or "image"
    filename = f"{slug}-{safe_color}.{ext}"
    media_type = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"

    return StreamingResponse(
        iter([img_bytes]),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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

    reply_to_line = (
        f'<p style="font-size:13px;color:#9ca3af;margin:0 0 20px">'
        f'Sent by <strong style="color:#6b7280">{from_email}</strong> — reply to reach them directly.</p>'
        if from_email else ""
    )
    message_block = (
        f'<div style="background:#f9fafb;border-left:3px solid #1B3A5C;padding:14px 18px;'
        f'border-radius:6px;margin:0 0 24px">'
        f'<p style="margin:0;color:#374151;font-size:14px;line-height:1.7;white-space:pre-line">{message}</p>'
        f'</div>'
        if message else ""
    )
    _img = getattr(product, "image_url", None)
    image_block = (
        f'<img src="{_img}" alt="{product.name}" '
        f'style="width:100%;max-width:340px;height:auto;border-radius:10px;'
        f'border:1px solid #e5e7eb;display:block;margin:0 auto 24px" />'
        if _img else ""
    )

    content_html = (
        '<p style="font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;'
        'color:#E8242A;margin:0 0 6px">Product Flyer</p>'
        f'<h1 style="font-size:24px;color:#1B3A5C;margin:0 0 20px;font-weight:800;line-height:1.25">{product.name}</h1>'
        f'{reply_to_line}'
        f'{message_block}'
        f'{image_block}'
        '<p style="margin:0 0 24px;color:#374151;font-size:15px;line-height:1.6">'
        f'Here is the product flyer for <strong>{product.name}</strong> — tap below to view or '
        'download the full PDF with all colors, sizes, and pricing.'
        '</p>'
        '<p style="margin:0 0 8px">'
        f'<a href="{flyer.url}" style="background:#1B3A5C;color:#ffffff;padding:14px 32px;'
        'border-radius:8px;text-decoration:none;font-weight:700;display:inline-block;font-size:15px">'
        'View / Download Flyer (PDF) &rarr;</a></p>'
    )

    body_html = EmailService._base_template(content_html)

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
