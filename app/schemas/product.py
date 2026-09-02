# backend/app/schemas/product.py
from uuid import UUID
from decimal import Decimal
from datetime import date, datetime
from typing import Any
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------

class CategoryOut(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None = None
    parent_id: UUID | None = None
    sort_order: int = 0
    is_active: bool = True
    image_url: str | None = None
    product_count: int = 0
    children: list["CategoryOut"] = []

    model_config = {"from_attributes": True}

CategoryOut.model_rebuild()


class CategoryCreate(BaseModel):
    name: str
    slug: str = ""
    description: str | None = None
    parent_id: UUID | None = None
    sort_order: int = 0
    image_url: str | None = None


# ---------------------------------------------------------------------------
# Images & Assets
# ---------------------------------------------------------------------------

class ProductImageOut(BaseModel):
    id: UUID
    url_thumbnail: str
    url_medium: str
    url_large: str
    url_thumbnail_webp: str | None
    url_medium_webp: str | None
    url_large_webp: str | None
    alt_text: str | None
    is_primary: bool
    position: int

    model_config = {"from_attributes": True}

    @field_validator(
        "url_thumbnail", "url_medium", "url_large",
        "url_thumbnail_webp", "url_medium_webp", "url_large_webp",
        mode="after",
    )
    @classmethod
    def _encode_url(cls, v: str | None) -> str | None:
        """Percent-encode a stored URL so a browser can actually fetch it.

        Images uploaded straight from a phone or a chat window keep their given
        name — "Screenshot 2026-08-28 at 10.26.22 AM.png" — and the spaces went
        into the S3 key and out again in the src attribute, where they are not
        valid. The picture was there all along; the address for it was not.

        Encoding on the way out fixes every image already stored without
        touching S3 or the database. Already-encoded URLs are left alone, so
        running this over a %20 does not turn it into %2520.
        """
        if not v:
            return v
        from urllib.parse import quote, urlsplit, urlunsplit

        parts = urlsplit(v)
        if not parts.scheme:          # a relative /media/... path
            return quote(v, safe="/%:")
        return urlunsplit((
            parts.scheme, parts.netloc,
            quote(parts.path, safe="/%"),
            parts.query, parts.fragment,
        ))


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------

class VariantOut(BaseModel):
    id: UUID
    sku: str
    color: str | None
    size: str | None
    retail_price: Decimal
    compare_price: Decimal | None = None
    # msrp: Decimal | None = None  # deprecated in admin UI; kept in DB for guest pricing
    cost_per_item: Decimal | None = None
    avg_cost: Decimal | None = None       # local weighted-avg from PO receipts (admin only)
    country_of_origin: str | None = None
    weight_grams: float | None = None
    effective_price: Decimal | None = None  # populated by pricing layer
    stock_quantity: int = 0               # summed across warehouses
    # Sellable past zero. Stock then runs negative — that figure is what is owed,
    # not what is on the shelf, so the storefront shows a date instead of a count.
    allow_backorder: bool = False
    # When the next purchase order for this variant is due in. Populated only for
    # backorder variants that are short, since that is the only time it answers a
    # question the shopper is actually asking.
    expected_restock_date: date | None = None
    # Every purchase order still to arrive, soonest first. Two deliveries of the
    # same thing three weeks apart are two different answers to "when can I have
    # it" — and which one applies depends on how many the shopper wants, so both
    # are shown rather than only the earliest.
    incoming: list["IncomingStock"] = []
    status: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Product list item (compact)
# ---------------------------------------------------------------------------

class IncomingStock(BaseModel):
    """A delivery of this variant that has been ordered but has not arrived."""

    #: What the supplier has been told, not a promise — labelled as expected
    #: everywhere it is shown.
    expected_date: date
    #: Still outstanding on that purchase order: ordered less already received.
    quantity: int


VariantOut.model_rebuild()


class ProductListItem(BaseModel):
    id: UUID
    name: str
    slug: str
    status: str
    moq: int
    sort_order: int = 0
    primary_image: ProductImageOut | None = None
    variants: list[VariantOut]
    categories: list[CategoryOut] = []
    fabric: str | None = None
    product_code: str | None = None
    weight: str | None = None
    gender: str | None = None
    tagline: str | None = None
    is_bestseller: bool = False

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Product asset
# ---------------------------------------------------------------------------

class ProductAssetOut(BaseModel):
    id: UUID
    asset_type: str
    url: str
    file_name: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Product detail (full)
# ---------------------------------------------------------------------------

class ProductDetail(BaseModel):
    id: UUID
    name: str
    slug: str
    description: str | None = None
    status: str = "draft"
    moq: int = 1
    images: list[ProductImageOut] = []
    variants: list[VariantOut] = []
    categories: list[CategoryOut] = []
    meta_title: str | None = None
    meta_description: str | None = None
    product_type: str | None = None
    vendor: str | None = None
    tags: list[str] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    fabric: str | None = None
    product_code: str | None = None
    weight: str | None = None
    gender: str | None = None
    care_instructions: str | None = None
    print_guide: dict | None = None
    size_chart_data: list | None = None
    assets: list[ProductAssetOut] = []
    highlight_text: str | None = None
    review_count: int = 0
    avg_rating: float = 0.0
    sort_order: int = 0
    tagline: str | None = None
    is_bestseller: bool = False

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Filter params
# ---------------------------------------------------------------------------

class FilterParams(BaseModel):
    category: str | None = None
    size: str | None = None
    color: str | None = None
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    q: str | None = None
    page: int = Field(1, ge=1)
    page_size: int = Field(24, ge=1, le=100)
    gender: str | None = None
    fabric: str | None = None
    weight: str | None = None
    in_stock: bool | None = None
    product_code: str | None = None
    is_bestseller: bool | None = None


# ---------------------------------------------------------------------------
# Admin write schemas (T103 — Phase 10)
# ---------------------------------------------------------------------------

class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    moq: int = Field(1, ge=1)
    status: str = "draft"
    meta_title: str | None = None
    meta_description: str | None = None
    product_type: str | None = None
    vendor: str | None = None
    tags: list[str] | None = None
    fabric: str | None = None
    product_code: str | None = None
    weight: str | None = None
    gender: str | None = None
    category_ids: list[UUID] = []
    tagline: str | None = None
    is_bestseller: bool = False


class ProductUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    moq: int | None = Field(None, ge=1)
    status: str | None = None
    meta_title: str | None = None
    meta_description: str | None = None
    product_type: str | None = None
    vendor: str | None = None
    tags: list[str] | None = None
    fabric: str | None = None
    product_code: str | None = None
    weight: str | None = None
    category_ids: list[UUID] | None = None
    gender: str | None = None
    care_instructions: str | None = None
    print_guide: dict | None = None
    size_chart_data: list | None = None
    highlight_text: str | None = None
    tagline: str | None = None
    is_bestseller: bool | None = None


class ImageUploadResponse(BaseModel):
    id: UUID
    url_thumbnail: str
    url_medium: str
    url_large: str


class VariantCreate(BaseModel):
    sku: str = Field(..., min_length=1, max_length=100)
    color: str | None = None
    size: str | None = None
    retail_price: Decimal = Field(Decimal("0"), ge=0)
    compare_price: Decimal | None = None
    # msrp: Decimal | None = None  # deprecated in admin UI; kept in DB for guest pricing
    cost_per_item: Decimal | None = None
    country_of_origin: str | None = None
    weight_grams: float | None = None
    status: str = "active"


class BulkGenerateRequest(BaseModel):
    colors: list[str] = Field(..., min_length=1)
    sizes: list[str] = Field(..., min_length=1)
    base_retail_price: Decimal


class BulkActionRequest(BaseModel):
    ids: list[UUID]
    action: str  # publish | unpublish | delete


class ImportResult(BaseModel):
    imported: int
    skipped: int
    errors: list[str]
