-- Backfill existing null stock values and enforce NOT NULL with default 0
-- Run once against the database before deploying the updated backend

-- 1. Add column if it does not already exist (safe no-op if it does)
ALTER TABLE product_variants ADD COLUMN IF NOT EXISTS stock INTEGER;

-- 2. Backfill any existing NULLs
UPDATE product_variants SET stock = 0 WHERE stock IS NULL;

-- 3. Set column-level default
ALTER TABLE product_variants ALTER COLUMN stock SET DEFAULT 0;

-- 4. Enforce NOT NULL
ALTER TABLE product_variants ALTER COLUMN stock SET NOT NULL;
