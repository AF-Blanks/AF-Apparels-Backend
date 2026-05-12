"""Public contact form endpoint."""
from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, EmailStr

router = APIRouter()


class ContactRequest(BaseModel):
    name: str
    email: EmailStr
    phone: str = ""
    company: str = ""
    department: str = "sales"
    message: str


DEPT_LABELS = {
    "sales": "Sales & Pricing",
    "support": "Order Support",
    "private-label": "Private Label",
    "shipping": "Shipping & Logistics",
    "accounting": "Billing & Accounts",
    "wholesale-sales": "Wholesale Sales",
    "account-support": "Account Support",
    "order-status": "Order Status / Shipping",
    "returns": "Returns & Exchanges",
    "other": "Other",
}

_ROW = '<tr style="border-bottom:1px solid #E2E0DA"><td style="padding:10px 0;color:#888;width:120px;vertical-align:top;font-size:13px;font-family:sans-serif">{label}</td><td style="padding:10px 0;color:#222;font-size:13px;font-family:sans-serif">{value}</td></tr>'


def _send_contact_email(data: ContactRequest) -> None:
    from app.core.config import settings
    from app.services.email_service import EmailService

    dept_label = DEPT_LABELS.get(data.department, data.department)

    if not settings.RESEND_API_KEY:
        print(f"[Contact Form] {dept_label} — {data.name} <{data.email}>: {data.message[:100]}")
        return

    import resend
    resend.api_key = settings.RESEND_API_KEY

    rows = [
        ("Name", data.name),
        ("Business", data.company or "—"),
        ("Email", f'<a href="mailto:{data.email}" style="color:#1B3A5C">{data.email}</a>'),
        ("Phone", data.phone or "—"),
        ("Department", dept_label),
        ("Message", f'<span style="white-space:pre-line">{data.message}</span>'),
    ]
    rows_html = "".join(_ROW.format(label=label, value=value) for label, value in rows)

    admin_html = EmailService._base_template(
        f'<h2 style="color:#1B3A5C;margin:0 0 20px;font-family:sans-serif">New Contact Form Submission</h2>'
        f'<table style="width:100%;border-collapse:collapse">{rows_html}</table>',
        footer_note=f'Reply directly to <a href="mailto:{data.email}" style="color:#1B3A5C">{data.email}</a>',
    )

    admin_to = getattr(settings, "ADMIN_NOTIFICATION_EMAIL", None) or settings.EMAIL_FROM_ADDRESS
    try:
        resend.Emails.send({
            "from": settings.EMAIL_FROM_ADDRESS,
            "to": admin_to,
            "reply_to": data.email,
            "subject": f"[Contact] {dept_label} — {data.name} ({data.company or '—'})",
            "html": admin_html,
        })
    except Exception as exc:
        print(f"Contact email failed: {exc}")

    confirm_html = EmailService._base_template(
        f'<h2 style="color:#1B3A5C;margin:0 0 12px;font-family:sans-serif">Thanks, {data.name}!</h2>'
        f'<p style="color:#444;font-size:14px;font-family:sans-serif">We\'ve received your message and will respond within 4 business hours (Mon–Fri, 8AM–6PM CT).</p>'
        f'<div style="background:#F4F6F9;border-radius:6px;padding:14px 16px;margin-top:16px">'
        f'<p style="color:#888;font-size:12px;margin:0 0 6px;text-transform:uppercase;letter-spacing:.06em;font-family:sans-serif">Your message</p>'
        f'<p style="color:#444;font-size:14px;margin:0;white-space:pre-line;font-family:sans-serif">{data.message}</p>'
        f'</div>',
    )
    try:
        resend.Emails.send({
            "from": settings.EMAIL_FROM_ADDRESS,
            "to": data.email,
            "subject": "We received your message — AF Apparels",
            "html": confirm_html,
        })
    except Exception as exc:
        print(f"Contact confirmation email failed: {exc}")


@router.post("/contact", status_code=202)
async def submit_contact(payload: ContactRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_send_contact_email, payload)
    return {"message": "Message received. We will get back to you within 1 business day."}
