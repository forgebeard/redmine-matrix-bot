#!/bin/sh
# Старт админки в Docker: подставить DATABASE_URL из файла пароля, миграции, uvicorn.
set -e
cd /app
python -c "from database.url_resolver import materialize_database_url_env; materialize_database_url_env()"
alembic upgrade head
exec uvicorn admin_main:app --host 0.0.0.0 --port 8080
