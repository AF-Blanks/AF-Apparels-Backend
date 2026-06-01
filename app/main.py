# """FastAPI application factory."""
# import os
# from contextlib import asynccontextmanager
# from typing import AsyncGenerator

# import sentry_sdk
# from fastapi import FastAPI, Request
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import JSONResponse
# from fastapi.staticfiles import StaticFiles

# from app.core.config import settings
# from app.core.database import check_db_connection
# from app.core.exceptions import AppException
# from app.core.redis import check_redis_connection
# from app.middleware.audit_middleware import AuditMiddleware
# from app.middleware.auth_middleware import AuthMiddleware


# # ── Sentry ────────────────────────────────────────────────────────────────────
# if settings.SENTRY_DSN:
#     sentry_sdk.init(
#         dsn=settings.SENTRY_DSN,
#         environment=settings.APP_ENV,
#         traces_sample_rate=0.1,
#     )


# # ── App factory ───────────────────────────────────────────────────────────────
# @asynccontextmanager
# async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
#     # Run DB migrations before accepting traffic. Non-fatal so the app can
#     # still start (and serve /health) even if alembic reports no changes.
#     try:
#         import subprocess
#         result = subprocess.run(
#             ["alembic", "upgrade", "head"],
#             capture_output=True,
#             text=True,
#             cwd="/app",
#         )
#         # Trim to last 2 KB so we don't flood logs
#         if result.stdout:
#             print("Migration stdout:", result.stdout[-2000:])
#         if result.stderr:
#             print("Migration stderr:", result.stderr[-2000:])
#         if result.returncode != 0:
#             print(f"Migration exited {result.returncode} (non-fatal — app will continue)")
#     except Exception as exc:
#         print(f"Migration error (non-fatal): {exc}")

#     assert await check_db_connection(), "Database connection failed on startup"
#     assert await check_redis_connection(), "Redis connection failed on startup"
#     yield


# app = FastAPI(
#     title="AF Apparels B2B Wholesale API",
#     description="B2B wholesale e-commerce platform API",
#     version="1.0.0",
#     docs_url="/docs" if settings.APP_ENV != "production" else None,
#     redoc_url="/redoc" if settings.APP_ENV != "production" else None,
#     lifespan=lifespan,
# )

# # ── Custom middleware ─────────────────────────────────────────────────────────
# # NOTE: add_middleware inserts at index 0; Starlette builds the stack by
# # iterating the list in REVERSE, so the LAST add_middleware call becomes the
# # OUTERMOST layer (runs first on request, last on response).
# # Order here (innermost → outermost after reversal):
# #   AuditMiddleware → AuthMiddleware → PricingMiddleware → CORSMiddleware
# app.add_middleware(AuditMiddleware)
# app.add_middleware(AuthMiddleware)


# # ── Global exception handlers ─────────────────────────────────────────────────
# @app.exception_handler(AppException)
# async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
#     return JSONResponse(
#         status_code=exc.status_code,
#         content={
#             "error": {
#                 "code": exc.error_code,
#                 "message": exc.message,
#                 "details": exc.details,
#             }
#         },
#     )


# @app.exception_handler(Exception)
# async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
#     if settings.DEBUG:
#         raise exc
#     return JSONResponse(
#         status_code=500,
#         content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}},
#     )


# # ── Health check ──────────────────────────────────────────────────────────────
# @app.get("/health", tags=["Health"])
# async def health_check() -> dict:
#     db_ok = await check_db_connection()
#     redis_ok = await check_redis_connection()
#     return {
#         "status": "ok" if (db_ok and redis_ok) else "degraded",
#         "version": "1.0.0",
#         "db": "ok" if db_ok else "error",
#         "redis": "ok" if redis_ok else "error",
#     }


# # ── Routers ───────────────────────────────────────────────────────────────────
# # Imported here (after app creation) to avoid circular imports at module load time.
# from app.api.v1 import auth, products, cart, checkout, orders, account, webhooks  # noqa: E402
# from app.api.v1.admin import (  # noqa: E402
#     customers,
#     pricing as admin_pricing,
#     shipping as admin_shipping,
#     settings as admin_settings,
#     orders as admin_orders,
#     reports as admin_reports,
#     quickbooks as admin_quickbooks,
#     products as admin_products,
#     inventory as admin_inventory,
# )
# from app.middleware.pricing_middleware import PricingMiddleware  # noqa: E402

# # PricingMiddleware runs after Auth has injected pricing_tier_id into request.state
# app.add_middleware(PricingMiddleware)

