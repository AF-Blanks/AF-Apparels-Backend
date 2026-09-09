# backend/app/api/v1/orders.py
import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

logger = logging.getLogger(__name__)
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

    # Surface any approved return/refund on each row (status itself stays the
    # fulfillment state, e.g. "delivered").
    from app.services.order_service import get_return_status_map
    ret_map = await get_return_status_map(db, [o.id for o in orders])
    for o in orders:
        o.return_status = ret_map.get(o.id)

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

    # Support order_number (AF-XXXXXX / DRAFT-XXXXXX / numeric 1001+) or UUID in URL
    if order_id.upper().startswith(("AF-", "DRAFT-")) or order_id.isdigit():
        order_num = order_id.upper() if order_id.upper().startswith(("AF-", "DRAFT-")) else order_id
        # Resolve order_number → UUID, then delegate to service methods so all
        # required relationships are eagerly loaded (prevents MissingGreenlet 500s).
        row = await db.execute(select(Order.id).where(Order.order_number == order_num))
        found_id = row.scalar_one_or_none()
        if not found_id:
            raise NotFoundError(f"Order {order_id} not found")
        if account_type == "retail" and user_id:
            return await svc.get_order_for_retail_user(found_id, user_id)
        elif company_id:
            return await svc.get_order(found_id, company_id)
        else:
            raise ForbiddenError("Company account required")

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
        .options(
            selectinload(Order.items),
            selectinload(Order.placed_by),
            selectinload(Order.company),
        )
        .where(Order.id == order_id, Order.company_id == company_uuid)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    return order


async def _load_order_for_auth(order_id_str: str, request: Request, db: AsyncSession) -> Order:
    """Load an order by UUID or order_number for the authenticated user (retail or wholesale)."""
    import uuid as _uuid
    from sqlalchemy.orm import selectinload

    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
    account_type = getattr(request.state, "account_type", "wholesale")

    def _q(where_clauses):
        return (
            select(Order)
            .options(
                selectinload(Order.items),
                selectinload(Order.placed_by),
                selectinload(Order.company),
            )
            .where(*where_clauses)
        )

    if order_id_str.upper().startswith(("AF-", "DRAFT-")) or order_id_str.isdigit():
        order_num = order_id_str.upper() if order_id_str.upper().startswith(("AF-", "DRAFT-")) else order_id_str
        if account_type == "retail" and user_id:
            stmt = _q([Order.order_number == order_num, Order.placed_by_id == _uuid.UUID(user_id)])
        elif company_id:
            stmt = _q([Order.order_number == order_num, Order.company_id == _uuid.UUID(str(company_id))])
        else:
            raise ForbiddenError("Company account required")
    else:
        try:
            oid = _uuid.UUID(order_id_str)
        except ValueError:
            raise NotFoundError(f"Order {order_id_str} not found")
        if account_type == "retail" and user_id:
            stmt = _q([Order.id == oid, Order.placed_by_id == _uuid.UUID(user_id)])
        elif company_id:
            stmt = _q([Order.id == oid, Order.company_id == _uuid.UUID(str(company_id))])
        else:
            raise ForbiddenError("Company account required")

    order = (await db.execute(stmt)).scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id_str} not found")
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
    order_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    order = await _load_order_for_auth(order_id, request, db)

    # Proxy QB invoice PDF when available
    if order.qb_invoice_id:
        try:
            import io
            import httpx as _httpx
            from app.services.quickbooks_service import QuickBooksService

            svc = await QuickBooksService().initialize()
            async with _httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    svc._url(f"invoice/{order.qb_invoice_id}/pdf"),
                    params={"minorversion": "65"},
                    headers={
                        "Authorization": f"Bearer {svc._access_token}",
                        "Accept": "application/pdf",
                    },
                )
            if resp.status_code == 200:
                return StreamingResponse(
                    io.BytesIO(resp.content),
                    media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="invoice-{order.order_number}.pdf"'},
                )
            logger.warning("QB PDF returned %s for invoice %s — falling back to local PDF", resp.status_code, order.qb_invoice_id)
        except Exception as exc:
            logger.error("QB PDF fetch failed: %s — falling back to local PDF", exc)

    # Local PDF fallback
    from app.services.pdf_service import PDFService
    try:
        pdf = PDFService().generate_invoice(order)
        if not pdf:
            raise ValueError("PDF generation returned empty bytes")
        return _pdf_response(pdf, f"invoice-{order.order_number}.pdf")
    except Exception as e:
        logger.error(f"Invoice PDF generation failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")


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


