"""
Тесты src/state.py: пути к data/state_*.json, load_json, save_json.

Повторяют контракт корневого bot.py для state (тот же формат файлов в data/).
"""

import json
from pathlib import Path
import tempfile

from state import state_file, load_json, save_json
from config import DATA_DIR


class TestStateFile:
    """Пути к state-файлам."""

    def test_returns_path_in_data_dir(self):
        p = state_file(1972, "sent")
        assert p.parent == DATA_DIR

    def test_correct_name(self):
        p = state_file(1972, "sent")
        assert p.name == "state_1972_sent.json"

    def test_different_users(self):
        p1 = state_file(1972, "sent")
        p2 = state_file(3254, "sent")
        assert p1 != p2

    def test_different_names(self):
        p1 = state_file(1972, "sent")
        p2 = state_file(1972, "journals")
        assert p1 != p2


class TestLoadJson:
    """Загрузка JSON."""

    def test_existing_file(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}', encoding="utf-8")
        data = load_json(f)
        assert data == {"key": "value"}

    def test_nonexistent_returns_default(self):
        data = load_json("/tmp/nonexistent_xyz_12345.json")
        assert data == {}

    def test_custom_default(self):
        data = load_json("/tmp/nonexistent_xyz_12345.json", default={"a": 1})
        assert data == {"a": 1}

    def test_corrupt_json_returns_default(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json {{{", encoding="utf-8")
        data = load_json(f)
        assert data == {}

    def test_empty_file_returns_default(self, tmp_path):
        f = tmp_path / "empty.json"
        f.write_text("", encoding="utf-8")
        data = load_json(f)
        assert data == {}


class TestSaveJson:
    """Атомарная запись JSON."""

    def test_write_and_read(self, tmp_path):
        f = tmp_path / "out.json"
        ok = save_json(f, {"число": 42, "список": [1, 2, 3]})
        assert ok is True
        data = load_json(f)
        assert data["число"] == 42
        assert data["список"] == [1, 2, 3]

    def test_overwrite(self, tmp_path):
        f = tmp_path / "out.json"
        save_json(f, {"v": 1})
        save_json(f, {"v": 2})
        data = load_json(f)
        assert data["v"] == 2

    def test_no_tmp_file_left(self, tmp_path):
        f = tmp_path / "out.json"
        save_json(f, {"ok": True})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_unicode(self, tmp_path):
        f = tmp_path / "uni.json"
        save_json(f, {"текст": "Привет мир 🔥"})
        data = load_json(f)
        assert data["текст"] == "Привет мир 🔥"

    def test_returns_bool(self, tmp_path):
        f = tmp_path / "out.json"
        result = save_json(f, {})
        assert isinstance(result, bool)