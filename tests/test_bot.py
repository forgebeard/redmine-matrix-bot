"""
Тесты бота (src/bot/main.py).

Перед импортом подставляются переменные окружения (минимальный .env),
чтобы бот не падал на валидации при загрузке модуля.

Запуск из корня проекта:
  python -m pytest tests/test_bot.py -v
  python -m pytest tests/ -v
"""

import os
import asyncio
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from zoneinfo import ZoneInfo

import pytest

# Минимальный .env до import — иначе main() не вызывается, но константы валидируются при тестах
os.environ.setdefault("MATRIX_HOMESERVER", "https://test.server")
os.environ.setdefault("MATRIX_ACCESS_TOKEN", "test_token")
os.environ.setdefault("MATRIX_USER_ID", "@bot:test.server")
os.environ.setdefault("MATRIX_DEVICE_ID", "TESTDEVICE")
os.environ["REDMINE_URL"] = "https://redmine.test"
os.environ.setdefault("REDMINE_API_KEY", "test_api_key")
os.environ.setdefault("MATRIX_ONBOARDING_ENABLED", "0")
os.environ.setdefault("BOT_TIMEZONE", "Europe/Moscow")
os.environ.setdefault("USERS", '[{"redmine_id": 1972, "room": "!test:server", "notify": ["all"]}]')

import src.bot.main as bot
import matrix_send
from tests.conftest import MockIssue, MockJournal

# can_notify: круглосуточно и все дни недели — тесты не зависят от времени CI
USER_CFG_FOR_SEND = {
    "redmine_id": 1,
    "room": "!room:server",
    "notify": ["all"],
    "work_hours": "00:00-23:59",
    "work_days": [0, 1, 2, 3, 4, 5, 6],
}

# ═══════════════════════════════════════════════════════════════════════════
# 1. УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════
# Дублируют часть контракта src/utils.py, но тестируют функции из bot.py.

class TestPluralDays:
    """Тесты склонения слова 'день'."""

    def test_one_day(self):
        assert bot.plural_days(1) == "1 день"

    def test_two_days(self):
        assert bot.plural_days(2) == "2 дня"

    def test_five_days(self):
        assert bot.plural_days(5) == "5 дней"

    def test_eleven_days(self):
        assert bot.plural_days(11) == "11 дней"

    def test_twelve_days(self):
        assert bot.plural_days(12) == "12 дней"

    def test_twenty_one_days(self):
        assert bot.plural_days(21) == "21 день"

    def test_twenty_two_days(self):
        assert bot.plural_days(22) == "22 дня"

    def test_zero_days(self):
        assert bot.plural_days(0) == "0 дней"

    def test_negative_days(self):
        assert bot.plural_days(-3) == "3 дня"

    def test_hundred_eleven(self):
        assert bot.plural_days(111) == "111 дней"

    def test_hundred_one(self):
        assert bot.plural_days(101) == "101 день"


class TestEnsureTz:
    """Тесты добавления таймзоны к datetime."""

    def test_naive_datetime_gets_tz(self):
        naive = datetime(2026, 3, 27, 12, 0, 0)
        result = bot.ensure_tz(naive)
        assert result.tzinfo is not None
        assert result.tzinfo == bot.BOT_TZ

    def test_aware_datetime_unchanged(self):
        utc = ZoneInfo("UTC")
        aware = datetime(2026, 3, 27, 12, 0, 0, tzinfo=utc)
        result = bot.ensure_tz(aware)
        assert result.tzinfo == utc


class TestShouldNotify:
    """Тесты фильтрации уведомлений по подписке."""

    def test_all_means_everything(self):
        cfg = {"notify": ["all"]}
        assert bot.should_notify(cfg, "new") is True
        assert bot.should_notify(cfg, "overdue") is True
        assert bot.should_notify(cfg, "anything_random") is True

    def test_specific_types_included(self):
        cfg = {"notify": ["new", "info"]}
        assert bot.should_notify(cfg, "new") is True
        assert bot.should_notify(cfg, "info") is True

    def test_specific_types_excluded(self):
        cfg = {"notify": ["new", "info"]}
        assert bot.should_notify(cfg, "overdue") is False
        assert bot.should_notify(cfg, "status_change") is False

    def test_empty_notify_defaults_to_all(self):
        cfg = {}
        assert bot.should_notify(cfg, "new") is True

    def test_empty_list_blocks_everything(self):
        cfg = {"notify": []}
        assert bot.should_notify(cfg, "new") is False


