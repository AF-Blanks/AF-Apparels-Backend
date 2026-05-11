# backend/app/api/v1/orders.py
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.order import Order, OrderComment
from app.models.user import User
from app.schemas.order import OrderListItem, OrderOut
from app.services.order_service import OrderService
from app.types.api import PaginatedResponse

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("", response_model=PaginatedResponse[OrderListItem])
async def list_orders(
    request: Request,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
    account_type = getattr(request.state, "account_type", "wholesale")

    svc = OrderService(db)

    if account_type == "retail" and user_id:
        orders, total = await svc.list_orders_for_retail_user(user_id, page, page_size, q=q, status=status)
    elif company_id:
        orders, total = await svc.list_orders_for_company(company_id, page, page_size, q=q, status=status)
    else:
        raise ForbiddenError("Company account required")

    return PaginatedResponse(
        items=orders,
        total=total,
        page=page,
        page_size=page_size,
        pages=max(1, (total + page_size - 1) // page_size),
    )


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    import uuid as _uuid
    from sqlalchemy.orm import selectinload as _sil

    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
    account_type = getattr(request.state, "account_type", "wholesale")

    svc = OrderService(db)

    # Support order_number (AF-XXXXXX) or UUID in URL
    if order_id.upper().startswith("AF-"):
        order_num = order_id.upper()
        if account_type == "retail" and user_id:
            result = await db.execute(
                select(Order).options(_sil(Order.items))
                .where(Order.order_number == order_num, Order.placed_by_id == _uuid.UUID(user_id))
            )
        elif company_id:
            result = await db.execute(
                select(Order).options(_sil(Order.items))
                .where(Order.order_number == order_num, Order.company_id == _uuid.UUID(str(company_id)))
            )
        else:
            raise ForbiddenError("Company account required")
        order = result.scalar_one_or_none()
        if not order:
            raise NotFoundError(f"Order {order_id} not found")
        return order

    oid = _uuid.UUID(order_id)
    if account_type == "retail" and user_id:
        return await svc.get_order_for_retail_user(oid, user_id)
    elif company_id:
        return await svc.get_order(oid, company_id)
    else:
        raise ForbiddenError("Company account required")


@router.post("/{order_id}/reorder")
async def reorder(
    order_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
    account_type = getattr(request.state, "account_type", "wholesale")

    if account_type != "retail" and not company_id:
        raise ForbiddenError("Company account required")

    from decimal import Decimal
    discount_percent = getattr(request.state, "tier_discount_percent", Decimal("0"))

    svc = OrderService(db)
    result = await svc.reorder(order_id, company_id, discount_percent)
    await db.commit()
    return result


# ── PDF endpoints ──────────────────────────────────────────────────────────────

def _pdf_response(pdf_bytes: bytes, filename: str) -> StreamingResponse:
    import io
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _load_order_for_company(order_id: UUID, company_id, db: AsyncSession) -> Order:
    import uuid as _uuid
    from sqlalchemy.orm import selectinload
    company_uuid = _uuid.UUID(str(company_id)) if not isinstance(company_id, _uuid.UUID) else company_id
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id, Order.company_id == company_uuid)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    return order


@router.get("/{order_id}/pdf/confirmation")
async def download_order_confirmation_pdf(
    order_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    order = await _load_order_for_company(order_id, company_id, db)
    from app.services.pdf_service import PDFService
    pdf = PDFService().generate_order_confirmation(order)
    return _pdf_response(pdf, f"order-confirmation-{order.order_number}.pdf")


@router.get("/{order_id}/pdf/invoice")
async def download_invoice_pdf(
    order_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    order = await _load_order_for_company(order_id, company_id, db)
    from app.services.pdf_service import PDFService
    pdf = PDFService().generate_invoice(order)
    return _pdf_response(pdf, f"invoice-{order.order_number}.pdf")


@router.get("/{order_id}/pdf/ship-confirmation")
async def download_ship_confirmation_pdf(
    order_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    order = await _load_order_for_company(order_id, company_id, db)
    from app.services.pdf_service import PDFService
    pdf = PDFService().generate_ship_confirmation(order)
    return _pdf_response(pdf, f"ship-confirmation-{order.order_number}.pdf")


@router.get("/{order_id}/pdf/pack-slip")
async def download_pack_slip_pdf(
    order_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    company_id = getattr(request.state, "company_id", None)
    if not company_id:
        raise ForbiddenError("Company account required")

    order = await _load_order_for_company(order_id, company_id, db)
    from app.services.pdf_service import PDFService
    pdf = PDFService().generate_pack_slip(order)
    return _pdf_response(pdf, f"pack-slip-{order.order_number}.pdf")


# ── Order comments ─────────────────────────────────────────────────────────────

from pydantic import BaseModel as _BaseModel, Field as _Field
from datetime import datetime as _datetime


class CommentIn(_BaseModel):
    body: str = _Field(..., min_length=1, max_length=2000)


class CommentOut(_BaseModel):
    id: UUID
    body: str
    is_admin: bool
    author_name: str | None
    created_at: _datetime

    model_config = {"from_attributes": True}


@router.get("/{order_id}/comments", response_model=list[CommentOut])
async def list_order_comments(
    order_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    import uuid as _uuid
    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
    account_type = getattr(request.state, "account_type", "wholesale")

    # Verify order ownership: retail via placed_by_id, wholesale via company_id
    if account_type == "retail" and user_id:
        order = (await db.execute(
            select(Order).where(Order.id == order_id, Order.placed_by_id == _uuid.UUID(user_id))
        )).scalar_one_or_none()
    elif company_id:
        order = (await db.execute(
            select(Order).where(Order.id == order_id, Order.company_id == _uuid.UUID(str(company_id)))
        )).scalar_one_or_none()
    else:
        raise ForbiddenError("Company account required")
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    from sqlalchemy.orm import selectinload
    result = await db.execute(
        select(OrderComment)
        .options(selectinload(OrderComment.author))
        .where(OrderComment.order_id == order_id)
        .order_by(OrderComment.created_at)
    )
    comments = result.scalars().all()

    return [
        CommentOut(
            id=c.id,
            body=c.body,
            is_admin=c.is_admin,
            author_name=c.author.full_name if c.author else None,
            created_at=c.created_at,
        )
        for c in comments
    ]


@router.post("/{order_id}/comments", response_model=CommentOut, status_code=201)
async def add_order_comment(
    order_id: UUID,
    payload: CommentIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    import uuid as _uuid
    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
    account_type = getattr(request.state, "account_type", "wholesale")

    # Verify order ownership: retail via placed_by_id, wholesale via company_id
    if account_type == "retail" and user_id:
        order = (await db.execute(
            select(Order).where(Order.id == order_id, Order.placed_by_id == _uuid.UUID(user_id))
        )).scalar_one_or_none()
    elif company_id:
        order = (await db.execute(
            select(Order).where(Order.id == order_id, Order.company_id == _uuid.UUID(str(company_id)))
        )).scalar_one_or_none()
    else:
        raise ForbiddenError("Company account required")
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    comment = OrderComment(
        order_id=order_id,
        author_id=user_id,
        body=payload.body,
        is_admin=False,
    )
    db.add(comment)
    await db.flush()

    author = None
    if user_id:
        author = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()

    await db.commit()
    await db.refresh(comment)

    return CommentOut(
        id=comment.id,
        body=comment.body,
        is_admin=comment.is_admin,
        author_name=author.full_name if author else None,
        created_at=comment.created_at,
    )
