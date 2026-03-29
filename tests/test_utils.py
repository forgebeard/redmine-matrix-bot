"""
Тесты src/utils.py: даты, таймзона, plural_days, safe_html.

Без сети и Redmine — быстрые unit-тесты на граничные значения и экранирование HTML.
"""

from datetime import datetime, date
from zoneinfo import ZoneInfo
import pytest

from utils import plural_days, now_tz, today_tz, ensure_tz, safe_html, set_timezone, BOT_TZ


# ═══════════════════════════════════════════════════════════════
# plural_days
# ═══════════════════════════════════════════════════════════════

class TestPluralDays:
    """Склонение слова 'день'."""

    def test_1(self):
        assert plural_days(1) == "1 день"

    def test_2(self):
        assert plural_days(2) == "2 дня"

    def test_5(self):
        assert plural_days(5) == "5 дней"

    def test_11(self):
        assert plural_days(11) == "11 дней"

    def test_21(self):
        assert plural_days(21) == "21 день"

    def test_0(self):
        assert plural_days(0) == "0 дней"

    def test_negative(self):
        assert plural_days(-3) == "3 дня"

    def test_101(self):
        assert plural_days(101) == "101 день"

    def test_111(self):
        assert plural_days(111) == "111 дней"

    def test_12(self):
        assert plural_days(12) == "12 дней"

    def test_22(self):
        assert plural_days(22) == "22 дня"


# ═══════════════════════════════════════════════════════════════
# ensure_tz
# ═══════════════════════════════════════════════════════════════

class TestEnsureTz:
    """Гарантия таймзоны у datetime."""

    def test_naive_gets_tz(self):
        dt = datetime(2026, 3, 28, 12, 0, 0)
        result = ensure_tz(dt)
        assert result.tzinfo is not None

    def test_aware_not_overwritten(self):
        utc = ZoneInfo("UTC")
        dt = datetime(2026, 3, 28, 12, 0, 0, tzinfo=utc)
        result = ensure_tz(dt)
        assert result.tzinfo == utc  # Не перезаписана на BOT_TZ


# ═══════════════════════════════════════════════════════════════
# now_tz / today_tz
# ═══════════════════════════════════════════════════════════════

class TestTimeHelpers:
    """Хелперы текущего времени."""

    def test_now_tz_has_timezone(self):
        assert now_tz().tzinfo is not None

    def test_today_tz_is_date(self):
        assert isinstance(today_tz(), date)

    def test_now_tz_matches_today(self):
        assert now_tz().date() == today_tz()


# ═══════════════════════════════════════════════════════════════
# safe_html
# ═══════════════════════════════════════════════════════════════

class TestSafeHtml:
    """XSS-защита при вставке в HTML."""

    def test_script_tag(self):
        assert "&lt;script&gt;" in safe_html("<script>alert(1)</script>")

    def test_ampersand(self):
        assert "&amp;" in safe_html("A & B")

    def test_quotes(self):
        result = safe_html('value="test"')
        assert "&quot;" in result

    def test_empty_string(self):
        assert safe_html("") == ""

    def test_none_returns_empty(self):
        assert safe_html(None) == ""

    def test_normal_text_unchanged(self):
        assert safe_html("Обычный текст 123") == "Обычный текст 123"

    def test_cyrillic_safe(self):
        text = "Задача «Тестовая» — готова"
        assert safe_html(text) == text


# ═══════════════════════════════════════════════════════════════
# set_timezone
# ═══════════════════════════════════════════════════════════════

class TestSetTimezone:
    """Смена глобальной таймзоны."""

    def test_change_timezone(self):
        set_timezone("Asia/Vladivostok")
        dt = now_tz()
        assert str(dt.tzinfo) == "Asia/Vladivostok"
        # Возвращаем обратно
        set_timezone("Europe/Moscow")

    def test_invalid_timezone_raises(self):
        with pytest.raises(Exception):
            set_timezone("Invalid/Timezone")