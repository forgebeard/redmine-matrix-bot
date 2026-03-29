"""
Утилиты общего назначения (чистые функции).

Даты/таймзона, склонение «день», safe_html для вставки текста из Redmine
в HTML сообщений Matrix. Импортируется из bot.py (после добавления src в sys.path)
и из других модулей src/.
"""

from datetime import datetime, date
from html import escape as _html_escape
from zoneinfo import ZoneInfo

# Таймзона по умолчанию (перезаписывается из config при импорте)
BOT_TZ = ZoneInfo("Europe/Moscow")


def set_timezone(tz_name: str):
    """Устанавливает глобальную таймзону бота."""
    global BOT_TZ
    BOT_TZ = ZoneInfo(tz_name)


def now_tz() -> datetime:
    """Текущее время в таймзоне бота."""
    return datetime.now(tz=BOT_TZ)


def today_tz() -> date:
    """Сегодняшняя дата в таймзоне бота."""
    return now_tz().date()


def ensure_tz(dt: datetime) -> datetime:
    """Гарантирует наличие таймзоны у datetime."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BOT_TZ)
    return dt


def plural_days(n: int) -> str:
    """
    Склонение слова 'день': 1 день, 2 дня, 5 дней.
    Работает для любых целых чисел включая отрицательные.
    """
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} дня"
    return f"{n} дней"


def safe_html(text: str) -> str:
    """
    Экранирует HTML-спецсимволы в пользовательском тексте.
    Защита от XSS при вставке subject/notes в HTML-сообщения Matrix.
    """
    if not text:
        return ""
    return _html_escape(str(text), quote=True)