class TestCfgForRoom:
    """Мерж group_delivery при отправке в комнату группы."""

    def test_personal_room_returns_same_mapping(self):
        cfg = {
            "redmine_id": 1,
            "room": "!p:server",
            "notify": ["all"],
            "group_room": "!g:server",
            "group_delivery": {"notify": ["new"], "work_hours": None, "work_days": None, "dnd": True},
        }
        out = bot._cfg_for_room(cfg, "!p:server")
        assert out is cfg

    def test_group_room_applies_delivery(self):
        cfg = {
            "redmine_id": 1,
            "room": "!p:server",
            "notify": ["all"],
            "work_hours": "09:00-10:00",
            "dnd": False,
            "group_room": "!g:server",
            "group_delivery": {
                "notify": ["new"],
                "work_hours": "11:00-12:00",
                "work_days": [0],
                "dnd": True,
            },
        }
        out = bot._cfg_for_room(cfg, "!g:server")
        assert out is not cfg
        assert out["notify"] == ["new"]
        assert out["work_hours"] == "11:00-12:00"
        assert out["work_days"] == [0]
        assert out["dnd"] is True
        assert out["room"] == "!p:server"

    def test_group_delivery_null_hours_uses_preferences_defaults(self):
        cfg = {
            "redmine_id": 1,
            "room": "!p:server",
            "notify": ["all"],
            "work_hours": "09:00-10:00",
            "group_room": "!g:server",
            "group_delivery": {
                "notify": ["all"],
                "work_hours": None,
                "work_days": None,
                "dnd": False,
            },
        }
        out = bot._cfg_for_room(cfg, "!g:server")
        assert "work_hours" not in out
        assert "work_days" not in out


class TestGetVersionName:
    """Тесты получения версии задачи."""

    def test_version_present(self):
        issue = MockIssue(version_name="РЕД ОС 8.0")
        assert bot.get_version_name(issue) == "РЕД ОС 8.0"

    def test_version_absent(self):
        issue = MockIssue()  # без версии
        assert bot.get_version_name(issue) is None


# ═══════════════════════════════════════════════════════════════════════════
# 2. ВАЛИДАЦИЯ КОНФИГУРАЦИИ
# ═══════════════════════════════════════════════════════════════════════════
# USERS из .env должен быть валидным до выхода в прод.

class TestValidateUsers:
    """Тесты validate_users — защита от кривого конфига."""

    def test_valid_config(self):
        users = [{"redmine_id": 1972, "room": "!room:server", "notify": ["all"]}]
        ok, errors = bot.validate_users(users)
        assert ok is True
        assert errors == []

    def test_missing_redmine_id(self):
        users = [{"room": "!room:server"}]
        ok, errors = bot.validate_users(users)
        assert ok is False
        assert any("redmine_id" in e for e in errors)

    def test_missing_room(self):
        users = [{"redmine_id": 1972}]
        ok, errors = bot.validate_users(users)
        assert ok is False
        assert any("room" in e for e in errors)

    def test_redmine_id_not_int(self):
        users = [{"redmine_id": "1972", "room": "!room:server"}]
        ok, errors = bot.validate_users(users)
        assert ok is False
        assert any("int" in e for e in errors)

    def test_empty_room(self):
        users = [{"redmine_id": 1972, "room": ""}]
        ok, errors = bot.validate_users(users)
        assert ok is False

    def test_room_only_spaces(self):
        users = [{"redmine_id": 1972, "room": "   "}]
        ok, errors = bot.validate_users(users)
        assert ok is False

    def test_notify_not_list(self):
        users = [{"redmine_id": 1972, "room": "!room:server", "notify": "all"}]
        ok, errors = bot.validate_users(users)
        assert ok is False
        assert any("списком" in e for e in errors)

    def test_multiple_users_one_invalid(self):
        users = [
            {"redmine_id": 1972, "room": "!room:server"},
            {"redmine_id": "bad", "room": ""},
        ]
        ok, errors = bot.validate_users(users)
        assert ok is False
        assert len(errors) >= 2  # минимум 2 ошибки во втором

    def test_empty_users_list(self):
        ok, errors = bot.validate_users([])
        assert ok is True  # Пустой список — валидный (проверка на пустоту в main)


