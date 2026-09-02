"""Admin — QuickBooks sync dashboard endpoints.

T195: GET /admin/quickbooks/status, POST /admin/quickbooks/retry/{log_id}
      GET /admin/quickbooks/connect, GET /admin/quickbooks/callback
"""
import base64
import logging
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

logger = logging.getLogger(__name__)


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

    # Which QuickBooks company we are pointed at, and whether the references we
    # hold were made there. Nobody should be asked to press "Switch to Connected
    # Company" — which clears every customer reference — without first being able
    # to read back, in words, which company they just connected to.
    from sqlalchemy import text as _t

    _realms = dict((await db.execute(_t(
        "SELECT key, value FROM settings WHERE key IN ('qb_realm_id', 'qb_ids_realm')"
    ))).all())
    connected_realm = _realms.get("qb_realm_id")
    ids_realm = _realms.get("qb_ids_realm")

    company_name: str | None = None
    if connected_realm:
        # The realm id is a long number that means nothing to a person reading it.
        # Ask QuickBooks what the company is actually called. Failing is fine —
        # the id is still shown, and this must never take the page down.
        try:
            import asyncio as _asyncio

            from app.services.quickbooks_service import QuickBooksService

            _info = await _asyncio.to_thread(
                QuickBooksService().query, "SELECT CompanyName FROM CompanyInfo"
            )
            _rows = (_info or {}).get("QueryResponse", {}).get("CompanyInfo", [])
            if _rows:
                company_name = _rows[0].get("CompanyName")
        except Exception as exc:  # noqa: BLE001 — informational only
            logger.warning("Could not read the QuickBooks company name: %s", exc)

    return {
        "last_sync_at": last_sync_at,
        "synced_today": synced_today,
        "connected": bool(connected_realm),
        "connected_realm": connected_realm,
        "company_name": company_name,
        "ids_realm": ids_realm,
        # True while we are connected to one company holding another's references:
        # syncing is paused on purpose until someone adopts the new company.
        "needs_switch": bool(connected_realm and ids_realm and connected_realm != ids_realm),
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


@router.post("/quickbooks/purge-queue")
async def purge_celery_queue(_: None = Depends(require_admin)):
    """Purge ALL pending Celery tasks from Redis (queues + scheduled retries).

    Call this BEFORE connecting a new Intuit app to prevent backed-up tasks
    from consuming the new app's monthly CorePlus call quota.
    """
    import redis as _redis
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    r = _redis.from_url(redis_url, decode_responses=True)
    deleted_keys: list[str] = []
    total_items = 0

    # Regular task queues (Redis lists)
    for queue in ("celery", "default", "email"):
        length = r.llen(queue)
        if length:
            r.delete(queue)
            deleted_keys.append(f"{queue}(list:{length})")
            total_items += length

    # Scheduled/ETA retry tasks — stored in kombu sorted sets
    # These are countdown tasks that haven't fired yet (e.g. "Retry in 960s")
    for pattern in ("_kombu*", "*kombu*"):
        for key in r.scan_iter(pattern, count=100):
            key_type = r.type(key)
            count = r.zcard(key) if key_type == "zset" else (
                r.llen(key) if key_type == "list" else 1
            )
            r.delete(key)
            deleted_keys.append(f"{key}({key_type}:{count})")
            total_items += count

    return {"purged": True, "total_deleted": total_items, "keys": deleted_keys}


@router.get("/quickbooks/connect")
async def quickbooks_connect():
    """Redirect to Intuit OAuth2 authorization page."""
    client_id = os.getenv("QB_CLIENT_ID", "")
    redirect_uri = os.getenv("QB_REDIRECT_URI", "")
    if not client_id or not redirect_uri:
        raise HTTPException(status_code=500, detail="QB_CLIENT_ID or QB_REDIRECT_URI not configured")

    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "com.intuit.quickbooks.accounting com.intuit.quickbooks.payment",
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
    frontend_url = os.getenv("FRONTEND_URL", "https://afblanks.com")

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

    # The settings page is where the connection is managed and where the
    # company being adopted is confirmed — sending someone to /admin/quickbooks
    # landed them on a 404 right after a successful connect.
    return RedirectResponse(url=f"{frontend_url}/admin/settings/quickbooks?connected=true")


@router.post("/quickbooks/adopt-company", response_model=dict)
async def adopt_quickbooks_company(
    payload: dict,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Accept the QuickBooks company we are now connected to, and forget the old one.

    A customer id, an item id, an invoice id — each is a number that means
    something only inside the company it was created in. Connect the app to a
    different company and those numbers refer to whatever happens to hold them
    there, which is how an invoice ends up billed to a stranger. So syncing
    refuses to run while the two disagree, and this is the deliberate act that
    resolves it.

    What it clears is only what would be read back and reused: the customer and
    item references. Invoices and payments already raised keep their numbers —
    they are a record of what was done in the old company, and they are not
    going to be sent anywhere again.

    Pass confirm=true. Nothing happens without it.
    """
    from sqlalchemy import text as _t

    if not payload.get("confirm"):
        raise HTTPException(
            status_code=422,
            detail="Pass confirm=true — this clears the QuickBooks references on every customer.",
        )

    connected = (await db.execute(
        _t("SELECT value FROM settings WHERE key = 'qb_realm_id'")
    )).scalar_one_or_none()
    if not connected:
        raise HTTPException(status_code=422, detail="Not connected to QuickBooks yet.")

    previous = (await db.execute(
        _t("SELECT value FROM settings WHERE key = 'qb_ids_realm'")
    )).scalar_one_or_none()
    if previous == connected:
        return {
            "message": "Already set up for this QuickBooks company — nothing to do.",
            "company": connected,
            "cleared": {"customers": 0, "variants": 0},
        }

    customers = (await db.execute(_t(
        "UPDATE companies SET qb_customer_id = NULL WHERE qb_customer_id IS NOT NULL"
    ))).rowcount or 0
    variants = (await db.execute(_t(
        "UPDATE product_variants SET qb_item_id = NULL WHERE qb_item_id IS NOT NULL"
    ))).rowcount or 0
    # Every invoice raised so far belongs to the company we are leaving. Say so
    # on the order itself, because clearing the invoice id is not an option —
    # it is the only record of where that invoice went — and without the stamp
    # a payment settling later would post the same number into the new books,
    # against whatever invoice happens to hold it there.
    stamped = (await db.execute(_t(
        "UPDATE orders SET qb_realm_id = :prev "
        "WHERE qb_invoice_id IS NOT NULL AND qb_realm_id IS NULL"
    ), {"prev": previous or "unknown"})).rowcount or 0
    await db.execute(_t(
        "INSERT INTO settings (key, value) VALUES ('qb_ids_realm', :v) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
    ), {"v": connected})
    await db.commit()

    logger.warning(
        "QuickBooks company adopted: %s (was %s) — cleared %d customer and %d item "
        "references; %d existing orders stamped as belonging to the previous company",
        connected, previous or "none", customers, variants, stamped,
    )
    return {
        "message": (
            f"Now set up for QuickBooks company {connected}. "
            f"{customers} customer and {variants} item references from the previous "
            "company were cleared; customers will be created fresh as orders come in. "
            f"Invoices already raised were left untouched — {stamped} orders are marked "
            "as belonging to the previous company and will not be synced again."
        ),
        "company": connected,
        "previous_company": previous,
        "cleared": {"customers": customers, "variants": variants},
        "kept_with_previous_company": stamped,
    }
