"""Тесты для хелперов из routes/events.py и routes/ops.py."""

from __future__ import annotations

import pytest

import admin.routes.events as events_mod
import admin.routes.ops as ops_mod


# ═══════════════════════════════════════════════════════════════════════════
# events._events_filter_query_dict
# ═══════════════════════════════════════════════════════════════════════════


class TestEventsFilterQueryDict:
    """Формирование query-параметров для фильтра событий."""

    def test_all_empty(self):
        d = events_mod._events_filter_query_dict("", "", "", 50)
        assert d == {"page_size": "50"}

    def test_date_from_only(self):
        d = events_mod._events_filter_query_dict("2024-01-01", "", "", 50)
        assert d == {"page_size": "50", "date_from": "2024-01-01"}

    def test_date_range(self):
        d = events_mod._events_filter_query_dict("2024-01-01", "2024-01-31", "", 100)
        assert d["date_from"] == "2024-01-01"
        assert d["date_to"] == "2024-01-31"
        assert d["page_size"] == "100"

    def test_time_filter(self):
        d = events_mod._events_filter_query_dict("", "", "14:30", 50)
        assert d == {"page_size": "50", "time_at": "14:30"}

    def test_all_filters(self):
        d = events_mod._events_filter_query_dict("2024-01-01", "2024-01-31", "09:00", 25)
        assert d == {
            "page_size": "25",
            "date_from": "2024-01-01",
            "date_to": "2024-01-31",
            "time_at": "09:00",
        }

    def test_strips_whitespace(self):
        d = events_mod._events_filter_query_dict("  2024-01-01  ", "", "  ", 50)
        assert d == {"page_size": "50", "date_from": "2024-01-01"}


# ═══════════════════════════════════════════════════════════════════════════
# events._normalize_time_filter
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeTimeFilter:
    """Нормализация фильтра времени."""

    def test_valid_time(self):
        assert events_mod._normalize_time_filter("14:30") == "14:30"

    def test_valid_time_zero_padded(self):
        assert events_mod._normalize_time_filter("09:05") == "09:05"

    def test_empty_string(self):
        assert events_mod._normalize_time_filter("") == ""

    def test_whitespace_only(self):
        assert events_mod._normalize_time_filter("   ") == ""

    def test_invalid_format_1digit_hour(self):
        assert events_mod._normalize_time_filter("9:30") == ""

    def test_invalid_format_no_colon(self):
        assert events_mod._normalize_time_filter("1430") == ""

    def test_invalid_too_many_digits(self):
        assert events_mod._normalize_time_filter("14:30:00") == ""

    def test_list_single_element(self):
        assert events_mod._normalize_time_filter(["14:30"]) == "14:30"

    def test_list_empty(self):
        assert events_mod._normalize_time_filter([]) == ""

    def test_tuple_single_element(self):
        assert events_mod._normalize_time_filter(("09:00",)) == "09:00"

    def test_non_string_converted(self):
        assert events_mod._normalize_time_filter(123) == ""

    def test_none_converted(self):
        assert events_mod._normalize_time_filter(None) == ""


# ═══════════════════════════════════════════════════════════════════════════
# ops._truncate_ops_detail
# ═══════════════════════════════════════════════════════════════════════════


class TestTruncateOpsDetail:
    """Обрезка длинных строк операций."""

    def test_short_string_unchanged(self):
        s = "Docker bot/start ok"
        assert ops_mod._truncate_ops_detail(s) == s

    def test_exact_max_len(self):
        s = "x" * 400
        assert ops_mod._truncate_ops_detail(s) == s

    def test_over_max_len_truncated(self):
        s = "x" * 410
        result = ops_mod._truncate_ops_detail(s)
        assert len(result) == 400
        assert result.endswith("…")
        assert result.startswith("xxxx")

    def test_newlines_replaced(self):
        s = "line1\nline2\nline3"
        result = ops_mod._truncate_ops_detail(s)
        assert "\n" not in result
        assert "line1 line2 line3" == result

    def test_carriage_returns_replaced(self):
        s = "line1\rline2"
        result = ops_mod._truncate_ops_detail(s)
        assert "\r" not in result

    def test_mixed_newlines(self):
        s = "line1\r\nline2\nline3"
        result = ops_mod._truncate_ops_detail(s)
        assert "\n" not in result
        assert "\r" not in result

    def test_none_becomes_empty(self):
        assert ops_mod._truncate_ops_detail(None) == ""

    def test_empty_string(self):
        assert ops_mod._truncate_ops_detail("") == ""

    def test_custom_max_len(self):
        s = "1234567890"
        result = ops_mod._truncate_ops_detail(s, max_len=5)
        assert len(result) == 5
        assert result == "1234…"

    def test_truncation_preserves_start(self):
        s = "IMPORTANT_PREFIX_1234567890..."
        result = ops_mod._truncate_ops_detail(s, max_len=20)
        assert result.startswith("IMPORTANT_PREFIX")
