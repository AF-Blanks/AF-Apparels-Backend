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


# ── DB token helpers ─────────────────────────────────────────────────────────

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


def _save_tokens_to_db_sync(
    access_token: str,
    refresh_token: str,
    expires_at_iso: str,
    realm_id: str | None = None,
) -> None:
    """Upsert QB tokens into app_settings using a synchronous psycopg2 connection.

    Safe to call from any context (FastAPI thread, Celery worker, asyncio.to_thread).
    Avoids asyncpg event-loop binding — psycopg2 has no loop affinity.
    """
    import psycopg2
    pairs = [
        ("qb_access_token",     access_token),
        ("qb_refresh_token",    refresh_token),
        ("qb_token_expires_at", expires_at_iso),
    ]
    if realm_id:
        pairs.append(("qb_realm_id", realm_id))
    try:
        conn = psycopg2.connect(settings.sync_db_url)
        cur = conn.cursor()
        for key, value in pairs:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (%s, %s, now())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, value),
            )
        conn.commit()
        cur.close()
        conn.close()
        logger.info("QB tokens saved to DB (sync)")
    except Exception as exc:
        logger.warning("QB token save to DB failed: %s", exc)


class _TokenBucket:
    """Simple thread-safe token bucket for rate limiting (250 req/min).

    Used as the per-process fallback when the distributed Redis limiter is
    unavailable (see _RedisRateLimiter).
    """

    def __init__(self, capacity: int = 250, refill_rate: float = 250 / 60):
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


class _RedisRateLimiter:
    """Distributed token bucket shared across ALL worker/API processes via Redis.

    QB enforces its rate limit per realm (company) server-side, so a per-process
    bucket lets N processes collectively exceed it (N × local_rate). Running 2
    Celery workers with a 400/min local bucket each can push ~800/min at a 500/min
    cap. This limiter coordinates ONE bucket per realm in Redis so the GLOBAL rate
    stays under the cap no matter how many workers/dynos are running.

    - Atomic refill+consume via a single Lua script (race-free across processes).
    - Keyed per realm: qb:ratelimit:{realm_id}.
    - Uses wall-clock time (time.time()), NOT time.monotonic(), because the bucket
      timestamp is shared across processes and monotonic clocks are not comparable
      between them.
    - Fails OPEN to a per-process _TokenBucket if Redis is unavailable, so a Redis
      blip degrades to the previous behaviour instead of halting all QB syncs.
    """

    # KEYS[1]=bucket key  ARGV: capacity, refill_per_sec, now_epoch, needed
    # returns {allowed(1/0), wait_seconds_as_string}
    _LUA = """
    local capacity = tonumber(ARGV[1])
    local refill   = tonumber(ARGV[2])
    local now      = tonumber(ARGV[3])
    local needed   = tonumber(ARGV[4])
    local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
    local tokens = tonumber(bucket[1])
    local ts     = tonumber(bucket[2])
    if tokens == nil then tokens = capacity; ts = now end
    local elapsed = now - ts
    if elapsed < 0 then elapsed = 0 end
    tokens = math.min(capacity, tokens + elapsed * refill)
    local allowed = 0
    local wait = 0.0
    if tokens >= needed then
        tokens = tokens - needed
        allowed = 1
    else
        wait = (needed - tokens) / refill
    end
    redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
    redis.call('PEXPIRE', KEYS[1], 120000)
    return {allowed, tostring(wait)}
    """

    def __init__(self, capacity: int = 250, refill_rate: float = 250 / 60):
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._local_fallback = _TokenBucket(capacity, refill_rate)
        self._client = None
        self._script = None
        self._init_failed = False

    def _redis(self):
        """Lazily connect a SYNC redis client + register the Lua script.

        _request() runs in a worker thread (via asyncio.to_thread), so the limiter
        must be synchronous — we use redis-py's sync client, not the async one.
        Returns None (and caches that) if Redis can't be reached, triggering the
        local fallback for the rest of this process's life.
        """
        if self._client is not None or self._init_failed:
            return self._client
        try:
            import redis as _redis  # redis-py (sync) — already present for Celery broker
            url = getattr(settings, "REDIS_URL", None) or getattr(settings, "CELERY_BROKER_URL", None)
            if not url:
                raise RuntimeError("no REDIS_URL / CELERY_BROKER_URL configured")
            client = _redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
            client.ping()
            self._client = client
            self._script = client.register_script(self._LUA)
            logger.info("QB rate limiter: using distributed Redis bucket (global cap)")
        except Exception as exc:
            self._init_failed = True
            logger.warning(
                "QB rate limiter: Redis unavailable (%s) — falling back to per-process bucket",
                exc,
            )
        return self._client

    def wait(self, tokens: int = 1, timeout: float = 30.0, realm: str | None = None) -> None:
        client = self._redis()
        if client is None:
            # Redis not reachable — degrade to the original per-process behaviour.
            return self._local_fallback.wait(tokens, min(timeout, 5.0))

        key = f"qb:ratelimit:{realm or 'default'}"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                res = self._script(
                    keys=[key],
                    args=[self._capacity, self._refill_rate, time.time(), tokens],
                )
                allowed = int(res[0])
            except Exception as exc:
                # Redis hiccup mid-flight — degrade to local bucket for this call.
                logger.warning("QB rate limiter: Redis error (%s) — local fallback", exc)
                return self._local_fallback.wait(tokens, max(0.05, deadline - time.monotonic()))
            if allowed == 1:
                return
            time.sleep(0.05)
        raise TimeoutError("QB rate limit: could not acquire token in time")


