"""JWT authentication middleware and rate limiting."""
import time

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.database import AsyncSessionLocal
from app.core.redis import redis_exists, redis_increment
from app.core.security import decode_token
from app.models.company import Company

# Paths that do not require authentication
PUBLIC_PATHS = {
    "/api/v1/login",
    "/api/v1/register-wholesale",
    "/api/v1/forgot-password",
    "/api/v1/reset-password",
    "/api/v1/refresh",
    "/api/v1/activate-account",
    "/api/v1/resend-activation",
    "/api/v1/products",
    "/api/v1/products/categories",
    "/api/v1/webhooks/stripe",
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
}

RATE_LIMIT_PATHS = {"/api/v1/auth/"}
RATE_LIMIT_MAX = 100  # requests per minute


async def require_admin(request: Request) -> None:
    """FastAPI dependency: raises 403 if the authenticated user is not an admin."""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "FORBIDDEN", "message": "Admin access required"},
        )


class AuthMiddleware(BaseHTTPMiddleware):
    """Decode JWT, inject user state, enforce rate limiting on public endpoints."""

    async def dispatch(self, request: Request, call_next: any) -> Response:
        # OPTIONS preflight requests must pass through without auth checks so
        # that CORS headers (added by the outermost CORSMiddleware) are returned.
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path

        # ── T209: Rate limiting for unauthenticated endpoints (100 req/min per IP) ──
        is_unauthenticated = not request.headers.get("Authorization", "").startswith("Bearer ")
        skip_rate_limit = any(path.startswith(p) for p in ["/docs", "/redoc", "/openapi", "/health"])
        if is_unauthenticated and not skip_rate_limit:
            client_ip = request.client.host if request.client else "unknown"
            rate_key = f"rate_limit:{client_ip}:{int(time.time() // 60)}"
            count = await redis_increment(rate_key, expire=60)
            if count > RATE_LIMIT_MAX:
                return JSONResponse(
                    status_code=429,
                    content={"error": {"code": "RATE_LIMITED", "message": "Too many requests. Please retry after 60 seconds."}},
                    headers={"Retry-After": "60"},
                )

        # ── Skip auth for public paths ────────────────────────────────────────
        # For public product/review routes: if a valid Bearer token is present,
        # inject user state so pricing middleware can return tier pricing.
        # If no token or invalid token, pass through silently as a guest.
        if self._is_public(path):
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header.split(" ", 1)[1]
                try:
                    payload = decode_token(token)
                    if payload.get("type") == "access":
                        jti = payload.get("jti")
                        if not (jti and await redis_exists(f"blacklist:{jti}")):
                            request.state.user_id = payload.get("sub")
                            request.state.is_admin = payload.get("is_admin", False)
                            request.state.company_id = payload.get("company_id")
                            request.state.pricing_tier_id = payload.get("pricing_tier_id")
                            request.state.company_role = payload.get("company_role")
                            request.state.account_type = payload.get("account_type", "wholesale")
                except (JWTError, Exception):
                    pass  # Invalid/expired token — treat as guest, don't block
            return await call_next(request)

        # ── Extract Bearer token ──────────────────────────────────────────────
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "UNAUTHORIZED", "message": "Authentication required"}},
            )

        token = auth_header.split(" ", 1)[1]

        try:
            payload = decode_token(token)
        except JWTError:
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "INVALID_TOKEN", "message": "Token is invalid or expired"}},
            )

        if payload.get("type") != "access":
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "WRONG_TOKEN_TYPE", "message": "Access token required"}},
            )

        # ── Check if token is blacklisted (logged out) ────────────────────────
        jti = payload.get("jti")
        if jti and await redis_exists(f"blacklist:{jti}"):
            return JSONResponse(
                status_code=401,
                content={"error": {"code": "TOKEN_REVOKED", "message": "Token has been revoked"}},
            )

        # ── Inject user info into request state ───────────────────────────────
        request.state.user_id = payload.get("sub")
        request.state.is_admin = payload.get("is_admin", False)
        request.state.company_id = payload.get("company_id")
        request.state.pricing_tier_id = payload.get("pricing_tier_id")
        request.state.company_role = payload.get("company_role")
        request.state.account_type = payload.get("account_type", "wholesale")

        # ── Company suspension check ──────────────────────────────────────────
        company_id = request.state.company_id
        if company_id and not request.state.is_admin:
            async with AsyncSessionLocal() as session:
                result = await session.execute(
                    select(Company.status).where(Company.id == company_id)
                )
                company_status = result.scalar_one_or_none()
            if company_status == "suspended":
                return JSONResponse(
                    status_code=403,
                    content={"error": {"code": "ACCOUNT_SUSPENDED", "message": "Your account has been suspended"}},
                )

        # ── Admin-only path enforcement ───────────────────────────────────────
        if path.startswith("/api/v1/admin/") and not request.state.is_admin:
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "FORBIDDEN", "message": "Admin access required"}},
            )

        return await call_next(request)

    def _is_public(self, path: str) -> bool:
        if path in PUBLIC_PATHS:
            return True
        # Allow GET /api/v1/products/* and /api/v1/reviews/* for guests
        if path.startswith("/api/v1/products"):
            return True
        if path.startswith("/api/v1/reviews"):
            return True
        # Guest checkout and order tracking — no auth required
        if path.startswith("/api/v1/guest"):
            return True
        # Card tokenization — guests need this too (card-save is skipped when no company_id)
        if path == "/api/v1/checkout/tokenize":
            return True
        # Public content pages — no auth required
        if path.startswith("/api/v1/style-sheets"):
            return True
        if path.startswith("/api/v1/product-specs"):
            return True
        # Tax rate — needed for guest checkout address step
        if path.startswith("/api/v1/tax-rate"):
            return True
        # Page SEO metadata — needed for SSR on all public pages
        if path.startswith("/api/v1/pages-seo"):
            return True
        # Blog posts — public listing and detail
        if path.startswith("/api/v1/blog-posts"):
            return True
        return False