class _PayInvoiceRequest(_BaseModel):
    #: QuickBooks one-time card token. Optional now that Stripe can take this
    #: payment instead — one of these has to be present, checked in the handler.
    card_token: str | None = None
    #: A payment method collected in the browser by Stripe Elements — a card or
    #: a US bank account. Raw numbers never reach this server.
    stripe_payment_method_id: str | None = None
    #: Charge a card the customer already saved, by its Stripe id.
    stripe_saved_method_id: str | None = None
    #: "card" or "ach". A bank debit does not settle here; it settles days later
    #: through the webhook, so this endpoint answers "pending", not "paid".
    payment_method: str = "card"
    #: Ticked box authorising us to debit the account. Required for ach.
    ach_authorized: bool = False
    ach_authorization_text: str | None = None
    #: Made once when the form is opened and reused on every retry of that same
    #: attempt, so a double-click cannot pay twice. See payment_attempt.py.
    attempt_key: str | None = None


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


@router.get("/{order_id}/invoice-summary")
async def get_order_invoice_summary(order_id: str, db: AsyncSession = Depends(get_db)):
    """Public endpoint — returns limited order data for invoice payment links."""
    from sqlalchemy.orm import selectinload as _sil
    result = await db.execute(
        select(Order).options(_sil(Order.items))
        .where(Order.order_number == order_id.upper())
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")
    _total = float(order.total)
    _paid = float(order.amount_paid or 0)
    return {
        "id": str(order.id),
        "order_number": order.order_number,
        "status": order.status,
        "payment_status": order.payment_status,
        "subtotal": float(order.subtotal or 0),
        "shipping_cost": float(order.shipping_cost or 0),
        "tax_amount": float(order.tax_amount or 0),
        "total": _total,
        "amount_paid": _paid,
        "balance_due": max(0.0, _total - _paid),
        "items": [
            {
                "product_name": item.product_name,
                "color": item.color,
                "size": item.size,
                "quantity": item.quantity,
                "unit_price": float(item.unit_price),
            }
            for item in (order.items or [])
        ],
    }


@router.post("/{order_id}/pay-invoice")
async def pay_invoice(
    order_id: UUID,
    payload: _PayInvoiceRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    import json as _json
    import uuid as _uuid
    from datetime import datetime as _dt, timezone as _tz
    from sqlalchemy import text as _text
    from sqlalchemy.orm import selectinload as _sil
    from app.services.qb_payments_service import QBPaymentsService

    company_id = getattr(request.state, "company_id", None)
    user_id = getattr(request.state, "user_id", None)
    account_type = getattr(request.state, "account_type", "wholesale")

    result = await db.execute(
        select(Order).options(_sil(Order.items)).where(Order.id == order_id)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise NotFoundError(f"Order {order_id} not found")

    if account_type == "retail" and user_id:
        if order.placed_by_id != _uuid.UUID(user_id):
            raise ForbiddenError("Access denied")
    elif company_id:
        if order.company_id != _uuid.UUID(str(company_id)):
            raise ForbiddenError("Access denied")
    else:
        raise ForbiddenError("Authentication required")

    from decimal import Decimal as _Dec
    _order_total = _Dec(str(order.total or 0))
    _already_paid = _Dec(str(order.amount_paid or 0))
    _balance_due = max(_Dec('0.00'), _order_total - _already_paid)

    if _balance_due <= _Dec('0.00'):
        raise HTTPException(status_code=400, detail="Order is already paid in full")

    # ── One press of Pay, however many times it arrives ───────────────────
    #
    # This endpoint had no guard at all: two clicks were two charges against the
    # same invoice. The key comes from the form and is reused on every retry of
    # that one attempt, so the second request is refused rather than charged.
    from app.services import payment_attempt as _attempt
    _akey = (payload.attempt_key or "").strip() or f"inv:{order_id}:{_balance_due}"
    try:
        await _attempt.claim(db, attempt_key=_akey, company_id=str(company_id or ""))
    except _attempt.AttemptAlreadyDone:
        return {
            "message": "This payment has already gone through.",
            "order_number": order.order_number,
            "already_paid": True,
        }
    except _attempt.AttemptInFlight:
        raise HTTPException(
            status_code=409,
            detail="This payment is already going through — please wait a moment.",
        )

    # ── Whoever is taking money today ─────────────────────────────────────
    #
    # This used to be QuickBooks and only QuickBooks, which meant a customer on
    # terms clicking Pay went through the provider we are moving off — including
    # its bank transfers, the thing that does not work. One switch decides it,
    # the same switch checkout reads.
    from app.services.stripe_service import is_stripe_active as _stripe_on

    _provider = "stripe" if _stripe_on() else "quickbooks"
    _settled = True          # did the money actually arrive during this request?
    _stripe_out: dict = {}

    try:
        if _provider == "stripe":
            from app.services.stripe_service import StripeService
            _svc = StripeService()

            _company = None
            if company_id:
                from app.models.company import Company as _Co
                _company = (await db.execute(
                    select(_Co).where(_Co.id == company_id)
                )).scalar_one_or_none()

            _cust = getattr(_company, "stripe_customer_id", None)
            if _company is not None and not _cust:
                _cust = await asyncio.to_thread(
                    lambda: _svc.find_or_create_customer(
                        company_id=str(_company.id),
                        name=_company.name or f"Company {_company.id}",
                        email=_company.company_email,
                    )
                )
                _company.stripe_customer_id = _cust
                await db.commit()

            _desc = f"Invoice — {order.order_number}"
            if (payload.payment_method or "card").lower() == "ach":
                if not payload.ach_authorized:
                    raise ValueError(
                        "Please tick the box authorising us to debit your bank account."
                    )
                if not payload.stripe_payment_method_id:
                    raise ValueError("Bank details are missing. Please re-enter them.")
                _stripe_out = await asyncio.to_thread(
                    lambda: _svc.charge_bank_account(
                        amount=float(_balance_due),
                        payment_method_id=payload.stripe_payment_method_id,
                        idempotency_key=f"inv-ach:{_akey}",
                        customer_id=_cust,
                        description=_desc,
                        metadata={"order_number": order.order_number,
                                  "company_id": str(company_id or "")},
                    )
                )
            elif payload.stripe_saved_method_id:
                if not _cust:
                    raise ValueError("No saved cards on this account yet.")
                _stripe_out = await asyncio.to_thread(
                    lambda: _svc.charge_saved_card(
                        amount=float(_balance_due),
                        customer_id=_cust,
                        payment_method_id=payload.stripe_saved_method_id,
                        idempotency_key=f"inv-card:{_akey}",
                        description=_desc,
                        metadata={"order_number": order.order_number,
                                  "company_id": str(company_id or "")},
                    )
                )
            elif payload.stripe_payment_method_id:
                _stripe_out = await asyncio.to_thread(
                    lambda: _svc.charge_card(
                        amount=float(_balance_due),
                        payment_method_id=payload.stripe_payment_method_id,
                        idempotency_key=f"inv-card:{_akey}",
                        customer_id=_cust,
                        description=_desc,
                        metadata={"order_number": order.order_number,
                                  "company_id": str(company_id or "")},
                    )
                )
            else:
                raise ValueError("Payment details are missing.")

            charge_resp = {"id": _stripe_out.get("id"), "status": _stripe_out.get("status")}
            # A bank debit leaves Stripe in "processing" and lands days later.
            # Calling that paid here is how goods go out against money that has
            # not arrived; the webhook marks it paid when it actually does.
            _settled = _stripe_out.get("payment_status") == "paid"
            if not _settled and _stripe_out.get("payment_status") == "failed":
                raise ValueError(
                    f"Payment was not approved (status: {_stripe_out.get('status')})."
                )
        else:
            if not payload.card_token:
                raise ValueError("Card details are missing.")
            qb = QBPaymentsService()
            charge_resp = qb.charge_card(
                token=payload.card_token,
                amount=float(_balance_due),
                description=f"Invoice — {order.order_number}",
            )
            if charge_resp.get("status") != "CAPTURED":
                raise ValueError(
                    f"Payment not captured (status={charge_resp.get('status')})"
                )
    except (RuntimeError, ValueError) as exc:
        await _attempt.failed(db, attempt_key=_akey, reason=str(exc))
        raise HTTPException(status_code=400, detail=f"Payment failed: {exc}") from exc
    except Exception as exc:
        # Stripe raises its own error types — a declined card is a CardError, not
        # a RuntimeError. Re-raising those bare turned an ordinary decline into a
        # 500 error page on the customer's invoice.
        await _attempt.failed(db, attempt_key=_akey, reason=str(exc))
        _uexc = getattr(exc, "user_message", None)
        if _uexc:
            raise HTTPException(status_code=400, detail=f"Payment failed: {_uexc}") from exc
        logger.exception("pay_invoice failed for order %s", order_id)
        raise HTTPException(
            status_code=400,
            detail=(
                "We could not take that payment. Please check your details and "
                "try again, or call us on 214-272-7213."
            ),
        ) from exc

    now = _dt.now(_tz.utc)
    timeline = list(order.timeline or [])
    timeline.append({
        "message": (
            f"Payment received via invoice link — ${float(_balance_due):.2f}"
            if _settled else
            f"Bank transfer started via invoice link — ${float(_balance_due):.2f}. "
            f"Takes 3–5 business days to clear; the order stays unpaid until it does."
        ),
        "status": "paid" if _settled else "pending",
        "created_by": "Customer",
        "created_at": now.isoformat(),
    })

    # Only money that has actually arrived counts as paid. A bank debit is still
    # in flight, so amount_paid is left alone and the webhook settles it — the
    # same rule checkout follows.
    if _settled:
        await db.execute(
            _text(
                "UPDATE orders SET payment_status='paid', marked_paid_at=:ts, "
                "amount_paid=:ap, timeline=CAST(:tl AS jsonb) WHERE id=:id"
            ),
            {"ts": now, "ap": float(_order_total), "tl": _json.dumps(timeline), "id": str(order_id)},
        )
    else:
        await db.execute(
            _text(
                "UPDATE orders SET payment_status='pending', "
                "timeline=CAST(:tl AS jsonb) WHERE id=:id"
            ),
            {"tl": _json.dumps(timeline), "id": str(order_id)},
        )

    # Who took it, so a refund months from now goes back the same way — and so
    # the webhook can find this order by its intent when the debit lands.
    if _provider == "stripe":
        await db.execute(
            _text(
                "UPDATE orders SET payment_provider='stripe', "
                "stripe_payment_intent_id=:pi, stripe_charge_id=:ch, "
                "stripe_payment_status=:st, stripe_customer_id=COALESCE(:cu, stripe_customer_id) "
                "WHERE id=:id"
            ),
            {
                "pi": _stripe_out.get("id"),
                "ch": _stripe_out.get("charge_id"),
                "st": _stripe_out.get("status"),
                "cu": _stripe_out.get("customer_id"),
                "id": str(order_id),
            },
        )

    await db.commit()
    await _attempt.completed(
        db, attempt_key=_akey, order_id=str(order_id),
        payment_reference=str(charge_resp.get("id") or ""),
    )
    return {
        "message": (
            "Payment successful" if _settled else
            "Bank transfer started — it takes 3–5 business days to clear."
        ),
        "settled": _settled,
        "order_number": order.order_number,
        "charge_id": charge_resp.get("id"),
    }