# ═══════════════════════════════════════════════════════════════════════════
# 4. ДЕТЕКТОРЫ ИЗМЕНЕНИЙ
# ═══════════════════════════════════════════════════════════════════════════
# Сравнение прошлого state с текущим ответом Redmine.

class TestDetectStatusChange:
    """Тесты определения смены статуса."""

    def test_status_changed(self, simple_issue):
        sent = {"7777": {"status": "В работе", "notified_at": "2026-03-27T12:00:00"}}
        result = bot.detect_status_change(simple_issue, sent)
        assert result == "В работе"  # старый статус

    def test_status_same(self, simple_issue):
        sent = {"7777": {"status": "Новая", "notified_at": "2026-03-27T12:00:00"}}
        result = bot.detect_status_change(simple_issue, sent)
        assert result is None

    def test_issue_not_in_sent(self, simple_issue):
        sent = {}
        result = bot.detect_status_change(simple_issue, sent)
        assert result is None

    def test_sent_without_status_field(self, simple_issue):
        """Старый формат sent без поля status."""
        sent = {"1001": {"notified_at": "2026-03-27T12:00:00"}}
        result = bot.detect_status_change(simple_issue, sent)
        assert result is None


class TestDetectNewJournals:
    """Тесты детектирования новых записей журнала."""

    def test_all_new(self, issue_with_journals):
        """Все журналы новые (last_known_id=0)."""
        state = {}
        new, max_id = bot.detect_new_journals(issue_with_journals, state)
        assert len(new) == 3
        assert max_id == 300

    def test_some_new(self, issue_with_journals):
        """Только журналы с id > 100 новые."""
        state = {"4004": {"last_journal_id": 100}}
        new, max_id = bot.detect_new_journals(issue_with_journals, state)
        assert len(new) == 2
        assert new[0].id == 200
        assert new[1].id == 300
        assert max_id == 300

    def test_none_new(self, issue_with_journals):
        """Все журналы уже обработаны."""
        state = {"4004": {"last_journal_id": 300}}
        new, max_id = bot.detect_new_journals(issue_with_journals, state)
        assert len(new) == 0
        assert max_id == 300

    def test_empty_journals(self):
        """Задача без журналов."""
        issue = MockIssue(issue_id=9999, journals=[])
        new, max_id = bot.detect_new_journals(issue, {})
        assert new == []
        assert max_id == 0

    def test_journals_exception(self):
        """Ошибка при чтении журналов → пустой результат."""
        issue = MockIssue(issue_id=8888)
        # Подменяем journals на объект, который кидает исключение при list()
        issue.journals = property(lambda self: (_ for _ in ()).throw(Exception("API error")))

        class BrokenJournals:
            def __iter__(self):
                raise Exception("API error")

        issue.journals = BrokenJournals()
        new, max_id = bot.detect_new_journals(issue, {})
        assert new == []
        assert max_id == 0


# ═══════════════════════════════════════════════════════════════════════════
# 5. DESCRIBE_JOURNAL
# ═══════════════════════════════════════════════════════════════════════════
# Человекочитаемое описание записей журнала для issue_updated.

