"""利用者設定(user_settings)のユニットテスト。

VBA `GetSetting`/`SaveSetting` の代替。拠点は複数画面が共有する設定なので、
保存と読み出しが確実に往復することを確認する。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import user_settings as svc


class UserSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch(
            "packaging_tool.config.USER_CONFIG_PATH", Path(self._tmp.name) / "cfg.json")
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(svc.load_all(), {})
        self.assertIsNone(svc.get("anything"))

    def test_save_then_get_round_trip(self):
        self.assertTrue(svc.save("k", "v"))
        self.assertEqual(svc.get("k"), "v")

    def test_save_keeps_other_keys(self):
        svc.save("a", 1)
        svc.save("b", 2)
        self.assertEqual(svc.load_all(), {"a": 1, "b": 2})

    def test_broken_file_falls_back_to_empty(self):
        from packaging_tool import config

        config.USER_CONFIG_PATH.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(svc.load_all(), {})

    def test_non_dict_json_is_ignored(self):
        from packaging_tool import config

        config.USER_CONFIG_PATH.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(svc.load_all(), {})

    def test_written_file_is_readable_json(self):
        from packaging_tool import config

        svc.set_position("HVC")
        data = json.loads(config.USER_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data[svc.KEY_POSITION], "HVC")


class PositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = mock.patch(
            "packaging_tool.config.USER_CONFIG_PATH", Path(self._tmp.name) / "cfg.json")
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_default_when_unset(self):
        self.assertEqual(svc.get_position(), svc.DEFAULT_POSITION)
        self.assertFalse(svc.is_position_set())

    def test_label_shows_unset_marker(self):
        self.assertEqual(svc.get_position_label(), svc.UNSET_LABEL)

    def test_set_then_get(self):
        self.assertTrue(svc.set_position("LVC"))
        self.assertEqual(svc.get_position(), "LVC")
        self.assertEqual(svc.get_position_label(), "LVC")
        self.assertTrue(svc.is_position_set())

    def test_blank_value_falls_back_to_default(self):
        svc.save(svc.KEY_POSITION, "")
        self.assertEqual(svc.get_position(), svc.DEFAULT_POSITION)
        self.assertEqual(svc.get_position_label(), svc.UNSET_LABEL)

    def test_explicit_default_is_honoured(self):
        self.assertEqual(svc.get_position(default="HVC"), "HVC")


if __name__ == "__main__":
    unittest.main()
