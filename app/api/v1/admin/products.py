# backend/app/api/v1/admin/products.py
"""Admin products router — full product catalog management with image processing."""
import io
import logging
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.exceptions import NotFoundError
from app.core.redis import redis_delete, redis_delete_pattern
from app.models.product import Category, Product, ProductAsset, ProductCategory, ProductImage, ProductVariant
from app.schemas.product import (
    BulkActionRequest,
    BulkGenerateRequest,
    CategoryCreate,
    CategoryOut,
    ImageUploadResponse,
    ImportResult,
    ProductCreate,
    ProductDetail,
    ProductListItem,
    ProductUpdate,
    VariantCreate,
    VariantOut,
)
from app.services.product_service import ProductService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/products", tags=["admin", "products"])


@router.get("", response_model=list[ProductDetail])
async def list_admin_products(
    q: str | None = None,
    status: str | None = None,
    category: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    svc = ProductService(db)  # noqa: F841
    from sqlalchemy.orm import selectinload
    from app.models.inventory import InventoryRecord

    query = select(Product).options(
        selectinload(Product.variants).selectinload(ProductVariant.inventory_records),
        selectinload(Product.images),
        selectinload(Product.assets),
        selectinload(Product.category_links)
        .selectinload(ProductCategory.category)
        .selectinload(Category.children),
    )
    if q:
        query = query.where(Product.name.ilike(f"%{q}%"))
    if status:
        query = query.where(Product.status == status)
    query = query.offset((page - 1) * page_size).limit(page_size)

    result = await db.execute(query)
    products = result.scalars().all()

    # Populate stock_quantity on each variant from inventory records
    for product in products:
        for variant in product.variants:
            variant.stock_quantity = sum(
                rec.quantity for rec in variant.inventory_records if rec.quantity > 0
            )

    return products


@router.post("", response_model=ProductDetail, status_code=status.HTTP_201_CREATED)
async def create_product(payload: ProductCreate, db: AsyncSession = Depends(get_db)):
    svc = ProductService(db)
    product = await svc.create_product(payload)
    await db.commit()

    # Reload with all relationships eagerly — prevents MissingGreenletError when
    # FastAPI serialises ProductDetail (images / variants / assets / category_links).
    result = await db.execute(
        select(Product)
        .where(Product.id == product.id)
        .options(
            selectinload(Product.variants),
            selectinload(Product.images),
            selectinload(Product.assets),
            selectinload(Product.category_links)
            .selectinload(ProductCategory.category)
            .selectinload(Category.children),
        )
    )
    return result.scalar_one()


@router.patch("/{product_id}", response_model=ProductDetail)
async def update_product(
    product_id: UUID, payload: ProductUpdate, db: AsyncSession = Depends(get_db)
):
    svc = ProductService(db)
    product = await svc.update_product(product_id, payload)
    await db.commit()

    result = await db.execute(
        select(Product)
        .where(Product.id == product.id)
        .options(
            selectinload(Product.variants),
            selectinload(Product.images),
            selectinload(Product.assets),
            selectinload(Product.category_links)
            .selectinload(ProductCategory.category)
            .selectinload(Category.children),
        )
    )
    return result.scalar_one()


@router.post("/{product_id}/images/from-url")
async def add_image_from_url(
    product_id: UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """Add a product image from an external URL — no upload/resize, same URL stored in all size slots."""
    result = await db.execute(select(Product).where(Product.id == product_id))
    if not result.scalar_one_or_none():
        raise NotFoundError(f"Product {product_id} not found")

    from sqlalchemy import func as sqlfunc
    count_result = await db.execute(
        select(sqlfunc.count()).where(ProductImage.product_id == product_id)
    )
    position = count_result.scalar_one() or 0

    url = str(payload.get("url", ""))
    image = ProductImage(
        product_id=product_id,
        url_thumbnail=url,
        url_medium=url,
        url_large=url,
        alt_text=payload.get("alt_text"),
        is_primary=bool(payload.get("is_primary", position == 0)),
        sort_order=position,
    )
    db.add(image)
    await db.commit()
    await db.refresh(image)
    return {"id": str(image.id), "url": url, "alt_text": image.alt_text}


@router.post("/{product_id}/images", response_model=ImageUploadResponse)
async def upload_product_image(
    product_id: UUID,
    file: UploadFile = File(...),
    alt_text: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Process image upload: resize to 3 sizes, convert to WebP, upload to S3."""
    # Verify product exists
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise NotFoundError(f"Product {product_id} not found")

    content = await file.read()
    urls = await _process_and_upload_image(content, product_id, file.filename or "image")

    # Determine position
    from sqlalchemy import func
    count_result = await db.execute(
        select(func.count()).where(ProductImage.product_id == product_id)
    )
    position = count_result.scalar_one()

    image = ProductImage(
        product_id=product_id,
        url_thumbnail=urls["thumbnail"],
        url_medium=urls["medium"],
        url_large=urls["large"],
        url_webp_thumbnail=urls["thumbnail_webp"],
        url_webp_medium=urls["medium_webp"],
        url_webp_large=urls["large_webp"],
        alt_text=alt_text,
        is_primary=position == 0,
        sort_order=position,
    )
    db.add(image)
    await db.commit()
    await db.refresh(image)

    return ImageUploadResponse(
        id=image.id,
        url_thumbnail=image.url_thumbnail,
        url_medium=image.url_medium,
        url_large=image.url_large,
    )


@router.patch("/{product_id}/images/reorder")
async def reorder_product_images(
    product_id: UUID,
    image_ids: list[UUID] = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """Reorder images by providing ordered list of image IDs."""
    for position, image_id in enumerate(image_ids):
        result = await db.execute(
            select(ProductImage).where(
                ProductImage.id == image_id,
                ProductImage.product_id == product_id,
            )
        )
        image = result.scalar_one_or_none()
        if image:
            image.sort_order = position
            image.is_primary = position == 0

    await db.commit()
    return {"reordered": len(image_ids)}


@router.post("/{product_id}/variants", response_model=VariantOut, status_code=201)
async def add_variant(
    product_id: UUID,
    payload: VariantCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a single variant to a product."""
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise NotFoundError(f"Product {product_id} not found")

    variant = ProductVariant(
        product_id=product_id,
        sku=payload.sku,
        color=payload.color,
        size=payload.size,
        retail_price=payload.retail_price,
        compare_price=payload.compare_price,
        status=payload.status,
    )
    db.add(variant)
    await db.commit()
    await db.refresh(variant)
    variant.stock_quantity = 0
    return variant


@router.post("/{product_id}/variants/bulk-generate")
async def bulk_generate_variants(
    product_id: UUID, payload: BulkGenerateRequest, db: AsyncSession = Depends(get_db)
):
    svc = ProductService(db)
    variants = await svc.bulk_generate_variants(
        product_id, payload.colors, payload.sizes, payload.base_retail_price
    )
    await db.commit()
    return {"generated": len(variants), "variants": [{"id": str(v.id), "sku": v.sku} for v in variants]}


@router.post("/{product_id}/variants/batch")
async def create_variants_batch(
    product_id: UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """Create multiple variants in one call with explicit SKUs (used by CSV import)."""
    result = await db.execute(select(Product).where(Product.id == product_id))
    if not result.scalar_one_or_none():
        raise NotFoundError(f"Product {product_id} not found")

    variants_data: list[dict] = payload.get("variants", [])
    created: list[ProductVariant] = []
    for v in variants_data:
        variant = ProductVariant(
            product_id=product_id,
            sku=str(v.get("sku", "")),
            color=v.get("color"),
            size=v.get("size"),
            retail_price=float(v.get("retail_price", 0)),
            status=str(v.get("status", "active")),
        )
        db.add(variant)
        created.append(variant)

    await db.flush()
    await db.commit()
    return {"created": len(created), "variants": [{"id": str(v.id), "sku": v.sku} for v in created]}


@router.patch("/{product_id}/variants/{variant_id}")
async def update_variant(
    product_id: UUID,
    variant_id: UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ProductVariant).where(
            ProductVariant.id == variant_id,
            ProductVariant.product_id == product_id,
        )
    )
    variant = result.scalar_one_or_none()
    if not variant:
        raise NotFoundError("Variant not found")

    # Numeric fields that need type coercion
    numeric_float = {"retail_price", "compare_price", "msrp", "cost_per_item"}
    numeric_int = {"sort_order"}
    skip_fields = {"stock_quantity"}  # handled separately via inventory records

    for field, value in payload.items():
        if field in skip_fields:
            continue
        if not hasattr(variant, field):
            continue
        if field in numeric_float:
            try:
                value = float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                value = None
        elif field in numeric_int:
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = 0
        setattr(variant, field, value)

    # Handle stock_quantity: update inventory record in default warehouse
    if "stock_quantity" in payload:
        try:
            requested_qty = max(0, int(payload["stock_quantity"]))
        except (TypeError, ValueError):
            requested_qty = 0

        from app.models.inventory import InventoryRecord, Warehouse
        from app.services.inventory_service import InventoryService

        # Get or pick first warehouse
        wh_result = await db.execute(
            select(Warehouse).where(Warehouse.is_active == True).limit(1)  # noqa: E712
        )
        warehouse = wh_result.scalar_one_or_none()

        if warehouse is None:
            # Auto-create a default warehouse
            warehouse = Warehouse(name="Main Warehouse", code="MAIN")
            db.add(warehouse)
            await db.flush()

        # Get current quantity for this variant + warehouse
        rec_result = await db.execute(
            select(InventoryRecord).where(
                InventoryRecord.variant_id == variant_id,
                InventoryRecord.warehouse_id == warehouse.id,
            )
        )
        rec = rec_result.scalar_one_or_none()
        current_qty = rec.quantity if rec else 0
        delta = requested_qty - current_qty

        if delta != 0 or rec is None:
            svc = InventoryService(db)
            await svc.adjust_stock_with_log(
                variant_id=variant_id,
                warehouse_id=warehouse.id,
                quantity_delta=delta if rec is not None else requested_qty,
                reason="correction",
                notes="Updated via admin product edit",
            )

    await db.commit()
    return {"id": str(variant.id), "sku": variant.sku}


async def _load_category(db: AsyncSession, category_id: UUID) -> Category | None:
    """Load category with children eagerly to avoid async lazy-load MissingGreenletError."""
    result = await db.execute(
        select(Category)
        .where(Category.id == category_id)
        .options(selectinload(Category.children))
    )
    return result.scalar_one_or_none()


@router.post("/categories", response_model=CategoryOut, status_code=201)
async def create_category(
    payload: CategoryCreate,
    db: AsyncSession = Depends(get_db),
):
    import re
    slug = payload.slug.strip() or re.sub(r"[^a-z0-9]+", "-", payload.name.lower()).strip("-")

    # Ensure uniqueness by appending a counter if the slug already exists
    base_slug = slug
    counter = 1
    while True:
        existing = await db.execute(select(Category).where(Category.slug == slug))
        if not existing.scalar_one_or_none():
            break
        slug = f"{base_slug}-{counter}"
        counter += 1

    cat = Category(
        name=payload.name,
        slug=slug,
        description=payload.description,
        parent_id=payload.parent_id,
        sort_order=payload.sort_order,
        image_url=payload.image_url,
    )
    db.add(cat)
    await db.commit()
    await redis_delete("categories:tree")
    return await _load_category(db, cat.id)


@router.patch("/categories/{category_id}", response_model=CategoryOut)
async def update_category(
    category_id: UUID,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    cat = await _load_category(db, category_id)
    if not cat:
        raise NotFoundError(f"Category not found: {category_id}")
    for field in ("name", "slug", "description", "is_active", "sort_order", "image_url"):
        if field in payload:
            setattr(cat, field, payload[field])
    await db.commit()
    await redis_delete("categories:tree")
    return await _load_category(db, category_id)


@router.delete("/categories/{category_id}", status_code=204)
async def delete_category(category_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Category).where(Category.id == category_id))
    cat = result.scalar_one_or_none()
    if not cat:
        raise NotFoundError(f"Category not found: {category_id}")
    await db.delete(cat)
    await db.commit()
    await redis_delete("categories:tree")


@router.post("/bulk-action")
async def bulk_action(payload: BulkActionRequest, db: AsyncSession = Depends(get_db)):
    svc = ProductService(db)
    count = await svc.apply_bulk_action(payload.ids, payload.action)
    await db.commit()
    return {"affected": count, "action": payload.action}


@router.post("/import-csv", response_model=ImportResult)
async def import_products_csv(
    file: UploadFile = File(...), db: AsyncSession = Depends(get_db)
):
    content = await file.read()
    svc = ProductService(db)
    result = await svc.import_from_csv(content.decode("utf-8"))
    await db.commit()
    return ImportResult(**result)


@router.get("/export-csv")
async def export_products_csv(db: AsyncSession = Depends(get_db)):
    svc = ProductService(db)
    csv_content = await svc.export_to_csv()
    return StreamingResponse(
        io.BytesIO(csv_content.encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=products_export.csv"},
    )


@router.get("/{slug}", response_model=ProductDetail)
async def get_admin_product(slug: str, db: AsyncSession = Depends(get_db)):
    """Fetch single product by slug (or UUID string). Defined after /export-csv to avoid conflict."""
    from sqlalchemy.orm import selectinload
    import uuid as _uuid

    try:
        uid = _uuid.UUID(slug)
        query = select(Product).where(Product.id == uid)
    except ValueError:
        query = select(Product).where(Product.slug == slug)

    query = query.options(
        selectinload(Product.variants).selectinload(ProductVariant.inventory_records),
        selectinload(Product.images),
        selectinload(Product.assets),
        selectinload(Product.category_links)
        .selectinload(ProductCategory.category)
        .selectinload(Category.children),
    )
    result = await db.execute(query)
    product = result.scalar_one_or_none()
    if not product:
        raise NotFoundError(f"Product '{slug}' not found")

    for variant in product.variants:
        variant.stock_quantity = sum(
            rec.quantity for rec in variant.inventory_records if rec.quantity > 0
        )

    return product


@router.delete("/{product_id}", status_code=204)
async def delete_product(product_id: UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise NotFoundError(f"Product {product_id} not found")
    await db.delete(product)
    await db.commit()


@router.patch("/{product_id}/images/{image_id}")
async def update_product_image(
    product_id: UUID, image_id: UUID, payload: dict = Body(...), db: AsyncSession = Depends(get_db)
):
    """Update image metadata — alt_text (color tag) and/or is_primary."""
    result = await db.execute(
        select(ProductImage).where(
            ProductImage.id == image_id, ProductImage.product_id == product_id
        )
    )
    image = result.scalar_one_or_none()
    if not image:
        raise NotFoundError("Image not found")
    if "alt_text" in payload:
        image.alt_text = payload["alt_text"]
    if "is_primary" in payload:
        image.is_primary = bool(payload["is_primary"])
    await db.commit()
    return {"id": str(image.id), "alt_text": image.alt_text, "is_primary": image.is_primary}


@router.delete("/{product_id}/images/{image_id}", status_code=204)
async def delete_product_image(
    product_id: UUID, image_id: UUID, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ProductImage).where(
            ProductImage.id == image_id, ProductImage.product_id == product_id
        )
    )
    image = result.scalar_one_or_none()
    if not image:
        raise NotFoundError("Image not found")
    await db.delete(image)
    await db.commit()


@router.delete("/{product_id}/variants/{variant_id}", status_code=204)
async def delete_product_variant(
    product_id: UUID, variant_id: UUID, db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(ProductVariant).where(
            ProductVariant.id == variant_id, ProductVariant.product_id == product_id
        )
    )
    variant = result.scalar_one_or_none()
    if not variant:
        raise NotFoundError("Variant not found")
    variant.status = "discontinued"
    await db.commit()


# ---------------------------------------------------------------------------
# Image processing helper
# ---------------------------------------------------------------------------

async def _process_and_upload_image(
    content: bytes, product_id: UUID, filename: str
) -> dict:
    """Resize to 150/400/800px + WebP. Uploads to S3 if configured, else saves locally."""
    from PIL import Image as PILImage
    import io as _io
    import os
    from app.core.config import get_settings

    settings = get_settings()
    use_s3 = bool(settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY)

    img = PILImage.open(_io.BytesIO(content)).convert("RGB")
    sizes = {"thumbnail": 150, "medium": 400, "large": 800}
    urls: dict[str, str] = {}

    base_name = filename.rsplit(".", 1)[0]
    base_key = f"products/{product_id}/{base_name}"

    if use_s3:
        import boto3
        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION,
        )
        bucket = settings.AWS_S3_BUCKET
        cdn = settings.CDN_BASE_URL.rstrip("/") if settings.CDN_BASE_URL else f"https://{bucket}.s3.amazonaws.com"
    else:
        local_dir = f"/app/media/products/{product_id}"
        os.makedirs(local_dir, exist_ok=True)

    for size_name, px in sizes.items():
        resized = img.copy()
        resized.thumbnail((px, px), PILImage.LANCZOS)

        if use_s3:
            jpeg_buf = _io.BytesIO()
            resized.save(jpeg_buf, "JPEG", quality=85, optimize=True)
            jpeg_key = f"{base_key}_{size_name}.jpg"
            s3.put_object(Bucket=bucket, Key=jpeg_key, Body=jpeg_buf.getvalue(), ContentType="image/jpeg")
            urls[size_name] = f"{cdn}/{jpeg_key}"

            webp_buf = _io.BytesIO()
            resized.save(webp_buf, "WEBP", quality=85)
            webp_key = f"{base_key}_{size_name}.webp"
            s3.put_object(Bucket=bucket, Key=webp_key, Body=webp_buf.getvalue(), ContentType="image/webp")
            urls[f"{size_name}_webp"] = f"{cdn}/{webp_key}"
        else:
            jpeg_path = f"/app/media/{base_key}_{size_name}.jpg"
            resized.save(jpeg_path, "JPEG", quality=85, optimize=True)
            urls[size_name] = f"/media/{base_key}_{size_name}.jpg"

            webp_path = f"/app/media/{base_key}_{size_name}.webp"
            resized.save(webp_path, "WEBP", quality=85)
            urls[f"{size_name}_webp"] = f"/media/{base_key}_{size_name}.webp"

    return urls


# ---------------------------------------------------------------------------
# Flyer upload for individual products
# ---------------------------------------------------------------------------

@router.post("/{product_id}/upload-flyer")
async def upload_product_flyer(
    product_id: UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload a PDF flyer for a product, replacing any existing one."""
    import uuid as _uuid
    import os
    from app.core.config import get_settings

    settings = get_settings()
    use_s3 = bool(settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY)

    result = await db.execute(
        select(Product).options(selectinload(Product.assets)).where(Product.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    content = await file.read()
    file_name = file.filename or "flyer.pdf"
    asset_id = str(_uuid.uuid4())

    if use_s3:
        import boto3
        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION,
        )
        bucket = settings.AWS_S3_BUCKET
        cdn = settings.CDN_BASE_URL.rstrip("/") if settings.CDN_BASE_URL else f"https://{bucket}.s3.amazonaws.com"
        key = f"flyers/{product_id}/{asset_id}/{file_name}"
        s3.put_object(Bucket=bucket, Key=key, Body=content, ContentType="application/pdf")
        url = f"{cdn}/{key}"
    else:
        local_dir = f"/app/media/flyers/{product_id}"
        os.makedirs(local_dir, exist_ok=True)
        local_path = f"{local_dir}/{file_name}"
        with open(local_path, "wb") as f_out:
            f_out.write(content)
        url = f"/media/flyers/{product_id}/{file_name}"

    # Delete existing flyer asset, then create new one
    for existing in [a for a in product.assets if a.asset_type == "flyer"]:
        await db.delete(existing)
    await db.flush()

    new_asset = ProductAsset(
        product_id=product.id,
        asset_type="flyer",
        url=url,
        file_name=file_name,
    )
    db.add(new_asset)
    await db.commit()
    await db.refresh(new_asset)

    await redis_delete_pattern(f"products:detail:{product.slug}:*")

    return {"url": url, "file_name": file_name, "id": str(new_asset.id)}


@router.delete("/{product_id}/flyer")
async def delete_product_flyer(
    product_id: UUID,
    db: AsyncSession = Depends(get_db),
):
    """Remove the flyer asset for a product."""
    result = await db.execute(
        select(Product).options(selectinload(Product.assets)).where(Product.id == product_id)
    )
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    deleted = False
    for existing in [a for a in product.assets if a.asset_type == "flyer"]:
        await db.delete(existing)
        deleted = True
    await db.commit()

    if not deleted:
        raise HTTPException(status_code=404, detail="No flyer to remove")

    await redis_delete_pattern(f"products:detail:{product.slug}:*")
    return {"message": "Flyer removed"}


# ---------------------------------------------------------------------------
# Generic image upload (collections, categories, etc.)
# ---------------------------------------------------------------------------

@router.post("/upload-image")
async def upload_generic_image(
    file: UploadFile = File(...),
):
    """Upload an image to S3 and return URLs. Used for collections and other non-product assets."""
    import uuid as _uuid
    import io as _io
    import os
    from app.core.config import get_settings

    settings = get_settings()
    use_s3 = bool(settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY)

    content = await file.read()
    asset_id = str(_uuid.uuid4())
    filename = file.filename or "image"
    base_name = filename.rsplit(".", 1)[0]
    base_key = f"uploads/{asset_id}/{base_name}"

    from PIL import Image as PILImage
    img = PILImage.open(_io.BytesIO(content)).convert("RGB")
    img.thumbnail((800, 800), PILImage.LANCZOS)

    if use_s3:
        import boto3
        s3 = boto3.client(
            "s3",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION,
        )
        bucket = settings.AWS_S3_BUCKET
        cdn = settings.CDN_BASE_URL.rstrip("/") if settings.CDN_BASE_URL else f"https://{bucket}.s3.amazonaws.com"

        jpeg_buf = _io.BytesIO()
        img.save(jpeg_buf, "JPEG", quality=85, optimize=True)
        jpeg_key = f"{base_key}.jpg"
        s3.put_object(Bucket=bucket, Key=jpeg_key, Body=jpeg_buf.getvalue(), ContentType="image/jpeg")
        url = f"{cdn}/{jpeg_key}"

        webp_buf = _io.BytesIO()
        img.save(webp_buf, "WEBP", quality=85)
        webp_key = f"{base_key}.webp"
        s3.put_object(Bucket=bucket, Key=webp_key, Body=webp_buf.getvalue(), ContentType="image/webp")
    else:
        local_dir = f"/app/media/uploads/{asset_id}"
        os.makedirs(local_dir, exist_ok=True)
        jpeg_path = f"{local_dir}/{base_name}.jpg"
        img.save(jpeg_path, "JPEG", quality=85, optimize=True)
        url = f"/media/uploads/{asset_id}/{base_name}.jpg"

    return {"url": url}
