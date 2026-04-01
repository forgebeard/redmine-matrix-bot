"""Юнит-тесты для admin.notify_prefs."""

from admin.notify_prefs import parse_notify, parse_work_days


def test_parse_notify_invalid_json():
    assert parse_notify("not json") == ["all"]


def test_parse_notify_object_not_list():
    assert parse_notify('{"a": 1}') == ["all"]


def test_parse_notify_valid_list():
    assert parse_notify('["new"]') == ["new"]


def test_parse_work_days_empty():
    assert parse_work_days("") is None
    assert parse_work_days("   ") is None


def test_parse_work_days_invalid():
    assert parse_work_days("x") is None


def test_parse_work_days_valid():
    assert parse_work_days("[1, 2]") == [1, 2]