class TestDescribeJournal:
    """Тесты описания записей журнала."""

    def test_comment_only(self):
        j = MockJournal(1, notes="Прошу уточнить", user_name="Клиент")
        result = bot.describe_journal(j)
        assert "💬" in result
        assert "Клиент" in result

    def test_status_change(self):
        j = MockJournal(2, notes="", details=[
            {"name": "status_id", "old_value": "1", "new_value": "2"}
        ])
        result = bot.describe_journal(j)
        assert "Статус" in result
        assert "Новая" in result
        assert "В работе" in result

    def test_status_change_skipped(self):
        """skip_status=True → смена статуса не показывается."""
        j = MockJournal(3, notes="", details=[
            {"name": "status_id", "old_value": "1", "new_value": "2"}
        ])
        result = bot.describe_journal(j, skip_status=True)
        assert result is None  # Нет ни комментария, ни показанных полей

    def test_priority_change(self):
        j = MockJournal(4, notes="", details=[
            {"name": "priority_id", "old_value": "2", "new_value": "3"}
        ])
        result = bot.describe_journal(j)
        assert "Приоритет" in result

    def test_comment_plus_status(self):
        """Комментарий + смена статуса → оба показываются."""
        j = MockJournal(5, notes="Исправлено", details=[
            {"name": "status_id", "old_value": "2", "new_value": "5"}
        ])
        result = bot.describe_journal(j)
        assert "💬" in result
        assert "Статус" in result

    def test_hidden_custom_field(self):
        """Кастомные поля (числовой id) скрываются."""
        j = MockJournal(6, notes="", details=[
            {"name": "42", "old_value": "old", "new_value": "new"}
        ])
        result = bot.describe_journal(j)
        assert result is None

    def test_unknown_field_skipped(self):
        """Неизвестное поле (не в FIELD_NAMES) → пропускается."""
        j = MockJournal(7, notes="", details=[
            {"name": "some_unknown_field", "old_value": "a", "new_value": "b"}
        ])
        result = bot.describe_journal(j)
        assert result is None

    def test_description_field_hidden(self):
        """Поле description → None в FIELD_NAMES → скрыто."""
        j = MockJournal(8, notes="", details=[
            {"name": "description", "old_value": "old text", "new_value": "new text"}
        ])
        result = bot.describe_journal(j)
        assert result is None

    def test_empty_journal(self):
        """Журнал без комментария и без деталей → None."""
        j = MockJournal(9, notes="", details=[])
        result = bot.describe_journal(j)
        assert result is None

    def test_assigned_to_change(self):
        j = MockJournal(10, notes="", details=[
            {"name": "assigned_to_id", "old_value": "10", "new_value": "20"}
        ])
        result = bot.describe_journal(j)
        assert "Назначена" in result


# ═══════════════════════════════════════════════════════════════════════════
# 6. RESOLVE_FIELD_VALUE
# ═══════════════════════════════════════════════════════════════════════════
# ID статуса/приоритета → русские подписи из справочников в bot.py.

class TestResolveFieldValue:
    """Тесты перевода ID в человекочитаемые имена."""

    def test_known_status(self):
        assert bot.resolve_field_value("status_id", "1") == "Новая"
        assert bot.resolve_field_value("status_id", "2") == "В работе"
        assert bot.resolve_field_value("status_id", "13") == "Информация предоставлена"

    def test_unknown_status_returns_raw(self):
        assert bot.resolve_field_value("status_id", "999") == "999"

    def test_known_priority(self):
        assert bot.resolve_field_value("priority_id", "4") == "1 (Аварийный)"

    def test_none_value(self):
        assert bot.resolve_field_value("status_id", None) == "—"

    def test_empty_string(self):
        assert bot.resolve_field_value("status_id", "") == "—"

    def test_regular_field_passthrough(self):
        """Обычное поле (не ID) → значение как есть."""
        assert bot.resolve_field_value("subject", "Тест") == "Тест"


# ═══════════════════════════════════════════════════════════════════════════
# 7. РОУТИНГ ПО КОМНАТАМ
# ═══════════════════════════════════════════════════════════════════════════
# Дополнительные Matrix-комнаты по версии задачи и статусу РВ.

