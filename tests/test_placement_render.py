"""配置図の描画計画(placement_render)のユニットテスト。

スケール計算・中央寄せ補正・カット検出・色分けなど、
GUIに依存しない部分の境界を検証する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import placement_render as render
from packaging_tool.models import PlacedBoardModel

LOWER = render.CATEGORY_LOWER
UPPER = render.CATEGORY_UPPER


def placed(x: int, y: int, w: int, l: int, category: str = LOWER, *,
           is_fill: bool = False, orig_w: int = 0, orig_l: int = 0) -> PlacedBoardModel:
    return PlacedBoardModel(
        x=x, y=y, width=w, length=l, board_category=category, is_fill_board=is_fill,
        original_width=orig_w or w, original_length=orig_l or l,
    )


class DetectBoardCutTests(unittest.TestCase):
    def test_no_cut_when_dimensions_match(self):
        cut = render.detect_board_cut(1150, 2500, 1150, 2500)
        self.assertFalse(cut.cut_width)
        self.assertFalse(cut.cut_length)
        self.assertEqual(cut.note, "")

    def test_length_cut_detected(self):
        cut = render.detect_board_cut(1150, 2500, 1150, 2300)
        self.assertTrue(cut.cut_length)
        self.assertEqual(cut.amount_length, 200)
        self.assertFalse(cut.cut_width)
        self.assertEqual(cut.note, "←200カット")

    def test_width_cut_detected(self):
        cut = render.detect_board_cut(1250, 2500, 1150, 2500)
        self.assertTrue(cut.cut_width)
        self.assertEqual(cut.amount_width, 100)
        self.assertEqual(cut.note, "↑100カット")

    def test_both_cuts_note_order(self):
        cut = render.detect_board_cut(1250, 2500, 1150, 2300)
        self.assertEqual(cut.note, "←200カット↑100カット")

    def test_tolerance_of_3mm_is_not_a_cut(self):
        self.assertFalse(render.detect_board_cut(1150, 2500, 1147, 2497).cut_width)
        self.assertTrue(render.detect_board_cut(1150, 2500, 1146, 2500).cut_width)

    def test_rotated_interpretation_is_preferred_when_smaller(self):
        # 元 100×2500 を 2500×100 として置いた場合、回転解釈ならカット0
        cut = render.detect_board_cut(100, 2500, 2500, 100)
        self.assertFalse(cut.cut_width)
        self.assertFalse(cut.cut_length)

    def test_zero_original_returns_no_cut(self):
        self.assertFalse(render.detect_board_cut(0, 2500, 1150, 2300).cut_length)


class ComputeScaleTests(unittest.TestCase):
    def test_limited_by_length_when_wide(self):
        # 丈方向が制約: (400*0.9)/2000 = 0.18 vs (200*0.9)/1000 = 0.18 → 同値
        self.assertAlmostEqual(render.compute_scale(400, 200, 1000, 2000), 0.18)

    def test_picks_the_smaller_of_two_axes(self):
        # 幅方向が厳しい: (400*0.9)/1000=0.36 vs (200*0.9)/2000=0.09
        self.assertAlmostEqual(render.compute_scale(400, 200, 2000, 1000), 0.09)

    def test_zero_base_returns_zero(self):
        self.assertEqual(render.compute_scale(400, 200, 0, 2000), 0.0)


class BoundingBoxTests(unittest.TestCase):
    def test_main_box_ignores_thin_boards(self):
        boards = [placed(0, 0, 1130, 2000), placed(2000, 0, 50, 500)]
        box = render.main_boards_bounding_box(boards, LOWER, 2650, 1150)
        # 短辺50mmのボードは主ボード扱いしないのでX端は2000のまま
        self.assertEqual((box.max_x, box.max_y), (2000, 1130))

    def test_narrow_box_includes_thin_boards(self):
        boards = [placed(0, 0, 1130, 2000), placed(2000, 0, 50, 500)]
        box = render.narrow_bounding_box(boards, LOWER, 2650, 1150)
        self.assertEqual(box.max_x, 2500)

    def test_falls_back_to_base_size_when_no_main_board(self):
        box = render.main_boards_bounding_box([], LOWER, 2650, 1150)
        self.assertEqual((box.min_x, box.min_y, box.max_x, box.max_y), (0, 0, 2650, 1150))

    def test_adjacent_fill_extends_box(self):
        boards = [placed(0, 0, 1080, 2000), placed(0, 1080, 50, 2000, is_fill=True)]
        box = render.main_boards_bounding_box(boards, LOWER, 2650, 1150)
        self.assertEqual(box.max_y, 1130)

    def test_detached_fill_does_not_extend_box(self):
        # 主ボードY端(1080)より手前から始まる補填は取り込まない
        boards = [placed(0, 0, 1080, 2000), placed(0, 500, 50, 2000, is_fill=True)]
        box = render.main_boards_bounding_box(boards, LOWER, 2650, 1150)
        self.assertEqual(box.max_y, 1080)

    def test_other_category_ignored(self):
        boards = [placed(0, 0, 1130, 2000, LOWER), placed(0, 0, 500, 5000, UPPER)]
        box = render.main_boards_bounding_box(boards, LOWER, 2650, 1150)
        self.assertEqual(box.max_x, 2000)

    def test_fill_adjacent_to_main_top_edge_extends_min_y(self):
        # 主ボードはy=30から始まり(幅補填を上に振り分けたぶんオフセット
        # 済み)、上側の幅補填(短辺30)はy=0〜30に接する → min_yが0に広がる
        boards = [placed(0, 30, 1080, 2000), placed(0, 0, 30, 2000, is_fill=True)]
        box = render.main_boards_bounding_box(boards, LOWER, 2650, 1150)
        self.assertEqual(box.min_y, 0)

    def test_fill_ending_past_main_top_edge_does_not_extend_min_y(self):
        # 補填の下端(y+width=35)が主ボード上端(30)を超えている
        # (=主ボードの範囲に食い込んでいる)ので取り込まない
        boards = [placed(0, 30, 1080, 2000), placed(0, 25, 10, 2000, is_fill=True)]
        box = render.main_boards_bounding_box(boards, LOWER, 2650, 1150)
        self.assertEqual(box.min_y, 30)

    def test_length_fill_at_y_zero_is_not_mistaken_for_upper_row(self):
        # 丈補填はy=main_top_y付近・x=主ボード右端以降に置かれるため、
        # pb.y + pb.width が main_top_y を超え、min_yの取り込み対象から
        # 自然に外れる(このテストの丈補填は主ボードと同じy=30に置かれ、
        # y+width=30+1150=1180 > main_top_y=30 → 対象外)
        boards = [
            placed(0, 30, 1080, 2000),
            placed(2000, 30, 1150, 100, is_fill=True),  # 丈補填
        ]
        box = render.main_boards_bounding_box(boards, LOWER, 2650, 1150)
        self.assertEqual(box.min_y, 30)


class ColorTests(unittest.TestCase):
    def test_category_colors(self):
        self.assertEqual(render.board_color(LOWER), render.COLOR_LOWER)
        self.assertEqual(render.board_color(UPPER), render.COLOR_UPPER)

    def test_fixed_colors_for_standard_fill_sizes(self):
        self.assertEqual(render.thin_board_color(30, 3), render.COLOR_THIN_30)
        self.assertEqual(render.thin_board_color(50, 3), render.COLOR_THIN_50)
        self.assertEqual(render.thin_board_color(100, 3), render.COLOR_THIN_100)

    def test_other_sizes_graded_by_screen_height(self):
        self.assertEqual(render.thin_board_color(70, 5), render.COLOR_THIN_TINY)
        self.assertEqual(render.thin_board_color(70, 11), render.COLOR_THIN_SMALL)
        self.assertEqual(render.thin_board_color(70, 13), render.COLOR_THIN_OTHER)


class CaptionTests(unittest.TestCase):
    def test_two_lines_when_tall(self):
        caption, size = render.build_caption(placed(0, 0, 1130, 750), render.CutInfo(), 100)
        self.assertEqual(caption, "幅1130\n丈750")
        self.assertEqual(size, 24)

    def test_one_line_when_medium(self):
        caption, _ = render.build_caption(placed(0, 0, 1130, 750), render.CutInfo(), 40)
        self.assertEqual(caption, "幅1130 丈750")

    def test_width_only_when_short(self):
        caption, size = render.build_caption(placed(0, 0, 1130, 750), render.CutInfo(), 20)
        self.assertEqual(caption, "幅1130")
        self.assertEqual(size, 22)

    def test_cut_note_appended(self):
        cut = render.CutInfo(cut_length=True, amount_length=200)
        caption, _ = render.build_caption(placed(0, 0, 1130, 750), cut, 100)
        self.assertEqual(caption, "幅1130\n丈750\n←200カット")


class BuildRenderPlanTests(unittest.TestCase):
    def test_empty_placement_still_draws_border(self):
        plan = render.build_render_plan([], LOWER, 1150, 2650, 400, 200)
        self.assertIsNotNone(plan.border)
        self.assertEqual(plan.boards, [])

    def test_zero_base_produces_empty_plan(self):
        plan = render.build_render_plan([], LOWER, 0, 0, 400, 200)
        self.assertIsNone(plan.border)

    def test_board_rect_uses_length_as_screen_width(self):
        # X方向=丈, Y方向=幅 の対応が入れ替わっていないこと
        boards = [placed(0, 0, 1150, 2650)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        rect = plan.boards[0]
        self.assertAlmostEqual(rect.width, 2650 * plan.scale)
        self.assertAlmostEqual(rect.height, 1150 * plan.scale)

    def test_full_size_board_is_centered_on_border(self):
        boards = [placed(0, 0, 1150, 2650)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        self.assertAlmostEqual(plan.boards[0].x, plan.border.x)
        self.assertAlmostEqual(plan.boards[0].y, plan.border.y)

    def test_half_length_board_is_centered_in_x(self):
        # 丈が枠の半分なら、左右に均等な余白ができる
        boards = [placed(0, 0, 1150, 1325)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        left_gap = plan.boards[0].x - plan.border.x
        right_gap = (plan.border.x + plan.border.width) - (plan.boards[0].x + plan.boards[0].width)
        self.assertAlmostEqual(left_gap, right_gap)

    def test_overflowing_board_is_not_shifted(self):
        # 枠をはみ出す配置は中央寄せ補正せず、はみ出しをそのまま見せる
        boards = [placed(0, 0, 1150, 4000)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        self.assertAlmostEqual(plan.boards[0].x, plan.border.x)

    def test_thin_board_gets_color_and_no_caption(self):
        boards = [placed(0, 0, 1150, 2650), placed(2650, 0, 30, 1150)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        thin = plan.boards[1]
        self.assertEqual(thin.caption, "")
        self.assertEqual(thin.fill, render.COLOR_THIN_30)

    def test_thin_board_appears_in_legend(self):
        boards = [placed(0, 0, 1150, 2650), placed(2650, 0, 30, 1150)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        self.assertEqual(plan.legend, [(30, render.COLOR_THIN_30)])

    def test_tooltip_always_carries_real_dimensions(self):
        boards = [placed(0, 0, 30, 1150)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        self.assertEqual(plan.boards[0].tooltip, "幅30 × 丈1150")

    def test_cut_produces_line_and_zone(self):
        boards = [placed(0, 0, 1150, 2000, orig_w=1150, orig_l=2500)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        fills = [r.fill for r in plan.cut_marks]
        self.assertIn(render.COLOR_CUT_LINE, fills)
        self.assertIn(render.COLOR_CUT_ZONE, fills)

    def test_uncut_fill_board_has_no_cut_marks(self):
        # 枠(1150×2650)にちょうど収まる。切る場所がどこにも無い
        boards = [placed(0, 0, 1150, 2000), placed(2000, 0, 50, 650, is_fill=True)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        self.assertEqual(plan.cut_marks, [])

    def test_overhanging_board_gets_cut_marks(self):
        """**枠からはみ出した分**にもカット表示を出す。

        在庫の実寸のまま置いて枠を超えているボードは、実寸と一致する
        ので `detect_board_cut` では見つからない。現物ではその分を
        切るので図にも出す(現場の声:「選定→配置ではカットがあっても
        描写なし。候補変更では出る」)。
        """
        boards = [placed(0, 0, 1150, 3000)]        # 丈3000 > パレット丈2650
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        fills = [r.fill for r in plan.cut_marks]
        self.assertIn(render.COLOR_CUT_ZONE, fills)
        self.assertIn(render.COLOR_CUT_LINE, fills)

    def test_はみ出したボードの寸法は残る側に寄せる(self):
        """現場の声:「カット描写まではよくなったが**ボードサイズが

        見えなくなってしまった**」。はみ出した分には切り落としの帯が
        重なるので、矩形の中心に置くと寸法が帯の下に隠れる。
        **切ったあとに残る側**へ寄せる ── 現物として残るのはそちらで、
        寸法はその板の呼び名だから。
        """
        boards = [placed(0, 0, 1150, 3000)]        # 丈3000 > パレット丈2650
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        rect = plan.boards[0]
        self.assertIsNotNone(rect.caption)
        frame_right = plan.offset_x + 2650 * plan.scale
        self.assertIsNotNone(rect.caption_cx)
        self.assertLess(rect.caption_cx, frame_right, "帯の下に入っていない")
        self.assertAlmostEqual(rect.caption_cx, (rect.x + frame_right) / 2,
                               places=3)
        self.assertIsNone(rect.caption_cy, "幅方向は はみ出していない")

    def test_はみ出していなければ寄せない(self):
        boards = [placed(0, 0, 1150, 2000)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        self.assertIsNone(plan.boards[0].caption_cx)
        self.assertIsNone(plan.boards[0].caption_cy)

    def test_幅がはみ出したら縦にも寄せる(self):
        boards = [placed(0, 0, 1400, 2000)]        # 幅1400 > パレット幅1150
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        rect = plan.boards[0]
        frame_bottom = plan.offset_y + 1150 * plan.scale
        self.assertIsNotNone(rect.caption_cy)
        self.assertLess(rect.caption_cy, frame_bottom)

    def test_overhang_marks_cover_both_directions(self):
        boards = [placed(0, 0, 1300, 3000)]        # 幅も丈も超える
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        captions = {r.caption for r in plan.cut_marks if r.caption}
        self.assertEqual(captions, {"|||", "==="})

    def test_overhang_line_spans_only_the_overhanging_boards(self):
        """切る線は**はみ出している板のぶんだけ**。枠の端いっぱいに

        引くと、はみ出していない板まで切るように見える。
        """
        boards = [placed(0, 0, 500, 2000),          # 収まっている
                  placed(0, 500, 400, 3000)]        # 丈がはみ出す
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        line = next(r for r in plan.cut_marks if r.fill == render.COLOR_CUT_LINE)
        scale = plan.scale
        self.assertAlmostEqual(line.y, plan.offset_y + 500 * scale, places=3)
        self.assertAlmostEqual(line.height, 400 * scale, places=3)

    def test_cut_fill_board_tooltip_mentions_cut(self):
        boards = [placed(0, 0, 1150, 2000, is_fill=True, orig_w=1150, orig_l=2500)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        self.assertIn("カット", plan.boards[0].tooltip)

    def test_only_requested_category_is_drawn(self):
        boards = [placed(0, 0, 1150, 2650, LOWER), placed(0, 0, 1122, 2502, UPPER)]
        plan = render.build_render_plan(boards, UPPER, 1122, 2502, 400, 200)
        self.assertEqual(len(plan.boards), 1)
        self.assertEqual(plan.boards[0].fill, render.COLOR_UPPER)

    def test_fill_board_gets_distinct_color_when_large_enough_for_caption(self):
        """補填ボードは、キャプションが入る大きさなら主ボードと同じ

        赤/緑ではなく専用色にする(現場の声:「補填ボードが解かりにくい」)。
        既存のTHIN_*色分け(細すぎて文字が入らないとき)とは別の仕組み。
        """
        boards = [placed(0, 0, 1150, 2650, is_fill=True)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        rect = plan.boards[0]
        self.assertNotEqual(rect.caption, "")   # 主ボードと同じ大きさ扱い
        self.assertEqual(rect.fill, render.COLOR_FILL)
        self.assertEqual(rect.outline, render.COLOR_FILL_OUTLINE)

    def test_main_board_keeps_category_color(self):
        boards = [placed(0, 0, 1150, 2650, is_fill=False)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        self.assertEqual(plan.boards[0].fill, render.COLOR_LOWER)

    def test_overhanging_board_gets_warning_outline(self):
        # 枠(1150x2650)の丈を大きく超える配置
        boards = [placed(0, 0, 1150, 4000)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        rect = plan.boards[0]
        self.assertEqual(rect.outline, render.COLOR_OVERHANG_LINE)
        self.assertGreater(rect.outline_width, 1.0)
        self.assertIn("はみ出し", rect.tooltip)
        # 塗りは変えない(縁取りだけで示す)
        self.assertEqual(rect.fill, render.COLOR_LOWER)

    def test_non_overhanging_board_keeps_normal_outline(self):
        boards = [placed(0, 0, 1150, 2650)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        rect = plan.boards[0]
        self.assertEqual(rect.outline, render.COLOR_BOARD_OUTLINE)
        self.assertEqual(rect.outline_width, 1.0)
        self.assertNotIn("はみ出し", rect.tooltip)

    def test_overhanging_fill_board_prefers_warning_outline(self):
        # はみ出し(警告)と補填(専用色)が両方成り立つ場合、輪郭は警告を優先
        boards = [placed(0, 0, 1150, 4000, is_fill=True)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        rect = plan.boards[0]
        self.assertEqual(rect.fill, render.COLOR_FILL)
        self.assertEqual(rect.outline, render.COLOR_OVERHANG_LINE)


class UsageRatioByCategoryTests(unittest.TestCase):
    """VBA `CalculateUsageRatioByCategory` の移植。基準は常にパレット面積。"""

    def test_no_boards_means_zero(self):
        self.assertEqual(render.usage_ratio_by_category([], LOWER, 1150, 2650), 0.0)

    def test_exact_full_coverage_is_100_percent(self):
        boards = [placed(0, 0, 1150, 2650)]
        self.assertAlmostEqual(
            render.usage_ratio_by_category(boards, LOWER, 1150, 2650), 100.0)

    def test_partial_coverage_is_the_area_ratio(self):
        boards = [placed(0, 0, 575, 1325)]      # 幅・丈とも半分 -> 面積は1/4
        self.assertAlmostEqual(
            render.usage_ratio_by_category(boards, LOWER, 1150, 2650), 25.0)

    def test_overhanging_part_is_clipped_before_summing_area(self):
        boards = [placed(x=900, y=0, w=1000, l=200)]   # 丈方向に100はみ出す
        self.assertAlmostEqual(
            render.usage_ratio_by_category(boards, LOWER, 1000, 1000), 10.0)

    def test_a_board_entirely_outside_the_palette_contributes_nothing(self):
        boards = [placed(x=1000, y=0, w=1000, l=200)]
        self.assertEqual(render.usage_ratio_by_category(boards, LOWER, 1000, 1000), 0.0)

    def test_other_categories_are_ignored(self):
        boards = [placed(0, 0, 1150, 2650, category=UPPER)]
        self.assertEqual(render.usage_ratio_by_category(boards, LOWER, 1150, 2650), 0.0)

    def test_zero_palette_area_is_zero(self):
        boards = [placed(0, 0, 1150, 2650)]
        self.assertEqual(render.usage_ratio_by_category(boards, LOWER, 0, 2650), 0.0)


class OverhangRatioTests(unittest.TestCase):
    """VBA `CalculateOverhangRatio` の移植。複数ボードは合算(最大値ではない)。"""

    def test_no_overhang_is_zero(self):
        boards = [placed(0, 0, 1000, 1000)]
        self.assertEqual(render.overhang_ratio(boards, LOWER, 1000, 1000), 0.0)

    def test_length_direction_overhang_only(self):
        boards = [placed(0, 0, 1000, 1200)]   # 丈方向に200はみ出し
        self.assertAlmostEqual(render.overhang_ratio(boards, LOWER, 1000, 1000), 20.0)

    def test_width_direction_overhang_only(self):
        boards = [placed(0, 0, 1200, 1000)]   # 幅方向に200はみ出し
        self.assertAlmostEqual(render.overhang_ratio(boards, LOWER, 1000, 1000), 20.0)

    def test_corner_overlap_is_subtracted_once(self):
        """両方向にはみ出るとき、コーナー分を二重計上しない。"""
        boards = [placed(0, 0, 1200, 1200)]
        # L字型のはみ出し面積 = 1200*1200 - 1000*1000 = 440,000 -> 44.0%
        self.assertAlmostEqual(render.overhang_ratio(boards, LOWER, 1000, 1000), 44.0)

    def test_multiple_boards_are_summed_not_maxed(self):
        boards = [placed(0, 0, 1000, 1100), placed(0, 0, 500, 1050)]
        expected = (100 * 1000 + 50 * 500) / (1000 * 1000) * 100
        self.assertAlmostEqual(render.overhang_ratio(boards, LOWER, 1000, 1000), expected)

    def test_other_categories_are_ignored(self):
        boards = [placed(0, 0, 1200, 1200, category=UPPER)]
        self.assertEqual(render.overhang_ratio(boards, LOWER, 1000, 1000), 0.0)

    def test_zero_palette_dimensions_is_zero(self):
        boards = [placed(0, 0, 1200, 1200)]
        self.assertEqual(render.overhang_ratio(boards, LOWER, 0, 1000), 0.0)


class CutSummaryTextTests(unittest.TestCase):
    """VBA `GetCutSummaryText` の移植(配置後サマリの「はみ出し/ボードカット」欄)。"""

    def test_no_boards_means_no_cut(self):
        self.assertEqual(render.cut_summary_text([], LOWER, 1150, 2650), "カットなし")

    def test_reports_overhang_beyond_the_base_frame(self):
        boards = [
            placed(0, 0, 1150, 2900),      # 丈方向に250はみ出し(2900-2650)
            placed(0, 100, 1150, 2000),    # 幅方向に100はみ出し(1250-1150)
        ]
        text = render.cut_summary_text(boards, LOWER, 1150, 2650)
        self.assertEqual(text, "はみ出し丈カット: 約250mm  はみ出し幅カット: 約100mm")

    def test_reports_board_cut_from_original_size(self):
        boards = [placed(0, 0, 1150, 2000, orig_w=1150, orig_l=2500)]
        text = render.cut_summary_text(boards, LOWER, 1150, 2650)
        self.assertEqual(text, "ボード丈カット: 500mm")

    def test_small_board_cuts_within_tolerance_are_ignored(self):
        """3mm以下のカットは丸め誤差として無視する(VBA: > 3)。"""
        boards = [placed(0, 0, 1150, 2497, orig_w=1150, orig_l=2500)]
        self.assertEqual(render.cut_summary_text(boards, LOWER, 1150, 2650), "カットなし")

    def test_other_categories_are_ignored(self):
        boards = [placed(0, 0, 1122, 2900, category=UPPER)]
        self.assertEqual(render.cut_summary_text(boards, LOWER, 1150, 2650), "カットなし")

    def test_takes_the_worst_case_across_multiple_boards(self):
        boards = [placed(0, 0, 1150, 2700), placed(0, 0, 1150, 2900)]
        text = render.cut_summary_text(boards, LOWER, 1150, 2650)
        self.assertEqual(text, "はみ出し丈カット: 約250mm")


class CoverageDeficitTextTests(unittest.TestCase):
    """VBA `GetCoverageDeficitText` の移植。基準は常に製品サイズ。"""

    def test_exact_coverage_is_ok(self):
        boards = [placed(0, 0, 1150, 2650)]
        self.assertEqual(
            render.coverage_deficit_text(boards, LOWER, 1150, 2650), "幅OK  丈OK")

    def test_within_3mm_tolerance_is_still_ok(self):
        boards = [placed(0, 0, 1148, 2648)]
        self.assertEqual(
            render.coverage_deficit_text(boards, LOWER, 1150, 2650), "幅OK  丈OK")

    def test_undercoverage_is_reported_as_shortfall(self):
        boards = [placed(0, 0, 1000, 2600)]
        self.assertEqual(
            render.coverage_deficit_text(boards, LOWER, 1150, 2650), "幅-150mm  丈-50mm")

    def test_overcoverage_beyond_tolerance_is_reported_as_excess(self):
        boards = [placed(0, 0, 1200, 2650)]
        self.assertEqual(
            render.coverage_deficit_text(boards, LOWER, 1150, 2650), "幅+50mm超  丈OK")

    def test_base_is_always_product_size_even_for_the_lower_category(self):
        """下用でもパレットサイズではなく製品サイズと比較する(VBA原文どおり)。"""
        boards = [placed(0, 0, 1150, 2650)]
        text_vs_product = render.coverage_deficit_text(boards, LOWER, 1150, 2650)
        text_vs_larger_pallet = render.coverage_deficit_text(boards, LOWER, 1300, 2900)
        self.assertNotEqual(text_vs_product, text_vs_larger_pallet)


class CutStatusColorTests(unittest.TestCase):
    """VBA `DisplayInfo_Normal`/`_Kyoyo` の3段階配色判定。"""

    def test_shortfall_takes_priority(self):
        self.assertEqual(
            render.cut_status_color("カットなし", "幅-150mm  丈OK"),
            (render.COLOR_INFO_DEFICIT_BG, render.COLOR_INFO_DEFICIT_FG))

    def test_cut_without_shortfall_is_the_cut_color(self):
        self.assertEqual(
            render.cut_status_color("ボード丈カット: 500mm", "幅OK  丈OK"),
            (render.COLOR_INFO_CUT_BG, render.COLOR_INFO_CUT_FG))

    def test_no_cut_and_no_shortfall_is_ok(self):
        self.assertEqual(
            render.cut_status_color("カットなし", "幅OK  丈OK"),
            (render.COLOR_INFO_OK_BG, render.COLOR_INFO_OK_FG))

    def test_shortfall_wins_even_when_a_cut_is_also_present(self):
        self.assertEqual(
            render.cut_status_color("ボード丈カット: 500mm", "幅-10mm  丈OK"),
            (render.COLOR_INFO_DEFICIT_BG, render.COLOR_INFO_DEFICIT_FG))


if __name__ == "__main__":
    unittest.main()
