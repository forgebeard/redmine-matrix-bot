"""Переменные окружения и константы админ-панели (единая точка для сплита admin_main)."""

from __future__ import annotations

import os

SESSION_COOKIE_NAME = os.getenv("ADMIN_SESSION_COOKIE", "admin_session")
CSRF_COOKIE_NAME = os.getenv("ADMIN_CSRF_COOKIE", "admin_csrf")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes", "on")
SETUP_PATH = "/setup"
# Первый вход после развёртывания: логин/пароль по умолчанию (см. README), затем обязательная смена.
BOOTSTRAP_ADMIN_LOGIN = "admin"
BOOTSTRAP_ADMIN_PASSWORD = "admin"
MUST_CHANGE_CREDENTIALS_PATH = "/me/bootstrap-credentials"
SESSION_IDLE_TIMEOUT_SECONDS = int(os.getenv("ADMIN_SESSION_IDLE_TIMEOUT", "1800"))
RUNTIME_STATUS_FILE = os.getenv("BOT_RUNTIME_STATUS_FILE", "/app/data/runtime_status.json")
GROUP_UNASSIGNED_NAME = "UNASSIGNED"

AUTH_TOKEN_SALT = os.getenv("AUTH_TOKEN_SALT", "dev-token-salt")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "86400"))
MATRIX_CODE_TTL_SECONDS = int(os.getenv("MATRIX_CODE_TTL_SECONDS", "300"))

APP_MASTER_KEY_FILE = os.getenv("APP_MASTER_KEY_FILE", "/run/secrets/app_master_key")
ADMIN_EXISTS_CACHE_TTL_SECONDS = int(os.getenv("ADMIN_EXISTS_CACHE_TTL_SECONDS", "20"))
INTEGRATION_STATUS_CACHE_TTL_SECONDS = int(os.getenv("INTEGRATION_STATUS_CACHE_TTL_SECONDS", "30"))
REQUIRED_SECRET_NAMES = [
    v.strip()
    for v in os.getenv(
        "REQUIRED_SECRET_NAMES",
        "REDMINE_URL,REDMINE_API_KEY,MATRIX_HOMESERVER,MATRIX_ACCESS_TOKEN,MATRIX_USER_ID,MATRIX_DEVICE_ID",
    ).split(",")
    if v.strip()
]
ONBOARDING_SKIPPED_SECRET = "__onboarding_skipped"
NOTIFY_TYPES = [
    ("new", "Новая задача"),
    ("info", "Информация предоставлена"),
    ("reminder", "Напоминание"),
    ("overdue", "Просроченная задача"),
    ("status_change", "Изменение статуса"),
    ("issue_updated", "Обновление задачи"),
    ("reopened", "Переоткрыта"),
]
NOTIFY_TYPE_KEYS = [k for k, _ in NOTIFY_TYPES]

# Fallback, если в БД (app_secrets) ещё нет значений — см. routers/redmine.py
REDMINE_URL = (os.getenv("REDMINE_URL") or "").strip()
REDMINE_API_KEY = (os.getenv("REDMINE_API_KEY") or "").strip()
