# Import all models here so SQLAlchemy's mapper registry is fully populated
# before any relationship string-references (e.g. "PricingTier") are resolved.
# Order matters: base types before models that reference them.
from app.models.base import BaseModel  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.pricing import PricingTier  # noqa: F401
from app.models.shipping import ShippingTier  # noqa: F401
from app.models.company import Company, CompanyUser, UserAddress  # noqa: F401
from app.models.product import Product, ProductVariant, ProductImage, Category  # noqa: F401
from app.models.inventory import Warehouse, InventoryRecord, InventoryAdjustment  # noqa: F401
from app.models.order import Order, OrderItem, CartItem, AbandonedCart, OrderTemplate, OrderComment  # noqa: F401
from app.models.wholesale import WholesaleApplication  # noqa: F401
from app.models.rma import RMARequest, RMAItem  # noqa: F401
from app.models.system import AuditLog, Settings  # noqa: F401
from app.models.communication import Message, EmailTemplate  # noqa: F401
from app.models.statement import StatementTransaction  # noqa: F401
from app.models.discount_group import DiscountGroup, VariantPricingOverride, VariantLevelPricingOverride  # noqa: F401
from app.models.discount import DiscountCode, DiscountUsage  # noqa: F401