# # CORS must be added LAST so it becomes the OUTERMOST middleware (runs first on
# # request). This ensures preflight OPTIONS responses include CORS headers before
# # any other middleware can short-circuit the request.
# _cors_origins = list({settings.FRONTEND_URL, *settings.allowed_origins_list} - {""})
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=_cors_origins,
#     # Wildcard for all Vercel preview + production deployments
#     allow_origin_regex=r"https://(.*\.vercel\.app|.*\.up\.railway\.app|.*\.railway\.app)",
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
#     expose_headers=["*"],
# )

# _V1 = "/api/v1"

# # Public API routers
# app.include_router(auth.router, prefix=_V1)
# app.include_router(products.router, prefix=_V1)
# app.include_router(cart.router, prefix=_V1)
# app.include_router(checkout.router, prefix=_V1)
# app.include_router(orders.router, prefix=_V1)
# app.include_router(account.router, prefix=_V1)
# app.include_router(webhooks.router, prefix=_V1)

# # Admin routers — customers has no own prefix, mount it under /admin
# app.include_router(customers.router, prefix=f"{_V1}/admin")
# app.include_router(admin_pricing.router, prefix=_V1)
# app.include_router(admin_shipping.router, prefix=_V1)
# app.include_router(admin_settings.router, prefix=_V1)
# app.include_router(admin_orders.router, prefix=_V1)
# app.include_router(admin_reports.router, prefix=_V1)
# app.include_router(admin_quickbooks.router, prefix=_V1)
# app.include_router(admin_products.router, prefix=_V1)
# app.include_router(admin_inventory.router, prefix=_V1)

# # Static files — local image uploads when S3 is not configured
# os.makedirs("/app/media", exist_ok=True)
# app.mount("/media", StaticFiles(directory="/app/media"), name="media")


"""FastAPI application factory."""
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import sentry_sdk
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.core.database import check_db_connection
from app.core.exceptions import AppException
from app.core.redis import check_redis_connection
from app.middleware.audit_middleware import AuditMiddleware
from app.middleware.auth_middleware import AuthMiddleware
from app.middleware.pricing_middleware import PricingMiddleware


# ── Sentry ────────────────────────────────────────────────────────────────────
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.APP_ENV,
        traces_sample_rate=0.1,
    )


