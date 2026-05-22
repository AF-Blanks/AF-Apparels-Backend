"""QuickBooks Online integration service.

Provides create_customer, create_invoice, token refresh, and rate limiting.
Uses the intuitlib + quickbooks-python SDK pattern via raw requests for
maximum control over token management.
"""
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

import httpx

from app.core.config import settings


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
    """Stateless service — tokens are read from settings / passed per call."""

    def __init__(self):
        self._access_token: str = settings.QB_ACCESS_TOKEN
        self._refresh_token: str = settings.QB_REFRESH_TOKEN
        self._company_id: str = settings.QB_COMPANY_ID
        self._base_url: str = QB_BASE_URL[settings.QB_ENVIRONMENT]
        self._token_expiry: datetime | None = None  # unknown on init — will refresh if needed

    # ── Token management ──────────────────────────────────────────────────────

    def refresh_token_if_expired(self) -> bool:
        """Refresh access token using the stored refresh token.

        Returns True on success. On network failure, logs a warning and
        continues using the existing token rather than crashing the request.
        """
        import logging
        _log = logging.getLogger(__name__)

        if not settings.QB_CLIENT_ID or not settings.QB_REFRESH_TOKEN:
            return False
        try:
            with httpx.Client(transport=httpx.HTTPTransport(retries=3)) as client:
                resp = client.post(
                    TOKEN_URL,
                    auth=(settings.QB_CLIENT_ID, settings.QB_CLIENT_SECRET),
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token or settings.QB_REFRESH_TOKEN,
                    },
                    timeout=10,
                )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data["access_token"]
            self._refresh_token = data.get("refresh_token", self._refresh_token)
            expires_in = data.get("expires_in", 3600)
            self._token_expiry = datetime.utcnow() + timedelta(seconds=expires_in - 60)
            return True
        except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
            # Network unreachable — fall back to the current token and try anyway.
            # If the token is truly expired the downstream 401 handler will surface it.
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
        # If expiry is unknown but a token is already loaded, trust it.
        # A 401 response will trigger an explicit refresh via the caller.
        if self._token_expiry is None:
            return not bool(self._access_token)
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
            lines.append({
                "Amount": float(item["amount"]),
                "DetailType": "SalesItemLineDetail",
                "Description": item["description"],
                "SalesItemLineDetail": {
                    "Qty": item["quantity"],
                    "UnitPrice": float(item["unit_price"]),
                },
            })

        payload: dict[str, Any] = {
            "CustomerRef": {"value": qb_customer_id},
            "DocNumber": order_number,
            "Line": lines,
        }
        if due_date:
            payload["DueDate"] = due_date

        resp = self._request("POST", "invoice", json=payload)
        return str(resp["Invoice"]["Id"])

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
