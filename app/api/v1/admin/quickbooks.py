"""Admin — QuickBooks sync dashboard endpoints.

T195: GET /admin/quickbooks/status, POST /admin/quickbooks/retry/{log_id}
      GET /admin/quickbooks/connect, GET /admin/quickbooks/callback
"""
import base64
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.middleware.auth_middleware import require_admin
from app.models.system import QBSyncLog

router = APIRouter(prefix="/admin", tags=["Admin — QuickBooks"])


@router.get("/quickbooks/status")
async def quickbooks_status(
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return QB sync dashboard data: last sync, today's count, failed entries."""
    from datetime import date, datetime, timezone

    today_start = datetime.combine(date.today(), datetime.min.time()).replace(tzinfo=timezone.utc)

    # Last successful sync
    last_success_q = (
        select(QBSyncLog)
        .where(QBSyncLog.status == "success")
        .order_by(QBSyncLog.updated_at.desc())
        .limit(1)
    )
    last_log = (await db.execute(last_success_q)).scalar_one_or_none()
    last_sync_at = last_log.updated_at.isoformat() if last_log else None

    # Synced today
    synced_today_q = (
        select(func.count(QBSyncLog.id))
        .where(QBSyncLog.status == "success")
        .where(QBSyncLog.updated_at >= today_start)
    )
    synced_today = (await db.execute(synced_today_q)).scalar_one() or 0

    # Failed syncs (most recent 50)
    failed_q = (
        select(QBSyncLog)
        .where(QBSyncLog.status == "failed")
        .order_by(QBSyncLog.updated_at.desc())
        .limit(50)
    )
    failed_logs = (await db.execute(failed_q)).scalars().all()

    return {
        "last_sync_at": last_sync_at,
        "synced_today": synced_today,
        "failed_syncs": [
            {
                "id": str(log.id),
                "entity_type": log.entity_type,
                "entity_id": str(log.entity_id),
                "attempt_count": log.attempt_count,
                "error_message": log.error_message,
                "updated_at": log.updated_at.isoformat() if log.updated_at else None,
            }
            for log in failed_logs
        ],
    }


@router.post("/quickbooks/retry/{log_id}")
async def retry_qb_sync(
    log_id: UUID,
    _: None = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger a QB sync retry for a failed log entry."""
    result = await db.execute(select(QBSyncLog).where(QBSyncLog.id == log_id))
    log = result.scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="Sync log entry not found")

    entity_id = str(log.entity_id)

    if log.entity_type == "company":
        from app.tasks.quickbooks_tasks import sync_customer_to_qb
        task = sync_customer_to_qb.delay(entity_id)
    elif log.entity_type == "order":
        from app.tasks.quickbooks_tasks import sync_order_invoice_to_qb
        task = sync_order_invoice_to_qb.delay(entity_id)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown entity type: {log.entity_type}")

    # Reset status to retry
    log.status = "retry"
    log.error_message = None
    await db.commit()

    return {"status": "queued", "task_id": task.id, "entity_type": log.entity_type, "entity_id": entity_id}


@router.get("/quickbooks/connect")
async def quickbooks_connect(_: None = Depends(require_admin)):
    """Return the Intuit OAuth2 authorization URL for the admin to open."""
    client_id = os.getenv("QB_CLIENT_ID", "")
    redirect_uri = os.getenv("QB_REDIRECT_URI", "")
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=500, detail="QB_CLIENT_ID or QB_REDIRECT_URI not configured")

    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "com.intuit.quickbooks.accounting",
        "redirect_uri": redirect_uri,
        "state": "afapparels_qb_auth",
    }
    auth_url = "https://appcenter.intuit.com/connect/oauth2?" + urlencode(params)
    return RedirectResponse(url=auth_url)


@router.get("/quickbooks/callback")
async def quickbooks_callback(
    code: str,
    realmId: str,
    db: AsyncSession = Depends(get_db),
):
    """Handle Intuit OAuth2 callback — exchange code for tokens and persist them."""
    client_id = os.getenv("QB_CLIENT_ID", "")
    client_secret = os.getenv("QB_CLIENT_SECRET", "")
    redirect_uri = os.getenv("QB_REDIRECT_URI", "")
    frontend_url = os.getenv("FRONTEND_URL", "https://af-apparels.vercel.app")

    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(status_code=500, detail="QuickBooks OAuth env vars not configured")

    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )

    if response.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to exchange QB code for tokens: {response.text}",
        )

    tokens = response.json()
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=tokens.get("expires_in", 3600))
    ).isoformat()

    # Upsert all four QB settings into app_settings
    await db.execute(text("""
        INSERT INTO app_settings (key, value, updated_at)
        VALUES
            ('qb_access_token',    :access_token,  now()),
            ('qb_refresh_token',   :refresh_token, now()),
            ('qb_realm_id',        :realm_id,      now()),
            ('qb_token_expires_at',:expires_at,    now())
        ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value, updated_at = now()
    """), {
        "access_token":  tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "realm_id":      realmId,
        "expires_at":    expires_at,
    })
    await db.commit()

    return RedirectResponse(url=f"{frontend_url}/admin/quickbooks?connected=true")
