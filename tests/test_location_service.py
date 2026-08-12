"""棚配置図と疲労度スコアのテスト。

配置図の座標は `floor_plan_default.json`(VBA frmLayout の実座標)に
あるので、それを前提にした値で確かめる。
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from packaging_tool import db, floor_plan, location_service as svc


def make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


class FloorPlanDataTests(unittest.TestCase):
    """座標はコードではなくJSONにある(置き場が変わっても直せる)。"""

    def setUp(self) -> None:
        self.plan = floor_plan.load(floor_plan.DEFAULT_PATH)

    def test_default_file_ships_with_the_repository(self):
        self.assertTrue(floor_plan.DEFAULT_PATH.exists())

    def test_base_points_match_the_vba_form(self):
        self.assertEqual(set(self.plan.base_point_names), {"L1", "HVC", "LVC"})
        l1 = self.plan.base_point("L1")
        self.assertEqual((l1.x, l1.y), (192.0, 186.0))

    def test_every_lblitem_is_present(self):
        names = set(self.plan.item_names)
        self.assertIn("lblItem1", names)
        self.assertIn("lblItem35", names)
        self.assertEqual(len(names), 29)

    def test_distance_uses_left_top_like_vba(self):
        """VBA CalcDistance は中心ではなく Left/Top の差で測る。"""
        # lblItem1 (156,162) と L1 (192,186) → dx=-36 dy=-24
        self.assertAlmostEqual(self.plan.distance("lblItem1", "L1"),
                               (36 ** 2 + 24 ** 2) ** 0.5, places=3)

    def test_unknown_names_give_none(self):
        self.assertIsNone(self.plan.distance("lblItemX", "L1"))
        self.assertIsNone(self.plan.distance("lblItem1", "どこか"))

    def test_nearest_picks_the_closest_and_counts_candidates(self):
        name, dist, count = self.plan.nearest(
            ["lblItem1", "lblItem7", "lblItem19"], "L1")
        self.assertEqual(name, "lblItem1")
        self.assertEqual(count, 3)
        self.assertGreater(dist, 0)

    def test_nearest_depends_on_the_base_point(self):
        from_l1, _, _ = self.plan.nearest(["lblItem1", "lblItem8"], "L1")
        from_hvc, _, _ = self.plan.nearest(["lblItem1", "lblItem8"], "HVC")
        self.assertEqual(from_l1, "lblItem1")
        self.assertEqual(from_hvc, "lblItem8")

    def test_nearest_ignores_labels_not_on_the_plan(self):
        name, _dist, count = self.plan.nearest(["lblItem1", "lblItemX"], "L1")
        self.assertEqual(name, "lblItem1")
        self.assertEqual(count, 1)


class EditingTests(unittest.TestCase):
    """置き場が変わったらラベルを動かすだけで距離も変わる。"""

    def setUp(self) -> None:
        self.plan = floor_plan.load(floor_plan.DEFAULT_PATH)

    def test_moving_a_label_changes_the_distance(self):
        before = self.plan.distance("lblItem1", "L1")
        self.plan.move_item("lblItem1", 192.0, 186.0)   # 拠点と同じ位置へ
        self.assertEqual(self.plan.distance("lblItem1", "L1"), 0.0)
        self.assertNotEqual(before, 0.0)

    def test_moving_an_unknown_label_reports_failure(self):
        self.assertFalse(self.plan.move_item("lblItemX", 1, 1))

    def test_add_and_remove(self):
        self.plan.add_item("lblItem99", 100, 100)
        self.assertIsNotNone(self.plan.item("lblItem99"))
        self.assertTrue(self.plan.remove_item("lblItem99"))
        self.assertIsNone(self.plan.item("lblItem99"))

    def test_round_trip_through_json(self):
        self.plan.move_item("lblItem1", 111.0, 222.0)
        restored = floor_plan.FloorPlan.from_dict(self.plan.to_dict())
        item = restored.item("lblItem1")
        self.assertEqual((item.x, item.y), (111.0, 222.0))
        self.assertEqual(len(restored.items), len(self.plan.items))

    def test_save_and_load_a_user_copy(self):
        tmp = Path(__file__).resolve().parent / "_tmp_plan.json"
        try:
            self.plan.move_item("lblItem1", 50.0, 60.0)
            self.assertTrue(floor_plan.save(self.plan, tmp))
            reloaded = floor_plan.load(tmp)
            item = reloaded.item("lblItem1")
            self.assertEqual((item.x, item.y), (50.0, 60.0))
        finally:
            tmp.unlink(missing_ok=True)

    def test_a_broken_file_falls_back_to_the_default(self):
        tmp = Path(__file__).resolve().parent / "_tmp_broken.json"
        try:
            tmp.write_text("{ これはJSONではない", encoding="utf-8")
            plan = floor_plan.load(tmp)
            self.assertEqual(len(plan.items), 0)   # 空で返り、例外にはしない
        finally:
            tmp.unlink(missing_ok=True)


class NearestShelfTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _add_board(self, w, l, label, board_type="ハードボード"):
        self.conn.execute(
            "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
            " VALUES (?, ?, ?, ?)", (w, l, board_type, label))
        self.conn.commit()

    def test_board_lookup_uses_the_data_label(self):
        self._add_board(1150, 2500, "lblItem1")
        r = svc.find_nearest_shelf_for_board(self.conn, 1150, 2500, "L1")
        self.assertEqual(r.shelf_name, "lblItem1")
        self.assertGreater(r.distance, 0)

    def test_rotated_size_matches_too(self):
        self._add_board(1150, 2500, "lblItem1")
        r = svc.find_nearest_shelf_for_board(self.conn, 2500, 1150, "L1")
        self.assertEqual(r.shelf_name, "lblItem1")

    def test_multiple_labels_pick_the_nearest(self):
        """VBAと同じで、同じサイズが複数の置き場にあるなら近い方を採る。"""
        self._add_board(900, 1800, "lblItem1,lblItem8")
        from_l1 = svc.find_nearest_shelf_for_board(self.conn, 900, 1800, "L1")
        from_hvc = svc.find_nearest_shelf_for_board(self.conn, 900, 1800, "HVC")
        self.assertEqual(from_l1.shelf_name, "lblItem1")
        self.assertEqual(from_hvc.shelf_name, "lblItem8")
        self.assertEqual(from_l1.candidate_count, 2)

    def test_no_data_label_means_no_shelf(self):
        self._add_board(1150, 2500, "")
        r = svc.find_nearest_shelf_for_board(self.conn, 1150, 2500, "L1")
        self.assertIsNone(r.shelf_name)
        self.assertEqual(r.candidate_count, 0)

    def test_board_type_narrows_the_search(self):
        self._add_board(1150, 2500, "lblItem1", "ハードボード")
        self._add_board(1150, 2500, "lblItem8", "プロテックボード")
        r = svc.find_nearest_shelf_for_board(
            self.conn, 1150, 2500, "HVC", "ハードボード")
        self.assertEqual(r.shelf_name, "lblItem1")

    def test_angle_lookup(self):
        self.conn.execute(
            "INSERT INTO CornerboardMaster (アングル丈, データラベル)"
            " VALUES (2000, 'lblItem34')")
        self.conn.commit()
        r = svc.find_nearest_shelf_for_angle(self.conn, 2000, "L1")
        self.assertEqual(r.shelf_name, "lblItem34")

    def test_unplaced_labels_are_reported(self):
        """配置図に無いラベルを使っていたら気づけるようにする。"""
        self._add_board(1150, 2500, "lblItem1,lblItemZZ")
        self.assertEqual(svc.unplaced_data_labels(self.conn), ["lblItemZZ"])


class ScoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_conn()
        self.conn.execute(
            "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
            " VALUES (1150, 2500, 'ハードボード', 'lblItem1')")
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    def test_distance_adds_to_the_score(self):
        near = svc.score_board_pick(0, 1000, 2000)
        far = svc.score_board_pick(500, 1000, 2000)
        self.assertGreater(far, near)


# 「距離x1 + 面積x枚数 + 幅カットx枚数 + 丈カットx1」(VBA [疲労式SSOT])を
# 通しで確かめるのは `tests/test_board_scoring.py`。合算しているのは
# `board_scoring.total_fatigue` で、ここはその材料になる1項の試験に絞る。


class LabelCategoriesTests(unittest.TestCase):
    """Board MAPの色分け用: 置き場ラベル→資材カテゴリの集計。"""

    def setUp(self) -> None:
        self.conn = make_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def _add_board(self, w, l, label, board_type="ハードボード"):
        self.conn.execute(
            "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
            " VALUES (?, ?, ?, ?)", (w, l, board_type, label))
        self.conn.commit()

    def _add_angle(self, length, label):
        self.conn.execute(
            "INSERT INTO CornerboardMaster (アングル丈, データラベル) VALUES (?, ?)",
            (length, label))
        self.conn.commit()

    def test_hard_board_maps_to_board_category(self):
        self._add_board(1150, 2500, "lblItem1", "ハードボード")
        cats = svc.label_categories(self.conn)
        self.assertEqual(cats["lblItem1"], {"ボード"})

    def test_ik_and_protec_board_types_map_to_their_own_category(self):
        self._add_board(1150, 2500, "lblItem1", "IKボード")
        self._add_board(1150, 2500, "lblItem2", "プロテックボード")
        cats = svc.label_categories(self.conn)
        self.assertEqual(cats["lblItem1"], {"IK"})
        self.assertEqual(cats["lblItem2"], {"プロテック"})

    def test_angle_maps_to_angle_category(self):
        self._add_angle(2000, "lblItem34")
        cats = svc.label_categories(self.conn)
        self.assertEqual(cats["lblItem34"], {"アングル"})

    def test_a_label_shared_by_two_categories_reports_both(self):
        self._add_board(1150, 2500, "lblItem1", "ハードボード")
        self._add_angle(2000, "lblItem1")
        cats = svc.label_categories(self.conn)
        self.assertEqual(cats["lblItem1"], {"ボード", "アングル"})

    def test_a_label_with_no_master_rows_is_absent(self):
        cats = svc.label_categories(self.conn)
        self.assertNotIn("lblItem99", cats)


class MaterialsAtLabelTests(unittest.TestCase):
    """Board MAPのラベルをクリックしたときに出す資材一覧。"""

    def setUp(self) -> None:
        self.conn = make_conn()

    def tearDown(self) -> None:
        self.conn.close()

    def test_lists_boards_assigned_to_the_label(self):
        self.conn.execute(
            "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
            " VALUES (1150, 2500, 'ハードボード', 'lblItem1')")
        self.conn.commit()
        materials = svc.materials_at_label(self.conn, "lblItem1")
        self.assertEqual(len(materials), 1)
        self.assertEqual(materials[0].category, "ボード")
        self.assertEqual((materials[0].width, materials[0].length), (1150, 2500))

    def test_angle_entries_have_no_width(self):
        self.conn.execute(
            "INSERT INTO CornerboardMaster (アングル丈, データラベル)"
            " VALUES (2000, 'lblItem34')")
        self.conn.commit()
        materials = svc.materials_at_label(self.conn, "lblItem34")
        self.assertEqual(materials[0].category, "アングル")
        self.assertIsNone(materials[0].width)
        self.assertEqual(materials[0].length, 2000)

    def test_a_label_used_by_multiple_rows_lists_them_all(self):
        self.conn.execute(
            "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
            " VALUES (1150, 2500, 'ハードボード', 'lblItem1,lblItem8')")
        self.conn.execute(
            "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
            " VALUES (900, 1800, 'プロテックボード', 'lblItem1')")
        self.conn.commit()
        materials = svc.materials_at_label(self.conn, "lblItem1")
        self.assertEqual(len(materials), 2)
        categories = {m.category for m in materials}
        self.assertEqual(categories, {"ボード", "プロテック"})

    def test_an_unassigned_label_gives_an_empty_list(self):
        self.assertEqual(svc.materials_at_label(self.conn, "lblItem99"), [])


if __name__ == "__main__":
    unittest.main()
