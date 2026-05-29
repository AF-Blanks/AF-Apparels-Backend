"""One-time script: hard-delete a product by ID, bypassing ORM cascade."""
import asyncio
import os
import sys


async def delete_product(product_id: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text

    raw_url = os.getenv("DATABASE_URL", "")
    if not raw_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)
    db_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(db_url)

    async with engine.begin() as conn:
        await conn.execute(text(
            "UPDATE po_line_items SET product_variant_id = NULL "
            "WHERE product_variant_id IN "
            "(SELECT id FROM product_variants WHERE product_id = :pid::uuid)"
        ), {"pid": product_id})
        print("Step 1 done: po_line_items cleared")

        await conn.execute(text(
            "DELETE FROM product_variants WHERE product_id = :pid::uuid"
        ), {"pid": product_id})
        print("Step 2 done: variants deleted (cart_items/inventory cascade automatically)")

        await conn.execute(text(
            "DELETE FROM products WHERE id = :pid::uuid"
        ), {"pid": product_id})
        print("Step 3 done: PRODUCT DELETED!")

    await engine.dispose()


if __name__ == "__main__":
    pid = sys.argv[1] if len(sys.argv) > 1 else "f630eef3-5fe7-437a-9ee9-43dfb6f34104"
    print(f"Deleting product {pid} ...")
    asyncio.run(delete_product(pid))
