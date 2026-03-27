"""
Общие фикстуры для тестов бота.

Создаёт mock-объекты, имитирующие структуры Redmine и Matrix,
чтобы тесты работали БЕЗ реальных серверов.
"""

import sys
from pathlib import Path
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

# Добавляем корень проекта в sys.path, чтобы import bot работал
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════════════
# MOCK-ОБЪЕКТЫ REDMINE
# ═══════════════════════════════════════════════════════════════════════════


class MockStatus:
    """Имитация issue.status"""
    def __init__(self, name="Новая"):
        self.name = name


class MockPriority:
    """Имитация issue.priority"""
    def __init__(self, name="3 (Нормальный)"):
        self.name = name


class MockVersion:
    """Имитация issue.fixed_version"""
    def __init__(self, name="РЕД ОС 8.0"):
        self.name = name


class MockUser:
    """Имитация journal.user"""
    def __init__(self, name="Иванов Пётр"):
        self.name = name


class MockJournal:
    """Имитация одной записи журнала."""
    def __init__(self, journal_id, notes="", details=None, user_name="Иванов Пётр"):
        self.id = journal_id
        self.notes = notes
        self.details = details or []
        self.user = MockUser(user_name)


class MockIssue:
    """
    Имитация задачи Redmine (issue).
    Позволяет задать все ключевые поля.
    """
    def __init__(
        self,
        issue_id=1001,
        subject="Тестовая задача",
        status="Новая",
        priority="3 (Нормальный)",
        due_date=None,
        version_name=None,
        journals=None,
    ):
        self.id = issue_id
        self.subject = subject
        self.status = MockStatus(status)
        self.priority = MockPriority(priority)
        self.due_date = due_date
        self.journals = journals or []

        # fixed_version — может отсутствовать (как в реальном Redmine)
        if version_name:
            self.fixed_version = MockVersion(version_name)

    def __getattr__(self, name):
        """Имитация python-redmine: отсутствующие атрибуты → AttributeError."""
        raise AttributeError(f"MockIssue has no attribute '{name}'")


# ═══════════════════════════════════════════════════════════════════════════
# ФИКСТУРЫ PYTEST
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def simple_issue():
    """Простая задача со статусом 'Новая', без журналов."""
    return MockIssue(issue_id=1001, subject="Установить ОС на сервер", status="Новая")


@pytest.fixture
def issue_with_version():
    """Задача с версией «РЕД Виртуализация 1.0»."""
    return MockIssue(
        issue_id=2002,
        subject="Баг в виртуализации",
        status="Новая",
        version_name="РЕД Виртуализация 1.0",
    )


@pytest.fixture
def overdue_issue():
    """Просроченная задача (срок — 3 дня назад)."""
    return MockIssue(
        issue_id=3003,
        subject="Просроченная задача",
        status="В работе",
        due_date=date.today() - timedelta(days=3),
    )


@pytest.fixture
def issue_with_journals():
    """Задача с 3 записями журнала."""
    journals = [
        MockJournal(100, notes="Старый комментарий", user_name="Петров"),
        MockJournal(200, notes="", details=[
            {"name": "status_id", "old_value": "1", "new_value": "2"}
        ]),
        MockJournal(300, notes="Свежий комментарий от заказчика", user_name="Сидоров"),
    ]
    return MockIssue(
        issue_id=4004,
        subject="Задача с журналами",
        status="В работе",
        journals=journals,
    )


@pytest.fixture
def info_provided_issue():
    """Задача со статусом 'Информация предоставлена'."""
    return MockIssue(
        issue_id=5005,
        subject="Запрос данных от клиента",
        status="Информация предоставлена",
    )


@pytest.fixture
def reopened_issue():
    """Задача со статусом 'Открыто повторно'."""
    return MockIssue(
        issue_id=6006,
        subject="Повторная проблема",
        status="Открыто повторно",
    )


@pytest.fixture
def rv_issue():
    """Задача 'Передано в работу.РВ' с версией Виртуализация."""
    return MockIssue(
        issue_id=7007,
        subject="Передано в РВ",
        status="Передано в работу.РВ",
        version_name="РЕД Виртуализация 2.0",
    )


@pytest.fixture
def mock_matrix_client():
    """Mock Matrix-клиента. room_send возвращает успех."""
    client = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.__class__ = type("RoomSendResponse", (), {})
    client.room_send = AsyncMock(return_value=mock_resp)
    return client


@pytest.fixture
def user_cfg_all():
    """Конфиг пользователя с полной подпиской."""
    return {
        "redmine_id": 1972,
        "room": "!testroom:server.example",
        "notify": ["all"],
    }


@pytest.fixture
def user_cfg_limited():
    """Конфиг пользователя с ограниченной подпиской."""
    return {
        "redmine_id": 3254,
        "room": "!limitedroom:server.example",
        "notify": ["new", "info", "issue_updated"],
    }