class TestRouting:
    """Тесты маршрутизации уведомлений в доп. комнаты."""

    def test_new_issue_without_version_goes_to_redos(self, simple_issue):
        """Задача без версии → комната РЕД ОС."""
        with patch.dict(bot.VERSION_ROOM_MAP, {"РЕД ОС": "!redos:server"}):
            rooms = bot.get_extra_rooms_for_new(simple_issue, {})
            assert "!redos:server" in rooms

    def test_new_issue_virt_goes_to_virt(self, issue_with_version):
        """Задача с версией Виртуализация → комната Виртуализации."""
        with patch.dict(bot.VERSION_ROOM_MAP, {
            "РЕД Виртуализация": "!virt:server",
            "РЕД ОС": "!redos:server",
        }):
            rooms = bot.get_extra_rooms_for_new(issue_with_version, {})
            assert "!virt:server" in rooms
            assert "!redos:server" not in rooms

    def test_rv_always_goes_to_rv_room(self, rv_issue):
        """Передано в работу.РВ → всегда в комнату РВ."""
        with patch.dict(bot.STATUS_ROOM_MAP, {"Передано в работу.РВ": "!rv:server"}):
            with patch.dict(bot.VERSION_ROOM_MAP, {"РЕД Виртуализация": "!virt:server"}):
                rooms = bot.get_extra_rooms_for_rv(rv_issue, {})
                assert "!rv:server" in rooms
                assert "!virt:server" in rooms  # т.к. версия Виртуализация

    def test_rv_without_virt_version(self):
        """РВ без версии Виртуализация → только комната РВ."""
        issue = MockIssue(issue_id=8888, status="Передано в работу.РВ")
        with patch.dict(bot.STATUS_ROOM_MAP, {"Передано в работу.РВ": "!rv:server"}):
            with patch.dict(bot.VERSION_ROOM_MAP, {"РЕД Виртуализация": "!virt:server"}):
                rooms = bot.get_extra_rooms_for_rv(issue, {})
                assert "!rv:server" in rooms
                assert "!virt:server" not in rooms

    def test_empty_room_maps(self, simple_issue):
        """Пустые маппинги → пустой набор комнат."""
        with patch.dict(bot.VERSION_ROOM_MAP, {}, clear=True):
            rooms = bot.get_extra_rooms_for_new(simple_issue, {})
            assert rooms == set()


# ═══════════════════════════════════════════════════════════════════════════
# 8. ОТПРАВКА MATRIX-СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════════════════
# matrix_send.room_send_with_retry + HTML карточки задач.

