"""保管位置マップのテスト (VBA `UFMAP` の位置ボタン配置)

座標は `pallet_map_default.json` にVBAの実座標を入れてある。
コードに書かないので、現場で位置が変わってもJSONを直すだけで済む
(画面の「配置編集」でドラッグして保存すればJSONができる)。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import map_data, pallet_map  # noqa: E402

# VBA `UFMAP` にあった保管位置(S1/S2 + A11 + B5 + C7 + D3 = 28か所)
EXPECTED = (["S1", "S2"]
            + [f"A{i}" for i in range(1, 12)]
            + [f"B{i}" for i in range(1, 6)]
            + [f"C{i}" for i in range(1, 8)]
            + [f"D{i}" for i in range(1, 4)])


class DefaultMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = pallet_map.load(pallet_map.DEFAULT_PATH)

    def test_the_default_file_ships_with_the_repository(self):
        self.assertTrue(pallet_map.DEFAULT_PATH.exists())

    def test_it_has_every_position_from_the_form(self):
        self.assertEqual(sorted(self.plan.names), sorted(EXPECTED))

    def test_no_position_sticks_out_of_the_map(self):
        for item in self.plan.positions:
            self.assertLessEqual(item.x + item.w, self.plan.width, item.name)
            self.assertLessEqual(item.y + item.h, self.plan.height, item.name)

    def test_the_a_row_keeps_the_order_it_had_on_the_form(self):
        """A1〜A11は左から順に並んでいた。並びが崩れると図として読めない。"""
        xs = [self.plan.position(f"A{i}").x for i in range(1, 12)]
        self.assertEqual(xs, sorted(xs))

    def test_the_a_row_sits_on_one_line(self):
        tops = {self.plan.position(f"A{i}").y for i in range(1, 12)}
        self.assertEqual(len(tops), 1)

    def test_the_d_column_is_stacked_vertically(self):
        """D1〜D3は右端に縦積み。横一列のA〜Cとは向きが違う。"""
        ds = [self.plan.position(f"D{i}") for i in range(1, 4)]
        # 現場で手で合わせた配置なので、1pt 程度のずれは同じ列とみなす
        self.assertLessEqual(max(d.x for d in ds) - min(d.x for d in ds), 2.0)
        self.assertEqual([d.y for d in ds], sorted(d.y for d in ds))
        for d in ds:
            self.assertGreater(d.x, self.plan.position("C7").x)

    def test_positions_do_not_pile_up_on_each_other(self):
        """位置ボタンが別のボタンの上に乗っていないこと。

        A列などは隙間なく突き合わせて並んでいて、元フォームの座標の
        丸め(A1の右端230.85 と A2の左端230.8)で0.05ptだけ重なる。
        これは並びとして正しいので、1pt までの重なりは許す。
        """
        items = self.plan.positions
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                dx = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
                dy = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
                self.assertFalse(dx > 1.0 and dy > 1.0,
                                 f"{a.name} と {b.name} が重なっています")


class EditAndSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = pallet_map.load(pallet_map.DEFAULT_PATH)

    def test_moving_a_position_changes_only_that_one(self):
        before = self.plan.position("B3")
        self.assertTrue(self.plan.move_position("B3", 100.0, 200.0))
        self.assertEqual((self.plan.position("B3").x,
                          self.plan.position("B3").y), (100.0, 200.0))
        # 大きさは変えない
        self.assertEqual(self.plan.position("B3").w, before.w)

    def test_moving_an_unknown_position_reports_failure(self):
        self.assertFalse(self.plan.move_position("Z9", 1.0, 1.0))

    def test_add_and_remove(self):
        self.plan.add_position("A12", 447.0, 6.0)
        self.assertIn("A12", self.plan.names)
        self.assertTrue(self.plan.remove_position("A12"))
        self.assertNotIn("A12", self.plan.names)
        self.assertFalse(self.plan.remove_position("A12"))

    def test_a_round_trip_through_json_keeps_everything(self):
        self.plan.background = map_data.Background(image="map.png", scale=0.5)
        restored = pallet_map.PalletMap.from_dict(self.plan.to_dict())
        self.assertEqual(restored.names, self.plan.names)
        self.assertEqual(restored.background.image, "map.png")
        self.assertEqual(restored.background.scale, 0.5)
        self.assertEqual(restored.width, self.plan.width)

    def test_saving_and_loading_a_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "pallet_map.json"
            self.plan.move_position("S1", 12.0, 34.0)
            self.assertTrue(pallet_map.save(self.plan, target))
            reloaded = pallet_map.load(target)
            self.assertEqual((reloaded.position("S1").x,
                              reloaded.position("S1").y), (12.0, 34.0))

    def test_a_broken_file_falls_back_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "broken.json"
            target.write_text("{ こわれている", encoding="utf-8")
            plan = pallet_map.load(target)
            self.assertEqual(plan.names, [])

    def test_positions_in_stock_that_are_missing_from_the_map(self):
        """位置を増やしてマップを直し忘れると図から押せない。気づけること。"""
        self.assertEqual(self.plan.unknown(["A1", "Z9", "D2", "Z9", ""]), ["Z9"])
        self.assertEqual(self.plan.unknown(["A1", "B2"]), [])


class MapDataTests(unittest.TestCase):
    """棚配置図とUFMAPで共通に使う部分。"""

    def test_a_missing_file_reads_as_none(self):
        self.assertIsNone(map_data.read_json(Path("/なにもない/どこか.json")))

    def test_write_then_read(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "深い" / "場所" / "a.json"
            self.assertTrue(map_data.write_json(target, {"あ": 1}))
            self.assertEqual(map_data.read_json(target), {"あ": 1})

    def test_dropping_the_user_file_is_safe_when_it_is_not_there(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            map_data.drop_user_file(Path(tmpdir) / "ない.json")   # 例外にならない

    def test_moving_searches_every_group(self):
        first = [map_data.MapItem("あ", 0, 0)]
        second = [map_data.MapItem("い", 0, 0)]
        self.assertTrue(map_data.move_in((first, second), "い", 5, 6))
        self.assertEqual((second[0].x, second[0].y), (5, 6))
        self.assertFalse(map_data.move_in((first, second), "う", 1, 1))

    def test_the_default_json_is_readable_as_plain_json(self):
        """人が直接開いて直せること(現場でのちょっとした修正のため)。"""
        data = json.loads(pallet_map.DEFAULT_PATH.read_text(encoding="utf-8"))
        self.assertIn("positions", data)
        self.assertIn("canvas", data)


if __name__ == "__main__":                # pragma: no cover
    unittest.main()
