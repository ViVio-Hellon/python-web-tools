"""配置パターン保存/読込(pattern_service)のユニットテスト。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import db, pattern_service as svc


class PatternServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()


class SaveAndListTests(PatternServiceTestCase):
    def test_save_and_list(self):
        pattern_id = svc.save_new_pattern(
            self.conn, pallet_width=1150, pallet_length=2650,
            boards_lower=[svc.PatternBoard(750, 1130, 3, "下用"), svc.PatternBoard(660, 1310, 1, "下用")],
            boards_upper=[svc.PatternBoard(1122, 2502, 1, "上用")],
            product_width=1080, product_length=2500,
        )
        self.assertGreater(pattern_id, 0)

        summaries = svc.get_pattern_list(self.conn, 1150, 2650)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].id, pattern_id)
        self.assertIn("750×1130(3枚)", summaries[0].board_summary)
        self.assertIn("1122×2502(1枚)", summaries[0].board_summary)

    def test_list_filters_by_pallet_size(self):
        svc.save_new_pattern(
            self.conn, pallet_width=1000, pallet_length=2000,
            boards_lower=[], boards_upper=[], product_width=900, product_length=1900,
        )
        svc.save_new_pattern(
            self.conn, pallet_width=1200, pallet_length=2200,
            boards_lower=[], boards_upper=[], product_width=1100, product_length=2100,
        )
        self.assertEqual(len(svc.get_pattern_list(self.conn, 1000, 2000)), 1)
        self.assertEqual(len(svc.get_pattern_list(self.conn)), 2)

    def test_more_than_10_boards_truncated(self):
        boards = [svc.PatternBoard(100 + i, 200, 1, "下用") for i in range(12)]
        pattern_id = svc.save_new_pattern(
            self.conn, pallet_width=1000, pallet_length=2000,
            boards_lower=boards, boards_upper=[], product_width=900, product_length=1900,
        )
        detail = svc.load_pattern_by_id(self.conn, pattern_id)
        self.assertEqual(len(detail.boards_lower), 10)


class LoadAndDeleteTests(PatternServiceTestCase):
    def test_load_splits_by_usage_and_increments_count(self):
        pattern_id = svc.save_new_pattern(
            self.conn, pallet_width=1000, pallet_length=2000,
            boards_lower=[svc.PatternBoard(500, 1000, 2, "下用")],
            boards_upper=[svc.PatternBoard(600, 1100, 1, "上用")],
            product_width=900, product_length=1900,
        )
        detail = svc.load_pattern_by_id(self.conn, pattern_id)
        self.assertEqual(len(detail.boards_lower), 1)
        self.assertEqual(len(detail.boards_upper), 1)
        self.assertEqual(detail.boards_lower[0].width, 500)

        row = self.conn.execute("SELECT 使用回数 FROM PalletPatterns WHERE 管理番号=?", (pattern_id,)).fetchone()
        self.assertEqual(row["使用回数"], 1)

        svc.load_pattern_by_id(self.conn, pattern_id)
        row = self.conn.execute("SELECT 使用回数 FROM PalletPatterns WHERE 管理番号=?", (pattern_id,)).fetchone()
        self.assertEqual(row["使用回数"], 2)

    def test_load_missing_returns_none(self):
        self.assertIsNone(svc.load_pattern_by_id(self.conn, 999))

    def test_unset_usage_defaults_to_lower(self):
        pattern_id = svc.save_new_pattern(
            self.conn, pallet_width=1000, pallet_length=2000,
            boards_lower=[svc.PatternBoard(500, 1000, 2, "")],
            boards_upper=[], product_width=900, product_length=1900,
        )
        detail = svc.load_pattern_by_id(self.conn, pattern_id)
        self.assertEqual(len(detail.boards_lower), 1)
        self.assertEqual(len(detail.boards_upper), 0)

    def test_delete(self):
        pattern_id = svc.save_new_pattern(
            self.conn, pallet_width=1000, pallet_length=2000,
            boards_lower=[], boards_upper=[], product_width=900, product_length=1900,
        )
        self.assertTrue(svc.delete_pattern_by_id(self.conn, pattern_id))
        self.assertIsNone(svc.load_pattern_by_id(self.conn, pattern_id))
        self.assertFalse(svc.delete_pattern_by_id(self.conn, pattern_id))  # 既に削除済み


if __name__ == "__main__":
    unittest.main()
