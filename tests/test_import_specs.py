"""取り込み定義の値変換(`import_specs._text` 等)のテスト。

【ここで守りたいこと】
sqlite3 は UTF-8 として読めない TEXT 列を bytes のまま返す(Access から
移行した際、丸数字やローマ数字など JIS拡張の文字を Shift-JIS のまま
書いた行が混ざっていると起きる)。読めない字を問答無用で `�` に
置き換えると、現場データが永久に文字化けする(現場の声:
「仕掛かり一覧に文字化けがある」)。CP932 として読み直せることを確かめる。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import import_specs


class TextConversionTests(unittest.TestCase):
    def test_none_is_empty(self) -> None:
        self.assertEqual(import_specs._text(None), "")

    def test_str_is_stripped(self) -> None:
        self.assertEqual(import_specs._text("  シャーシ  "), "シャーシ")

    def test_number_becomes_text(self) -> None:
        self.assertEqual(import_specs._text(1234), "1234")

    def test_cp932_bytes_are_decoded(self) -> None:
        """UTF-8として読めない列は bytes のまま返る。CP932として読み直す。"""
        raw = "シャーシ①".encode("cp932")
        self.assertEqual(import_specs._text(raw), "シャーシ①")

    def test_neither_encoding_falls_back_without_crashing(self) -> None:
        """CP932としても読めない生バイト列でも、例外を投げずに文字列にする。"""
        raw = b"\xff\xfe\x00\x01"
        text = import_specs._text(raw)
        self.assertIsInstance(text, str)


class ToIntTests(unittest.TestCase):
    def test_blank_is_none(self) -> None:
        self.assertIsNone(import_specs.to_int(""))
        self.assertIsNone(import_specs.to_int(None))

    def test_parses_float_looking_text(self) -> None:
        self.assertEqual(import_specs.to_int("42.0"), 42)

    def test_non_numeric_is_none(self) -> None:
        self.assertIsNone(import_specs.to_int("abc"))


class ToRealTests(unittest.TestCase):
    def test_blank_is_none(self) -> None:
        self.assertIsNone(import_specs.to_real(""))

    def test_parses(self) -> None:
        self.assertEqual(import_specs.to_real("3.14"), 3.14)


if __name__ == "__main__":
    unittest.main()
