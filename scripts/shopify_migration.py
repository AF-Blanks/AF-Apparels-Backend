"""
Shopify → AF Apparels PostgreSQL migration script.

Usage:
    python shopify_migration.py --customers customers_export.csv
    python shopify_migration.py --orders orders_export.csv
    python shopify_migration.py --customers customers_export.csv --orders orders_export.csv
    python shopify_migration.py --customers customers_export.csv --dry-run

See MIGRATION.md for full usage guide.
"""

import argparse
import csv
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Tag prefix used in Shopify to identify pricing tier  (e.g. "tier-2")
TIER_TAG_PREFIX = "tier-"

# Map Shopify tag suffix → AF Apparels pricing tier name (case-insensitive name lookup)
# If not found, the customer is left without a tier.
TIER_TAG_MAP = {
    "1": "Tier 1",
    "2": "Tier 2",
    "3": "Tier 3",
    "4": "Tier 4",
}

# Shopify fulfillment status → AF Apparels order status
FULFILLMENT_STATUS_MAP = {
    "fulfilled": "shipped",
    "partial": "processing",
    "unfulfilled": "processing",
    "restocked": "cancelled",
    "": "processing",
    None: "processing",
}

# Shopify financial status → AF Apparels payment_status
FINANCIAL_STATUS_MAP = {
    "paid": "paid",
    "pending": "pending",
    "refunded": "refunded",
    "voided": "cancelled",
    "partially_refunded": "refunded",
    "partially_paid": "pending",
    "": "pending",
    None: "pending",
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_connection() -> psycopg2.extensions.connection:
    # Try backend/.env first, then environment
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit(
            "DATABASE_URL not set. Export it or add it to backend/.env"
        )

    # psycopg2 wants postgresql://, not postgres://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    return psycopg2.connect(db_url, cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_migration_columns(cur: psycopg2.extensions.cursor) -> None:
    """Add migration-specific columns that don't exist yet (idempotent)."""
    ddl_statements = [
        # users
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='users' AND column_name='shopify_customer_id'
            ) THEN
                ALTER TABLE users ADD COLUMN shopify_customer_id BIGINT;
            END IF;
        END $$;
        """,
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='users' AND column_name='requires_password_reset'
            ) THEN
                ALTER TABLE users ADD COLUMN requires_password_reset BOOLEAN NOT NULL DEFAULT FALSE;
            END IF;
        END $$;
        """,
        # orders
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='orders' AND column_name='shopify_order_id'
            ) THEN
                ALTER TABLE orders ADD COLUMN shopify_order_id BIGINT;
            END IF;
        END $$;
        """,
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='orders' AND column_name='is_migrated'
            ) THEN
                ALTER TABLE orders ADD COLUMN is_migrated BOOLEAN NOT NULL DEFAULT FALSE;
            END IF;
        END $$;
        """,
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='orders' AND column_name='discount_code'
            ) THEN
                ALTER TABLE orders ADD COLUMN discount_code VARCHAR(100);
            END IF;
        END $$;
        """,
        """
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='orders' AND column_name='discount_amount'
            ) THEN
                ALTER TABLE orders ADD COLUMN discount_amount NUMERIC(10,2) NOT NULL DEFAULT 0;
            END IF;
        END $$;
        """,
    ]
    for stmt in ddl_statements:
        cur.execute(stmt)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _dec(value: str | None, default: float = 0.0) -> float:
    """Parse a decimal string; return default on failure."""
    if not value:
        return default
    try:
        return float(value.replace(",", "").strip())
    except ValueError:
        return default


def _parse_shopify_ts(ts_str: str | None) -> datetime | None:
    """Parse Shopify timestamp like '2026-05-08 08:54:55 -0600'."""
    if not ts_str or not ts_str.strip():
        return None
    ts_str = ts_str.strip()
    # Normalize offset: remove space before ±
    ts_str = re.sub(r"\s([+-]\d{4})$", r"\1", ts_str)
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(ts_str, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _extract_tier_tag(tags_str: str | None) -> str | None:
    """Return the first 'tier-N' suffix found in a comma-separated tags string."""
    if not tags_str:
        return None
    for tag in tags_str.split(","):
        tag = tag.strip().lower()
        if tag.startswith(TIER_TAG_PREFIX):
            suffix = tag[len(TIER_TAG_PREFIX):]
            return suffix
    return None


def _parse_lineitem_color_size(lineitem_name: str) -> tuple[str | None, str | None]:
    """
    Extract color and size from Shopify line-item name.
    Format: "Product Name - Color - Size"  (last two dash-separated segments)
    """
    parts = [p.strip() for p in lineitem_name.split(" - ")]
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    if len(parts) == 2:
        return None, parts[-1]
    return None, None


# ---------------------------------------------------------------------------
# Customer migration
# ---------------------------------------------------------------------------

def migrate_customers(
    cur: psycopg2.extensions.cursor,
    csv_path: str,
    dry_run: bool,
    errors: list[dict],
) -> dict[str, int]:
    stats = {"processed": 0, "created": 0, "skipped": 0, "errors": 0}

    # Pre-load pricing tiers: name → id
    cur.execute("SELECT id, name FROM pricing_tiers WHERE is_active = TRUE")
    tier_rows = cur.fetchall()
    tier_name_to_id: dict[str, str] = {r["name"].lower(): str(r["id"]) for r in tier_rows}

    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            stats["processed"] += 1
            email = (row.get("Email") or "").strip().lower()
            if not email:
                stats["skipped"] += 1
                continue

            try:
                _migrate_single_customer(
                    cur, row, email, tier_name_to_id, dry_run
                )
                stats["created"] += 1
            except Exception as exc:
                stats["errors"] += 1
                errors.append({
                    "source": "customers",
                    "identifier": email or row.get("Customer ID", ""),
                    "error": str(exc),
                })
                if not dry_run:
                    cur.connection.rollback()

    return stats


def _migrate_single_customer(
    cur: psycopg2.extensions.cursor,
    row: dict,
    email: str,
    tier_name_to_id: dict[str, str],
    dry_run: bool,
) -> None:
    shopify_customer_id = row.get("Customer ID", "").strip() or None
    first_name = (row.get("First Name") or "").strip()
    last_name = (row.get("Last Name") or "").strip()
    phone = (row.get("Phone") or row.get("Default Address Phone") or "").strip() or None
    tax_exempt_raw = (row.get("Tax Exempt") or "").strip().lower()
    tax_exempt = tax_exempt_raw in ("yes", "true", "1")
    tags_str = row.get("Tags", "")

    # Company info
    company_name = (row.get("Default Address Company") or "").strip()
    if not company_name:
        company_name = f"{first_name} {last_name}".strip() or email

    address1 = (row.get("Default Address Address1") or "").strip() or None
    address2 = (row.get("Default Address Address2") or "").strip() or None
    city = (row.get("Default Address City") or "").strip() or None
    state = (row.get("Default Address Province Code") or "").strip() or None
    country = (row.get("Default Address Country Code") or "").strip() or "US"
    postal_code = (row.get("Default Address Zip") or "").strip() or None
    company_phone = (row.get("Default Address Phone") or phone or "").strip() or None

    # Resolve pricing tier
    tier_suffix = _extract_tier_tag(tags_str)
    pricing_tier_id: str | None = None
    if tier_suffix:
        tier_display_name = TIER_TAG_MAP.get(tier_suffix, "")
        pricing_tier_id = tier_name_to_id.get(tier_display_name.lower())
        if not pricing_tier_id:
            # Fuzzy: try "tier N" or "tier-N" match
            for k, v in tier_name_to_id.items():
                if tier_suffix in k:
                    pricing_tier_id = v
                    break

    # Check if user already exists
    cur.execute("SELECT id FROM users WHERE email = %s", (email,))
    existing_user = cur.fetchone()
    if existing_user:
        return  # skip duplicate

    if dry_run:
        print(f"  [DRY RUN] Would create user: {email} / company: {company_name}")
        return

    now = datetime.now(timezone.utc)
    user_id = str(uuid.uuid4())

    # Insert user (no real password — migrated users must reset)
    cur.execute(
        """
        INSERT INTO users (
            id, email, hashed_password, first_name, last_name, phone,
            account_type, is_active, email_verified,
            shopify_customer_id, requires_password_reset, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            'wholesale', TRUE, TRUE,
            %s, TRUE, %s, %s
        )
        """,
        (
            user_id, email,
            "$migrated$no_password_set",  # sentinel; user must reset
            first_name, last_name, phone,
            int(shopify_customer_id) if shopify_customer_id else None,
            now, now,
        ),
    )

    # Check if company already exists by name
    cur.execute("SELECT id FROM companies WHERE name = %s", (company_name,))
    existing_company = cur.fetchone()

    if existing_company:
        company_id = str(existing_company["id"])
    else:
        company_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO companies (
                id, name, address_line1, address_line2, city, state_province,
                postal_code, country, phone, pricing_tier_id, status, tax_exempt,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, 'approved', %s,
                %s, %s
            )
            """,
            (
                company_id, company_name, address1, address2, city, state,
                postal_code, country, company_phone, pricing_tier_id, tax_exempt,
                now, now,
            ),
        )

    # Link user → company
    cur.execute(
        """
        INSERT INTO company_users (company_id, user_id, role, is_active)
        VALUES (%s, %s, 'owner', TRUE)
        ON CONFLICT (company_id, user_id) DO NOTHING
        """,
        (company_id, user_id),
    )

    cur.connection.commit()


