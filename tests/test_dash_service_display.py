"""Тесты для src/dash_service_display.py."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

import dash_service_display as dsd


# ═══════════════════════════════════════════════════════════════════════════
# parse_docker_started_at
# ═══════════════════════════════════════════════════════════════════════════


class TestParseDockerStartedAt:
    """parse_docker_started_at: парсинг StartedAt из Docker inspect."""

    def test_valid_rfc3339(self):
        dt = dsd.parse_docker_started_at("2024-01-15T10:30:00Z")
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 1
        assert dt.day == 15

    def test_empty_string(self):
        assert dsd.parse_docker_started_at("") is None

    def test_none(self):
        assert dsd.parse_docker_started_at(None) is None

    def test_zero_date(self):
        assert dsd.parse_docker_started_at("0001-01-01T00:00:00Z") is None

    def test_whitespace_only(self):
        assert dsd.parse_docker_started_at("   ") is None

    def test_with_fractional_seconds(self):
        dt = dsd.parse_docker_started_at("2024-06-01T12:00:00.123456789Z")
        assert dt is not None
        assert dt.hour == 12

    def test_with_timezone_offset(self):
        dt = dsd.parse_docker_started_at("2024-06-01T12:00:00+03:00")
        assert dt is not None

    def test_invalid_format(self):
        assert dsd.parse_docker_started_at("not-a-date") is None


# ═══════════════════════════════════════════════════════════════════════════
# humanize_uptime_ru
# ═══════════════════════════════════════════════════════════════════════════


class TestHumanizeUptimeRu:
    """humanize_uptime_ru: человеко-читаемый uptime на русском."""

    def test_none_returns_dash(self):
        assert dsd.humanize_uptime_ru(None) == "—"

    def test_zero_seconds(self):
        now = datetime.now(timezone.utc)
        assert dsd.humanize_uptime_ru(now, now) == "0 секунд"

    def test_1_second(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(seconds=1)
        assert "1 секунда" in dsd.humanize_uptime_ru(started, now)

    def test_5_seconds(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(seconds=5)
        result = dsd.humanize_uptime_ru(started, now)
        assert "5 секунд" in result

    def test_1_minute(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(minutes=1)
        result = dsd.humanize_uptime_ru(started, now)
        assert "1 минута" in result

    def test_5_minutes(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(minutes=5)
        result = dsd.humanize_uptime_ru(started, now)
        assert "5 минут" in result

    def test_1_hour(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(hours=1)
        result = dsd.humanize_uptime_ru(started, now)
        assert "1 час" in result

    def test_2_hours(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(hours=2)
        result = dsd.humanize_uptime_ru(started, now)
        assert "2 часа" in result

    def test_5_hours(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(hours=5)
        result = dsd.humanize_uptime_ru(started, now)
        assert "5 часов" in result

    def test_1_day(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(days=1)
        result = dsd.humanize_uptime_ru(started, now)
        assert "1 день" in result

    def test_2_days(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(days=2)
        result = dsd.humanize_uptime_ru(started, now)
        assert "2 дня" in result

    def test_5_days(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(days=5)
        result = dsd.humanize_uptime_ru(started, now)
        assert "5 дней" in result

    def test_1_month(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(days=30)
        result = dsd.humanize_uptime_ru(started, now)
        assert "1 месяц" in result

    def test_1_year(self):
        now = datetime.now(timezone.utc)
        started = now - timedelta(days=365)
        result = dsd.humanize_uptime_ru(started, now)
        assert "1 год" in result

    def test_negative_seconds_returns_dash(self):
        now = datetime.now(timezone.utc)
        started = now + timedelta(seconds=10)  # будущее
        assert dsd.humanize_uptime_ru(started, now) == "—"

    def test_naive_datetime_treated_as_utc(self):
        now = datetime.now(timezone.utc)
        started_naive = datetime(2024, 1, 1, 0, 0, 0)  # без tzinfo
        result = dsd.humanize_uptime_ru(started_naive, now)
        assert result != "—"
        assert "год" in result or "месяц" in result or "день" in result


# ═══════════════════════════════════════════════════════════════════════════
# format_local_started_at
# ═══════════════════════════════════════════════════════════════════════════


class TestFormatLocalStartedAt:
    """format_local_started_at: форматирование даты старта в локальной таймзоне."""

    def test_none_returns_dash(self):
        assert dsd.format_local_started_at(None, "Europe/Moscow") == "—"

    def test_formats_datetime(self):
        dt = datetime(2024, 6, 15, 10, 30, 45, tzinfo=timezone.utc)
        result = dsd.format_local_started_at(dt, "Europe/Moscow")
        assert "2024" in result
        assert "06" in result or "6" in result

    def test_default_timezone(self):
        dt = datetime(2024, 6, 15, 10, 30, 45, tzinfo=timezone.utc)
        result = dsd.format_local_started_at(dt, "")
        assert "2024" in result

    def test_invalid_timezone_falls_back(self):
        dt = datetime(2024, 6, 15, 10, 30, 45, tzinfo=timezone.utc)
        result = dsd.format_local_started_at(dt, "Invalid/Zone")
        assert "2024" in result


# ═══════════════════════════════════════════════════════════════════════════
# bot_status_label_ru
# ═══════════════════════════════════════════════════════════════════════════


class TestBotStatusLabelRu:
    """bot_status_label_ru: подписи состояния бота."""

    def test_error_state(self):
        assert dsd.bot_status_label_ru({"state": "error"}) == "Ошибка Docker"

    def test_not_found_state(self):
        assert dsd.bot_status_label_ru({"state": "not_found"}) == "Контейнер не найден"

    def test_restarting(self):
        assert dsd.bot_status_label_ru({"docker_status": "restarting"}) == "Рестарт"

    def test_running_true(self):
        assert dsd.bot_status_label_ru({"running": True}) == "Включен"

    def test_running_status(self):
        assert dsd.bot_status_label_ru({"docker_status": "running"}) == "Включен"

    def test_exited(self):
        assert dsd.bot_status_label_ru({"docker_status": "exited"}) == "Выключен"

    def test_dead(self):
        assert dsd.bot_status_label_ru({"docker_status": "dead"}) == "Выключен"

    def test_paused(self):
        assert dsd.bot_status_label_ru({"docker_status": "paused"}) == "Пауза"

    def test_created(self):
        assert dsd.bot_status_label_ru({"docker_status": "created"}) == "Запуск…"

    def test_removing(self):
        assert dsd.bot_status_label_ru({"docker_status": "removing"}) == "Запуск…"

    def test_running_false(self):
        assert dsd.bot_status_label_ru({"running": False}) == "Выключен"

    def test_empty_dict(self):
        assert dsd.bot_status_label_ru({}) == "Неизвестно"


# ═══════════════════════════════════════════════════════════════════════════
# service_card_context
# ═══════════════════════════════════════════════════════════════════════════


class TestServiceCardContext:
    """service_card_context: контекст для карточки «Сервис» на дашборде."""

    def test_running_container(self):
        docker = {
            "running": True,
            "started_at": "2024-01-01T00:00:00Z",
            "docker_status": "running",
            "state": "",
        }
        ctx = dsd.service_card_context(docker, {}, "Europe/Moscow")
        assert ctx["bot_status_label"] == "Включен"
        assert ctx["started_display"] != "—"
        assert ctx["uptime_display"] != "—"
        assert ctx["error_count"] == 0

    def test_stopped_container(self):
        docker = {
            "running": False,
            "docker_status": "exited",
            "state": "",
        }
        ctx = dsd.service_card_context(docker, {}, "Europe/Moscow")
        assert ctx["bot_status_label"] == "Выключен"
        assert ctx["started_display"] == "—"
        assert ctx["uptime_display"] == "—"

    def test_error_container(self):
        docker = {"state": "error"}
        ctx = dsd.service_card_context(docker, {}, "Europe/Moscow")
        assert ctx["bot_status_label"] == "Ошибка Docker"
        assert ctx["started_display"] == "—"
        assert ctx["uptime_display"] == "—"

    def test_error_count_from_cycle(self):
        docker = {"running": True}
        cycle = {"error_count": 5}
        ctx = dsd.service_card_context(docker, cycle, "Europe/Moscow")
        assert ctx["error_count"] == 5

    def test_invalid_error_count(self):
        docker = {"running": True}
        cycle = {"error_count": "not_a_number"}
        ctx = dsd.service_card_context(docker, cycle, "Europe/Moscow")
        assert ctx["error_count"] == 0

    def test_missing_error_count(self):
        docker = {"running": True}
        ctx = dsd.service_card_context(docker, {}, "Europe/Moscow")
        assert ctx["error_count"] == 0