# Distributed limiter shared across every worker + API process (global QB cap).
# Falls back to a per-process token bucket automatically if Redis is unreachable.
_rate_limiter = _RedisRateLimiter()

# Process-level cache for QB account IDs (Income/Asset/COGS).
# These IDs are permanent in QB and never change, so no TTL needed.
# Survives across Celery tasks in the same worker process — saves 3 Query
# calls per variant sync after the first lookup.
_qb_account_id_cache: dict[str, str] = {}

# Process-level cache for QB vendor IDs.
# Vendor names don't change so this is safe to cache indefinitely per process.
# Prevents repeated CorePlus Query calls on every PO receipt sync.
_qb_vendor_id_cache: dict[str, str] = {}

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

    def initialize_sync(self) -> "QuickBooksService":
        """Load live tokens from app_settings DB using psycopg2 (sync).

        Use when initialize() cannot be awaited — e.g., inside a sync __init__.
        Falls back to env vars on any DB error.
        """
        import psycopg2
        try:
            conn = psycopg2.connect(settings.sync_db_url)
            cur = conn.cursor()
            cur.execute(
                "SELECT key, value FROM app_settings "
                "WHERE key IN ('qb_access_token','qb_refresh_token',"
                "              'qb_realm_id','qb_token_expires_at')"
            )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            db = {row[0]: row[1] for row in rows if row[1]}
        except Exception as exc:
            logger.warning("QB initialize_sync DB load failed (%s) — using env vars", exc)
            return self
        if db.get("qb_access_token"):
            self._access_token = db["qb_access_token"]
        if db.get("qb_refresh_token"):
            self._refresh_token = db["qb_refresh_token"]
        if db.get("qb_realm_id"):
            self._company_id = db["qb_realm_id"]
        expires_str = db.get("qb_token_expires_at")
        if expires_str:
            try:
                dt = datetime.fromisoformat(expires_str)
                if dt.tzinfo is not None:
                    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                self._token_expiry = dt
            except ValueError:
                pass
        logger.info(
            "QB initialize_sync: token loaded from DB — expiry=%s",
            self._token_expiry,
        )
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

            # Persist to DB — sync write avoids asyncpg event-loop binding issues
            _save_tokens_to_db_sync(new_access, new_refresh, expires_iso)
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
        # Global rate limit, keyed per realm so the whole fleet shares one budget.
        _rate_limiter.wait(realm=self._company_id)
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

    def query(self, soql: str) -> dict[str, Any]:
        """Run a raw SOQL query against the QB Accounting API (rate-limited via _request)."""
        return self._request("GET", f"query?query={soql}&minorversion=65")

    # ── Customer ──────────────────────────────────────────────────────────────

    def create_customer(
        self,
        company_name: str,
        email: str,
        phone: str | None = None,
        ref_id: str | None = None,
        bill_addr: dict | None = None,
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
        if bill_addr:
            payload["BillAddr"] = bill_addr
            payload["ShipAddr"] = bill_addr

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
        shipping_addr: dict | None = None,
    ) -> str:
        """Create a QB Invoice. Returns QB invoice Id (idempotent by DocNumber).

        line_items: list of {description, quantity, unit_price, amount}
        shipping_addr: optional dict with keys Line1, City, CountrySubDivisionCode, PostalCode
        """
        # DocNumber idempotency is handled at the task level (order.qb_invoice_id check).
        # Skipping the CorePlus SELECT query here to preserve monthly API call budget.

        lines = []
        for item in line_items:
            _qb_item_id = item.get("qb_item_id")
            if not _qb_item_id:
                logger.critical(
                    "QB invoice line missing qb_item_id — falling back to item '1' (Services). "
                    "Revenue and COGS will be misclassified. description=%s",
                    item.get("description"),
                )
            line_detail: dict[str, Any] = {
                "ItemRef": {"value": _qb_item_id or "1"},
                "Qty": item["quantity"],
                "UnitPrice": float(item["unit_price"]),
            }
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
            # QB Automated Sales Tax — calculates tax based on ShipAddr
            "TxnTaxDetail": {"TxnTaxCodeRef": {"value": "TAX"}},
            "GlobalTaxCalculation": "TaxExcluded",
        }
        if due_date:
            payload["DueDate"] = due_date
        if shipping_addr:
            payload["ShipAddr"] = shipping_addr

        logger.info("QB create_invoice payload: %s", payload)
        resp = self._request("POST", "invoice", json=payload)
        return str(resp["Invoice"]["Id"])

    # ── Items (Products / Inventory) ─────────────────────────────────────────

    def _get_account_id(self, name: str, account_type: str) -> str:
        """Return the QB account Id for the given name+type.

        Lookup priority:
          1. Process-level dict  (same worker process, no I/O)
          2. Redis cache         (survives worker restarts — saves 1 QB call per key)
          3. QB API              (only on very first lookup ever, or after Redis flush)
        """
        cache_key = f"{account_type}:{name}"
        redis_key = f"qb:account_id:{cache_key}"

        # 1. Process-level cache
        if cache_key in _qb_account_id_cache:
            return _qb_account_id_cache[cache_key]

        # 2. Instance-level cache
        if cache_key in self._account_id_cache:
            return self._account_id_cache[cache_key]

        # 3. Redis cache (no TTL — QB account IDs never change)
        _redis_client = None
        try:
            import redis  # redis-py (sync) — already in requirements for Celery
            _redis_client = redis.Redis.from_url(
                getattr(settings, "REDIS_URL", None) or settings.CELERY_BROKER_URL,
                socket_timeout=2,
            )
            cached = _redis_client.get(redis_key)
            if cached:
                account_id = cached.decode()
                _qb_account_id_cache[cache_key] = account_id
                self._account_id_cache[cache_key] = account_id
                logger.info("QB _get_account_id — Redis hit: %s → %s", cache_key, account_id)
                return account_id
        except Exception:
            _redis_client = None  # Redis unavailable — fall through to QB API

        # 4. QB API call (only on very first lookup or after Redis flush)
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
        _qb_account_id_cache[cache_key] = account_id
        self._account_id_cache[cache_key] = account_id

        # Persist to Redis so next worker restart skips the QB API call
        if _redis_client is not None:
            try:
                _redis_client.set(redis_key, account_id)
            except Exception:
                pass

        logger.info("QB _get_account_id — QB API: %s → %s (cached Redis+process)", cache_key, account_id)
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
        description: str = "",
    ) -> str:
        """Find a QB Inventory Item by Name or create one. Returns QB item Id.

        Search uses Name (not Sku) because Sku is read-only on Inventory items
        in QB API v3 and cannot be set on creation — sending it causes 400 error
        code 2010 (unsupported property).
        """
        from datetime import date

        # Search by Name (Name includes SKU so it's unique per variant)
        escaped_name = name[:100].replace("'", "\\'")
        result = self._request(
            "GET",
            f"query?query=SELECT * FROM Item WHERE Name = '{escaped_name}'&minorversion=65",
        )
        items = result.get("QueryResponse", {}).get("Item", [])
        if items:
            logger.info("QB find_or_create_item — found existing for sku=%s id=%s", sku, items[0]["Id"])
            return str(items[0]["Id"])

        income_id  = self._get_account_id("Sales of Product Income", "Income")
        asset_id   = self._get_account_id("Inventory Asset", "Other Current Asset")
        expense_id = self._get_account_id("Cost of Goods Sold", "Cost of Goods Sold")

        # Note: Sku field intentionally omitted — QB API rejects it on Inventory items (read-only)
        payload: dict[str, Any] = {
            "Name": name[:100],
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
        if description:
            payload["Description"] = description[:4000]
        # SalesDesc and PurchaseDesc intentionally omitted — unsupported on Inventory items in QB API v3

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
        """Sparse-update a QB Item's price and/or inventory quantity.

        SyncToken is cached in Redis to avoid a GET /item call on every update.
        Cache key: qb:synctoken:item:{qb_item_id} — updated after every successful write.
        On 409 Conflict (stale token) the cache is cleared and we fall back to GET once.
        """
        from datetime import date

        redis_token_key = f"qb:synctoken:item:{qb_item_id}"

        def _get_redis():
            try:
                import redis
                return redis.Redis.from_url(
                    getattr(settings, "REDIS_URL", None) or settings.CELERY_BROKER_URL,
                    socket_timeout=2,
                )
            except Exception:
                return None

        def _cached_sync_token() -> str | None:
            r = _get_redis()
            if r is None:
                return None
            try:
                val = r.get(redis_token_key)
                return val.decode() if val else None
            except Exception:
                return None

        def _cache_sync_token(token: str) -> None:
            r = _get_redis()
            if r is None:
                return
            try:
                r.set(redis_token_key, token, ex=3600)  # 1-hour TTL as safety net
            except Exception:
                pass

        def _clear_sync_token() -> None:
            r = _get_redis()
            if r is None:
                return
            try:
                r.delete(redis_token_key)
            except Exception:
                pass

        def _do_update(sync_token: str) -> dict:
            from datetime import date as _date
            payload: dict[str, Any] = {
                "Id": qb_item_id,
                "SyncToken": sync_token,
                "sparse": True,
            }
            if unit_price is not None:
                payload["UnitPrice"] = unit_price
            if cost is not None:
                payload["PurchaseCost"] = cost
            if qty_on_hand is not None:
                payload["QtyOnHand"] = qty_on_hand
                payload["InvStartDate"] = str(_date.today())
            import json as _json
            logger.info("QB update_item payload — id=%s: %s", qb_item_id, _json.dumps(payload, default=str))
            return self._request("POST", "item", json=payload)

        try:
            # Try cached SyncToken first (saves 1 GET call)
            token = _cached_sync_token()
            if token:
                try:
                    resp = _do_update(token)
                    new_token = resp.get("Item", {}).get("SyncToken")
                    if new_token:
                        _cache_sync_token(new_token)
                    logger.info("QB update_item success (cached token) — id=%s", qb_item_id)
                    return True
                except Exception as cache_exc:
                    # 409 = stale token; any other error also falls back to fresh GET
                    logger.info("QB update_item cached token failed (%s) — falling back to GET", cache_exc)
                    _clear_sync_token()

            # Fallback: GET item for fresh SyncToken
            item = self.get_item(qb_item_id)
            if not item:
                logger.warning("QB update_item — item %s not found in QB (stale qb_item_id?)", qb_item_id)
                return False

            resp = _do_update(item["SyncToken"])
            new_token = resp.get("Item", {}).get("SyncToken")
            if new_token:
                _cache_sync_token(new_token)
            logger.info("QB update_item success — id=%s", qb_item_id)
            return True

        except Exception as exc:
            logger.error("QB update_item failed for %s: %s", qb_item_id, exc)
            return False

    def create_payment_for_invoice(
        self,
        invoice_id: str,
        amount: float,
        payment_method: str = "card",
        payment_date: str | None = None,
        qb_customer_id: str | None = None,
    ) -> dict:
        """Create a QB Payment record linked to an invoice (marks it as paid).

        Used to record card/ACH payments on QB invoices created for non-Net-30 orders.
        Returns the QB Payment dict on success; raises on failure.

        Pass qb_customer_id to skip the GET /invoice call — saves 1 Core API call.
        When omitted, fetches the invoice for CustomerRef + balance (legacy path).
        """
        from datetime import date as _date
        txn_date = payment_date or str(_date.today())

        if qb_customer_id:
            # Fast path: caller already knows the QB customer ID — skip GET /invoice.
            # Use passed amount directly (invoice was just created in this task run
            # so balance == total; or on retry, no partial payments will have been applied).
            pay_amount = round(amount, 2)
            if pay_amount <= 0:
                logger.info("QB create_payment_for_invoice — amount %.2f <= 0, skipping", pay_amount)
                return {}
            customer_ref: dict[str, Any] = {"value": qb_customer_id}
            logger.info(
                "QB create_payment_for_invoice (no-fetch) — invoice=%s customer=%s amount=%.2f",
                invoice_id, qb_customer_id, pay_amount,
            )
        else:
            # Fallback: fetch the invoice to get CustomerRef and remaining balance.
            # Use the invoice's actual outstanding balance to prevent 400 errors when
            # the invoice total differs from order total (old invoices without shipping/tax).
            invoice_resp = self._request("GET", f"invoice/{invoice_id}?minorversion=65")
            invoice = invoice_resp.get("Invoice", {})
            customer_ref = invoice.get("CustomerRef", {})
            if not customer_ref:
                raise ValueError(f"Cannot create QB payment — invoice {invoice_id} has no CustomerRef")
            balance = float(invoice.get("Balance", 0))
            if balance <= 0:
                logger.info("QB create_payment_for_invoice — invoice %s already fully paid, skipping", invoice_id)
                return {}
            pay_amount = round(balance, 2)
            logger.info(
                "QB create_payment_for_invoice — invoice_id=%s balance=%.2f (order_total=%.2f)",
                invoice_id, pay_amount, amount,
            )

        payload: dict[str, Any] = {
            "TotalAmt": pay_amount,
            "CustomerRef": customer_ref,
            "TxnDate": txn_date,
            "Line": [
                {
                    "Amount": pay_amount,
                    "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}],
                }
            ],
        }
        resp = self._request("POST", "payment", json=payload)
        return resp.get("Payment", {})

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

    async def find_or_create_vendor(self, vendor_name: str, email: str = "") -> str:
        """Find vendor in QB by name; create if not found. Returns QB vendor Id.

        Handles QB error 6240 (Duplicate Name Exists) — raised when the same
        DisplayName is already in use by a Customer.  The error detail contains
        the existing entity's Id, which we extract and return so the PO/bill
        sync can proceed without crashing.
        """
        import re
        escaped = vendor_name.replace("'", "\\'")

        # ── 0. Process-level cache (avoids repeated CorePlus Query calls) ─────
        if vendor_name in _qb_vendor_id_cache:
            logger.info("find_or_create_vendor: cache hit for '%s' → Id=%s", vendor_name, _qb_vendor_id_cache[vendor_name])
            return _qb_vendor_id_cache[vendor_name]

        # ── 1. Search existing vendor ─────────────────────────────────────────
        result = await self._make_request(
            "GET",
            f"query?query=SELECT * FROM Vendor WHERE DisplayName = '{escaped}'&minorversion=65",
        )
        vendors = result.get("QueryResponse", {}).get("Vendor", [])
        if vendors:
            vid = str(vendors[0]["Id"])
            _qb_vendor_id_cache[vendor_name] = vid
            logger.info("find_or_create_vendor: found existing vendor Id=%s (cached)", vid)
            return vid

        # ── 2. Try to create ──────────────────────────────────────────────────
        vendor_data: dict[str, Any] = {"DisplayName": vendor_name}
        if email:
            vendor_data["PrimaryEmailAddr"] = {"Address": email}

        try:
            create_result = await self._make_request("POST", "vendor", vendor_data)
            vendor_id = create_result.get("Vendor", {}).get("Id")
            if not vendor_id:
                logger.error("QB create vendor returned no Id: %s", create_result)
                raise ValueError(f"Could not create QB vendor for '{vendor_name}'")
            vid = str(vendor_id)
            _qb_vendor_id_cache[vendor_name] = vid
            logger.info("find_or_create_vendor: created vendor Id=%s (cached)", vid)
            return vid

        except httpx.HTTPStatusError as exc:
            body = exc.response.text if exc.response is not None else ""
            is_duplicate = exc.response is not None and exc.response.status_code == 400 and (
                "6240" in body or "Duplicate Name" in body
            )
            if not is_duplicate:
                raise

            # QB error 6240 — "Duplicate Name Exists" because the same DisplayName
            # is already used by a Customer (or other entity).
            # The error detail includes "Id=NUMBER" — extract and reuse it.
            match = re.search(r'\bId=(\d+)', body)
            if match:
                vid = match.group(1)
                _qb_vendor_id_cache[vendor_name] = vid
                logger.warning(
                    "find_or_create_vendor: '%s' exists as Customer/other entity "
                    "in QB; reusing Id=%s as vendor reference (cached)",
                    vendor_name, vid,
                )
                return vid

            # ID not in error body — retry vendor search in case of a race condition
            logger.warning(
                "find_or_create_vendor: duplicate name error but no Id in error "
                "body; retrying vendor search for '%s'",
                vendor_name,
            )
            retry = await self._make_request(
                "GET",
                f"query?query=SELECT * FROM Vendor WHERE DisplayName = '{escaped}'&minorversion=65",
            )
            retry_vendors = retry.get("QueryResponse", {}).get("Vendor", [])
            if retry_vendors:
                return str(retry_vendors[0]["Id"])

            # Last resort: suffix the name to avoid the conflict
            logger.warning(
                "find_or_create_vendor: creating '%s (Vendor)' to avoid name conflict",
                vendor_name,
            )
            vendor_data["DisplayName"] = f"{vendor_name} (Vendor)"
            suffix_result = await self._make_request("POST", "vendor", vendor_data)
            suffix_id = suffix_result.get("Vendor", {}).get("Id")
            if not suffix_id:
                raise ValueError(
                    f"Could not create QB vendor for '{vendor_name}' (with suffix)"
                )
            return str(suffix_id)

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

        # Resolve COGS account ID dynamically (cached process-wide after first lookup)
        cogs_account_id = await asyncio.to_thread(
            self._get_account_id, "Cost of Goods Sold", "Cost of Goods Sold"
        )

        qb_lines = []
        for i, item in enumerate(line_items):
            amount = round(float(item["qty"]) * float(item["unit_price"]), 2)
            qb_item_id = item.get("qb_item_id")
            if qb_item_id:
                # Item details tab in QB — shows product name, qty, unit cost
                qb_lines.append({
                    "Id": str(i + 1),
                    "LineNum": i + 1,
                    "Amount": amount,
                    "Description": item.get("description", ""),
                    "DetailType": "ItemBasedExpenseLineDetail",
                    "ItemBasedExpenseLineDetail": {
                        "ItemRef": {"value": str(qb_item_id)},
                        "Qty": float(item["qty"]),
                        "UnitPrice": float(item["unit_price"]),
                    },
                })
            else:
                # Category details tab fallback — used for new/unsynced products
                qb_lines.append({
                    "Id": str(i + 1),
                    "LineNum": i + 1,
                    "Amount": amount,
                    "Description": item.get("description", "Item"),
                    "DetailType": "AccountBasedExpenseLineDetail",
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {"value": cogs_account_id, "name": "Cost of Goods Sold"},
                        "BillableStatus": "NotBillable",
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
