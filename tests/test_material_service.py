"""保護材選定・看板在庫薄判定(material_service)のユニットテスト。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import db, material_service as svc


class FindInListTests(unittest.TestCase):
    def test_blank_tmp_is_true(self):
        self.assertTrue(svc.find_in_list("", "SUS304"))

    def test_blank_tmp_with_blank_flag_is_not_auto_true(self):
        self.assertFalse(svc.find_in_list("", "SUS304", blank_flag="NoBlank"))

    def test_exact_match(self):
        self.assertTrue(svc.find_in_list("SUS304", "SUS304"))
        self.assertFalse(svc.find_in_list("SUS304", "SUS316"))

    def test_comma_list_match(self):
        self.assertTrue(svc.find_in_list("SUS304,SUS316", "SUS316"))

    def test_wildcard_prefix_suffix_contains(self):
        self.assertTrue(svc.find_in_list("A5052*", "A5052-H34"))
        self.assertTrue(svc.find_in_list("*H34", "A5052-H34"))
        self.assertTrue(svc.find_in_list("*505*", "A5052-H34"))

    def test_exclusion_overrides_positive_match(self):
        # 例: tmp="SUS304,≠SUS304" は最終的にFalse(除外が優先)
        self.assertFalse(svc.find_in_list("SUS304,≠SUS304", "SUS304"))

    def test_exclusion_wildcard(self):
        self.assertFalse(svc.find_in_list("≠*H34", "A5052-H34"))
        self.assertTrue(svc.find_in_list("≠*H34", "A5052-O"))


class CheckConditionTests(unittest.TestCase):
    def test_normalizes_fullwidth_and_dash(self):
        # 全角"Ｓ"などが半角化され、半角カナ長音"ｰ"が"-"に変換される
        self.assertTrue(svc.check_condition("Ａ5052ｰH34", "A5052-H34"))

    def test_range_all_blank_means_no_restriction(self):
        self.assertTrue(svc._check_range(None, None, None, 100))

    def test_range_check(self):
        self.assertTrue(svc._check_range(1.0, 3.0, None, 2.0))
        self.assertFalse(svc._check_range(1.0, 3.0, None, 5.0))


class GetUpperPartMaterialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _insert(self, **kwargs):
        cols = ", ".join(f"[{k}]" for k in kwargs)
        placeholders = ", ".join("?" for _ in kwargs)
        self.conn.execute(f"INSERT INTO 梱包保護材 ({cols}) VALUES ({placeholders})", list(kwargs.values()))
        self.conn.commit()

    def test_out_of_scope_pack_returns_empty(self):
        result = svc.get_upper_part_material(
            self.conn, pack="9P9999", zai="A5052", tyo="H34", you="", atu=1, hab=100, tak=100,
        )
        self.assertEqual(result, "")

    def test_pass1_exact_pack_match(self):
        self._insert(包装仕様書="1P0001", 材質="A5052", 調質="H34", 用途コード="", 使用保護材="アングル")
        result = svc.get_upper_part_material(
            self.conn, pack="1P0001", zai="A5052", tyo="H34", you="", atu=1, hab=100, tak=100,
        )
        self.assertEqual(result, "アングル")

    def test_pass2_generic_row_used_when_pack_specific_not_found(self):
        self._insert(包装仕様書="", 材質="A5052", 調質="H34", 用途コード="", 使用保護材="厚紙")
        result = svc.get_upper_part_material(
            self.conn, pack="1P0104", zai="A5052", tyo="H34", you="", atu=1, hab=100, tak=100,
        )
        self.assertEqual(result, "厚紙")

    def test_no_match_returns_literal(self):
        self._insert(包装仕様書="", 材質="A6061", 調質="H34", 用途コード="", 使用保護材="厚紙")
        result = svc.get_upper_part_material(
            self.conn, pack="1P0104", zai="A5052", tyo="H34", you="", atu=1, hab=100, tak=100,
        )
        self.assertEqual(result, "一致なし")

    def test_thickness_range_filters_out(self):
        self._insert(包装仕様書="", 材質="A5052", 調質="H34", 用途コード="", 板厚下=5.0, 板厚上=10.0, 使用保護材="厚紙")
        result = svc.get_upper_part_material(
            self.conn, pack="1P0104", zai="A5052", tyo="H34", you="", atu=1, hab=100, tak=100,
        )
        self.assertEqual(result, "一致なし")


class KanbanStockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_parse_kanban_size(self):
        self.assertEqual(svc.parse_kanban_size("660 × 1050 : 100枚"), (660, 1050))
        self.assertEqual(svc.parse_kanban_size("６６０×１０５０"), (660, 1050))
        self.assertIsNone(svc.parse_kanban_size("サイズ不明"))

    def test_build_board_stock_map_low_and_has_stock(self):
        self.conn.execute("INSERT INTO 看板_AIM (資材, サイズ, 欲, 不) VALUES ('板A', '600×900', '〇', '')")
        self.conn.execute("INSERT INTO 看板_HVC (資材, サイズ, 欲, 不) VALUES ('板B', '700×900', '', '〇')")
        self.conn.commit()

        stock_map = svc.build_board_stock_map(self.conn)
        self.assertTrue(svc.is_board_low(stock_map, "板A", 600, 900))
        self.assertFalse(svc.is_board_low(stock_map, "板B", 700, 900))
        # 未登録の組み合わせはFalse(妨げない)
        self.assertFalse(svc.is_board_low(stock_map, "板C", 100, 100))
        self.assertFalse(svc.is_board_low(None, "板A", 600, 900))

    def test_has_stock_wins_over_low_regardless_of_scan_order(self):
        # 同じキーで一方が"不=〇"(有り)、別テーブルで"欲=〇"(低)でも"有"が勝つ
        self.conn.execute("INSERT INTO 看板_AIM (資材, サイズ, 欲, 不) VALUES ('板A', '600×900', '〇', '')")
        self.conn.execute("INSERT INTO 看板_HVC (資材, サイズ, 欲, 不) VALUES ('板A', '600×900', '', '〇')")
        self.conn.commit()
        stock_map = svc.build_board_stock_map(self.conn)
        self.assertFalse(svc.is_board_low(stock_map, "板A", 600, 900))


if __name__ == "__main__":
    unittest.main()
