"""QuickBooks Payments API service.

Provides tokenize, charge, saved-card management, and refund methods.
Uses the same OAuth tokens as QuickBooksService (QB_ACCESS_TOKEN / QB_REFRESH_TOKEN).

PCI note: server-side tokenization routes raw card data through the backend —
this requires SAQ D compliance in production. For a lighter scope, use the
QB.js client-side tokenizer and only pass the resulting token to the backend.
"""
import logging
from typing import Any

import httpx

from app.core.config import settings
from app.services.quickbooks_service import QuickBooksService

logger = logging.getLogger(__name__)

QB_PAYMENTS_BASE = {
    "sandbox": "https://sandbox.api.intuit.com/quickbooks/v4/payments",
    "production": "https://api.intuit.com/quickbooks/v4/payments",
}

QB_CUSTOMERS_BASE = {
    "sandbox": "https://sandbox.api.intuit.com/quickbooks/v4/customers",
    "production": "https://api.intuit.com/quickbooks/v4/customers",
}


class QBPaymentsService:
    """Stateless service — reuses OAuth tokens from QuickBooksService."""

    def __init__(self):
        # initialize_sync loads the latest tokens from DB via psycopg2 so we
        # don't use stale env-var tokens (QB refreshes write to DB, not env vars).
        self._qb = QuickBooksService().initialize_sync()
        self._base_url: str = QB_PAYMENTS_BASE[settings.QB_ENVIRONMENT]

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        import uuid as _uuid
        token = self._qb.get_access_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            # QB Payments requires a unique Request-Id on every call.
            # Repeating or omitting it causes 401 AuthenticationFailed.
            "Request-Id": str(_uuid.uuid4()),
        }

    def _url(self, path: str) -> str:
        """Build URL under the payments base (tokens, charges)."""
        return f"{self._base_url}/{path.lstrip('/')}"

    def _customer_url(self, path: str) -> str:
        """Build URL under the customers base (customer profiles, saved cards)."""
        return f"{QB_CUSTOMERS_BASE[settings.QB_ENVIRONMENT]}/{path.lstrip('/')}"

    def _do_request(self, method: str, url: str, label: str, **kwargs) -> dict[str, Any]:
        """Execute an httpx request with one 401-refresh retry. Raises RuntimeError on failure."""
        try:
            resp = httpx.request(method, url, headers=self._headers(), timeout=15, **kwargs)
        except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
            raise RuntimeError("QB Payments service unavailable — check network connectivity") from exc

        if resp.status_code == 401:
            logger.warning(
                "QB Payments 401 on %s %s — token prefix: %s... | body: %s",
                method, label,
                (self._qb._access_token or "")[:20],
                resp.text[:300],
            )
            # Force-refresh regardless of stored expiry — 401 means QB rejected
            # the token outright (expired, revoked, or missing payments scope).
            refreshed = self._qb.refresh_token_if_expired()
            logger.info("QB Payments token force-refresh result: %s", "OK" if refreshed else "FAILED")
            try:
                resp = httpx.request(method, url, headers=self._headers(), timeout=15, **kwargs)
            except (httpx.ConnectError, httpx.TimeoutException, OSError) as exc:
                raise RuntimeError("QB Payments service unavailable — check network connectivity") from exc
            if resp.status_code == 401:
                logger.error(
                    "QB Payments 401 persists after token refresh — %s %s | body: %s",
                    method, label, resp.text[:300],
                )

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = ""
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise RuntimeError(f"QB Payments {method} {label} failed [{resp.status_code}]: {body}") from exc
        return resp.json() if resp.content else {}

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """Request against the payments base (tokens, charges)."""
        return self._do_request(method, self._url(path), path, **kwargs)

    def _customer_request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        """Request against the customers base (saved cards)."""
        return self._do_request(method, self._customer_url(path), path, **kwargs)

    # ── Tokenize (server-side — SAQ D) ───────────────────────────────────────

    def create_token(
        self,
        card_number: str,
        exp_month: str,
        exp_year: str,
        cvc: str,
        name: str | None = None,
        postal_code: str | None = None,
    ) -> str:
        """Tokenize raw card data. Returns an opaque QB card token.

        ⚠ SAQ D: card data passes through this server. In production, prefer
        QB.js (client-side) to tokenize and skip this method entirely.
        """
        card: dict[str, Any] = {
            "number": card_number,
            "expMonth": exp_month,
            "expYear": exp_year,
            "cvc": cvc,
        }
        if name:
            card["name"] = name
        if postal_code:
            card["address"] = {"postalCode": postal_code}

        resp = self._request("POST", "tokens", json={"card": card})
        token = resp.get("value") or resp.get("token")
        if not token:
            raise RuntimeError(f"QB Payments tokenize: unexpected response {resp}")
        return token

    # ── Charges ───────────────────────────────────────────────────────────────

    def charge_card(
        self,
        token: str,
        amount: float,
        currency: str = "USD",
        description: str | None = None,
        capture: bool = True,
    ) -> dict[str, Any]:
        """Charge a one-time token. Returns the full charge response dict."""
        payload: dict[str, Any] = {
            "amount": f"{amount:.2f}",
            "currency": currency,
            "token": token,
            "capture": capture,
        }
        if description:
            payload["description"] = description
        return self._request("POST", "charges", json=payload)

    def charge_saved_card(
        self,
        customer_id: str,
        card_id: str,
        amount: float,
        currency: str = "USD",
        description: str | None = None,
        capture: bool = True,
    ) -> dict[str, Any]:
        """Charge a previously saved card on a QB customer profile."""
        payload: dict[str, Any] = {
            "amount": f"{amount:.2f}",
            "currency": currency,
            "cardOnFile": card_id,
            "capture": capture,
            "context": {
                "mobile": False,
                "isEcommerce": True,
            },
        }
        if description:
            payload["description"] = description
        return self._request("POST", "charges", json=payload)

    def get_charge(self, charge_id: str) -> dict[str, Any]:
        """Retrieve a charge by ID."""
        return self._request("GET", f"charges/{charge_id}")

    def refund_charge(self, charge_id: str, amount: float | None = None) -> dict[str, Any]:
        """Issue a full or partial refund on a charge."""
        payload: dict[str, Any] = {}
        if amount is not None:
            payload["amount"] = f"{amount:.2f}"
        return self._request("POST", f"charges/{charge_id}/refunds", json=payload)

    # ── Saved cards (QB customer wallet) ─────────────────────────────────────

    def create_customer(self, customer_id: str) -> str:
        """Create a QB Payments customer profile (idempotent).

        Returns the QB customer ID. Falls back to the provided ID on any error
        so callers can still attempt card saves (some API versions auto-create
        the customer on first card save).
        """
        try:
            resp = httpx.request(
                "POST",
                QB_CUSTOMERS_BASE[settings.QB_ENVIRONMENT],
                headers=self._headers(),
                json={"id": customer_id},
                timeout=15,
            )
            if resp.status_code in (200, 201):
                return resp.json().get("id", customer_id)
            if resp.status_code == 409:  # already exists
                return customer_id
            logger.warning("QB Payments create_customer [%s]: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("QB Payments create_customer failed: %s", exc)
        return customer_id

    def save_card(
        self,
        customer_id: str,
        card_number: str,
        exp_month: str,
        exp_year: str,
        cvc: str,
        name: str | None = None,
    ) -> dict[str, Any]:
        """Save raw card data to a QB customer wallet. Returns the saved card object.

        Note: QB Payments saved-card endpoint requires raw card fields, not a charge token.
        Charge tokens (from POST /tokens) are one-time use for charges only.
        """
        body: dict[str, Any] = {
            "number": card_number,
            "expMonth": exp_month,
            "expYear": exp_year,
            "cvc": cvc,
        }
        if name:
            body["name"] = name
        return self._customer_request("POST", f"{customer_id}/cards", json=body)

    def list_saved_cards(self, customer_id: str) -> list[dict[str, Any]]:
        """Return all saved cards for a QB customer."""
        resp = self._customer_request("GET", f"{customer_id}/cards")
        return resp if isinstance(resp, list) else resp.get("cards", [])

    def delete_saved_card(self, customer_id: str, card_id: str) -> bool:
        """Remove a saved card from a QB customer wallet."""
        try:
            self._customer_request("DELETE", f"{customer_id}/cards/{card_id}")
            return True
        except Exception as exc:
            logger.warning("QB Payments delete card failed: %s", exc)
            return False
