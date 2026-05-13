# backend/app/schemas/product.py
from uuid import UUID
from decimal import Decimal
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


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
    country_of_origin: str | None = None
    effective_price: Decimal | None = None  # populated by pricing layer
    stock_quantity: int = 0               # summed across warehouses
    status: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Product list item (compact)
# ---------------------------------------------------------------------------

class ProductListItem(BaseModel):
    id: UUID
    name: str
    slug: str
    status: str
    moq: int
    primary_image: ProductImageOut | None = None
    variants: list[VariantOut]
    categories: list[CategoryOut] = []
    fabric: str | None = None
    product_code: str | None = None
    weight: str | None = None
    gender: str | None = None

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
