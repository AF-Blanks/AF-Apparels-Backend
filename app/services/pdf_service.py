"""PDFService — ReportLab document generation for orders."""
from __future__ import annotations

import io
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

if TYPE_CHECKING:
    from app.models.order import Order

# ── Brand colours ─────────────────────────────────────────────────────────────
BRAND_BLUE = colors.HexColor("#1d4ed8")
LIGHT_GRAY = colors.HexColor("#f3f4f6")
MID_GRAY = colors.HexColor("#6b7280")
DARK_GRAY = colors.HexColor("#111827")
WHITE = colors.white

_styles = getSampleStyleSheet()

_h1 = ParagraphStyle(
    "H1",
    parent=_styles["Normal"],
    fontSize=18,
    textColor=DARK_GRAY,
    fontName="Helvetica-Bold",
    spaceAfter=4,
)
_h2 = ParagraphStyle(
    "H2",
    parent=_styles["Normal"],
    fontSize=11,
    textColor=BRAND_BLUE,
    fontName="Helvetica-Bold",
    spaceBefore=12,
    spaceAfter=4,
)
_body = ParagraphStyle(
    "Body",
    parent=_styles["Normal"],
    fontSize=9,
    textColor=DARK_GRAY,
    fontName="Helvetica",
    leading=14,
)
_small = ParagraphStyle(
    "Small",
    parent=_styles["Normal"],
    fontSize=8,
    textColor=MID_GRAY,
    fontName="Helvetica",
    leading=12,
)
_label = ParagraphStyle(
    "Label",
    parent=_styles["Normal"],
    fontSize=8,
    textColor=MID_GRAY,
    fontName="Helvetica",
)


def _doc(buf: io.BytesIO) -> SimpleDocTemplate:
    return SimpleDocTemplate(
        buf,
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )


def _header(doc_title: str) -> list:
    return [
        Paragraph("AF Apparels", _h1),
        Paragraph("Wholesale Division · afapparels.com", _small),
        Spacer(1, 6),
        HRFlowable(width="100%", thickness=2, color=BRAND_BLUE),
        Spacer(1, 4),
        Paragraph(doc_title, _h2),
        Spacer(1, 8),
    ]


def _order_meta(order: "Order", extra_rows: list[tuple[str, str]] | None = None) -> list:
    rows = [
        ["Order #", order.order_number],
        ["Date", order.created_at.strftime("%B %d, %Y") if order.created_at else "—"],
        ["Status", order.status.capitalize()],
        ["Payment", order.payment_status.capitalize()],
    ]
    if order.po_number:
        rows.append(["PO Number", order.po_number])
    if extra_rows:
        rows.extend(extra_rows)

    tbl = Table(rows, colWidths=[1.2 * inch, 3 * inch])
    tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), MID_GRAY),
        ("TEXTCOLOR", (1, 0), (1, -1), DARK_GRAY),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, LIGHT_GRAY]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return [tbl, Spacer(1, 12)]


def _address_block(order: "Order") -> list:
    """Render shipping address from snapshot or placeholder."""
    import json as _json
    elements: list = [Paragraph("Ship To", _h2)]
    addr = None
    if order.shipping_address_snapshot:
        try:
            addr = _json.loads(order.shipping_address_snapshot)
        except Exception:
            addr = None

    if addr:
        lines = [
            addr.get("full_name") or "",
            addr.get("line1") or addr.get("address_line1") or "",
            addr.get("line2") or addr.get("address_line2") or "",
            f"{addr.get('city', '')}, {addr.get('state', '')} {addr.get('postal_code', '')}",
            addr.get("country", "US"),
        ]
        for ln in lines:
            if ln and ln.strip().strip(","):
                elements.append(Paragraph(ln.strip(), _body))
    else:
        elements.append(Paragraph("Address on file", _body))

    elements.append(Spacer(1, 12))
    return elements


def _items_table(order: "Order") -> list:
    """Build line-items table."""
    header_row = ["SKU", "Product", "Color", "Size", "Qty", "Unit Price", "Total"]
    data = [header_row]
    for item in order.items:
        data.append([
            item.sku,
            item.product_name,
            item.color or "—",
            item.size or "—",
            str(item.quantity),
            f"${float(item.unit_price):.2f}",
            f"${float(item.line_total):.2f}",
        ])

    col_widths = [1.0 * inch, 2.2 * inch, 0.8 * inch, 0.6 * inch,
                  0.5 * inch, 0.85 * inch, 0.85 * inch]
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        # Header row
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_BLUE),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        # Body rows
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 9),
        ("TEXTCOLOR", (0, 1), (-1, -1), DARK_GRAY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
        # Alignment
        ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
        # Padding
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        # Border
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, BRAND_BLUE),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, MID_GRAY),
    ]))
    return [tbl, Spacer(1, 10)]


