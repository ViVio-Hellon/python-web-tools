"""入力文字の正規化のテスト(VBA ToNarrowAlphaNumeric / NormalizeKeyLocal)。"""
from __future__ import annotations

import unittest

from packaging_tool import text_input


class NarrowAlnumTests(unittest.TestCase):
    """ロット番号の入力欄に使う正規化。"""

    def test_lowercase_becomes_uppercase(self):
        self.assertEqual(text_input.to_narrow_alnum("r4085q0"), "R4085Q0")

    def test_fullwidth_becomes_halfwidth(self):
        self.assertEqual(text_input.to_narrow_alnum("Ｒ４０８５Ｑ０"), "R4085Q0")

    def test_fullwidth_lowercase(self):
        self.assertEqual(text_input.to_narrow_alnum("ｒ４０８５ｑ０"), "R4085Q0")

    def test_symbols_and_spaces_are_dropped(self):
        self.assertEqual(text_input.to_narrow_alnum("R-4085 Q0"), "R4085Q0")
        self.assertEqual(text_input.to_narrow_alnum("　R4085Q0　"), "R4085Q0")

    def test_japanese_is_dropped(self):
        self.assertEqual(text_input.to_narrow_alnum("ロットR4085"), "R4085")

    def test_empty_and_none(self):
        self.assertEqual(text_input.to_narrow_alnum(""), "")
        self.assertEqual(text_input.to_narrow_alnum(None), "")

    def test_already_clean_is_unchanged(self):
        """余計な書き換えをしない(カーソルが飛ばないように)。"""
        self.assertEqual(text_input.to_narrow_alnum("R4085Q0"), "R4085Q0")


class NormalizeKeyTests(unittest.TestCase):
    """業界・記号の突き合わせに使う正規化。英数字以外も残る。"""

    def test_multiply_signs_are_unified(self):
        for source in ("4x8", "4X8", "4×8", "４×８", "４Ｘ８"):
            self.assertEqual(text_input.normalize_key(source), "4×8",
                             f"{source} が揃わない")

    def test_uppercase_and_halfwidth(self):
        self.assertEqual(text_input.normalize_key("ｃ1"), "C1")
        self.assertEqual(text_input.normalize_key("Ｃ１"), "C1")

    def test_surrounding_spaces_are_trimmed(self):
        self.assertEqual(text_input.normalize_key("  C1  "), "C1")

    def test_other_characters_survive(self):
        """英数字以外を落とさない点が to_narrow_alnum との違い。"""
        self.assertEqual(text_input.normalize_key("A-2"), "A-2")

    def test_empty(self):
        self.assertEqual(text_input.normalize_key(""), "")


class AllowedCharTests(unittest.TestCase):
    def test_alphanumerics_are_allowed(self):
        for char in ("a", "Z", "0", "９", "Ａ"):
            self.assertTrue(text_input.is_allowed_lot_char(char), char)

    def test_symbols_are_not(self):
        for char in ("-", " ", "　", "あ", ""):
            self.assertFalse(text_input.is_allowed_lot_char(char), repr(char))


if __name__ == "__main__":
    unittest.main()
