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
1. Создаёт `.env` (если отсутствует).
2. Загружает образы (PostgreSQL, Python).
3. Сервис `init` заполняет `.env` случайными `POSTGRES_PASSWORD` и `APP_MASTER_KEY`.
4. Запускает БД, веб-панель и бота.

> ⚠️ Сохраните `.env` — credentials для восстановления системы.

## 5. Первичная настройка

1. Откройте `http://<IP>:8080/setup` — создайте администратора.
2. Войдите: `http://<IP>:8080/login`.
3. Перейдите в **Настройки** (`/onboarding`):
   - Скопируйте credentials из раздела **«База данных сервиса»**.
   - Введите параметры Redmine и Matrix в разделе **«Параметры сервиса»**.
   - Нажмите **«Проверить доступ»** → **«Сохранить»**.
4. Перезапустите бота: `docker compose restart bot`.

Подробности: [ADMINISTRATOR_GUIDE.md](ADMINISTRATOR_GUIDE.md).

## 6. Полезные команды

```bash
docker compose logs -f bot       # Логи бота
docker compose restart bot       # Перезапуск бота
docker compose ps                # Статус контейнеров
docker compose down --rmi all -v # Полная очистка (данные БД удалятся!)
```

## 7. Безопасность

- Используйте SSH-ключи вместо паролей.
- Регулярно обновляйте систему и Docker-образы.
- Храните `.env` в защищённом месте.
- Для production используйте Docker secrets вместо `APP_MASTER_KEY` в `.env` (см. [secrets-storage.md](secrets-storage.md)).
