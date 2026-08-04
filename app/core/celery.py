"""Celery application instance."""
from celery import Celery
from celery.signals import worker_process_init

from app.core.config import settings

celery_app = Celery(
    "afapparel",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.email_tasks",
        "app.tasks.quickbooks_tasks",
        "app.tasks.pricelist_tasks",
        "app.tasks.inventory_tasks",
        "app.tasks.cart_tasks",
    ],
)

celery_app.config_from_object("celeryconfig")


@worker_process_init.connect
def _init_worker_db_engine(**_kwargs) -> None:
    """Give each forked worker process its own NullPool async DB engine.

    Celery prefork forks worker processes, and every task runs its async code in
    a fresh event loop via asyncio.run(). A pooled asyncpg connection created in
    one loop cannot be reused in another loop (or in a different forked child) —
    that is exactly the "cannot perform operation: another operation is in
    progress" and "got Future attached to a different loop" errors that were
    crashing every DB-touching task (emails, QuickBooks sync, etc.).

    NullPool opens and closes a fresh connection per use, so nothing is ever
    shared across event loops or processes. The web app's pooled engine is left
    untouched — this only rebinds inside worker children.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from app.core import database

    new_engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    database.engine = new_engine
    database.AsyncSessionLocal.configure(bind=new_engine)


if __name__ == "__main__":
    celery_app.start()
