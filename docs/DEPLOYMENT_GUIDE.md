```markdown
# 📘 Руководство по развёртыванию Via (RHEL/AlmaLinux/Rocky)

> Для настройки после развёртывания см. [ADMINISTRATOR_GUIDE.md](ADMINISTRATOR_GUIDE.md).
> Для обзора проекта см. [README.md](../README.md).

## 1. Подготовка ВМ

- **ОС:** Red OS 8.0+, AlmaLinux 9, Rocky Linux 9 или RHEL 9
- **Доступ:** root или `sudo`

## 2. Установка Docker

```bash
dnf update -y
dnf install -y git nano docker-ce docker-ce-cli docker-compose
systemctl enable --now docker
usermod -aG docker $USER && newgrp docker
docker --version && docker compose version
```

Если Docker отсутствует в репозиториях — подключите официальный:
```bash
dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
```

## 3. Firewall и SELinux

```bash
# Открыть порт админки
firewall-cmd --permanent --add-port=8080/tcp
firewall-cmd --reload

# При проблемах с томами Docker (диагностика):
setenforce 0
sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config
# После развёртывания вернуть enforcing и настроить политики
```

## 4. Развёртывание

```bash
mkdir -p ~/via && cd ~/via
git clone git@github.com:forgebeard/Via.git
cd Via
chmod +x deploy.sh && ./deploy.sh
```

**Что делает `deploy.sh`:**
1. Создаёт/дополняет `.env` при первом запуске.
2. Загружает образы (PostgreSQL, Python).
3. Генерирует `POSTGRES_PASSWORD` и `APP_MASTER_KEY` (сервис `init`).
4. Запускает сервис миграций БД (`migrate`: `alembic upgrade head`) и дожидается успешного завершения.
5. Запускает веб-панель и бота после миграций.

> ⚠️ Сохраните `.env` — credentials для восстановления системы.

## 5. Первичная настройка

1. Откройте `http://<IP>:8080/setup` — создайте администратора.
2. Войдите: `http://<IP>:8080/login`.
3. Перейдите в **Настройки** (`/onboarding`):
   - Скопируйте credentials из раздела **«База данных сервиса»** в безопасное место.
   - Введите параметры Redmine и Matrix в разделе **«Параметры сервиса»**.
   - Нажмите **«Проверить доступ»** → **«Сохранить»**.
4. Перезапустите бота: `docker compose restart bot`.

Подробности: [ADMINISTRATOR_GUIDE.md](ADMINISTRATOR_GUIDE.md).

### Логи и таймзона

Приложение **бота** и **админки** формируют метку `%(asctime)s` в логах по **таймзоне сервиса** из БД (`cycle_settings` → `BOT_TIMEZONE`, см. загрузку при старте). Это не зависит от `TZ` в контейнере для строк, которые пишут через настроенные handlers.

После **смены** таймзоны сервиса в панели перезапустите процессы **admin** и **bot**, чтобы применились новые formatters (hot reload бота не переустанавливает логирование).

Переменная **`TZ`** в Docker по-прежнему полезна для других утилит и для единообразия с системными логами вне Python; для самих логов Via достаточно `BOT_TIMEZONE` в БД.

## 6. Обновление

```bash
cd ~/via/Via
git pull
docker compose up -d --build
```

Если в релизе есть новые миграции БД — они применяются автоматически одноразовым сервисом `migrate` до старта `admin` и `bot`.

## 7. Полезные команды

```bash
docker compose logs -f bot       # Логи бота
docker compose restart bot       # Перезапуск бота
docker compose ps                # Статус контейнеров
docker compose down              # Остановка (данные БД сохраняются)
docker compose down -v           # Полная очистка (данные БД удалятся!)
```

## 8. Безопасность

- Используйте SSH-ключи вместо паролей.
- Регулярно обновляйте систему и Docker-образы.
- Храните `.env` в защищённом месте.
- Для production используйте Docker secrets вместо `APP_MASTER_KEY` в `.env` (см. [secrets-storage.md](secrets-storage.md)).
```