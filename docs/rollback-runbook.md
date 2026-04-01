# Rollback Runbook (Admin Portal)

Краткий регламент быстрого отката после проблемного релиза.

## 1) Перед деплоем: бэкап БД

```bash
docker compose exec postgres sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" > /tmp/pre_release.sql'
docker compose cp postgres:/tmp/pre_release.sql ./pre_release.sql
```

## 2) Быстрый откат контейнера admin

Если используется тегированный образ:

```bash
docker pull your-registry/matrix-bot-admin:<previous-tag>
docker compose up -d admin
```

Если работа из локального compose/ветки, используйте предыдущий commit:

```bash
git checkout <previous-working-commit>
docker compose up -d --build admin
```

## 3) Откат миграции (если безопасно)

```bash
docker compose exec admin alembic downgrade -1
```

Если миграция помечена как `irreversible`, вместо downgrade использовать восстановление из бэкапа.

## 4) Восстановление БД из дампа

```bash
cat ./pre_release.sql | docker compose exec -T postgres sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

## 5) Проверка после отката

```bash
curl -sSf http://localhost:8080/health/live
curl -sSf http://localhost:8080/health/ready
curl -sS http://localhost:8080/health/smtp
```

## 6) Восстановление админ-доступа при аварии

```bash
python scripts/manage_admin_credentials.py reset-password --login admin --password 'NewStrongPassword123'
# или: python scripts/reset_admin_password.py --login admin --password '...'  (совместимость)
```

