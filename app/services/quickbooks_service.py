"""QuickBooks Online integration service.

Provides create_customer, create_invoice, token refresh, and rate limiting.
Uses the intuitlib + quickbooks-python SDK pattern via raw requests for
maximum control over token management.

Token priority: app_settings DB table (set via OAuth callback) > env vars.
Tokens are saved back to app_settings after every successful refresh.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


# ── DB token helpers (async, called via _run_sync_safe) ──────────────────────

async def _load_tokens_from_db() -> dict[str, str]:
    """Read QB token fields from app_settings. Returns {} if table is empty."""
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import text
    try:
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                text("SELECT key, value FROM app_settings "
                     "WHERE key IN ('qb_access_token','qb_refresh_token','qb_realm_id','qb_token_expires_at')")
            )).fetchall()
            return {r.key: r.value for r in rows if r.value}
    except Exception:
        return {}


async def _save_tokens_to_db(
    access_token: str,
    refresh_token: str,
    expires_at_iso: str,
    realm_id: str | None = None,
) -> None:
    """Upsert QB tokens into app_settings."""
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import text
    pairs = [
        ("qb_access_token",    access_token),
        ("qb_refresh_token",   refresh_token),
        ("qb_token_expires_at", expires_at_iso),
    ]
    if realm_id:
        pairs.append(("qb_realm_id", realm_id))
    try:
        async with AsyncSessionLocal() as session:
            for key, value in pairs:
                await session.execute(text("""
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (:k, :v, now())
                    ON CONFLICT (key) DO UPDATE
                        SET value = EXCLUDED.value, updated_at = now()
                """), {"k": key, "v": value})
            await session.commit()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("QB token save to DB failed: %s", exc)


def _run_sync_safe(coro) -> Any:
    """Run an async coroutine from synchronous code safely."""
    try:
        loop = asyncio.get_event_loop()
        if not loop.is_running():
            return loop.run_until_complete(coro)
    except RuntimeError:
        pass
    return asyncio.run(coro)


class _TokenBucket:
    """Simple thread-safe token bucket for rate limiting (400 req/min)."""

    def __init__(self, capacity: int = 400, refill_rate: float = 400 / 60):
        self._capacity = capacity
        self._tokens = float(capacity)
        self._refill_rate = refill_rate  # tokens per second
        self._last_refill = time.monotonic()
        self._lock = Lock()

    def consume(self, tokens: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
            self._last_refill = now
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def wait(self, tokens: int = 1, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.consume(tokens):
                return
            time.sleep(0.05)
        raise TimeoutError("QB rate limit: could not acquire token in time")


_rate_limiter = _TokenBucket()

QB_BASE_URL = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


class QuickBooksService:
    """Token-aware QB service. Call `await svc.initialize()` after construction
    to load live tokens from app_settings DB; falls back to env vars.
    Saves updated tokens back to DB after every successful refresh.
    """

    def __init__(self):
        # Synchronous defaults from env vars — no async work here
        self._access_token: str  = settings.QB_ACCESS_TOKEN
        self._refresh_token: str = settings.QB_REFRESH_TOKEN
        self._company_id: str    = settings.QB_COMPANY_ID
        self._base_url: str      = QB_BASE_URL[settings.QB_ENVIRONMENT]
        self._token_expiry: datetime | None = None
        self._account_id_cache: dict[str, str] = {}

    async def initialize(self) -> "QuickBooksService":
        """Load live tokens from app_settings DB. Await this before first API use."""
        db = await _load_tokens_from_db()
        if db.get("qb_access_token"):
            self._access_token  = db["qb_access_token"]
        if db.get("qb_refresh_token"):
            self._refresh_token = db["qb_refresh_token"]
        if db.get("qb_realm_id"):
            self._company_id    = db["qb_realm_id"]
        expires_str = db.get("qb_token_expires_at")
        if expires_str:
            try:
                dt = datetime.fromisoformat(expires_str)
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                self._token_expiry = dt
            except ValueError:
                pass
        return self

    # ── Token management ──────────────────────────────────────────────────────

    def refresh_token_if_expired(self) -> bool:
        """Exchange the refresh token for a new access token.

        Saves updated tokens to app_settings DB so they survive restarts.
        Returns True on success; logs a warning and returns False on failure
        so the caller can still attempt the request with the current token.
        """
        import logging
        _log = logging.getLogger(__name__)

        refresh_token = self._refresh_token or settings.QB_REFRESH_TOKEN
        client_id     = settings.QB_CLIENT_ID
        client_secret = settings.QB_CLIENT_SECRET

        if not client_id or not refresh_token:
            _log.warning("QB token refresh skipped — QB_CLIENT_ID or refresh_token not set")
            return False

        try:
            with httpx.Client(transport=httpx.HTTPTransport(retries=3)) as client:
                resp = client.post(
                    TOKEN_URL,
                    auth=(client_id, client_secret),
                    data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                    timeout=10,
                )
            resp.raise_for_status()
            data = resp.json()

            new_access  = data["access_token"]
            new_refresh = data.get("refresh_token", refresh_token)
            expires_in  = data.get("expires_in", 3600)
            # Subtract 5 min so we refresh before the window actually closes
            expiry_dt   = datetime.utcnow() + timedelta(seconds=expires_in - 300)
            expires_iso = (expiry_dt.replace(tzinfo=timezone.utc)).isoformat()

            # Update in-memory state
            self._access_token  = new_access
            self._refresh_token = new_refresh
            self._token_expiry  = expiry_dt

            # Persist to DB so other workers and restarts pick up the new tokens
            _run_sync_safe(_save_tokens_to_db(new_access, new_refresh, expires_iso))
            _log.info("QB access token refreshed; expires ~%s", expiry_dt.strftime("%Y-%m-%dT%H:%M"))
            return True

        except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
            _log.warning("QB token refresh skipped (network): %s — using existing token", exc)
            return False
        except Exception as exc:
            _log.warning("QB token refresh failed: %s — using existing token", exc)
            return False

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing if needed."""
        if self._needs_refresh():
            self.refresh_token_if_expired()
        return self._access_token

    def _needs_refresh(self) -> bool:
        if self._token_expiry is None:
            # Expiry unknown — trust the token if present; 401 will trigger a retry
            return not bool(self._access_token)
        # Refresh when within 5 minutes of expiry (expiry already has 5-min buffer baked in)
        return datetime.utcnow() >= self._token_expiry

    def _headers(self) -> dict[str, str]:
        if self._needs_refresh():
            self.refresh_token_if_expired()
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._base_url}/v3/company/{self._company_id}/{path}"

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        _rate_limiter.wait()
        url = self._url(path)
        with httpx.Client(transport=httpx.HTTPTransport(retries=3)) as client:
            resp = client.request(method, url, headers=self._headers(), timeout=15, **kwargs)
            if resp.status_code == 401:
                # Token may have been revoked externally — try one refresh
                self.refresh_token_if_expired()
                resp = client.request(method, url, headers=self._headers(), timeout=15, **kwargs)
        if resp.status_code >= 400:
            logger.error(
                "QB API %s %s → %s: %s",
                method, url, resp.status_code, resp.text,
            )
        resp.raise_for_status()
        return resp.json()

    # ── Customer ──────────────────────────────────────────────────────────────

    def create_customer(
        self,
        company_name: str,
        email: str,
        phone: str | None = None,
        ref_id: str | None = None,
    ) -> str:
        """Create or find a QB Customer. Returns QB customer Id."""
        # Check for existing by display name to avoid duplicates
        escaped = company_name.replace("'", "\\'")
        query_resp = self._request(
            "GET",
            f"query?query=SELECT * FROM Customer WHERE DisplayName = '{escaped}'&minorversion=65",
        )
        entities = query_resp.get("QueryResponse", {}).get("Customer", [])
        if entities:
            return str(entities[0]["Id"])

        payload: dict[str, Any] = {
            "DisplayName": company_name,
            "PrimaryEmailAddr": {"Address": email},
            "CompanyName": company_name,
        }
        if phone:
            payload["PrimaryPhone"] = {"FreeFormNumber": phone}
        if ref_id:
            payload["Notes"] = f"AF Apparels Company ID: {ref_id}"

        resp = self._request("POST", "customer", json={"DisplayName": company_name, **payload})
        return str(resp["Customer"]["Id"])

    # ── Invoice ───────────────────────────────────────────────────────────────

    def create_invoice(
        self,
        qb_customer_id: str,
        order_number: str,
        line_items: list[dict],
        total: float,
        due_date: str | None = None,
    ) -> str:
        """Create a QB Invoice. Returns QB invoice Id.

        line_items: list of {description, quantity, unit_price, amount}
        """
        lines = []
        for item in line_items:
            line_detail: dict[str, Any] = {
                "Qty": item["quantity"],
                "UnitPrice": float(item["unit_price"]),
            }
            if item.get("qb_item_id"):
                line_detail["ItemRef"] = {"value": item["qb_item_id"]}
            lines.append({
                "Amount": float(item["amount"]),
                "DetailType": "SalesItemLineDetail",
                "Description": item["description"],
                "SalesItemLineDetail": line_detail,
            })

        payload: dict[str, Any] = {
            "CustomerRef": {"value": qb_customer_id},
            "DocNumber": order_number,
            "Line": lines,
        }
        if due_date:
            payload["DueDate"] = due_date

        logger.info("QB create_invoice payload: %s", payload)
        resp = self._request("POST", "invoice", json=payload)
        return str(resp["Invoice"]["Id"])

    # ── Items (Products / Inventory) ─────────────────────────────────────────

    def _get_account_id(self, name: str, account_type: str) -> str:
        """Return the QB account Id for the given name+type, caching per instance.

        Raises ValueError if QB returns no matching account.
        """
        cache_key = f"{account_type}:{name}"
        if cache_key in self._account_id_cache:
            return self._account_id_cache[cache_key]

        escaped_name = name.replace("'", "\\'")
        escaped_type = account_type.replace("'", "\\'")
        result = self._request(
            "GET",
            f"query?query=SELECT * FROM Account WHERE Name = '{escaped_name}'"
            f" AND AccountType = '{escaped_type}'&minorversion=65",
        )
        accounts = result.get("QueryResponse", {}).get("Account", [])
        if not accounts:
            raise ValueError(f"QB account not found: name='{name}' type='{account_type}'")

        account_id = str(accounts[0]["Id"])
        self._account_id_cache[cache_key] = account_id
        logger.info("QB _get_account_id — name=%s type=%s → id=%s", name, account_type, account_id)
        return account_id

    def get_item(self, qb_item_id: str) -> dict[str, Any]:
        """Fetch a QB Item by ID. Returns the Item dict (includes SyncToken)."""
        resp = self._request("GET", f"item/{qb_item_id}?minorversion=65")
        return resp.get("Item", {})

    def find_or_create_item(
        self,
        sku: str,
        name: str,
        unit_price: float,
        cost: float | None,
        qty_on_hand: int = 0,
    ) -> str:
        """Find a QB Inventory Item by SKU or create one. Returns QB item Id."""
        from datetime import date

        escaped = sku.replace("'", "\\'")
        result = self._request(
            "GET",
            f"query?query=SELECT * FROM Item WHERE Sku = '{escaped}'&minorversion=65",
        )
        items = result.get("QueryResponse", {}).get("Item", [])
        if items:
            logger.info("QB find_or_create_item — found existing for sku=%s id=%s", sku, items[0]["Id"])
            return str(items[0]["Id"])

        income_id  = self._get_account_id("Sales of Product Income", "Income")
        asset_id   = self._get_account_id("Inventory Asset", "Other Current Asset")
        expense_id = self._get_account_id("Cost of Goods Sold", "Cost of Goods Sold")

        payload: dict[str, Any] = {
            "Name": name[:100],
            "Sku": sku,
            "Type": "Inventory",
            "TrackQtyOnHand": True,
            "QtyOnHand": qty_on_hand,
            "InvStartDate": str(date.today()),
            "UnitPrice": unit_price,
            "IncomeAccountRef":  {"value": income_id,  "name": "Sales of Product Income"},
            "AssetAccountRef":   {"value": asset_id,   "name": "Inventory Asset"},
            "ExpenseAccountRef": {"value": expense_id, "name": "Cost of Goods Sold"},
        }
        if cost is not None:
            payload["PurchaseCost"] = cost

        import json as _json
        logger.info(
            "QB find_or_create_item — creating sku=%s name=%s qty=%d PAYLOAD: %s",
            sku, name[:40], qty_on_hand, _json.dumps(payload, default=str),
        )
        resp = self._request("POST", "item", json=payload)
        return str(resp["Item"]["Id"])

    def update_item(
        self,
        qb_item_id: str,
        unit_price: float | None = None,
        cost: float | None = None,
        qty_on_hand: int | None = None,
    ) -> bool:
        """Sparse-update a QB Item's price and/or inventory quantity."""
        from datetime import date

        try:
            item = self.get_item(qb_item_id)
            if not item:
                logger.warning("QB update_item — item %s not found in QB", qb_item_id)
                return False

            payload: dict[str, Any] = {
                "Id": qb_item_id,
                "SyncToken": item["SyncToken"],
                "sparse": True,
            }
            if unit_price is not None:
                payload["UnitPrice"] = unit_price
            if cost is not None:
                payload["PurchaseCost"] = cost
            if qty_on_hand is not None:
                payload["QtyOnHand"] = qty_on_hand
                payload["InvStartDate"] = str(date.today())

            self._request("POST", "item", json=payload)
            logger.info("QB update_item success — id=%s", qb_item_id)
            return True
        except Exception as exc:
            logger.error("QB update_item failed for %s: %s", qb_item_id, exc)
            return False

    def void_invoice(self, invoice_id: str) -> bool:
        """Void a QB invoice by ID."""
        try:
            # Need current SyncToken first
            resp = self._request("GET", f"invoice/{invoice_id}")
            sync_token = resp["Invoice"]["SyncToken"]
            self._request(
                "POST",
                "invoice",
                params={"operation": "void"},
                json={"Id": invoice_id, "SyncToken": sync_token, "sparse": True},
            )
            return True
        except Exception:
            return False

    # ── Async helpers for PO sync ──────────────────────────────────────────────

    async def _make_request(self, method: str, path: str, data: dict | None = None) -> dict[str, Any]:
        """Async wrapper around sync _request, runs in thread pool."""
        kwargs: dict[str, Any] = {}
        if data is not None:
            kwargs["json"] = data
        return await asyncio.to_thread(self._request, method, path, **kwargs)

    # ── Vendor ────────────────────────────────────────────────────────────────

    async def find_or_create_vendor(self, vendor_name: str) -> str:
        """Find vendor in QB by name; create if not found. Returns QB vendor Id."""
        import logging
        _log = logging.getLogger(__name__)
        escaped = vendor_name.replace("'", "\\'")
        result = await self._make_request(
            "GET",
            f"query?query=SELECT * FROM Vendor WHERE DisplayName = '{escaped}'&minorversion=65",
        )
        vendors = result.get("QueryResponse", {}).get("Vendor", [])
        if vendors:
            return str(vendors[0]["Id"])
        result = await self._make_request("POST", "vendor", {"DisplayName": vendor_name})
        vendor_id = result.get("Vendor", {}).get("Id")
        if not vendor_id:
            _log.error("QB create vendor returned no Id: %s", result)
            raise ValueError(f"Could not create QB vendor for '{vendor_name}'")
        return str(vendor_id)

    # ── Purchase Order ────────────────────────────────────────────────────────

    async def create_purchase_order(
        self,
        vendor_name: str,
        line_items: list[dict],
        po_number: str,
        expected_date: str | None = None,
    ) -> dict[str, Any]:
        """Create a Purchase Order in QuickBooks. Returns the QB PurchaseOrder dict."""
        from datetime import date as _date
        vendor_id = await self.find_or_create_vendor(vendor_name)
        qb_lines = []
        for i, item in enumerate(line_items):
            amount = round(item["qty"] * item["unit_price"], 2)
            qb_lines.append({
                "Id": str(i + 1),
                "LineNum": i + 1,
                "Amount": amount,
                "DetailType": "ItemBasedExpenseLineDetail",
                "Description": item.get("description", ""),
                "ItemBasedExpenseLineDetail": {
                    "ItemRef": {"value": "1", "name": "Services"},
                    "Qty": item["qty"],
                    "UnitPrice": item["unit_price"],
                },
            })
        po_data = {
            "VendorRef": {"value": vendor_id},
            "Line": qb_lines,
            "DocNumber": po_number,
            "TxnDate": expected_date or str(_date.today()),
            "POStatus": "Open",
        }
        result = await self._make_request("POST", "purchaseorder", po_data)
        qb_po = result.get("PurchaseOrder", {})
        return {"id": qb_po.get("Id"), **qb_po}

    # ── Vendor Bill ───────────────────────────────────────────────────────────

    async def create_vendor_bill(
        self,
        vendor_name: str,
        line_items: list[dict],
        po_number: str,
        bill_date: str | None = None,
    ) -> dict[str, Any]:
        """Create a Vendor Bill in QuickBooks. Returns the QB Bill dict."""
        from datetime import date as _date
        vendor_id = await self.find_or_create_vendor(vendor_name)
        qb_lines = []
        for i, item in enumerate(line_items):
            amount = round(item["qty"] * item["unit_price"], 2)
            qb_lines.append({
                "Id": str(i + 1),
                "LineNum": i + 1,
                "Amount": amount,
                "DetailType": "AccountBasedExpenseLineDetail",
                "Description": item.get("description", ""),
                "AccountBasedExpenseLineDetail": {
                    "AccountRef": {"value": "1", "name": "Cost of Goods Sold"},
                },
            })
        bill_data = {
            "VendorRef": {"value": vendor_id},
            "Line": qb_lines,
            "DocNumber": po_number,
            "TxnDate": bill_date or str(_date.today()),
        }
        result = await self._make_request("POST", "bill", bill_data)
        qb_bill = result.get("Bill", {})
        return {"id": qb_bill.get("Id"), **qb_bill}


quickbooks_service = QuickBooksService()