# ---------------------------------------------------------------------------
# Order migration
# ---------------------------------------------------------------------------

def migrate_orders(
    cur: psycopg2.extensions.cursor,
    csv_path: str,
    dry_run: bool,
    errors: list[dict],
) -> dict[str, int]:
    stats = {"processed": 0, "created": 0, "skipped": 0, "errors": 0}

    # Group rows by order Name (#XXXX)
    orders: dict[str, list[dict]] = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = (row.get("Name") or "").strip()
            if not name:
                continue
            orders.setdefault(name, []).append(row)

    stats["processed"] = len(orders)

    for order_name, rows in orders.items():
        first = rows[0]
        try:
            created = _migrate_single_order(cur, order_name, first, rows, dry_run)
            if created:
                stats["created"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:
            stats["errors"] += 1
            errors.append({
                "source": "orders",
                "identifier": order_name,
                "error": str(exc),
            })
            if not dry_run:
                cur.connection.rollback()

    return stats


def _migrate_single_order(
    cur: psycopg2.extensions.cursor,
    order_name: str,
    first: dict,
    all_rows: list[dict],
    dry_run: bool,
) -> bool:
    """Return True if order was created, False if skipped."""

    email = (first.get("Email") or "").strip().lower()
    shopify_order_id_str = (first.get("Id") or "").strip().lstrip("#")
    shopify_order_id = int(shopify_order_id_str) if shopify_order_id_str.isdigit() else None

    # Skip if already migrated
    if shopify_order_id:
        cur.execute(
            "SELECT id FROM orders WHERE shopify_order_id = %s", (shopify_order_id,)
        )
        if cur.fetchone():
            return False

    # Also skip by order_number
    order_number = order_name.lstrip("#")
    cur.execute(
        "SELECT id FROM orders WHERE order_number = %s", (f"ORD-{order_number}",)
    )
    if cur.fetchone():
        return False

    # Resolve user/company
    user_id: str | None = None
    company_id: str | None = None
    if email:
        cur.execute("SELECT id FROM users WHERE email = %s", (email,))
        u = cur.fetchone()
        if u:
            user_id = str(u["id"])
            cur.execute(
                "SELECT company_id FROM company_users WHERE user_id = %s AND is_active = TRUE LIMIT 1",
                (user_id,),
            )
            cu = cur.fetchone()
            if cu:
                company_id = str(cu["company_id"])

    # Financials
    subtotal = _dec(first.get("Subtotal"))
    shipping_cost = _dec(first.get("Shipping"))
    tax_amount = _dec(first.get("Taxes"))
    total = _dec(first.get("Total"))
    discount_code = (first.get("Discount Code") or "").strip() or None
    discount_amount = _dec(first.get("Discount Amount"))

    # Status
    fulfillment_raw = (first.get("Fulfillment Status") or "").strip().lower()
    financial_raw = (first.get("Financial Status") or "").strip().lower()
    order_status = FULFILLMENT_STATUS_MAP.get(fulfillment_raw, "processing")
    payment_status = FINANCIAL_STATUS_MAP.get(financial_raw, "pending")

    # Handle cancelled
    if first.get("Cancelled at", "").strip():
        order_status = "cancelled"

    # Timestamps
    created_at = _parse_shopify_ts(first.get("Created at")) or datetime.now(timezone.utc)

    # Payment method
    payment_method = (first.get("Payment Method") or "").strip().lower() or None
    if payment_method in ("custom", "manual"):
        payment_method = "net_30"
    elif "credit" in (payment_method or ""):
        payment_method = "credit_card"

    # Notes
    notes = (first.get("Notes") or "").strip() or None

    # Shipping address (stored in notes if no address table)
    shipping_name = (first.get("Shipping Name") or "").strip()
    shipping_addr = " ".join(filter(None, [
        first.get("Shipping Address1", "").strip(),
        first.get("Shipping Address2", "").strip(),
        first.get("Shipping City", "").strip(),
        first.get("Shipping Province", "").strip(),
        first.get("Shipping Zip", "").strip(),
        first.get("Shipping Country", "").strip(),
    ]))

    if dry_run:
        print(
            f"  [DRY RUN] Would create order {order_name} "
            f"(user={email}, total={total}, items={len(all_rows)})"
        )
        return True

    now = datetime.now(timezone.utc)
    order_id = str(uuid.uuid4())

    cur.execute(
        """
        INSERT INTO orders (
            id, order_number, company_id, placed_by_id, guest_email, is_guest_order,
            status, payment_status, subtotal, shipping_cost, tax_amount, total,
            discount_code, discount_amount, notes, payment_method,
            shopify_order_id, is_migrated, created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, TRUE, %s, %s
        )
        """,
        (
            order_id,
            f"ORD-{order_number}",
            company_id,
            user_id,
            email if not user_id else None,
            user_id is None,
            order_status, payment_status,
            subtotal, shipping_cost, tax_amount, total,
            discount_code, discount_amount,
            notes, payment_method,
            shopify_order_id,
            created_at, now,
        ),
    )

    # Insert line items
    for row in all_rows:
        qty_str = (row.get("Lineitem quantity") or "").strip()
        if not qty_str:
            continue
        try:
            qty = int(qty_str)
        except ValueError:
            qty = 1

        lineitem_name = (row.get("Lineitem name") or "").strip()
        sku = (row.get("Lineitem sku") or "").strip() or None
        unit_price = _dec(row.get("Lineitem price"))
        line_total = unit_price * qty

        color, size = _parse_lineitem_color_size(lineitem_name)

        # Try to find variant by SKU
        variant_id: str | None = None
        if sku:
            cur.execute(
                "SELECT id FROM product_variants WHERE sku = %s LIMIT 1", (sku,)
            )
            v = cur.fetchone()
            if v:
                variant_id = str(v["id"])

        # Extract product name (everything before last two " - " segments)
        name_parts = [p.strip() for p in lineitem_name.split(" - ")]
        product_name = " - ".join(name_parts[:-2]) if len(name_parts) >= 3 else lineitem_name

        cur.execute(
            """
            INSERT INTO order_items (
                id, order_id, variant_id, quantity, unit_price, line_total,
                product_name, sku, color, size
            ) VALUES (
                %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s
            )
            """,
            (
                str(uuid.uuid4()), order_id, variant_id, qty, unit_price, line_total,
                product_name, sku, color, size,
            ),
        )

    cur.connection.commit()
    return True


# ---------------------------------------------------------------------------
# Error CSV writer
# ---------------------------------------------------------------------------

def write_errors_csv(errors: list[dict], path: str) -> None:
    if not errors:
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["source", "identifier", "error"])
        writer.writeheader()
        writer.writerows(errors)
    print(f"\nErrors written to: {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate Shopify CSV exports into AF Apparels PostgreSQL database."
    )
    parser.add_argument("--customers", metavar="CSV", help="Path to Shopify customers export CSV")
    parser.add_argument("--orders", metavar="CSV", help="Path to Shopify orders export CSV")
    parser.add_argument(
        "--customers-only", action="store_true",
        help="Migrate customers only (shorthand, requires --customers)",
    )
    parser.add_argument(
        "--orders-only", action="store_true",
        help="Migrate orders only (shorthand, requires --orders)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be migrated without writing to the database",
    )
    parser.add_argument(
        "--errors-csv", metavar="PATH", default="migration_errors.csv",
        help="Output path for failed-row CSV (default: migration_errors.csv)",
    )
    args = parser.parse_args()

    run_customers = bool(args.customers)
    run_orders = bool(args.orders)

    if args.customers_only and not args.customers:
        parser.error("--customers-only requires --customers <CSV>")
    if args.orders_only and not args.orders:
        parser.error("--orders-only requires --orders <CSV>")
    if args.customers_only:
        run_orders = False
    if args.orders_only:
        run_customers = False

    if not run_customers and not run_orders:
        parser.print_help()
        sys.exit(1)

    print("Connecting to database…")
    conn = get_connection()
    cur = conn.cursor()

    print("Applying schema migrations…")
    ensure_migration_columns(cur)
    if not args.dry_run:
        conn.commit()

    errors: list[dict] = []
    all_stats: dict[str, Any] = {}

    if run_customers and args.customers:
        print(f"\nMigrating customers from: {args.customers}")
        cstats = migrate_customers(cur, args.customers, args.dry_run, errors)
        all_stats["customers"] = cstats
        print(
            f"  Customers → processed={cstats['processed']}, "
            f"created={cstats['created']}, skipped={cstats['skipped']}, "
            f"errors={cstats['errors']}"
        )

    if run_orders and args.orders:
        print(f"\nMigrating orders from: {args.orders}")
        ostats = migrate_orders(cur, args.orders, args.dry_run, errors)
        all_stats["orders"] = ostats
        print(
            f"  Orders    → processed={ostats['processed']}, "
            f"created={ostats['created']}, skipped={ostats['skipped']}, "
            f"errors={ostats['errors']}"
        )

    cur.close()
    conn.close()

    write_errors_csv(errors, args.errors_csv)

    print("\nMigration complete.")
    if args.dry_run:
        print("(Dry run — no data was written.)")


if __name__ == "__main__":
    main()
