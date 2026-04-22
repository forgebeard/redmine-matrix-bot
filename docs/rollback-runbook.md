```markdown
# Rollback Runbook

Краткий регламент быстрого отката после проблемного релиза.

## 1. Перед деплоем: бэкап БД

```bash
docker compose exec postgres sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' > pre_release_$(date +%F).sql
```

## 2. Откат на предыдущую версию кода

```bash
cd ~/via/Via
git log --oneline -5              # найти предыдущий рабочий коммит
git checkout <previous-commit>
docker compose up -d --build
```

## 3. Откат миграции (если безопасно)

```bash
docker compose exec admin alembic downgrade -1
```

> Важно: при одной initial-ревизии `downgrade -1` может снять всю схему до `base`.
> Для такого случая использовать восстановление из бэкапа (шаг 4), а не частичный downgrade.
> Если миграция помечена как `irreversible` — также использовать шаг 4.

## 4. Восстановление БД из дампа

```bash
cat ./pre_release_*.sql | docker compose exec -T postgres sh -lc \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

## 5. Проверка после отката

```bash
docker compose ps
curl -sSf http://localhost:8080/health/live
curl -sSf http://localhost:8080/health/ready
docker compose logs --tail=20 bot admin
```

## 6. Восстановление админ-доступа при аварии

```bash
docker compose exec admin python scripts/reset_admin_password.py \
  --login admin --password 'NewStrongPassword123'
```
```