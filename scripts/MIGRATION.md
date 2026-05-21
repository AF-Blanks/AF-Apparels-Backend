# Shopify Migration Guide

Migrates customers and orders from Shopify CSV exports into the AF Apparels PostgreSQL database.

## Prerequisites

```bash
cd backend
pip install psycopg2-binary python-dotenv
```

`DATABASE_URL` must be set — either exported in your shell or present in `backend/.env`:

```
DATABASE_URL=postgresql://user:password@localhost:5432/af_apparels
```

## Export CSVs from Shopify

1. **Customers**: Shopify Admin → Customers → Export → All customers → CSV for Excel, Numbers, or other spreadsheet apps
2. **Orders**: Shopify Admin → Orders → Export → All orders → CSV for Excel, Numbers, or other spreadsheet apps

Place both files anywhere accessible; pass their paths via `--customers` / `--orders`.

## Usage

Run from the `backend/` directory:

```bash
# Migrate both customers and orders
python scripts/shopify_migration.py \
  --customers /path/to/customers_export.csv \
  --orders    /path/to/orders_export.csv

# Customers only
python scripts/shopify_migration.py \
  --customers customers_export.csv --customers-only

# Orders only (customers must already exist for linking)
python scripts/shopify_migration.py \
  --orders orders_export.csv --orders-only

# Dry run (no DB writes — prints what would happen)
python scripts/shopify_migration.py \
  --customers customers_export.csv \
  --orders    orders_export.csv \
  --dry-run

# Custom errors output file
python scripts/shopify_migration.py \
  --customers customers_export.csv \
  --errors-csv my_errors.csv
```

## What the script does

### Schema migrations (always run, idempotent)

Adds the following columns if they don't exist:

| Table    | Column                    | Type        |
|----------|---------------------------|-------------|
| users    | `shopify_customer_id`     | BIGINT      |
| users    | `requires_password_reset` | BOOLEAN     |
| orders   | `shopify_order_id`        | BIGINT      |
| orders   | `is_migrated`             | BOOLEAN     |
| orders   | `discount_code`           | VARCHAR     |
| orders   | `discount_amount`         | NUMERIC     |

### Customer migration

For each row in the customers CSV:

1. Skips if a user with the same `email` already exists.
2. Creates a `users` row (`account_type='wholesale'`, `is_active=TRUE`, `requires_password_reset=TRUE`).
   - Password is set to the sentinel `$migrated$no_password_set` — users **must** reset via the Forgot Password flow.
3. Creates a `companies` row using the "Default Address Company" field (or falls back to the customer's full name).
   - Skips company creation if a company with the same name already exists; links the user to the existing company instead.
4. Looks up the pricing tier from Shopify tags (e.g. tag `tier-2` → AF Apparels "Tier 2").
5. Creates a `company_users` link with `role='owner'`.

### Order migration

Orders are grouped by the Shopify "Name" column (e.g. `#2779`). For each order:

1. Skips if `shopify_order_id` or `ORD-XXXX` order number already exists in the DB.
2. Resolves `placed_by_id` and `company_id` by looking up the order's email in `users`.
3. Maps Shopify `Fulfillment Status` → AF Apparels `status`.
4. Maps Shopify `Financial Status` → AF Apparels `payment_status`.
5. Inserts one `orders` row and one `order_items` row per CSV line belonging to that order.
6. Attempts to resolve `variant_id` from the line-item SKU; leaves it `NULL` if not found.

### Status mapping

| Shopify Fulfillment Status | AF Apparels Order Status |
|----------------------------|--------------------------|
| fulfilled                  | shipped                  |
| partial                    | processing               |
| unfulfilled                | processing               |
| restocked                  | cancelled                |
| (empty)                    | processing               |

| Shopify Financial Status | AF Apparels Payment Status |
|--------------------------|----------------------------|
| paid                     | paid                       |
| pending                  | pending                    |
| refunded                 | refunded                   |
| voided                   | cancelled                  |
| partially_refunded       | refunded                   |
| partially_paid           | pending                    |

### Error handling

- Rows that fail are written to `migration_errors.csv` (or the path given by `--errors-csv`).
- Each failed row is rolled back individually; successful rows are committed immediately.
- A summary is printed at the end showing processed / created / skipped / error counts.

## Post-migration steps

1. Send a **password reset email** to all migrated customers:
   - Filter `users` where `requires_password_reset = TRUE`.
   - Trigger the existing `/api/v1/auth/forgot-password` flow for each.

2. Verify pricing tiers are assigned correctly by checking `companies.pricing_tier_id`.

3. Review `migration_errors.csv` and re-run for any failed rows after fixing the root cause.

## Tier tag mapping

Edit `TIER_TAG_MAP` at the top of `shopify_migration.py` if your Shopify tier tags differ from the default:

```python
TIER_TAG_MAP = {
    "1": "Tier 1",
    "2": "Tier 2",
    "3": "Tier 3",
    "4": "Tier 4",
}
```
