-- Add timeline JSONB column to orders table (idempotent)
ALTER TABLE orders ADD COLUMN IF NOT EXISTS timeline JSONB DEFAULT '[]'::jsonb;
UPDATE orders SET timeline = '[]'::jsonb WHERE timeline IS NULL;
