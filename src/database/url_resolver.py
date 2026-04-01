"""Построение DATABASE_URL без хранения пароля в репозитории.

Приоритет:
1) Явный ``DATABASE_URL`` в окружении.
2) Файл ``DATABASE_PASSWORD_FILE`` + переменные ``POSTGRES_USER``, ``POSTGRES_DB``,
   ``POSTGRES_HOST``, ``POSTGRES_PORT`` (типичный сценарий Docker Compose).
"""

from __future__ import annotations

import os
import urllib.parse
from pathlib import Path


def resolve_database_url() -> str:
    explicit = (os.environ.get("DATABASE_URL") or "").strip()
    if explicit:
        return explicit

    pw_file = (os.environ.get("DATABASE_PASSWORD_FILE") or "").strip()
    if not pw_file:
        raise RuntimeError(
            "Задайте DATABASE_URL или пару DATABASE_PASSWORD_FILE + POSTGRES_* "
            "(см. .env.example и docker-compose.yml)."
        )

    path = Path(pw_file)
    if not path.is_file():
        raise RuntimeError(f"Файл пароля БД не найден: {pw_file}")

    password_raw = path.read_text(encoding="utf-8").strip()
    user = (os.environ.get("POSTGRES_USER") or "bot").strip()
    db = (os.environ.get("POSTGRES_DB") or "redmine_matrix").strip()
    host = (os.environ.get("POSTGRES_HOST") or "localhost").strip()
    port = (os.environ.get("POSTGRES_PORT") or "5432").strip()
    pwd_enc = urllib.parse.quote(password_raw, safe="")
    return f"postgresql://{user}:{pwd_enc}@{host}:{port}/{db}"


def materialize_database_url_env() -> None:
    """Если ``DATABASE_URL`` пуст, вычисляет его и кладёт в ``os.environ`` (Alembic, SQLAlchemy)."""
    if (os.environ.get("DATABASE_URL") or "").strip():
        return
    os.environ["DATABASE_URL"] = resolve_database_url()
