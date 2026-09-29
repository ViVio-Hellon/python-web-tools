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

    def test_補填の色は厚みだけで決まる(self):
        """30/50/100 で統一する(現場の指摘:「補填3種は30,50,100で色を統一」)。"""
        self.assertEqual(render.fill_board_color(30), render.COLOR_THIN_30)
        self.assertEqual(render.fill_board_color(50), render.COLOR_THIN_50)
        self.assertEqual(render.fill_board_color(100), render.COLOR_THIN_100)
        # 3種以外は端数。補填の地色のまま(種類ではないので色分けしない)
        self.assertEqual(render.fill_board_color(70), render.COLOR_FILL)

    def test_補填の色は文字が入るかどうかで変わらない(self):
        """**これが直したかった食い違い。**

        以前は色を決める分岐が「キャプションが入るか」の分岐と一体で、
        入る補填は黄色1色、入らない細い補填は30/50/100の色分けだった。
        つまり**同じ補填材が、図の中での大きさによって色を変えて**いた。
        """
        for thickness in (30, 50, 100):
            with self.subTest(厚み=thickness):
                # 細い図(文字が入らない)と大きい図(文字が入る)
                narrow = render.build_render_plan(
                    [placed(0, 0, thickness, 2000, is_fill=True)],
                    LOWER, 1100, 2000, 980, 460)
                wide = render.build_render_plan(
                    [placed(0, 0, thickness, 2000, is_fill=True)],
                    LOWER, thickness, 2000, 980, 460)
                self.assertEqual(narrow.boards[0].fill, wide.boards[0].fill)
                self.assertEqual(narrow.boards[0].fill,
                                 render.fill_board_color(thickness))


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

    def test_普通の大きさの補填の板は主ボードと同じ色(self):
        """現場の声:「カットする部分の色を変えるのはよいが、ボード全部の色を
        変えてしまってよくわからない」。補填の色は細い補填材だけにし、
        普通の大きさの板は補填に使っても主ボードの色のまま(切る部分は斜線)。"""
        boards = [placed(0, 0, 1150, 2650, is_fill=True)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        rect = plan.boards[0]
        self.assertNotEqual(rect.caption, "")
        self.assertEqual(rect.fill, render.COLOR_LOWER)
        self.assertEqual(rect.outline, render.COLOR_BOARD_OUTLINE)

    def test_細い補填材は補填の色(self):
        boards = [placed(0, 0, 70, 2650, is_fill=True)]
        plan = render.build_render_plan(boards, LOWER, 1150, 2650, 400, 200)
        self.assertEqual(plan.boards[0].fill, render.COLOR_FILL)
        self.assertEqual(plan.boards[0].outline, render.COLOR_FILL_OUTLINE)

    def test_切り落とす部分は枠の外へ描かない(self):
        """30×2500 を 500 に切って枠の途中に置くと、捨てる 2000mm が
        枠の外まで伸びて、はみ出しに見えていた。"""
        pb = placed(2500, 20, 30, 500, UPPER)
        pb.original_width, pb.original_length = 30, 2500
        plan = render.build_render_plan([pb], UPPER, 1783, 3838, 882, 440)
        right = plan.border.x + plan.border.width
        zones = [c for c in plan.cut_marks if c.fill == render.COLOR_CUT_ZONE]
        self.assertTrue(zones)
        for zone in zones:
            self.assertLessEqual(zone.x + zone.width, right + 0.01)

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
        boards = [placed(0, 0, 80, 4000, is_fill=True)]
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


class 入らない文字は凡例に回す(unittest.TestCase):
    """**文字も凡例も出ないボードを作らない。**

    現場の指摘:「50補填は50mmと左下に出て分かったが100は出ていなかった」。

    丈補填は縦長の帯で、縦は製品幅いっぱいあるが横は板厚ぶんしかない。
    文字を出すかどうかは縦横どちらかが30px未満かで決めていたので、
    厚み100mm(画面で38px)は「文字を出す」側になり、そこへ24ptで
    「幅812」と書こうとしていた ── 入るはずがない。しかも凡例の条件
    (細さ)からは外れるので、**何も名乗らないボード**になっていた。
    """

    def fill(self, thickness: int, base_w: int = 812, base_l: int = 2302):
        """丈補填の帯を1枚だけ置いた図を作る。"""
        board = PlacedBoardModel(
            width=base_w, length=thickness, x=2000, y=0,
            board_category="上用", original_width=base_w,
            original_length=thickness, is_fill_board=True)
        return render.build_render_plan([board], "上用", base_w, base_l, 980, 460)

    def test_厚み100は凡例に出る(self) -> None:
        plan = self.fill(100)
        self.assertEqual(plan.boards[0].caption, "")
        self.assertEqual([t for t, _c in plan.legend], [100])

    def test_厚み50はこれまでどおり凡例(self) -> None:
        plan = self.fill(50)
        self.assertEqual([t for t, _c in plan.legend], [50])

    def test_太ければ文字を出す(self) -> None:
        """全部を凡例に回すのは行き過ぎ。入るなら板の上に書く。"""
        plan = self.fill(300)
        self.assertIn("丈300", plan.boards[0].caption)
        self.assertEqual(plan.legend, [])

    def test_凡例の色は板の色と同じ(self) -> None:
        """凡例は色で引く。板と違う色を出したら引けない。"""
        plan = self.fill(100)
        self.assertEqual(plan.legend[0][1], plan.boards[0].fill)

    # -- 色の案内 ----------------------------------------------------
    def test_厚みで色が決まる補填は必ず凡例に出る(self) -> None:
        """**色だけ出して意味を言わない状態を作らない。**

        30/50/100 は「何ミリの補填か」を色で言っている。以前は「文字が
        入らなかったもの」だけを凡例に出していたので、帯が太くて文字が
        入る図では、色は付いているのに凡例から消えていた(現場の指摘:
        「補填用ボードの30,50,100が出るときと出ないときがある(色での
        案内)」)。
        """
        for thickness in (30, 50, 100):
            with self.subTest(thickness=thickness):
                plan = self.fill(thickness, base_w=420, base_l=985)
                self.assertEqual([t for t, _c in plan.legend], [thickness])

    def test_凡例の色は図に出ている色(self) -> None:
        for thickness in (30, 50, 100):
            with self.subTest(thickness=thickness):
                plan = self.fill(thickness, base_w=420, base_l=985)
                self.assertEqual(plan.legend[0][1], plan.boards[0].fill)

    def test_端数の厚みは色の案内に出さない(self) -> None:
        """30/50/100 以外は補填の地色で、**色が種類を指していない。**

        文字が入る大きさで試す(入らなければ、そちらの規則で凡例に回る
        ── それは色の案内ではなく「名乗る手段が無いから」)。
        """
        plan = self.fill(300, base_w=420, base_l=985)
        self.assertTrue(plan.boards[0].caption)
        self.assertEqual(plan.legend, [])

    def test_主ボードは色の案内に出さない(self) -> None:
        """細くない主ボードの色は上用/下用の区別で、厚みではない。"""
        board = PlacedBoardModel(
            width=50, length=985, x=0, y=0, board_category="上用",
            original_width=50, original_length=985, is_fill_board=False)
        plan = render.build_render_plan([board], "上用", 420, 985, 980, 460)
        self.assertEqual(plan.legend, [])

    # -- 入るかどうかの見積もり --------------------------------------
    def test_全角と半角を分けて数える(self) -> None:
        """ひとまとめに全角で見ると、入るはずの板まで文字をやめる。"""
        # 「幅812」= 全角1 + 半角3 = 1.0 + 1.8 = 2.8em
        self.assertAlmostEqual(render.caption_line_em("幅812"), 2.8)

    def test_一番長い行で決める(self) -> None:
        self.assertFalse(render.caption_fits("幅812\n丈1200", 24, 70))
        self.assertTrue(render.caption_fits("幅812\n丈1200", 24, 100))

    def test_文字が無ければ収まる扱い(self) -> None:
        self.assertTrue(render.caption_fits("", 24, 1))
