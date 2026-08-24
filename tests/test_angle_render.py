"""アングル配置図(angle_render)のユニットテスト。

VBA `DrawAngleBoards` の対称配置・段組み・カット/被り表示の境界を検証する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import angle_render as ar


class ComputeBarPositionsTests(unittest.TestCase):
    def test_single_bar_starts_at_product_left(self):
        got = ar.compute_bar_positions([2000], prod_left=10.0, prod_screen_w=100.0,
                                       scale=0.05, pallet_len=2600, product_len=2000)
        self.assertEqual(got, [10.0])

    def test_two_bars_butt_together_when_they_fit(self):
        # 合計2200 <= パレット丈2600 → 被りなし。中央揃えで突き合わせ
        got = ar.compute_bar_positions([1200, 1000], prod_left=100.0, prod_screen_w=200.0,
                                       scale=0.1, pallet_len=2600, product_len=2000)
        # ずれ = ((2200-2000)/2)*0.1 = 10
        self.assertAlmostEqual(got[0], 90.0)
        # 2本目は1本目の右端に接する
        self.assertAlmostEqual(got[1], 90.0 + 1200 * 0.1)

    def test_two_bars_go_to_both_ends_when_they_overlap(self):
        # 合計3000 > パレット丈2600 → 被りあり。左端と右端に寄せる
        got = ar.compute_bar_positions([1500, 1500], prod_left=100.0, prod_screen_w=200.0,
                                       scale=0.1, pallet_len=2600, product_len=2000)
        self.assertAlmostEqual(got[0], 100.0)
        self.assertAlmostEqual(got[1], 100.0 + 200.0 - 1500 * 0.1)

    def test_two_bars_use_50mm_threshold_without_pallet(self):
        # パレット丈不明のときは「製品丈+50mm以内なら被りなし」
        fit = ar.compute_bar_positions([1000, 1040], prod_left=0.0, prod_screen_w=200.0,
                                       scale=0.1, pallet_len=0, product_len=2000)
        self.assertLess(fit[0], 0.0)   # 中央揃えで左にはみ出す
        over = ar.compute_bar_positions([1000, 1060], prod_left=0.0, prod_screen_w=200.0,
                                        scale=0.1, pallet_len=0, product_len=2000)
        self.assertEqual(over[0], 0.0)  # 被りあり → 左端

    def test_three_bars_cover_the_product_without_gaps(self):
        # VBAのコメントにある検証例: [2000,1500,2000] / 製品5500mm
        lens = [2000, 1500, 2000]
        scale = 0.1
        prod_w = 5500 * scale
        got = ar.compute_bar_positions(lens, prod_left=0.0, prod_screen_w=prod_w,
                                       scale=scale, pallet_len=6000, product_len=5500)
        self.assertAlmostEqual(got[0], 0.0)
        self.assertAlmostEqual(got[2], prod_w - 2000 * scale)
        # センターは左右の内端の中点
        self.assertAlmostEqual(got[1], 2000 * scale)
        # 隙間なく連続していること
        self.assertAlmostEqual(got[0] + lens[0] * scale, got[1])
        self.assertAlmostEqual(got[1] + lens[1] * scale, got[2])

    def test_four_bars_split_evenly_left_and_right(self):
        lens = [1000, 1000, 1000, 1000]
        got = ar.compute_bar_positions(lens, prod_left=0.0, prod_screen_w=400.0,
                                       scale=0.1, pallet_len=5000, product_len=4000)
        self.assertAlmostEqual(got[0], 0.0)
        self.assertAlmostEqual(got[1], 100.0)
        self.assertAlmostEqual(got[3], 300.0)
        self.assertAlmostEqual(got[2], 200.0)

    def test_empty_input_returns_empty(self):
        self.assertEqual(ar.compute_bar_positions([], 0, 0, 1, 0, 0), [])


class ComputeBarRowsTests(unittest.TestCase):
    def test_single_bar_is_on_the_top_row(self):
        tops, order = ar.compute_bar_rows([2000], 3.0, 2600, 2000)
        self.assertEqual(tops, [3.0])
        self.assertEqual(order, [0])

    def test_overlapping_pair_is_split_into_two_rows(self):
        tops, order = ar.compute_bar_rows([1500, 1500], 3.0, 2600, 2000)
        self.assertEqual(order, [0, 1])
        self.assertEqual(tops[0], 3.0)
        self.assertAlmostEqual(tops[1], 3.0 + ar.ANGLE_BAR_H + ar.ANGLE_BAR_GAP)

    def test_non_overlapping_pair_shares_one_row(self):
        tops, _ = ar.compute_bar_rows([1200, 1000], 3.0, 2600, 2000)
        self.assertEqual(tops[0], tops[1])

    def test_odd_multi_puts_center_on_top_and_draws_it_last(self):
        tops, order = ar.compute_bar_rows([2000, 1500, 2000], 3.0, 6000, 5500)
        self.assertEqual(order[-1], 1)                 # センターを最後に描く(前面)
        self.assertEqual(tops[1], 3.0)                 # センターは上段
        self.assertAlmostEqual(tops[0], 3.0 + ar.ANGLE_BAR_H + ar.ANGLE_BAR_GAP)  # サイドは下段

    def test_even_multi_shares_one_row(self):
        tops, order = ar.compute_bar_rows([1000] * 4, 3.0, 5000, 4000)
        self.assertEqual(order, [0, 1, 2, 3])
        self.assertEqual(len(set(tops)), 1)


class BuildAnglePlanTests(unittest.TestCase):
    def test_no_product_size_gives_empty_plan(self):
        plan = ar.build_angle_plan([2000], product_len=0, pallet_len=2600,
                                   leg_count=3, canvas_w=400)
        self.assertIsNone(plan.pallet_bar)
        self.assertEqual(plan.bars, [])

    def test_pallet_bar_is_centered_on_the_canvas(self):
        plan = ar.build_angle_plan([], product_len=2000, pallet_len=2600,
                                   leg_count=3, canvas_w=400)
        bar = plan.pallet_bar
        self.assertIsNotNone(bar)
        self.assertAlmostEqual(bar.width, 400 * ar.CANVAS_FILL_RATIO)
        self.assertAlmostEqual(bar.x, (400 - bar.width) / 2)

    def test_product_size_is_used_as_base_when_no_pallet(self):
        plan = ar.build_angle_plan([], product_len=2000, pallet_len=0,
                                   leg_count=0, canvas_w=400)
        self.assertAlmostEqual(plan.scale, (400 * ar.CANVAS_FILL_RATIO) / 2000)

    def test_leg_count_falls_back_to_two(self):
        plan = ar.build_angle_plan([], product_len=2000, pallet_len=2600,
                                   leg_count=0, canvas_w=400)
        self.assertEqual(len(plan.legs), 2)

    def test_legs_are_drawn_as_squares(self):
        plan = ar.build_angle_plan([], product_len=2000, pallet_len=2600,
                                   leg_count=4, canvas_w=400)
        self.assertEqual(len(plan.legs), 4)
        for leg in plan.legs:
            self.assertEqual(leg.width, leg.height)

    def test_product_edges_are_two_red_lines(self):
        plan = ar.build_angle_plan([], product_len=2000, pallet_len=2600,
                                   leg_count=3, canvas_w=400)
        self.assertEqual(len(plan.product_edges), 2)
        for edge in plan.product_edges:
            self.assertEqual(edge.fill, ar.COLOR_PRODUCT_EDGE)
        left, right = plan.product_edges
        self.assertAlmostEqual(right.x - left.x, 2000 * plan.scale)

    def test_bars_carry_length_captions_and_distinct_colors(self):
        plan = ar.build_angle_plan([2000, 1500, 2000], product_len=5500, pallet_len=6000,
                                   leg_count=3, canvas_w=400)
        self.assertEqual(len(plan.bars), 3)
        captions = {b.caption for b in plan.bars}
        self.assertEqual(captions, {"2000mm", "1500mm"})
        self.assertEqual(plan.bars[0].fill, ar.BAR_COLORS[0])

    def test_bar_captions_have_a_legible_font_size(self):
        """未指定(0)だと描画側の既定7になり、実機では読めない大きさだった
        (現場の声:「アングルがどのサイズか配置後わからない」)。

        その後さらに「全画面表示・配置図印刷でも文字が小さくて読み取れない」
        という声で2倍(9→18)にした(一度3倍にしたが「大きすぎる」と
        声が出たため2倍に落ち着けた)。バー自体の高さ(ANGLE_BAR_H)も
        同じ2倍にしてあるので、キャプションがバーからはみ出さない。
        """
        plan = ar.build_angle_plan([2000], product_len=1800, pallet_len=2600,
                                   leg_count=3, canvas_w=400)
        for bar in plan.bars:
            self.assertEqual(bar.font_size, 18)
            self.assertLess(bar.font_size, ar.ANGLE_BAR_H)

    def test_zero_length_angles_are_ignored(self):
        plan = ar.build_angle_plan([0, 0], product_len=2000, pallet_len=2600,
                                   leg_count=3, canvas_w=400)
        self.assertEqual(plan.bars, [])

    def test_cut_shows_waste_zone_and_cut_line(self):
        plan = ar.build_angle_plan([2500], product_len=2000, pallet_len=2600,
                                   leg_count=3, canvas_w=400, need_cut=True)
        fills = [r.fill for r in plan.cut_marks]
        self.assertIn(ar.COLOR_WASTE, fills)
        self.assertIn(ar.COLOR_CUT_LINE, fills)
        waste = next(r for r in plan.cut_marks if r.fill == ar.COLOR_WASTE)
        self.assertEqual(waste.caption, "廃材 500mm")
        self.assertTrue(any("カット 2500->2000mm" in n.text for n in plan.notes))

    def test_no_cut_marks_when_cut_not_needed(self):
        plan = ar.build_angle_plan([2500], product_len=2000, pallet_len=2600,
                                   leg_count=3, canvas_w=400, need_cut=False)
        self.assertEqual(plan.cut_marks, [])

    def test_cut_only_applies_to_a_single_angle(self):
        plan = ar.build_angle_plan([2500, 2500], product_len=2000, pallet_len=6000,
                                   leg_count=3, canvas_w=400, need_cut=True)
        self.assertEqual(plan.cut_marks, [])

    def test_overlap_note_is_shown_when_bars_exceed_product(self):
        # 合計3000 > パレット丈2600 → 被りあり
        plan = ar.build_angle_plan([1500, 1500], product_len=2000, pallet_len=2600,
                                   leg_count=3, canvas_w=400)
        self.assertTrue(any("ｱﾝｸﾞﾙ被り1000mm" in n.text for n in plan.notes))

    def test_no_overlap_note_when_pair_fits_on_the_pallet(self):
        plan = ar.build_angle_plan([1200, 1000], product_len=2000, pallet_len=2600,
                                   leg_count=3, canvas_w=400)
        self.assertFalse(any("被り" in n.text for n in plan.notes))

    def test_overlap_note_for_three_bars(self):
        plan = ar.build_angle_plan([2000, 2000, 2000], product_len=5500, pallet_len=6000,
                                   leg_count=3, canvas_w=400)
        self.assertTrue(any("ｱﾝｸﾞﾙ被り500mm" in n.text for n in plan.notes))


if __name__ == "__main__":
    unittest.main()