def _totals_block(order: "Order") -> list:
    """Right-aligned subtotal / shipping / total block."""
    rows = [
        ["", "Subtotal:", f"${float(order.subtotal):.2f}"],
        ["", "Shipping:", f"${float(order.shipping_cost):.2f}"],
        ["", "TOTAL:", f"${float(order.total):.2f}"],
    ]
    tbl = Table(rows, colWidths=[4.85 * inch, 1.3 * inch, 0.85 * inch])
    tbl.setStyle(TableStyle([
        ("FONTNAME", (1, 0), (1, -2), "Helvetica"),
        ("FONTNAME", (1, -1), (2, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (1, 0), (1, -2), MID_GRAY),
        ("TEXTCOLOR", (1, -1), (2, -1), DARK_GRAY),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LINEABOVE", (1, -1), (2, -1), 1, DARK_GRAY),
    ]))
    return [tbl, Spacer(1, 20)]


def _footer(note: str = "") -> list:
    elements: list = [
        HRFlowable(width="100%", thickness=0.5, color=MID_GRAY),
        Spacer(1, 4),
    ]
    if note:
        elements.append(Paragraph(note, _small))
    elements.append(
        Paragraph(
            f"Generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC · AF Apparels Wholesale",
            _small,
        )
    )
    return elements


class PDFService:
    """Generate PDF documents for orders using ReportLab."""

    def generate_order_confirmation(self, order: "Order") -> bytes:
        buf = io.BytesIO()
        doc = _doc(buf)
        story = (
            _header("Order Confirmation")
            + _order_meta(order)
            + _address_block(order)
            + _items_table(order)
            + _totals_block(order)
            + _footer("Thank you for your order. You will receive a shipping notification once your order ships.")
        )
        doc.build(story)
        return buf.getvalue()

    def generate_invoice(self, order: "Order") -> bytes:
        import json as _json

        buf = io.BytesIO()
        doc = _doc(buf)

        now = order.created_at if order.created_at else datetime.utcnow()
        year = now.year
        if order.qb_invoice_id:
            inv_num = f"INV-{year}-{order.qb_invoice_id}"
        else:
            suffix = order.order_number.rsplit("-", 1)[-1] if order.order_number else "0001"
            inv_num = f"INV-{year}-{suffix}"

        invoice_date = now.strftime("%B %d, %Y")
        due_date = (now + timedelta(days=30)).strftime("%B %d, %Y")

        extra = [
            ["Invoice #", inv_num],
            ["Invoice Date", invoice_date],
            ["Due Date", due_date],
        ]
        if order.po_number:
            extra.append(["PO Number", order.po_number])

        # ── Bill To block ──────────────────────────────────────────────────────
        bill_to: list = [Paragraph("Bill To", _h2)]
        addr = None
        if order.shipping_address_snapshot:
            try:
                addr = _json.loads(order.shipping_address_snapshot)
            except Exception:
                addr = None
        if addr:
            lines = [
                addr.get("full_name") or "",
                addr.get("line1") or addr.get("address_line1") or "",
                addr.get("line2") or addr.get("address_line2") or "",
                ", ".join(filter(None, [
                    addr.get("city", ""), addr.get("state", ""),
                    addr.get("postal_code", ""),
                ])),
                addr.get("country", "US"),
            ]
            for ln in lines:
                if ln and ln.strip().strip(","):
                    bill_to.append(Paragraph(ln.strip(), _body))
        else:
            bill_to.append(Paragraph("Billing address on file", _body))
        bill_to.append(Spacer(1, 12))

        # ── Items table (Style # column) ───────────────────────────────────────
        header_row = ["Style #", "Product Name", "Color", "Size", "Qty", "Unit Price", "Total"]
        data = [header_row]
        for item in order.items:
            data.append([
                item.sku,
                item.product_name,
                item.color or "—",
                item.size or "—",
                str(item.quantity),
                f"${float(item.unit_price):.2f}",
                f"${float(item.line_total):.2f}",
            ])

        col_widths = [1.0 * inch, 2.2 * inch, 0.8 * inch, 0.6 * inch,
                      0.5 * inch, 0.85 * inch, 0.85 * inch]
        items_tbl = Table(data, colWidths=col_widths, repeatRows=1)
        items_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 1), (-1, -1), 9),
            ("TEXTCOLOR", (0, 1), (-1, -1), DARK_GRAY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
            ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, BRAND_BLUE),
            ("LINEBELOW", (0, -1), (-1, -1), 0.5, MID_GRAY),
        ]))

        # ── Summary block with discount + tax ─────────────────────────────────
        subtotal_val = float(order.subtotal)
        shipping_val = float(order.shipping_cost)
        tax_val = float(order.tax_amount) if order.tax_amount else 0.0
        discount_val = float(getattr(order, "discount_amount", 0) or 0)
        total_val = float(order.total)

        summary_rows: list = [["", "Subtotal:", f"${subtotal_val:.2f}"]]
        if discount_val > 0:
            summary_rows.append(["", "Discount:", f"−${discount_val:.2f}"])
        summary_rows.append(["", "Shipping:", f"${shipping_val:.2f}"])
        if tax_val > 0:
            summary_rows.append(["", "Tax:", f"${tax_val:.2f}"])
        summary_rows.append(["", "TOTAL DUE:", f"${total_val:.2f}"])

        sum_tbl = Table(summary_rows, colWidths=[4.85 * inch, 1.3 * inch, 0.85 * inch])
        sum_tbl.setStyle(TableStyle([
            ("FONTNAME", (1, 0), (1, -2), "Helvetica"),
            ("FONTNAME", (1, -1), (2, -1), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -2), 9),
            ("FONTSIZE", (1, -1), (2, -1), 10),
            ("TEXTCOLOR", (1, 0), (1, -2), MID_GRAY),
            ("TEXTCOLOR", (1, -1), (2, -1), DARK_GRAY),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEABOVE", (1, -1), (2, -1), 1, DARK_GRAY),
        ]))

        # ── Payment terms ──────────────────────────────────────────────────────
        terms: list = [
            Paragraph("Payment Terms", _h2),
            Paragraph(
                "Payment due within <b>30 days</b> of invoice date (Net&nbsp;30). "
                "Please remit payment referencing your order number and invoice number. "
                "Late payments may be subject to a 1.5%/month finance charge.",
                _body,
            ),
            Spacer(1, 20),
        ]

        story = (
            _header("Invoice")
            + _order_meta(order, extra_rows=extra)
            + bill_to
            + [items_tbl, Spacer(1, 10)]
            + [sum_tbl, Spacer(1, 20)]
            + terms
            + _footer(f"Invoice {inv_num} · Due {due_date} · AF Apparels Wholesale Division")
        )
        doc.build(story)
        return buf.getvalue()

    def generate_ship_confirmation(self, order: "Order") -> bytes:
        buf = io.BytesIO()
        doc = _doc(buf)
        extra = []
        if order.tracking_number:
            extra.append(["Tracking #", order.tracking_number])
        if order.carrier:
            extra.append(["Carrier", order.carrier])
        story = (
            _header("Shipping Confirmation")
            + _order_meta(order, extra_rows=extra or None)
            + _address_block(order)
            + _items_table(order)
            + _footer("Your order has shipped. Use the tracking number above to monitor delivery.")
        )
        doc.build(story)
        return buf.getvalue()

    def generate_pack_slip(self, order: "Order") -> bytes:
        buf = io.BytesIO()
        doc = _doc(buf)
        # Pack slip: no pricing, just quantities
        header_row = ["SKU", "Product", "Color", "Size", "Qty Ordered", "Qty Packed"]
        data = [header_row]
        for item in order.items:
            data.append([
                item.sku,
                item.product_name,
                item.color or "—",
                item.size or "—",
                str(item.quantity),
                "______",
            ])

        col_widths = [1.0 * inch, 2.2 * inch, 0.85 * inch, 0.65 * inch, 0.9 * inch, 0.9 * inch]
        tbl = Table(data, colWidths=col_widths, repeatRows=1)
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_BLUE),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 9),
            ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE", (0, 1), (-1, -1), 9),
            ("TEXTCOLOR", (0, 1), (-1, -1), DARK_GRAY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
            ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, BRAND_BLUE),
            ("LINEBELOW", (0, -1), (-1, -1), 0.5, MID_GRAY),
        ]))

        story = (
            _header("Packing Slip")
            + _order_meta(order)
            + _address_block(order)
            + [tbl, Spacer(1, 10)]
            + _footer("Please verify quantities and sign. Return this slip with any discrepancies.")
        )
        doc.build(story)
        return buf.getvalue()