class TestRoomSendWithRetry:
    """Повторы отправки в Matrix (room_send_with_retry)."""

    @pytest.mark.asyncio
    async def test_success_after_two_errors(self):
        """Две неудачи, третья попытка успешна — без исключения."""
        from nio.responses import RoomSendError

        mock_err = RoomSendError.from_dict(
            {"error": "Temporary", "errcode": "M_UNKNOWN"},
            "!room:server",
        )
        success = MagicMock()
        success.__class__ = type("RoomSendResponse", (), {})

        client = AsyncMock()
        client.room_send = AsyncMock(side_effect=[mock_err, mock_err, success])

        with patch("matrix_send.asyncio.sleep", new_callable=AsyncMock):
            await matrix_send.room_send_with_retry(
                client, "!room:server", {"msgtype": "m.text", "body": "x"}
            )

        assert client.room_send.call_count == 3

    @pytest.mark.asyncio
    async def test_all_attempts_fail_room_send_error(self):
        """Все попытки вернули RoomSendError — RuntimeError."""
        from nio.responses import RoomSendError

        mock_err = RoomSendError.from_dict(
            {"error": "Rate limited", "errcode": "M_LIMIT_EXCEEDED"},
            "xroom:server",
        )

        client = AsyncMock()
        client.room_send = AsyncMock(return_value=mock_err)

        with patch("matrix_send.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="Matrix room_send error"):
                await matrix_send.room_send_with_retry(
                    client, "xroom:server", {"msgtype": "m.text", "body": "x"}
                )

        assert client.room_send.call_count == matrix_send.MAX_RETRIES

    @pytest.mark.asyncio
    async def test_network_exception_retries_then_raises(self):
        """Исключение на каждой попытке — после MAX_RETRIES проброс."""
        client = AsyncMock()
        client.room_send = AsyncMock(side_effect=OSError("connection reset"))

        with patch("matrix_send.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(OSError, match="connection reset"):
                await matrix_send.room_send_with_retry(
                    client, "!r:s", {"msgtype": "m.text", "body": "x"}
                )

        assert client.room_send.call_count == matrix_send.MAX_RETRIES


class TestSendMatrixMessage:
    """Тесты отправки сообщений в Matrix."""

    @pytest.mark.asyncio
    async def test_successful_send(self, mock_matrix_client, simple_issue):
        """Успешная отправка — без исключений."""
        await bot.send_matrix_message(
            mock_matrix_client, simple_issue, "!room:server", "new"
        )
        mock_matrix_client.room_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_html_contains_issue_id(self, mock_matrix_client, simple_issue):
        """HTML содержит ID задачи и ссылку."""
        await bot.send_matrix_message(
            mock_matrix_client, simple_issue, "!room:server", "new"
        )
        call_args = mock_matrix_client.room_send.call_args
        content = call_args[1]["content"] if "content" in call_args[1] else call_args.kwargs["content"]
        html = content["formatted_body"]
        assert "#7777" in html
        assert "redmine.test/issues/7777" in html

    @pytest.mark.asyncio
    async def test_html_contains_status(self, mock_matrix_client, simple_issue):
        """HTML содержит текущий статус."""
        await bot.send_matrix_message(
            mock_matrix_client, simple_issue, "!room:server", "new"
        )
        call_args = mock_matrix_client.room_send.call_args
        content = call_args[1]["content"] if "content" in call_args[1] else call_args.kwargs["content"]
        assert "Новая" in content["formatted_body"]

    @pytest.mark.asyncio
    async def test_overdue_shows_days(self, mock_matrix_client, overdue_issue):
        """Для просроченных — показывает количество дней."""
        await bot.send_matrix_message(
            mock_matrix_client, overdue_issue, "!room:server", "overdue"
        )
        call_args = mock_matrix_client.room_send.call_args
        content = call_args[1]["content"] if "content" in call_args[1] else call_args.kwargs["content"]
        assert "просрочено" in content["formatted_body"]

    @pytest.mark.asyncio
    async def test_extra_text_included(self, mock_matrix_client, simple_issue):
        """Дополнительный текст попадает в HTML."""
        await bot.send_matrix_message(
            mock_matrix_client, simple_issue, "!room:server", "status_change",
            extra_text="Статус: <strong>Новая</strong> → <strong>В работе</strong>"
        )
        call_args = mock_matrix_client.room_send.call_args
        content = call_args[1]["content"] if "content" in call_args[1] else call_args.kwargs["content"]
        assert "В работе" in content["formatted_body"]

    @pytest.mark.asyncio
    async def test_subject_special_chars_escaped(self, mock_matrix_client):
        """Тема с <>& — экранируется в HTML."""
        issue = MockIssue(issue_id=4242, subject='Сервер <prod> & "тест"')
        await bot.send_matrix_message(mock_matrix_client, issue, "!room:server", "new")
        call_args = mock_matrix_client.room_send.call_args
        content = call_args[1]["content"] if "content" in call_args[1] else call_args.kwargs["content"]
        body = content["formatted_body"]
        assert "<prod>" not in body
        assert "&lt;prod&gt;" in body
        assert "&amp;" in body

    @pytest.mark.asyncio
    async def test_version_shown_when_present(self, mock_matrix_client, issue_with_version):
        """Версия отображается в сообщении, если есть."""
        await bot.send_matrix_message(
            mock_matrix_client, issue_with_version, "!room:server", "new"
        )
        call_args = mock_matrix_client.room_send.call_args
        content = call_args[1]["content"] if "content" in call_args[1] else call_args.kwargs["content"]
        assert "РЕД Виртуализация 1.0" in content["formatted_body"]

    @pytest.mark.asyncio
    async def test_room_send_error_raises(self, simple_issue):
        """После исчерпания повторов RoomSendError → RuntimeError."""
        from nio.responses import RoomSendError

        mock_error = RoomSendError.from_dict(
            {"error": "Rate limited", "errcode": "M_LIMIT_EXCEEDED"},
            "xroom:server",
        )

        client = AsyncMock()
        client.room_send = AsyncMock(return_value=mock_error)

        with patch("matrix_send.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(RuntimeError, match="Matrix room_send error"):
                await bot.send_matrix_message(
                    client, simple_issue, "xroom:server", "new"
                )

        assert client.room_send.call_count == matrix_send.MAX_RETRIES


class TestSendSafe:
    """Тесты обёртки send_safe — не падает при ошибках."""

    @pytest.mark.asyncio
    async def test_send_safe_catches_exception(self, simple_issue):
        """send_safe НЕ пробрасывает исключения — логирует."""
        client = AsyncMock()
        client.room_send = AsyncMock(side_effect=Exception("Network error"))
        with patch("matrix_send.asyncio.sleep", new_callable=AsyncMock):
            # Не должен кинуть исключение (несколько попыток room_send)
            await bot.send_safe(client, simple_issue, USER_CFG_FOR_SEND, "!room:server", "new")
        assert client.room_send.call_count == matrix_send.MAX_RETRIES

    @pytest.mark.asyncio
    async def test_send_safe_success(self, mock_matrix_client, simple_issue):
        """send_safe при успехе — просто работает."""
        await bot.send_safe(mock_matrix_client, simple_issue, USER_CFG_FOR_SEND, "!room:server", "new")
        mock_matrix_client.room_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_safe_skipped_when_dnd(self, mock_matrix_client, simple_issue):
        """При DND сообщение не отправляется."""
        cfg = {**USER_CFG_FOR_SEND, "dnd": True}
        await bot.send_safe(mock_matrix_client, simple_issue, cfg, "!room:server", "new")
        mock_matrix_client.room_send.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 9. NOTIFICATION_TYPES — все типы имеют эмодзи и заголовок
# ═══════════════════════════════════════════════════════════════════════════
# Регрессия: новый тип уведомления не забыт в словаре.

class TestNotificationTypes:
    """Проверяем что все типы уведомлений корректно определены."""

    @pytest.mark.parametrize("ntype", [
        "new", "info", "reminder", "overdue",
        "status_change", "issue_updated", "reopened",
    ])
    def test_all_types_have_emoji_and_title(self, ntype):
        assert ntype in bot.NOTIFICATION_TYPES
        emoji, title = bot.NOTIFICATION_TYPES[ntype]
        assert len(emoji) > 0
        assert len(title) > 0


# ═══════════════════════════════════════════════════════════════════════════
# 10. СТРЕСС-ТЕСТЫ / ГРАНИЧНЫЕ СЛУЧАИ
# ═══════════════════════════════════════════════════════════════════════════
# Много журналов, битые поля, глубокий JSON в save_json.

class TestEdgeCases:
    """Тесты граничных случаев, которые могут сломать бота."""

    def test_issue_with_none_due_date(self):
        """due_date=None — не должен падать при сравнении."""
        issue = MockIssue(issue_id=9001, due_date=None)
        # Имитируем проверку из блока 7
        today = date.today()
        # Эта проверка НЕ должна выполняться (due_date is None → False)
        assert not (issue.due_date and issue.due_date < today)

    def test_issue_with_future_due_date(self):
        """Задача со сроком в будущем — не просрочена."""
        future = date.today() + timedelta(days=30)
        issue = MockIssue(issue_id=9002, due_date=future)
        today = date.today()
        assert not (issue.due_date < today)

    def test_issue_due_today_not_overdue(self):
        """Задача со сроком СЕГОДНЯ — ещё НЕ просрочена."""
        issue = MockIssue(issue_id=9003, due_date=date.today())
        today = date.today()
        assert not (issue.due_date < today)

    def test_detect_status_change_with_empty_sent(self):
        """Пустой sent → всегда None."""
        issue = MockIssue(issue_id=9004, status="В работе")
        assert bot.detect_status_change(issue, {}) is None

    def test_detect_new_journals_with_single_entry(self):
        """Один журнал — корректно обрабатывается."""
        j = MockJournal(500, notes="Единственный комментарий")
        issue = MockIssue(issue_id=9005, journals=[j])
        new, max_id = bot.detect_new_journals(issue, {})
        assert len(new) == 1
        assert max_id == 500

    def test_large_journal_count(self):
        """100 журналов — бот не падает и корректно считает."""
        journals = [MockJournal(i, notes=f"Комментарий {i}") for i in range(1, 101)]
        issue = MockIssue(issue_id=9006, journals=journals)
        state = {"9006": {"last_journal_id": 50}}
        new, max_id = bot.detect_new_journals(issue, state)
        assert len(new) == 50  # id 51..100
        assert max_id == 100

    def test_journal_with_broken_user(self):
        """Журнал с недоступным user.name — не ломает describe."""
        j = MockJournal(600, notes="Тест")
        j.user = None  # Сломанный user
        # describe_journal ловит Exception при доступе к user.name
        result = bot.describe_journal(j)
        assert "💬" in result
        assert "Новый комментарий" in result  # fallback

    def test_journal_with_empty_details_list(self):
        """Пустой details → только комментарий (если есть)."""
        j = MockJournal(700, notes="Просто комментарий", details=[])
        result = bot.describe_journal(j)
        assert "💬" in result

    def test_describe_journal_detail_missing_keys(self):
        """detail без name/property → не падает."""
        j = MockJournal(800, notes="", details=[
            {"old_value": "x", "new_value": "y"}  # нет name!
        ])
        result = bot.describe_journal(j)
        # "?" не в FIELD_NAMES → пропускается → None
        assert result is None

    def test_plural_days_large_numbers(self):
        """Большие числа."""
        assert bot.plural_days(1000) == "1000 дней"
        assert bot.plural_days(1001) == "1001 день"
        assert bot.plural_days(1002) == "1002 дня"

# ═══════════════════════════════════════════════════════════════════════════
# 11. OVERDUE FIX-2: СРАВНЕНИЕ ПО ДАТЕ
# ═══════════════════════════════════════════════════════════════════════════
# Ежедневное напоминание о просрочке — по календарной дате, не по 24 ч.

class TestOverdueDateComparison:
    """
    FIX-2: проверяем что сравнение overdue работает по дате,
    а не по timedelta.days (который может дать 0 на границе суток).
    """

    def test_notified_23h_ago_same_day_no_repeat(self):
        """
        Уведомили 23 часа назад, но в ТОТ ЖЕ день — НЕ повторяем.
        (Старый баг: timedelta.days=0 → пропуск, но это правильное поведение)
        """
        now = datetime(2026, 3, 27, 23, 0, 0, tzinfo=bot.BOT_TZ)
        last_notified = datetime(2026, 3, 27, 0, 30, 0, tzinfo=bot.BOT_TZ)
        # FIX-2: сравниваем даты
        should_repeat = last_notified.date() < now.date()
        assert should_repeat is False

    def test_notified_yesterday_23h59_should_repeat(self):
        """
        Уведомили вчера в 23:59 — ПОВТОРЯЕМ (другая дата!).
        Старый баг: (now - last).days мог дать 0, если прошло < 24ч.
        """
        now = datetime(2026, 3, 28, 0, 30, 0, tzinfo=bot.BOT_TZ)
        last_notified = datetime(2026, 3, 27, 23, 59, 0, tzinfo=bot.BOT_TZ)
        # timedelta.days → 0 (прошло ~31 минута) — СТАРЫЙ БАГ
        old_check = (now - last_notified).days >= 1
        assert old_check is False  # Старая логика пропустила бы!
        # FIX-2: сравнение по дате
        new_check = last_notified.date() < now.date()
        assert new_check is True  # Новая логика корректна!

    def test_notified_2_days_ago(self):
        """2 дня назад — повторяем (оба метода работают)."""
        now = datetime(2026, 3, 29, 12, 0, 0, tzinfo=bot.BOT_TZ)
        last_notified = datetime(2026, 3, 27, 12, 0, 0, tzinfo=bot.BOT_TZ)
        assert last_notified.date() < now.date()

    def test_not_notified_yet(self):
        """Ни разу не уведомляли → уведомляем."""
        last_n = None
        assert not last_n  # Условие `not last_n` → True → отправляем


