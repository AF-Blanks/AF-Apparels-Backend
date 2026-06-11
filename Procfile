web: uvicorn app.main:app --host 0.0.0.0 --port $PORT
worker: celery -A app.core.celery worker --loglevel=info -Q celery,default,email,quickbooks,pricelist,inventory --concurrency=2
beat: celery -A app.core.celery beat --loglevel=info
