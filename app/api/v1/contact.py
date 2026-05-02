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
    "other": "Other",
}


def _send_contact_email(data: ContactRequest) -> None:
    from app.core.config import settings

    if not settings.RESEND_API_KEY:
        print(f"[Contact Form] {data.department} — {data.name} <{data.email}>: {data.message[:100]}")
        return

    import resend
    resend.api_key = settings.RESEND_API_KEY

    dept_label = DEPT_LABELS.get(data.department, data.department)
    phone_line = f"<p><strong>Phone:</strong> {data.phone}</p>" if data.phone else ""
    company_line = f"<p><strong>Company:</strong> {data.company}</p>" if data.company else ""

    html = f"""
    <h2>New Contact Form Submission</h2>
    <p><strong>Name:</strong> {data.name}</p>
    <p><strong>Email:</strong> {data.email}</p>
    {phone_line}
    {company_line}
    <p><strong>Department:</strong> {dept_label}</p>
    <hr />
    <h3>Message:</h3>
    <p style="white-space: pre-line">{data.message}</p>
    """

    try:
        resend.Emails.send({
            "from": settings.EMAIL_FROM_ADDRESS,
            "to": "info@afblanks.com",
            "reply_to": data.email,
            "subject": f"[Contact] {dept_label} — {data.name}",
            "html": html,
        })
    except Exception as exc:
        print(f"Contact email failed: {exc}")


@router.post("/contact", status_code=202)
async def submit_contact(payload: ContactRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_send_contact_email, payload)
    return {"message": "Message received. We will get back to you within 1 business day."}
