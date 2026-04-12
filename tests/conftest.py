"""
Общая конфигурация тестов (фикстуры для pytest).
"""

from __future__ import annotations

import os
from collections.abc import Generator
from datetime import date, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

# До импорта admin_main / database.session (иначе engine создастся без NullPool).
os.environ.setdefault("SQLALCHEMY_NULL_POOL", "1")

# Не писать лог в data/bot.log во время pytest — иначе админка «События» показывает
# строки из тестов (!room:server, Matrix send failed из моков и т.д.).
os.environ["LOG_TO_FILE"] = "0"

# Для password auth и encrypted-secrets на старте нужен master key.
os.environ.setdefault("APP_MASTER_KEY", "0123456789abcdef0123456789abcdef")

# Отключаем rate limiter до импорта admin_main (иначе _rate_limiter инициализируется до тестов)
os.environ["ADMIN_DISABLE_RATE_LIMITS"] = "1"

# Тесты /setup и /login не должны зависеть от локального ADMIN_LOGINS в окружении разработчика.
os.environ.pop("ADMIN_LOGINS", None)


# ── Фикстура TestClient ────────────────────────────────────────────────────


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    """TestClient для admin-приложения. Используется всеми интеграционными тестами."""
    # Импорт здесь чтобы избежать ранней инициализации engine.
    import admin.main as admin_main  # noqa: PLC0415

    with TestClient(admin_main.app) as c:
        yield c


# ── Хелпер: создать админа и войти ──────────────────────────────────────


def _setup_and_login_admin(
    client: TestClient, login: str = "test_admin@example.com", password: str = "StrongPassword123"
) -> None:
    """Создаёт первого админа через /setup и входит через /login.

    На одной БД несколько тестов: первый создаёт админа, остальные получают 409.
    """
    client.get("/setup", follow_redirects=True)
    token = client.cookies.get("admin_csrf")
    created = client.post(
        "/setup",
        data={
            "login": login,
            "password": password,
            "password_confirm": password,
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert created.status_code in (302, 303, 409), created.status_code
    client.get("/login")
    ltoken = client.cookies.get("admin_csrf")
    logged = client.post(
        "/login",
        data={"login": login, "password": password, "csrf_token": ltoken},
        follow_redirects=False,
    )
    if logged.status_code == 401:
        pytest.skip(
            "Вход тестового admin не удался (в БД другой пароль или нет пользователя). "
            "Используйте чистую БД или задайте учётные данные под вашу БД."
        )
    assert logged.status_code in (302, 303), logged.status_code


# ── Вспомогательные классы ──────────────────────────────────────────────


class _Named:
    """Объект с атрибутом .name (для status, priority, fixed_version)."""

    def __init__(self, name: str) -> None:
        self.name = name


class MockIssue:
    """Мок Redmine-задачи."""

    def __init__(
        self,
        issue_id: int = 12345,
        subject: str | None = None,
        version_name: str | None = None,
        status: str = "Новая",
        priority: str = "Нормальный",
        due_date: date | None = None,
        journals: list[MockJournal] | None = None,
    ) -> None:
        self.id: int = issue_id
        self.subject: str = subject if subject is not None else f"Тестовая задача #{issue_id}"
        self.status: _Named = _Named(status)
        self.priority: _Named = _Named(priority)
        self.due_date: date | None = due_date
        self.journals: list[MockJournal] = journals if journals is not None else []
        self.fixed_version: _Named | None = _Named(version_name) if version_name else None


class MockJournal:
    """Мок записи журнала Redmine."""

    def __init__(
        self,
        journal_id: int = 1,
        notes: str = "",
        user_name: str = "Тестовый пользователь",
        details: list[Any] | None = None,
    ) -> None:
        self.id: int = journal_id
        self.notes: str = notes
        self.user: _Named = _Named(user_name)
        self.details: list[Any] = details if details is not None else []


# ── Фикстуры ────────────────────────────────────────────────────────────


@pytest.fixture
def simple_issue() -> MockIssue:
    """Простая задача без версии."""
    return MockIssue(issue_id=7777, status="Новая")


@pytest.fixture
def issue_with_version() -> MockIssue:
    """Задача с версией РЕД Виртуализация 1.0."""
    return MockIssue(issue_id=8001, version_name="РЕД Виртуализация 1.0")


@pytest.fixture
def issue_with_journals() -> MockIssue:
    """Задача с тремя записями журнала (id=100, 200, 300)."""
    journals = [
        MockJournal(journal_id=100, notes="Первый комментарий"),
        MockJournal(journal_id=200, notes="Второй комментарий"),
        MockJournal(journal_id=300, notes="Третий комментарий"),
    ]
    return MockIssue(issue_id=4004, journals=journals)


@pytest.fixture
def rv_issue() -> MockIssue:
    """Задача со статусом 'Передано в работу.РВ' и версией Виртуализация."""
    return MockIssue(
        issue_id=8002,
        status="Передано в работу.РВ",
        version_name="РЕД Виртуализация 2.0",
    )


@pytest.fixture
def overdue_issue() -> MockIssue:
    """Просроченная задача (due_date = 3 дня назад)."""
    return MockIssue(
        issue_id=9999,
        due_date=date.today() - timedelta(days=3),
    )


@pytest.fixture
def mock_matrix_client() -> AsyncMock:
    """Мок Matrix-клиента с успешным room_send."""
    client = AsyncMock()
    success = MagicMock()
    success.event_id = "$fake_event_id"
    success.__class__ = type("RoomSendResponse", (), {})
    client.room_send = AsyncMock(return_value=success)
    return client


# Rate limiter теперь отключается через ADMIN_DISABLE_RATE_LIMITS=1 в CI env.
# Фикстура _no_admin_rate_limits_for_http_tests удалена — она не срабатывала
# т.к. _rate_limiter инициализируется до начала работы фикстур.
