from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Body, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ForbiddenError
from app.schemas.cart import CartResponse, MatrixAddRequest, QuickOrderRequest, QuickOrderResult, ValidationResultItem
from app.services.cart_service import CartService

router = APIRouter(prefix="/cart", tags=["cart"])


def _require_company(request: Request) -> UUID:
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")
    return company_id


def _discount(request: Request) -> Decimal:
    return getattr(request.state, "tier_discount_percent", Decimal("0"))


def _group_id(request: Request) -> str | None:
    return getattr(request.state, "discount_group_id", None)


@router.get("", response_model=CartResponse)
async def get_cart(request: Request, db: AsyncSession = Depends(get_db)):
    company_id = _require_company(request)
    svc = CartService(db)
    return await svc.get_cart_with_pricing(company_id, _discount(request), _group_id(request))


@router.post("/add-matrix", response_model=CartResponse, status_code=status.HTTP_200_OK)
async def add_matrix(
    payload: MatrixAddRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = _require_company(request)
    svc = CartService(db)
    result = await svc.add_matrix_items(company_id, payload, _discount(request), _group_id(request))
    await db.commit()
    return result


@router.patch("/items/{item_id}", response_model=CartResponse)
async def update_item(
    item_id: UUID,
    quantity: int = Body(..., embed=True, ge=1),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    company_id = _require_company(request)
    svc = CartService(db)
    result = await svc.update_item_quantity(
        company_id, item_id, quantity, _discount(request), _group_id(request)
    )
    await db.commit()
    return result


@router.delete("/items/{item_id}", response_model=CartResponse)
async def remove_item(
    item_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = _require_company(request)
    svc = CartService(db)
    result = await svc.remove_item(company_id, item_id, _discount(request), _group_id(request))
    await db.commit()
    return result


@router.post("/quick-order", response_model=QuickOrderResult)
async def quick_order(
    payload: QuickOrderRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Validate SKU/qty pairs; add valid items to cart; return categorized results."""
    company_id = _require_company(request)
    svc = CartService(db)
    validation = await svc.validate_sku_list([{"sku": i.sku, "quantity": i.quantity} for i in payload.items])

    # Bulk-add valid items
    added = await svc.bulk_add_validated_items(company_id, validation["valid"], _discount(request), _group_id(request))
    if added:
        await db.commit()

    def _to_item(d: dict) -> ValidationResultItem:
        return ValidationResultItem(**d)

    return QuickOrderResult(
        valid=[_to_item(i) for i in validation["valid"]],
        invalid=[_to_item(i) for i in validation["invalid"]],
        insufficient_stock=[_to_item(i) for i in validation["insufficient_stock"]],
        added_to_cart=added,
    )


@router.post("/save-template", status_code=status.HTTP_201_CREATED)
async def save_template(
    name: str = Body(..., embed=True, min_length=1),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    company_id = _require_company(request)
    user_id = getattr(request.state, "user_id", None)
    svc = CartService(db)
    template = await svc.save_as_template(company_id, user_id, name)
    await db.commit()
    return {"id": str(template.id), "name": template.name}
