#backend/app/services/email_service.py
"""EmailService — DB-stored Jinja2 templates + Resend delivery."""
import base64
import json
import logging
from uuid import UUID

import resend
from jinja2 import BaseLoader, Environment, TemplateError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.models.communication import EmailTemplate

logger = logging.getLogger(__name__)

_jinja_env = Environment(loader=BaseLoader(), autoescape=True)


class EmailService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_template(self, trigger_event: str) -> EmailTemplate:
        result = await self.db.execute(
            select(EmailTemplate).where(
                EmailTemplate.trigger_event == trigger_event,
                EmailTemplate.is_active.is_(True),
            )
        )
        tpl = result.scalar_one_or_none()
        if not tpl:
            raise NotFoundError(f"Email template '{trigger_event}' not found or inactive")
        return tpl

    async def get_template_by_id(self, template_id: UUID) -> EmailTemplate:
        result = await self.db.execute(
            select(EmailTemplate).where(EmailTemplate.id == template_id)
        )
        tpl = result.scalar_one_or_none()
        if not tpl:
            raise NotFoundError(f"Email template {template_id} not found")
        return tpl

    async def list_templates(self) -> list[EmailTemplate]:
        result = await self.db.execute(select(EmailTemplate).order_by(EmailTemplate.trigger_event))
        return list(result.scalars().all())

    def render_template(self, template_str: str, variables: dict) -> str:
        try:
            tpl = _jinja_env.from_string(template_str)
            return tpl.render(**variables)
        except TemplateError as exc:
            logger.error("Template render error: %s", exc)
            return template_str  # fallback — return unrendered

    async def render(self, trigger_event: str, variables: dict) -> dict:
        """Returns rendered {subject, body_html, body_text}."""
        tpl = await self.get_template(trigger_event)
        return {
            "subject": self.render_template(tpl.subject, variables),
            "body_html": self.render_template(tpl.body_html, variables),
            "body_text": self.render_template(tpl.body_text, variables) if tpl.body_text else None,
        }

    async def send(self, trigger_event: str, to_email: str, variables: dict) -> bool:
        """Render template and send via Resend. Returns True on success."""
        rendered = await self.render(trigger_event, variables)
        return self._send_via_resend(
            to_email=to_email,
            subject=rendered["subject"],
            body_html=rendered["body_html"],
            body_text=rendered.get("body_text"),
        )

    def send_raw(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        attachments: list[dict] | None = None,
    ) -> bool:
        """Send an ad-hoc email without requiring a DB template."""
        return self._send_via_resend(
            to_email=to_email,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            cc=cc,
            bcc=bcc,
            reply_to=reply_to,
            attachments=attachments,
        )

    def _send_via_resend(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        attachments: list[dict] | None = None,
    ) -> bool:
        if not settings.RESEND_API_KEY:
            logger.warning("RESEND_API_KEY not set — skipping email to %s", to_email)
            return False

        resend.api_key = settings.RESEND_API_KEY
        from_addr = f"{settings.EMAIL_FROM_NAME} <{settings.EMAIL_FROM_ADDRESS}>"

        # In dev/test: redirect all emails to admin notification address
        recipient = to_email
        if settings.APP_ENV in ("development", "test") and settings.ADMIN_NOTIFICATION_EMAIL:
            recipient = settings.ADMIN_NOTIFICATION_EMAIL

        params: resend.Emails.SendParams = {
            "from": from_addr,
            "to": [recipient],
            "subject": subject,
            "html": body_html,
        }
        if body_text:
            params["text"] = body_text
        if cc:
            params["cc"] = cc
        if bcc:
            params["bcc"] = bcc
        if reply_to:
            params["reply_to"] = [reply_to]
        if attachments:
            params["attachments"] = [
                {
                    "filename": a["filename"],
                    "content": base64.b64encode(a["content"]).decode()
                    if isinstance(a["content"], bytes)
                    else a["content"],
                }
                for a in attachments
            ]

        try:
            result = resend.Emails.send(params)
            email_id = result.get("id") if isinstance(result, dict) else getattr(result, "id", None)
            logger.info("Resend email sent to %s (id=%s)", recipient, email_id)
            return True
        except Exception as exc:
            logger.error("Resend exception for %s: %s", recipient, exc)
            return False

    # ── Branded template wrapper ───────────────────────────────────────────────

    @staticmethod
    def _base_template(content_html: str, footer_note: str = "") -> str:
        """AF Apparels navy-branded HTML email wrapper."""
        from app.core.config import settings as _cfg
        note_html = (
            f'<p style="color:#9ca3af;font-size:12px;margin:4px 0 0">{footer_note}</p>'
            if footer_note else ""
        )
        logo_url = _cfg.LOGO_URL or f"{_cfg.FRONTEND_URL}/Af-apparel%20logo.png"
        if logo_url:
            logo_html = (
                f'<img src="{logo_url}" alt="AF Apparels" '
                f'style="height:44px;width:auto;display:block;margin:0 auto" />'
            )
        else:
            logo_html = (
                '<span style="font-size:28px;font-weight:900;color:#ffffff;'
                'letter-spacing:-.5px">AF</span>'
                '<span style="color:rgba(255,255,255,.55);font-size:13px;margin-left:8px;'
                'letter-spacing:.18em;text-transform:uppercase;font-weight:600">APPARELS</span>'
            )
        return (
            '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
            'Helvetica,Arial,sans-serif;max-width:600px;margin:0 auto;background:#ffffff">'
            '<div style="background:#1B3A5C;padding:24px 32px;text-align:center;'
            'border-bottom:3px solid #E8242A">'
            + logo_html +
            '</div>'
            '<div style="padding:32px">'
            + content_html
            + '<div style="border-top:1px solid #e5e7eb;margin-top:28px;padding-top:20px">'
            '<p style="color:#9ca3af;font-size:12px;margin:0 0 4px">'
            'Questions? Call <a href="tel:4693679753" style="color:#1B3A5C;font-weight:700">'
            '+1\xa0(469)\xa0367-9753</a> or '
            '<a href="mailto:info@afblanks.com" style="color:#1B3A5C">'
            'info@afblanks.com</a></p>'
            f'{note_html}'
            '<p style="color:#9ca3af;font-size:12px;margin:4px 0 0">'
            '— AF Apparels Wholesale Team</p>'
            '</div>'
            '</div>'
            '</div>'
        )

    # ── High-level transactional senders ──────────────────────────────────────

    def send_order_confirmation(self, order: "Order", to_email: str) -> bool:  # type: ignore[name-defined]
        """Branded order confirmation with order-confirmation PDF attached."""
        from app.core.config import settings as _s
        from app.services.pdf_service import PDFService

        name = getattr(order, "guest_name", None) or "Valued Customer"
        order_url = f"{_s.FRONTEND_URL}/account/orders/{order.id}"

        rows_html = "".join(
            f'<tr>'
            f'<td style="padding:9px 12px;font-size:13px;color:#111827;border-bottom:1px solid #f3f4f6">'
            f'{item.product_name}</td>'
            f'<td style="padding:9px 12px;font-size:13px;color:#374151;border-bottom:1px solid #f3f4f6">'
            f'{item.color or "—"} / {item.size or "—"}</td>'
            f'<td style="padding:9px 12px;font-size:13px;font-weight:700;text-align:center;'
            f'border-bottom:1px solid #f3f4f6">{item.quantity}</td>'
            f'<td style="padding:9px 12px;font-size:13px;text-align:right;'
            f'border-bottom:1px solid #f3f4f6">${float(item.unit_price):.2f}</td>'
            f'<td style="padding:9px 12px;font-size:13px;font-weight:700;text-align:right;'
            f'border-bottom:1px solid #f3f4f6">${float(item.line_total):.2f}</td>'
            f'</tr>'
            for item in order.items
        )

        discount_val = float(getattr(order, "discount_amount", 0) or 0)
        discount_row = (
            f'<tr><td style="padding:4px 0;font-size:13px;color:#059669;font-weight:600">Discount</td>'
            f'<td style="padding:4px 0;font-size:13px;color:#059669;font-weight:600;text-align:right">'
            f'&#8722;${discount_val:.2f}</td></tr>'
            if discount_val > 0 else ""
        )
        tax_row = (
            f'<tr><td style="padding:4px 0;font-size:13px;color:#6b7280">Tax</td>'
            f'<td style="padding:4px 0;font-size:13px;color:#6b7280;text-align:right">'
            f'${float(order.tax_amount):.2f}</td></tr>'
            if getattr(order, "tax_amount", None) and float(order.tax_amount) > 0 else ""
        )

        cta = (
            f'<p style="margin:20px 0 0">'
            f'<a href="{order_url}" style="background:#E8242A;color:#fff;padding:12px 28px;'
            f'border-radius:6px;font-weight:700;text-decoration:none;font-size:14px;'
            f'display:inline-block">View Order →</a></p>'
            if not getattr(order, "is_guest_order", False) else ""
        )

        content_html = (
            f'<h2 style="color:#1B3A5C;font-size:22px;font-weight:800;margin:0 0 8px">'
            f'Order Received!</h2>'
            f'<p style="color:#374151;margin:0 0 24px">Hi {name}, your order has been received '
            f'and is now being processed.</p>'
            f'<div style="background:#F9F8F4;border-radius:8px;padding:16px 20px;margin-bottom:20px">'
            f'<table style="width:100%"><tr>'
            f'<td><div style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;'
            f'color:#6b7280">Order Number</div>'
            f'<div style="font-size:20px;font-weight:800;color:#1B3A5C;margin-top:2px">'
            f'{order.order_number}</div></td>'
            f'<td style="text-align:right">'
            f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.08em;'
            f'color:#6b7280">Order Total</div>'
            f'<div style="font-size:20px;font-weight:800;color:#059669;margin-top:2px">'
            f'${float(order.total):.2f}</div></td>'
            f'</tr></table></div>'
            f'<table style="width:100%;border-collapse:collapse;margin-bottom:20px;'
            f'table-layout:fixed;word-wrap:break-word">'
            f'<thead><tr style="background:#1B3A5C">'
            f'<th style="padding:8px 12px;color:#fff;text-align:left;font-size:11px;'
            f'font-weight:700;letter-spacing:.06em">PRODUCT</th>'
            f'<th style="padding:8px 12px;color:#fff;text-align:left;font-size:11px;'
            f'font-weight:700;letter-spacing:.06em">VARIANT</th>'
            f'<th style="padding:8px 12px;color:#fff;text-align:center;font-size:11px;'
            f'font-weight:700;letter-spacing:.06em">QTY</th>'
            f'<th style="padding:8px 12px;color:#fff;text-align:right;font-size:11px;'
            f'font-weight:700;letter-spacing:.06em">UNIT</th>'
            f'<th style="padding:8px 12px;color:#fff;text-align:right;font-size:11px;'
            f'font-weight:700;letter-spacing:.06em">TOTAL</th>'
            f'</tr></thead>'
            f'<tbody>{rows_html}</tbody></table>'
            f'<table style="width:100%;margin-bottom:20px">'
            f'<tr><td style="padding:4px 0;font-size:13px;color:#6b7280">Subtotal</td>'
            f'<td style="padding:4px 0;font-size:13px;color:#6b7280;text-align:right">'
            f'${float(order.subtotal):.2f}</td></tr>'
            f'{discount_row}'
            f'<tr><td style="padding:4px 0;font-size:13px;color:#6b7280">Shipping</td>'
            f'<td style="padding:4px 0;font-size:13px;color:#6b7280;text-align:right">'
            f'${float(order.shipping_cost or 0):.2f}</td></tr>'
            f'{tax_row}'
            f'<tr><td style="padding:8px 0 0;font-size:17px;font-weight:800;color:#1B3A5C;'
            f'border-top:2px solid #e5e7eb">Total</td>'
            f'<td style="padding:8px 0 0;font-size:17px;font-weight:800;color:#1B3A5C;'
            f'text-align:right;border-top:2px solid #e5e7eb">${float(order.total):.2f}</td>'
            f'</tr></table>'
            f'{cta}'
        )

        attachments = None
        try:
            pdf_bytes = PDFService().generate_order_confirmation(order)
            attachments = [{"filename": f"order-{order.order_number}.pdf", "content": pdf_bytes}]
        except Exception as _exc:
            logger.warning("PDF generation failed (order confirmation): %s", _exc)

        return self._send_via_resend(
            to_email=to_email,
            subject=f"Order Received — {order.order_number} | AF Apparels",
            body_html=self._base_template(content_html),
            attachments=attachments,
        )

    def send_invoice(
        self,
        order: "Order",  # type: ignore[name-defined]
        to_email: str,
        payment_terms: str | None = None,
        customer_name: str | None = None,
    ) -> bool:
        """Send invoice PDF email to customer with payment terms and bank details."""
        from datetime import timedelta as _td
        from app.services.pdf_service import PDFService

        terms = payment_terms or getattr(order, 'payment_terms', None) or 'net_30'
        name = customer_name or "Valued Customer"

        _terms_display = {
            'net_30': ('Net 30 — due within 30 days', 30),
            'net_15': ('Net 15 — due within 15 days', 15),
            'due_on_receipt': ('Due on Receipt', 0),
        }
        terms_label, days = _terms_display.get(terms, _terms_display['net_30'])
        from datetime import datetime as _dt
        now = _dt.now()
        due_str = (now + _td(days=days)).strftime('%B %d, %Y')
        inv_date = now.strftime('%B %d, %Y')
        order_num = getattr(order, 'order_number', 'N/A')
        total_val = float(getattr(order, 'total', 0))
        amount_paid_val = float(getattr(order, 'amount_paid', None) or 0)
        balance_due_val = max(0.0, total_val - amount_paid_val)
        pay_url = f"{settings.FRONTEND_URL}/checkout/invoice/{order_num}"
        btn_label = f'Pay Balance — ${balance_due_val:.2f}' if amount_paid_val > 0 else f'Pay Now — ${total_val:.2f}'

        if amount_paid_val > 0:
            amount_row = (
                f'<tr><td style="font-size:12px;color:#6b7280;padding:5px 0">Order Total</td>'
                f'<td style="font-size:13px;color:#374151;text-align:right">${total_val:.2f}</td></tr>'
                f'<tr><td style="font-size:12px;color:#059669;padding:5px 0">Amount Paid</td>'
                f'<td style="font-size:13px;color:#059669;font-weight:600;text-align:right">&#8722;${amount_paid_val:.2f}</td></tr>'
                f'<tr style="border-top:1px solid #e5e7eb">'
                f'<td style="font-size:12px;color:#6b7280;font-weight:700;padding:10px 0 5px">Balance Due</td>'
                f'<td style="font-size:17px;font-weight:800;color:#E8242A;text-align:right;padding-top:10px">'
                f'${balance_due_val:.2f}</td></tr>'
            )
        else:
            amount_row = (
                f'<tr><td style="font-size:12px;color:#6b7280;padding:5px 0">Amount Due</td>'
                f'<td style="font-size:17px;font-weight:800;color:#1B3A5C;text-align:right">'
                f'${total_val:.2f}</td></tr>'
            )

        content_html = (
            f'<h2 style="color:#1B3A5C;font-size:22px;font-weight:800;margin:0 0 8px">'
            f'Invoice — {order_num}</h2>'
            f'<p style="color:#374151;margin:0 0 20px">'
            f'Hi {name}, please find your invoice attached to this email.</p>'
            f'<div style="background:#F9F8F4;border-radius:8px;padding:16px 20px;margin-bottom:20px">'
            f'<table style="width:100%;border-collapse:collapse">'
            f'<tr><td style="font-size:12px;color:#6b7280;padding:5px 0">Order #</td>'
            f'<td style="font-size:14px;font-weight:800;color:#1B3A5C;text-align:right">{order_num}</td></tr>'
            f'<tr><td style="font-size:12px;color:#6b7280;padding:5px 0">Invoice Date</td>'
            f'<td style="font-size:13px;color:#374151;text-align:right">{inv_date}</td></tr>'
            f'<tr><td style="font-size:12px;color:#6b7280;padding:5px 0">Payment Terms</td>'
            f'<td style="font-size:13px;color:#374151;text-align:right">{terms_label}</td></tr>'
            f'<tr style="border-top:1px solid #e5e7eb">'
            f'<td style="font-size:12px;color:#6b7280;font-weight:700;padding:10px 0 5px">Due Date</td>'
            f'<td style="font-size:17px;font-weight:800;color:#E8242A;text-align:right;padding-top:10px">'
            f'{due_str}</td></tr>'
            f'{amount_row}'
            f'</table></div>'
            f'<div style="text-align:center;margin:28px 0;">'
            f'<p style="color:#444;font-size:14px;margin:0 0 16px;">'
            f'Click below to complete your payment securely online:</p>'
            f'<a href="{pay_url}" '
            f'style="display:inline-block;background:#E8242A;color:#fff;'
            f'padding:14px 40px;border-radius:6px;font-size:16px;'
            f'font-weight:700;text-decoration:none;letter-spacing:0.5px;">'
            f'{btn_label}</a>'
            f'<p style="color:#888;font-size:12px;margin:12px 0 0;">'
            f'Secure payment · Order {order_num}</p>'
            f'</div>'
            f'<p style="color:#6b7280;font-size:13px;margin:0">'
            f'The invoice PDF is attached. Questions? Call '
            f'<a href="tel:4693679753" style="color:#1B3A5C">+1\xa0(469)\xa0367-9753</a> or '
            f'<a href="mailto:info@afblanks.com" style="color:#1B3A5C">info@afblanks.com</a></p>'
        )

        # Temporarily set payment_terms on the order object so PDF picks it up
        _orig_terms = getattr(order, 'payment_terms', None)
        try:
            object.__setattr__(order, 'payment_terms', terms)
        except AttributeError:
            pass

        attachments = None
        try:
            pdf_bytes = PDFService().generate_invoice(order)
            attachments = [{"filename": f"invoice-{order_num}.pdf", "content": pdf_bytes}]
        except Exception as _exc:
            logger.warning("PDF generation failed (invoice): %s", _exc)
        finally:
            try:
                object.__setattr__(order, 'payment_terms', _orig_terms)
            except AttributeError:
                pass

        return self._send_via_resend(
            to_email=to_email,
            subject=f"Invoice {order_num} — Due {due_str} | AF Apparels",
            body_html=self._base_template(content_html),
            attachments=attachments,
        )

    def send_admin_new_order_alert(self, order: "Order") -> bool:  # type: ignore[name-defined]
        """Notify admin of a new order placement."""
        from app.core.config import settings as _s
        if not _s.ADMIN_NOTIFICATION_EMAIL:
            return False
        order_url = f"{_s.FRONTEND_URL}/admin/orders/{order.id}"
        is_guest = getattr(order, "is_guest_order", False)
        customer = (
            f"{order.guest_name} ({order.guest_email})"
            if is_guest
            else f"Wholesale order"
        )
        content_html = (
            f'<h2 style="color:#1B3A5C;font-size:20px;font-weight:800;margin:0 0 8px">'
            f'New Order Received</h2>'
            f'<div style="background:#F9F8F4;border-radius:8px;padding:16px 20px;margin-bottom:20px">'
            f'<table style="width:100%">'
            f'<tr><td style="font-size:12px;color:#6b7280;padding:3px 0">Order</td>'
            f'<td style="font-size:13px;font-weight:700;color:#1B3A5C;text-align:right">'
            f'{order.order_number}</td></tr>'
            f'<tr><td style="font-size:12px;color:#6b7280;padding:3px 0">Customer</td>'
            f'<td style="font-size:13px;color:#374151;text-align:right">{customer}</td></tr>'
            f'<tr><td style="font-size:12px;color:#6b7280;padding:3px 0">Total</td>'
            f'<td style="font-size:16px;font-weight:800;color:#059669;text-align:right">'
            f'${float(order.total):.2f}</td></tr>'
            f'<tr><td style="font-size:12px;color:#6b7280;padding:3px 0">Payment</td>'
            f'<td style="font-size:13px;color:#374151;text-align:right">'
            f'{getattr(order, "payment_method", "card").upper()}</td></tr>'
            f'</table></div>'
            f'<p style="margin:0"><a href="{order_url}" '
            f'style="background:#1B3A5C;color:#fff;padding:12px 24px;border-radius:6px;'
            f'font-weight:700;text-decoration:none;font-size:14px;display:inline-block">'
            f'View Order →</a></p>'
        )
        return self._send_via_resend(
            to_email=_s.ADMIN_NOTIFICATION_EMAIL,
            subject=f"New Order {order.order_number} — ${float(order.total):.2f} | AF Apparels",
            body_html=self._base_template(content_html),
        )

    def send_admin_low_stock_alert(
        self, product_name: str, sku: str, qty: int
    ) -> bool:
        """Notify admin when a SKU drops below LOW_STOCK_THRESHOLD."""
        from app.core.config import settings as _s
        if not _s.ADMIN_NOTIFICATION_EMAIL:
            return False
        content_html = (
            f'<h2 style="color:#D97706;font-size:20px;font-weight:800;margin:0 0 8px">'
            f'⚠️ Low Stock Alert</h2>'
            f'<p style="color:#374151;margin:0 0 20px">A SKU has dropped below the '
            f'threshold of {_s.LOW_STOCK_THRESHOLD} units.</p>'
            f'<div style="background:#fff8f0;border:1.5px solid #fed7aa;border-radius:8px;'
            f'padding:16px 20px;margin-bottom:20px">'
            f'<div style="font-size:16px;font-weight:800;color:#1B3A5C;margin-bottom:6px">'
            f'{product_name}</div>'
            f'<div style="font-size:13px;color:#6b7280;margin-bottom:4px">'
            f'SKU: <span style="font-family:monospace;color:#374151">{sku}</span></div>'
            f'<div style="font-size:20px;font-weight:900;color:#D97706">'
            f'{qty} units remaining</div>'
            f'</div>'
            f'<p style="margin:0"><a href="{_s.FRONTEND_URL}/admin/products" '
            f'style="background:#D97706;color:#fff;padding:12px 24px;border-radius:6px;'
            f'font-weight:700;text-decoration:none;font-size:14px;display:inline-block">'
            f'Manage Inventory →</a></p>'
        )
        return self._send_via_resend(
            to_email=_s.ADMIN_NOTIFICATION_EMAIL,
            subject=f"Low Stock: {sku} ({qty} left) | AF Apparels",
            body_html=self._base_template(content_html),
        )

    def send_application_approved(
        self, to_email: str, first_name: str, company_name: str
    ) -> bool:
        """Notify applicant their account has been approved."""
        from app.core.config import settings as _s
        content_html = (
            f'<h2 style="color:#059669;font-size:22px;font-weight:800;margin:0 0 8px">'
            f'Application Approved! ✅</h2>'
            f'<p style="color:#374151;margin:0 0 20px">Hi {first_name},</p>'
            f'<p style="color:#374151;margin:0 0 16px">'
            f'Great news — your application for <b>{company_name}</b> has been '
            f'<b style="color:#059669">approved</b>. Your account is now active and you can '
            f'start placing orders.</p>'
            f'<p style="margin:0"><a href="{_s.FRONTEND_URL}/login" '
            f'style="background:#059669;color:#fff;padding:12px 28px;border-radius:6px;'
            f'font-weight:700;text-decoration:none;font-size:14px;display:inline-block">'
            f'Log In to Your Account →</a></p>'
        )
        return self._send_via_resend(
            to_email=to_email,
            subject="Your AF Apparels Account is Approved!",
            body_html=self._base_template(content_html),
        )

    def send_application_rejected(
        self, to_email: str, first_name: str, company_name: str
    ) -> bool:
        """Notify applicant their wholesale application was not approved."""
        content_html = (
            f'<h2 style="color:#374151;font-size:22px;font-weight:800;margin:0 0 8px">'
            f'Application Update</h2>'
            f'<p style="color:#374151;margin:0 0 16px">Hi {first_name},</p>'
            f'<p style="color:#374151;margin:0 0 16px">'
            f'Thank you for applying to the AF Apparels wholesale program. After reviewing your '
            f'application for <b>{company_name}</b>, we are unable to approve it at this time.</p>'
            f'<p style="color:#374151;margin:0 0 16px">'
            f'If you believe this is an error or would like more information, please contact us '
            f'at <a href="tel:2142727213" style="color:#1B3A5C">(214)\xa0272-7213</a> and we '
            f'will be happy to assist.</p>'
        )
        return self._send_via_resend(
            to_email=to_email,
            subject="AF Apparels Wholesale Application Update",
            body_html=self._base_template(content_html),
        )

    def send_retail_account_activation(
        self,
        customer_email: str,
        first_name: str,
        activation_url: str,
        order_number: str | None = None,
    ) -> bool:
        """Send retail account activation email after guest checkout."""
        order_line = (
            f'<p style="color:#374151;margin:0 0 16px">'
            f'Your order <strong style="color:#1B3A5C">{order_number}</strong> '
            f'has been placed and is being processed.</p>'
        ) if order_number else ""

        content_html = (
            f'<h2 style="color:#1B3A5C;font-size:22px;font-weight:800;margin:0 0 8px">'
            f'Welcome to AF Apparels, {first_name}!</h2>'
            f'{order_line}'
            f'<p style="color:#374151;margin:0 0 16px">'
            f'Create a password to access your order history, view invoices, '
            f'and manage your account anytime.</p>'
            f'<p style="color:#6b7280;font-size:13px;margin:0 0 24px">'
            f'This link expires in 7 days.</p>'
            f'<p style="margin:0">'
            f'<a href="{activation_url}" style="background:#E8242A;color:#fff;'
            f'padding:12px 28px;border-radius:6px;font-weight:700;'
            f'text-decoration:none;font-size:14px;display:inline-block">'
            f'Activate My Account →</a></p>'
        )
        return self._send_via_resend(
            to_email=customer_email,
            subject="Activate Your AF Apparels Account",
            body_html=self._base_template(content_html),
        )

    @staticmethod
    def get_available_variables(tpl: EmailTemplate) -> list[str]:
        if not tpl.available_variables:
            return []
        try:
            return json.loads(tpl.available_variables)
        except (ValueError, TypeError):
            return []