# ── Content tables (migration fallback) ──────────────────────────────────────
async def _ensure_content_tables() -> None:
    """Create style_sheets and product_specs if they don't exist (Alembic fallback)."""
    from sqlalchemy import text
    from app.core.database import engine
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS style_sheets (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    style_number VARCHAR(50) NOT NULL,
                    image_url VARCHAR(500),
                    pdf_url VARCHAR(500),
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT true,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS product_specs (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    title VARCHAR(200) NOT NULL,
                    description TEXT,
                    pdf_url VARCHAR(500),
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    is_active BOOLEAN NOT NULL DEFAULT true,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """))
            # Add tax columns to orders if missing (idempotent)
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='orders' AND column_name='tax_rate'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN tax_rate NUMERIC(6,4);
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='orders' AND column_name='tax_region'
                    ) THEN
                        ALTER TABLE orders ADD COLUMN tax_region VARCHAR(10);
                    END IF;
                END$$;
            """))
            # Add payment_method + ACH columns to orders if missing (idempotent)
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='payment_method') THEN
                        ALTER TABLE orders ADD COLUMN payment_method VARCHAR(20);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='ach_bank_name') THEN
                        ALTER TABLE orders ADD COLUMN ach_bank_name VARCHAR(255);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='ach_account_holder') THEN
                        ALTER TABLE orders ADD COLUMN ach_account_holder VARCHAR(255);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='ach_routing_number') THEN
                        ALTER TABLE orders ADD COLUMN ach_routing_number VARCHAR(20);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='ach_account_last4') THEN
                        ALTER TABLE orders ADD COLUMN ach_account_last4 VARCHAR(4);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='ach_account_type') THEN
                        ALTER TABLE orders ADD COLUMN ach_account_type VARCHAR(20);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='ach_verified') THEN
                        ALTER TABLE orders ADD COLUMN ach_verified BOOLEAN DEFAULT false;
                    END IF;
                END$$;
            """))
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='payment_terms') THEN
                        ALTER TABLE orders ADD COLUMN payment_terms VARCHAR(20) DEFAULT 'net_30';
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='invoice_sent_at') THEN
                        ALTER TABLE orders ADD COLUMN invoice_sent_at TIMESTAMPTZ;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='marked_paid_at') THEN
                        ALTER TABLE orders ADD COLUMN marked_paid_at TIMESTAMPTZ;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='marked_paid_by') THEN
                        ALTER TABLE orders ADD COLUMN marked_paid_by VARCHAR(255);
                    END IF;
                END$$;
            """))
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='amount_paid') THEN
                        ALTER TABLE orders ADD COLUMN amount_paid NUMERIC(10,2) DEFAULT 0.00;
                        UPDATE orders SET amount_paid = total WHERE payment_status = 'paid';
                    END IF;
                END$$;
            """))
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='label_url') THEN
                        ALTER TABLE orders ADD COLUMN label_url TEXT;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='orders' AND column_name='tracking_url') THEN
                        ALTER TABLE orders ADD COLUMN tracking_url TEXT;
                    END IF;
                END$$;
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS page_seo (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    page_slug VARCHAR(100) UNIQUE NOT NULL,
                    meta_title VARCHAR(60),
                    meta_description VARCHAR(160),
                    keywords TEXT,
                    og_image_url TEXT,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS blog_posts (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    title VARCHAR(255) NOT NULL,
                    slug VARCHAR(255) UNIQUE NOT NULL,
                    cover_image_url TEXT,
                    published_date DATE,
                    read_time VARCHAR(50),
                    excerpt TEXT,
                    article_body JSONB,
                    faq JSONB,
                    tags TEXT[],
                    status VARCHAR(20) NOT NULL DEFAULT 'draft',
                    meta_title VARCHAR(60),
                    meta_description VARCHAR(160),
                    keywords TEXT,
                    og_image_url TEXT,
                    created_at TIMESTAMPTZ DEFAULT now(),
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
            """))
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='product_variants' AND column_name='weight_grams'
                    ) THEN
                        ALTER TABLE product_variants ADD COLUMN weight_grams FLOAT;
                    END IF;
                END$$;
            """))
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name='products' AND column_name='highlight_text'
                    ) THEN
                        ALTER TABLE products ADD COLUMN highlight_text TEXT;
                    END IF;
                END$$;
            """))
            # Fix wholesale_applications column type mismatches — DB may have
            # created these as wrong types vs what the model expects (VARCHAR).
            await conn.execute(text("""
                DO $$
                BEGIN
                    -- business_type: DB created as ENUM, model expects VARCHAR
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'wholesale_applications'
                          AND column_name = 'business_type'
                          AND data_type = 'USER-DEFINED'
                    ) THEN
                        ALTER TABLE wholesale_applications
                            ALTER COLUMN business_type TYPE VARCHAR(100)
                            USING business_type::text;
                    END IF;
                    -- estimated_annual_volume: DB created as NUMERIC, model expects VARCHAR
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'wholesale_applications'
                          AND column_name = 'estimated_annual_volume'
                          AND data_type = 'numeric'
                    ) THEN
                        ALTER TABLE wholesale_applications
                            ALTER COLUMN estimated_annual_volume TYPE VARCHAR(100)
                            USING estimated_annual_volume::text;
                    END IF;
                END$$;
            """))
            # ── Purchase Order tables ──────────────────────────────────────────
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS manufacturers (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name VARCHAR(255) NOT NULL,
                    contact_name VARCHAR(255),
                    email VARCHAR(255),
                    phone VARCHAR(50),
                    address TEXT,
                    notes TEXT,
                    is_active BOOLEAN DEFAULT true,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS purchase_orders (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    po_number VARCHAR(50) UNIQUE NOT NULL DEFAULT '',
                    manufacturer_id UUID REFERENCES manufacturers(id),
                    status VARCHAR(50) DEFAULT 'draft',
                    order_date DATE DEFAULT CURRENT_DATE,
                    expected_delivery DATE,
                    notes TEXT,
                    total_expected DECIMAL(10,2) DEFAULT 0,
                    total_received DECIMAL(10,2) DEFAULT 0,
                    qb_synced BOOLEAN DEFAULT false,
                    qb_po_id VARCHAR(255),
                    qb_bill_id VARCHAR(255),
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS po_line_items (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    po_id UUID REFERENCES purchase_orders(id) ON DELETE CASCADE,
                    product_variant_id UUID REFERENCES product_variants(id),
                    new_product_name VARCHAR(255),
                    new_product_sku VARCHAR(255),
                    new_product_size VARCHAR(50),
                    new_product_color VARCHAR(50),
                    qty_ordered INTEGER NOT NULL DEFAULT 0,
                    unit_cost_expected DECIMAL(10,2) NOT NULL DEFAULT 0,
                    total_expected DECIMAL(10,2) GENERATED ALWAYS AS (qty_ordered * unit_cost_expected) STORED,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS po_receivings (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    po_id UUID REFERENCES purchase_orders(id) ON DELETE CASCADE,
                    received_date DATE DEFAULT CURRENT_DATE,
                    notes TEXT,
                    qb_bill_id VARCHAR(255),
                    qb_synced BOOLEAN DEFAULT false,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS po_receiving_items (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    receiving_id UUID REFERENCES po_receivings(id) ON DELETE CASCADE,
                    po_line_item_id UUID REFERENCES po_line_items(id),
                    qty_received INTEGER NOT NULL DEFAULT 0,
                    unit_cost_actual DECIMAL(10,2) NOT NULL DEFAULT 0,
                    total_actual DECIMAL(10,2) GENERATED ALWAYS AS (qty_received * unit_cost_actual) STORED,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            await conn.execute(text("""
                CREATE OR REPLACE FUNCTION generate_po_number()
                RETURNS TRIGGER AS $$
                DECLARE
                    year_part TEXT;
                    seq_num INTEGER;
                    new_po_number TEXT;
                BEGIN
                    year_part := TO_CHAR(NOW(), 'YYYY');
                    SELECT COUNT(*) + 1 INTO seq_num
                    FROM purchase_orders
                    WHERE EXTRACT(YEAR FROM created_at) = EXTRACT(YEAR FROM NOW());
                    new_po_number := 'PO-' || year_part || '-' || LPAD(seq_num::TEXT, 3, '0');
                    NEW.po_number := new_po_number;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql
            """))
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_trigger WHERE tgname = 'set_po_number'
                    ) THEN
                        CREATE TRIGGER set_po_number
                            BEFORE INSERT ON purchase_orders
                            FOR EACH ROW
                            EXECUTE FUNCTION generate_po_number();
                    END IF;
                END$$;
            """))
        print("Content tables: OK")
    except Exception as exc:
        print(f"Content tables warning (non-fatal): {exc}")


# ── Email templates seed ──────────────────────────────────────────────────────
async def _seed_email_templates() -> None:
    """Insert default email templates if they don't exist."""
    from sqlalchemy import text
    from app.core.database import engine
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                INSERT INTO email_templates 
                (id, trigger_event, name, subject, body_html, body_text, is_active, available_variables, created_at, updated_at)
                SELECT gen_random_uuid(), t.trigger_event::email_trigger_event, t.name, t.subject, t.body_html, t.body_text, true, t.available_variables::jsonb, NOW(), NOW()
                FROM (VALUES
                    (
                        'order_confirmation',
                        'Order Confirmation',
                        'Order Confirmed — {{ order_number }}',
                        '<h1>Thanks {{ first_name }}!</h1><p>Order <b>{{ order_number }}</b> received.</p><p>Total: {{ order_total }}</p><p><a href="{{ order_url }}">View Order</a></p><p>— AF Apparels</p>',
                        'Order {{ order_number }} confirmed. Total: {{ order_total }}.',
                        '["first_name","order_number","order_total","order_url","items"]'
                    ),
                    (
                        'order_shipped',
                        'Order Shipped',
                        'Your Order {{ order_number }} Has Shipped!',
                        '<h1>Your Order is On Its Way! 🚚</h1><p>Hi {{ first_name }},</p><p>Order <b>{{ order_number }}</b> has shipped.</p><p>Courier: <b>{{ courier }}</b></p><p>Tracking: <b>{{ tracking_number }}</b></p><p>— AF Apparels</p>',
                        'Order {{ order_number }} shipped. Tracking: {{ tracking_number }}',
                        '["first_name","order_number","courier","tracking_number"]'
                    ),
                    (
                        'wholesale_approved',
                        'Wholesale Application Approved',
                        'Your Wholesale Account is Approved!',
                        '<h1>Welcome to AF Apparels Wholesale! 🎉</h1><p>Hi {{ first_name }},</p><p>Your account for <b>{{ company_name }}</b> has been approved.</p><a href="{{ login_url }}" style="background:#E8242A;color:#fff;padding:12px 24px;text-decoration:none;border-radius:6px;display:inline-block;margin-top:16px;font-weight:700">Log In Now →</a><p>— AF Apparels</p>',
                        'Hi {{ first_name }}, your wholesale account for {{ company_name }} is approved!',
                        '["first_name","company_name","login_url"]'
                    ),
                    (
                        'wholesale_rejected',
                        'Wholesale Application Update',
                        'Update on Your Wholesale Application',
                        '<h1>Application Update</h1><p>Hi {{ first_name }},</p><p>Unfortunately we are unable to approve your wholesale application for <b>{{ company_name }}</b> at this time.</p><p>Reason: {{ reason }}</p><p>Questions? Call (214) 272-7213</p><p>— AF Apparels</p>',
                        'Hi {{ first_name }}, your application for {{ company_name }} was not approved. Reason: {{ reason }}',
                        '["first_name","company_name","reason"]'
                    ),
                    (
                        'password_reset',
                        'Password Reset',
                        'Reset Your AF Apparels Password',
                        '<h1>Password Reset</h1><p>Hi {{ first_name }},</p><p><a href="{{ reset_url }}" style="background:#E8242A;color:#fff;padding:12px 24px;text-decoration:none;border-radius:6px;">Reset Password</a></p><p>Expires in {{ expiry_hours }} hour(s).</p><p>— AF Apparels</p>',
                        'Hi {{ first_name }}, reset here: {{ reset_url }}',
                        '["first_name","reset_url","expiry_hours"]'
                    ),
                    (
                        'welcome',
                        'Welcome to AF Apparels',
                        'Welcome to AF Apparels Wholesale!',
                        '<h1>Welcome, {{ first_name }}! 👋</h1><p>Your account is ready. Start browsing our wholesale catalog.</p><a href="{{ shop_url }}" style="background:#1A5CFF;color:#fff;padding:12px 24px;text-decoration:none;border-radius:6px;display:inline-block;margin-top:16px;">Shop Now →</a><p>— AF Apparels</p>',
                        'Welcome {{ first_name }}! Your AF Apparels wholesale account is ready.',
                        '["first_name","shop_url"]'
                    ),
                    (
                        'payment_failed',
                        'Payment Failed',
                        'Payment Failed for Order {{ order_number }}',
                        '<h1>Payment Issue</h1><p>Hi {{ first_name }},</p><p>Payment for order <b>{{ order_number }}</b> failed.</p><p>Please update your payment method.</p><a href="{{ account_url }}" style="background:#E8242A;color:#fff;padding:12px 24px;text-decoration:none;border-radius:6px;display:inline-block;margin-top:16px;">Update Payment →</a><p>— AF Apparels</p>',
                        'Hi {{ first_name }}, payment failed for order {{ order_number }}.',
                        '["first_name","order_number","account_url"]'
                    )
                ) AS t(trigger_event, name, subject, body_html, body_text, available_variables)
                WHERE NOT EXISTS (
                    SELECT 1 FROM email_templates 
                    WHERE email_templates.trigger_event::text = t.trigger_event
                )
            """))
        print("Email templates seeded successfully.")
    except Exception as exc:
        print(f"Email template seed warning (non-fatal): {exc}")


# ── App factory ───────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    # Run DB migrations before accepting traffic
    try:
        import subprocess
        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            capture_output=True,
            text=True,
            cwd="/app",
        )
        if result.stdout:
            print("Migration stdout:", result.stdout[-2000:])
        if result.stderr:
            print("Migration stderr:", result.stderr[-2000:])
        if result.returncode != 0:
            print(f"Migration exited {result.returncode} (non-fatal — app will continue)")
    except Exception as exc:
        print(f"Migration error (non-fatal): {exc}")

    assert await check_db_connection(), "Database connection failed on startup"
    assert await check_redis_connection(), "Redis connection failed on startup"

    # Ensure content tables exist (fallback in case Alembic migration didn't run)
    await _ensure_content_tables()

    # Seed email templates
    await _seed_email_templates()

    yield


app = FastAPI(
    title="AF Apparels B2B Wholesale API",
    description="B2B wholesale e-commerce platform API",
    version="1.0.0",
    docs_url="/docs" if settings.APP_ENV != "production" else None,
    redoc_url="/redoc" if settings.APP_ENV != "production" else None,
    lifespan=lifespan,
)

# ── Custom middleware ─────────────────────────────────────────────────────────
app.add_middleware(AuditMiddleware)
# PricingMiddleware must be added BEFORE AuthMiddleware so it runs AFTER Auth
# (last added = outermost = runs first on request; so Auth → Pricing → Audit → routes)
app.add_middleware(PricingMiddleware)
app.add_middleware(AuthMiddleware)


# ── Global exception handlers ─────────────────────────────────────────────────
@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.error_code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if settings.DEBUG:
        raise exc
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred"}},
    )


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health_check() -> dict:
    db_ok = await check_db_connection()
    redis_ok = await check_redis_connection()
    return {
        "status": "ok" if (db_ok and redis_ok) else "degraded",
        "version": "1.0.0",
        "db": "ok" if db_ok else "error",
        "redis": "ok" if redis_ok else "error",
    }


# ── Routers ───────────────────────────────────────────────────────────────────
from app.api.v1 import auth, products, cart, checkout, orders, account, webhooks, reviews, discounts, guest, contact, style_sheets, product_specs, upload, tax_rate, tax, pages_seo, blog_posts, shipping as public_shipping  # noqa: E402
from app.api.v1.admin import (  # noqa: E402
    customers,
    pricing as admin_pricing,
    shipping as admin_shipping,
    settings as admin_settings,
    orders as admin_orders,
    reports as admin_reports,
    quickbooks as admin_quickbooks,
    products as admin_products,
    inventory as admin_inventory,
    reviews as admin_reviews,
    discount_groups as admin_discount_groups,
    discounts as admin_discounts,
    users as admin_users,
    analytics as admin_analytics,
    taxes as admin_taxes,
    style_sheets as admin_style_sheets,
    product_specs as admin_product_specs,
    pages_seo as admin_pages_seo,
    blog_posts as admin_blog_posts,
    purchase_orders as admin_purchase_orders,
)

_cors_origins = list({settings.FRONTEND_URL, *settings.allowed_origins_list} - {""})
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_origin_regex=r"https://(.*\.vercel\.app|.*\.up\.railway\.app|.*\.railway\.app)",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

_V1 = "/api/v1"

app.include_router(auth.router, prefix=_V1)
app.include_router(products.router, prefix=_V1)
app.include_router(reviews.router, prefix=_V1)
app.include_router(cart.router, prefix=_V1)
app.include_router(checkout.router, prefix=_V1)
app.include_router(orders.router, prefix=_V1)
app.include_router(account.router, prefix=_V1)
app.include_router(webhooks.router, prefix=_V1)
app.include_router(discounts.router, prefix=_V1)
app.include_router(guest.router, prefix=_V1)
app.include_router(contact.router, prefix=_V1)
app.include_router(style_sheets.router, prefix=_V1)
app.include_router(product_specs.router, prefix=_V1)
app.include_router(upload.router, prefix=_V1)
app.include_router(tax_rate.router, prefix=_V1)
app.include_router(tax.router, prefix=_V1)
app.include_router(public_shipping.router, prefix=_V1)

app.include_router(customers.router, prefix=f"{_V1}/admin")
app.include_router(admin_pricing.router, prefix=_V1)
app.include_router(admin_shipping.router, prefix=_V1)
app.include_router(admin_settings.router, prefix=_V1)
app.include_router(admin_orders.router, prefix=_V1)
app.include_router(admin_reports.router, prefix=_V1)
app.include_router(admin_quickbooks.router, prefix=_V1)
app.include_router(admin_products.router, prefix=_V1)
app.include_router(admin_inventory.router, prefix=_V1)
app.include_router(admin_reviews.router, prefix=_V1)
app.include_router(admin_discount_groups.router, prefix=_V1)
app.include_router(admin_discounts.router, prefix=_V1)
app.include_router(admin_users.router, prefix=_V1)
app.include_router(admin_analytics.router, prefix=_V1)
app.include_router(admin_taxes.router, prefix=_V1)
app.include_router(admin_style_sheets.router, prefix=_V1)
app.include_router(admin_product_specs.router, prefix=_V1)
app.include_router(pages_seo.router, prefix=_V1)
app.include_router(blog_posts.router, prefix=_V1)
app.include_router(admin_pages_seo.router, prefix=_V1)
app.include_router(admin_blog_posts.router, prefix=_V1)
app.include_router(admin_purchase_orders.router, prefix=f"{_V1}/admin/purchase-orders", tags=["purchase-orders"])

# ── Debug: log all registered routes at import time ──────────────────────────
for _route in app.routes:
    _path = getattr(_route, "path", "?")
    _methods = getattr(_route, "methods", None)
    print(f"[ROUTE] {','.join(_methods) if _methods else 'MOUNT'} {_path}")

# Static files
os.makedirs("/app/media", exist_ok=True)
app.mount("/media", StaticFiles(directory="/app/media"), name="media")