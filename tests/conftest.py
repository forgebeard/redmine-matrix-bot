"""
Общая конфигурация тестов (фикстуры для pytest).

Что делает:
  - Добавляет каталог src/ в sys.path, чтобы импортировать модули проекта
    (config, utils, matrix_client, …) так же, как в проде через pytest.
  - Даёт моки Redmine: MockIssue (задача), MockJournal (запись журнала) —
    без HTTP к реальному Redmine.
  - mock_matrix_client — AsyncClient с успешным room_send (для тестов bot.py).
  - Включает NullPool для async Postgres (см. database.session): иначе пул
    соединений, созданный в event loop Starlette TestClient, переиспользуется
    в pytest-asyncio и даёт asyncpg «another operation is in progress».

Тесты корневого bot.py дополнительно импортируют bot из корня; этот файл
обслуживает и src-тесты, и test_bot.py.

Автопатч rate limiter в admin_main (см. фикстуру ниже), чтобы серия POST /login
в интеграционных тестах не упиралась в лимит 5/мин с одного IP.
"""

import os
import sys
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT / "src"))
sys.path.insert(0, str(_PROJECT_ROOT))  # Для import src.bot.main

# До импорта admin_main / database.session (иначе engine создастся без NullPool).
os.environ.setdefault("SQLALCHEMY_NULL_POOL", "1")

# Не писать лог в data/bot.log во время pytest — иначе админка «События» показывает
# строки из тестов (!room:server, Matrix send failed из моков и т.д.).
os.environ["LOG_TO_FILE"] = "0"


# ── Вспомогательные классы ──────────────────────────────────────────────

class _Named:
    """Объект с атрибутом .name (для status, priority, fixed_version)."""
    def __init__(self, name):
        self.name = name


class MockIssue:
    """
    Мок Redmine-задачи.
    Поддерживает: id, subject, fixed_version, status, priority, due_date, journals.
    """
    def __init__(
        self,
        issue_id=12345,
        subject=None,
        version_name=None,
        status="Новая",
        priority="Нормальный",
        due_date=None,
        journals=None,
    ):
        self.id = issue_id
        self.subject = subject if subject is not None else f"Тестовая задача #{issue_id}"
        self.status = _Named(status)
        self.priority = _Named(priority)
        self.due_date = due_date
        self.journals = journals if journals is not None else []

        if version_name:
            self.fixed_version = _Named(version_name)
        else:
            self.fixed_version = None


class MockJournal:
    """
    Мок записи журнала Redmine.
    Поддерживает: id, notes, user.name, details.
    """
    def __init__(self, journal_id=1, notes="", user_name="Тестовый пользователь", details=None):
        self.id = journal_id
        self.notes = notes
        self.user = _Named(user_name)
        self.details = details if details is not None else []


# ── Фикстуры ────────────────────────────────────────────────────────────

@pytest.fixture
def simple_issue():
    """Простая задача без версии."""
    return MockIssue(issue_id=7777, status="Новая")


@pytest.fixture
def issue_with_version():
    """Задача с версией РЕД Виртуализация 1.0."""
    return MockIssue(issue_id=8001, version_name="РЕД Виртуализация 1.0")


@pytest.fixture
def issue_with_journals():
    """Задача с тремя записями журнала (id=100, 200, 300)."""
    journals = [
        MockJournal(journal_id=100, notes="Первый комментарий"),
        MockJournal(journal_id=200, notes="Второй комментарий"),
        MockJournal(journal_id=300, notes="Третий комментарий"),
    ]
    return MockIssue(issue_id=4004, journals=journals)


@pytest.fixture
def rv_issue():
    """Задача со статусом 'Передано в работу.РВ' и версией Виртуализация."""
    return MockIssue(
        issue_id=8002,
        status="Передано в работу.РВ",
        version_name="РЕД Виртуализация 2.0",
    )


@pytest.fixture
def overdue_issue():
    """Просроченная задача (due_date = 3 дня назад)."""
    return MockIssue(
        issue_id=9999,
        due_date=date.today() - timedelta(days=3),
    )


@pytest.fixture
def mock_matrix_client():
    """Мок Matrix-клиента с успешным room_send."""
    client = AsyncMock()
    # Возвращаем простой объект, который НЕ является RoomSendError
    success = MagicMock()
    success.event_id = "$fake_event_id"
    # Убеждаемся что isinstance(resp, RoomSendError) == False
    success.__class__ = type("RoomSendResponse", (), {})
    client.room_send = AsyncMock(return_value=success)
    return client


@pytest.fixture(autouse=True)
def _no_admin_rate_limits_for_http_tests(monkeypatch):
    """
    Несколько интеграционных тестов подряд делают POST /login с одного IP TestClient —
    иначе срабатывает лимит 5/мин в admin_main.
    """
    try:
        import admin_main
    except ImportError:
        return
    monkeypatch.setattr(admin_main._rate_limiter, "hit", lambda key, limit, window_seconds: True)
