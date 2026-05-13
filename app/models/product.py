"""Product catalog models: Category, Product, Variant, Image, Asset."""
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import BaseModel

if TYPE_CHECKING:
    from app.models.inventory import InventoryRecord
    from app.models.order import CartItem, OrderItem


class Category(BaseModel):
    __tablename__ = "categories"

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    parent: Mapped["Category | None"] = relationship("Category", remote_side="Category.id")
    children: Mapped[list["Category"]] = relationship("Category", back_populates="parent", foreign_keys=[parent_id])
    product_links: Mapped[list["ProductCategory"]] = relationship("ProductCategory", back_populates="category")


class Product(BaseModel):
    __tablename__ = "products"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    short_description: Mapped[str | None] = mapped_column(String(500))

    # Business rules
    moq: Mapped[int] = mapped_column(Integer, default=1, nullable=False, comment="Minimum order quantity")

    # Status: draft | active | archived
    status: Mapped[str] = mapped_column(
        Enum("draft", "active", "archived", name="product_status"),
        default="draft",
        nullable=False,
        index=True,
    )

    # SEO
    meta_title: Mapped[str | None] = mapped_column(String(255))
    meta_description: Mapped[str | None] = mapped_column(String(500))

    # Organization
    product_type: Mapped[str | None] = mapped_column(String(100))
    vendor: Mapped[str | None] = mapped_column(String(255))
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)), nullable=True)

    fabric: Mapped[str | None] = mapped_column(String(255), nullable=True)
    product_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    weight: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gender: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Tab content fields
    care_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    print_guide: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    size_chart_data: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Full-text search vector (updated via PostgreSQL trigger)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR)

    # ── Relationships ─────────────────────────────────────────────────────────
    variants: Mapped[list["ProductVariant"]] = relationship(
        "ProductVariant", back_populates="product", cascade="all, delete-orphan"
    )
    images: Mapped[list["ProductImage"]] = relationship(
        "ProductImage",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="ProductImage.sort_order",
    )
    assets: Mapped[list["ProductAsset"]] = relationship(
        "ProductAsset", back_populates="product", cascade="all, delete-orphan"
    )
    category_links: Mapped[list["ProductCategory"]] = relationship(
        "ProductCategory", back_populates="product", cascade="all, delete-orphan"
    )
    reviews: Mapped[list["ProductReview"]] = relationship(
        "ProductReview", back_populates="product", cascade="all, delete-orphan",
    )

    # ── Computed properties for schema compatibility ───────────────────────────
    @property
    def primary_image(self) -> "ProductImage | None":
        if not self.images:
            return None
        for img in self.images:
            if img.is_primary:
                return img
        return self.images[0]

    @property
    def categories(self) -> list:
        return [link.category for link in self.category_links if getattr(link, "category", None)]


class ProductVariant(BaseModel):
    __tablename__ = "product_variants"

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    color: Mapped[str | None] = mapped_column(String(100))
    size: Mapped[str | None] = mapped_column(String(50))
    retail_price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    compare_price: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    msrp: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)  # kept for guest pricing logic; hidden from admin UI
    cost_per_item: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    country_of_origin: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Status: active | discontinued | out_of_stock
    status: Mapped[str] = mapped_column(
        Enum("active", "discontinued", "out_of_stock", name="variant_status"),
        default="active",
        nullable=False,
    )
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    product: Mapped["Product"] = relationship("Product", back_populates="variants")
    inventory_records: Mapped[list["InventoryRecord"]] = relationship(
        "InventoryRecord", back_populates="variant", cascade="all, delete-orphan"
    )


class ProductImage(BaseModel):
    __tablename__ = "product_images"

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # S3/CDN keys for each size
    url_thumbnail: Mapped[str] = mapped_column(String(1000), nullable=False)  # 150px
    url_medium: Mapped[str] = mapped_column(String(1000), nullable=False)     # 400px
    url_large: Mapped[str] = mapped_column(String(1000), nullable=False)      # 800px
    url_webp_thumbnail: Mapped[str | None] = mapped_column(String(1000))
    url_webp_medium: Mapped[str | None] = mapped_column(String(1000))
    url_webp_large: Mapped[str | None] = mapped_column(String(1000))
    alt_text: Mapped[str | None] = mapped_column(String(255))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    product: Mapped["Product"] = relationship("Product", back_populates="images")

    # ── Schema field-name aliases ──────────────────────────────────────────────
    @property
    def position(self) -> int:
        return self.sort_order

    @property
    def url_thumbnail_webp(self) -> str | None:
        return self.url_webp_thumbnail

    @property
    def url_medium_webp(self) -> str | None:
        return self.url_webp_medium

    @property
    def url_large_webp(self) -> str | None:
        return self.url_webp_large


class ProductAsset(BaseModel):
    """Marketing flyers, spec sheets, etc."""

    __tablename__ = "product_assets"

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_type: Mapped[str] = mapped_column(
        Enum("flyer", "spec_sheet", "size_chart", "other", name="asset_type"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)

    product: Mapped["Product"] = relationship("Product", back_populates="assets")


class ProductCategory(BaseModel):
    """Many-to-many Product ↔ Category."""

    __tablename__ = "product_categories"

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )

    product: Mapped["Product"] = relationship("Product", back_populates="category_links")
    category: Mapped["Category"] = relationship("Category", back_populates="product_links")


class ProductReview(BaseModel):
    """Customer reviews for products."""

    __tablename__ = "product_reviews"

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    reviewer_name: Mapped[str] = mapped_column(String(150), nullable=False)
    reviewer_company: Mapped[str | None] = mapped_column(String(150), nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_approved: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    product: Mapped["Product"] = relationship("Product", back_populates="reviews")
