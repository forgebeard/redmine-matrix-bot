"""
Отображение лога на странице «События»: таблица (дата, время, уровень, сообщение), фильтр по датам, CSV.

Префикс времени в формате logging `YYYY-MM-DD HH:MM:SS,mmm` обычно в **UTC** в контейнере Docker
(локаль процесса). По умолчанию парсим как UTC и переводим в BOT_TIMEZONE для показа.
Отключение: ADMIN_EVENTS_LOG_PARSE_AS_UTC=0 — тогда время в логе считается уже в BOT_TIMEZONE
(только убираем миллисекунды и переворачиваем порядок).
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

# Стандартный asctime logging: 2026-04-02 06:21:14,317
_RE_ISO_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})(?:[.,]\d+)?(\s.*)?$",
)
# Уже записано админкой: 02.04.2026 09:21:14
_RE_DMY_TS = re.compile(
    r"^(\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2})(\s.*)?$",
)


def _display_tz() -> ZoneInfo:
    name = (os.getenv("BOT_TIMEZONE") or "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def admin_events_log_timestamp_now() -> str:
    """Метка времени для строк [ADMIN] в том же файле, что и «События» (ДД.ММ.ГГГГ, зона BOT_TIMEZONE)."""
    return datetime.now(_display_tz()).strftime("%d.%m.%Y %H:%M:%S")


def _parse_as_utc() -> bool:
    v = (os.getenv("ADMIN_EVENTS_LOG_PARSE_AS_UTC") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def reformat_log_line(line: str, *, display_tz: ZoneInfo, assume_utc: bool) -> str:
    """Одна строка: ISO+мс → ДД.ММ.ГГГГ ЧЧ:ММ:СС в display_tz; строки уже в ДД.ММ.ГГГГ — без изменений."""
    if not line.strip():
        return line
    m = _RE_ISO_TS.match(line)
    if m:
        date_s, time_s, tail = m.group(1), m.group(2), m.group(3) or ""
        try:
            naive = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return line
        if assume_utc:
            aware = naive.replace(tzinfo=timezone.utc)
        else:
            aware = naive.replace(tzinfo=display_tz)
        local = aware.astimezone(display_tz)
        return f"{local.strftime('%d.%m.%Y %H:%M:%S')}{tail}"
    if _RE_DMY_TS.match(line):
        return line
    return line


@dataclass(frozen=True)
class ParsedLogLine:
    """Одна строка файла событий после разбора (для таблицы и CSV)."""

    date_ui: str
    time_ui: str
    level: str
    message: str
    sort_key: datetime | None
    raw: str


def _unparsed_line(raw: str) -> ParsedLogLine:
    s = raw.rstrip("\n\r")
    return ParsedLogLine("—", "—", "—", s[:8000] if s else "—", None, raw)


def _safe_startswith(val, prefix: str) -> bool:
    return str(val).startswith(prefix)


def parse_events_log_line(line: str, *, display_tz: ZoneInfo, assume_utc: bool) -> ParsedLogLine:
    """Разбор одной строки: ISO asctime бота или ДД.ММ.ГГГГ с опциональным [LEVEL]."""
    raw = line
    s = line.rstrip("\n\r")
    if not s.strip():
        return _unparsed_line(raw)

    m = _RE_ISO_TS.match(s)
    if m:
        date_s, time_s, tail = m.group(1), m.group(2), (m.group(3) or "").lstrip()
        level = "—"
        message = tail
        if _safe_startswith(tail, "["):
            bm = re.match(r"^\[(\w+)\]\s*(.*)$", tail, re.DOTALL)
            if bm:
                level = bm.group(1)
                message = bm.group(2)
        try:
            naive = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return _unparsed_line(raw)
        if assume_utc:
            aware = naive.replace(tzinfo=timezone.utc)
        else:
            aware = naive.replace(tzinfo=display_tz)
        local = aware.astimezone(display_tz)
        return ParsedLogLine(
            local.strftime("%d.%m.%Y"),
            local.strftime("%H:%M:%S"),
            level,
            message if message.strip() else "—",
            aware,
            raw,
        )

    m = _RE_DMY_TS.match(s)
    if m:
        dmy_time = m.group(1)
        tail = (m.group(2) or "").lstrip()
        level = "—"
        message = tail
        if _safe_startswith(tail, "["):
            bm = re.match(r"^\[(\w+)\]\s*(.*)$", tail, re.DOTALL)
            if bm:
                level = bm.group(1)
                message = bm.group(2)
        try:
            naive = datetime.strptime(dmy_time, "%d.%m.%Y %H:%M:%S")
        except ValueError:
            return _unparsed_line(raw)
        aware = naive.replace(tzinfo=display_tz)
        local = aware.astimezone(display_tz)
        return ParsedLogLine(
            local.strftime("%d.%m.%Y"),
            local.strftime("%H:%M:%S"),
            level,
            message if message.strip() else "—",
            aware,
            raw,
        )

    return _unparsed_line(raw)


def _safe_startswith(val, prefix: str) -> bool:
    """Безопасный startswith: приводит val к строке, если это не строка."""
    return str(val).startswith(prefix)


def parse_events_log_for_table(raw: str) -> list[ParsedLogLine]:
    """
    Все строки файла → список; сортировка от новых к старым по разобранному времени.
    Служебные сообщения об отсутствии файла — одна строка таблицы без sort_key.
    """
    raw_str = str(raw)
    if not raw_str or raw_str.startswith("Файл лога не найден") or raw_str.startswith("Не удалось прочитать"):
        return [_unparsed_line(raw_str)]

    tz = _display_tz()
    assume = _parse_as_utc()
    out: list[ParsedLogLine] = []
    for line in raw_str.splitlines():
        if not line.strip():
            continue
        out.append(parse_events_log_line(line, display_tz=tz, assume_utc=assume))

    min_utc = datetime.min.replace(tzinfo=timezone.utc)

    def sk(pl: ParsedLogLine) -> datetime:
        return pl.sort_key if pl.sort_key is not None else min_utc

    out.sort(key=sk, reverse=True)
    return out


def parse_ui_date_param(raw: str) -> date | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def filter_parsed_lines_by_local_date(
    lines: list[ParsedLogLine],
    date_from: date | None,
    date_to: date | None,
    display_tz: ZoneInfo,
) -> list[ParsedLogLine]:
    """Фильтр по календарной дате в display_tz (как поля «с даты / по дату» в UI)."""
    if date_from is None and date_to is None:
        return lines
    out: list[ParsedLogLine] = []
    for pl in lines:
        if pl.sort_key is None:
            continue
        local_d = pl.sort_key.astimezone(display_tz).date()
        if date_from and local_d < date_from:
            continue
        if date_to and local_d > date_to:
            continue
        out.append(pl)
    return out


def events_log_to_csv_bytes(lines: list[ParsedLogLine], *, max_rows: int = 50_000) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "time", "level", "message"])
    for pl in lines[:max_rows]:
        msg = pl.message.replace("\n", " ").replace("\r", " ")
        if len(msg) > 8000:
            msg = msg[:7997] + "…"
        writer.writerow([pl.date_ui, pl.time_ui, pl.level, msg])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def format_events_log_for_ui(raw: str) -> str:
    raw_str = str(raw)
    if not raw_str or raw_str.startswith("Файл лога не найден") or raw_str.startswith("Не удалось прочитать"):
        return raw_str
    assume_utc = _parse_as_utc()
    tz = _display_tz()
    lines = raw_str.splitlines()
    out = [reformat_log_line(line, display_tz=tz, assume_utc=assume_utc) for line in lines]
    out.reverse()
    return "\n".join(out)
