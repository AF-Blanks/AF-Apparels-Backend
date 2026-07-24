"""Custom color name → hex overrides — shared across admin swatch pickers and the customer-facing product page."""
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class CustomSwatchColor(BaseModel):
    __tablename__ = "custom_swatch_colors"

    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    hex: Mapped[str] = mapped_column(String(7), nullable=False)
