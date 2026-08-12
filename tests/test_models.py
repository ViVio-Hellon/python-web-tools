"""BoardModel / PlacedBoardModel (models.py) のユニットテスト。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool.models import BoardCategoryError, BoardModel, PlacedBoardModel


class BoardModelTests(unittest.TestCase):
    def test_valid_categories(self):
        for cat in ("下用", "上用", ""):
            m = BoardModel(board_category=cat)
            self.assertEqual(m.board_category, cat)

    def test_invalid_category_raises(self):
        with self.assertRaises(BoardCategoryError):
            BoardModel(board_category="不正")

    def test_setter_validates_after_construction(self):
        m = BoardModel(board_category="下用")
        with self.assertRaises(BoardCategoryError):
            m.board_category = "不正"
        self.assertEqual(m.board_category, "下用")  # 失敗時は元の値のまま

    def test_clone_is_independent_copy(self):
        original = BoardModel(id=1, width=100, length=200, instance_id="X1",
                               is_rotated=True, count=3, board_category="上用", stock_low=True)
        clone = original.clone()
        self.assertIsNot(clone, original)
        self.assertEqual(clone.id, original.id)
        self.assertEqual(clone.board_category, original.board_category)

        clone.width = 999
        self.assertEqual(original.width, 100)  # 元は変化しない


class PlacedBoardModelTests(unittest.TestCase):
    def test_defaults(self):
        m = PlacedBoardModel()
        self.assertEqual(m.width, 0)
        self.assertEqual(m.board_category, "")
        self.assertFalse(m.is_fill_board)

    def test_no_validation_on_category(self):
        # VBA版のPlacedBoardModelはバリデーション無しの単純なProperty Letだった
        m = PlacedBoardModel(board_category="任意の文字列")
        self.assertEqual(m.board_category, "任意の文字列")


if __name__ == "__main__":
    unittest.main()
