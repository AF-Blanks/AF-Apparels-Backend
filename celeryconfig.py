"""Celery configuration for AF Apparels platform."""
import os

# ── Broker / Backend ──────────────────────────────────────────────────────────
broker_url = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/1")
result_backend = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")

# ── Serialization ─────────────────────────────────────────────────────────────
task_serializer = "json"
result_serializer = "json"
accept_content = ["json"]
result_expires = 3600  # 1 hour

# ── Timezone ──────────────────────────────────────────────────────────────────
timezone = "UTC"
enable_utc = True

# ── Task routing ──────────────────────────────────────────────────────────────
task_routes = {
    "app.tasks.email_tasks.*": {"queue": "email"},
    "app.tasks.quickbooks_tasks.*": {"queue": "quickbooks"},
    "app.tasks.pricelist_tasks.*": {"queue": "pricelist"},
    "app.tasks.inventory_tasks.*": {"queue": "inventory"},
    "app.tasks.cart_tasks.*": {"queue": "default"},
}

# ── Worker settings ───────────────────────────────────────────────────────────
worker_prefetch_multiplier = 1
task_acks_late = True
worker_max_tasks_per_child = 500

# ── Retry defaults ────────────────────────────────────────────────────────────
task_default_retry_delay = 60  # seconds
task_max_retries = 5

# ── Beat schedule (periodic tasks) ───────────────────────────────────────────
from celery.schedules import crontab  # noqa: E402

beat_schedule = {
    "detect-abandoned-carts": {
        "task": "app.tasks.cart_tasks.detect_abandoned_carts",
        "schedule": crontab(hour="*/1"),  # every hour
    },
    "check-low-stock": {
        "task": "app.tasks.inventory_tasks.check_low_stock_levels",
        "schedule": crontab(hour="6", minute="0"),  # daily at 6am UTC
    },
    "check-card-chargebacks": {
        "task": "app.tasks.quickbooks_tasks.check_card_payment_chargebacks",
        "schedule": crontab(hour="7", minute="0"),  # daily at 7am UTC — staggered from other daily jobs
    },
}
