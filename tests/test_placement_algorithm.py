"""資材配置アルゴリズム(placement_algorithm)のユニットテスト。

VBA `PlaceBoardsFromList` 系の境界条件・タグ別の配置経路・
移植時に踏襲したVBA側の既知の不具合を検証する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import placement_algorithm as pl
# 内側(下線で始まる名前)は持ち主のモジュールから引く。
# ハブ(`pl`)が公開するのは外向けの名前だけ
from packaging_tool import placement_fill as pfill
from packaging_tool.board_selection_service import Palette, ProductSize, SelectedBoard
from packaging_tool.models import BoardModel, PlacedBoardModel

LOWER = pl.CATEGORY_LOWER
UPPER = pl.CATEGORY_UPPER


def make_palette(width: int, length: int) -> Palette:
    return Palette(width=width, length=length, overhang_ratio=1.10,
                   max_width=width * 1.10, max_length=length * 1.10)


def make_ctx(pal_w: int = 1150, pal_l: int = 2650,
             prod_w: int = 1122, prod_l: int = 2502) -> pl.PlacementContext:
    return pl.PlacementContext(
        palette=make_palette(pal_w, pal_l),
        product=ProductSize(width=prod_w, length=prod_l),
    )


def model(w: int, l: int, category: str = LOWER, *, count: int = 1) -> BoardModel:
    return BoardModel(width=w, length=l, count=count,
                      instance_id="t", board_category=category)


def placed(x: int, y: int, w: int, l: int, category: str = LOWER,
           *, is_fill: bool = False) -> PlacedBoardModel:
    return PlacedBoardModel(x=x, y=y, width=w, length=l,
                            board_category=category, is_fill_board=is_fill)


def _check(ctx: pl.PlacementContext, place_x: int, b_short: int, limit_w: int) -> bool:
    return pfill._check_length_fill_overlap(ctx, LOWER, place_x, b_short, limit_w)


class EvaluatePlacementTests(unittest.TestCase):
    def test_prefers_top_left(self):
        # 左上ほど良い(小さい)
        self.assertLess(pl.evaluate_placement(0, 0), pl.evaluate_placement(10, 0))
        self.assertLess(pl.evaluate_placement(0, 10), pl.evaluate_placement(10, 10))

    def test_x_and_y_have_equal_weight(self):
        self.assertEqual(pl.evaluate_placement(30, 10), pl.evaluate_placement(10, 30))


class CanPlaceBoardAtTests(unittest.TestCase):
    def test_upper_forbids_x_at_or_beyond_product_length(self):
        ctx = make_ctx(prod_l=2502)
        b = model(300, 500, UPPER)
        self.assertTrue(pl.can_place_board_at(ctx, 2501, 0, b))
        # x >= 製品丈 は「ちょうど」でも禁止(VBA踏襲)
        self.assertFalse(pl.can_place_board_at(ctx, 2502, 0, b))

    def test_upper_allows_width_tolerance_of_80(self):
        ctx = make_ctx(prod_w=1122)
        b = model(1202, 500, UPPER)  # 1122 + 80 ちょうど
        self.assertTrue(pl.can_place_board_at(ctx, 0, 0, b))
        self.assertFalse(pl.can_place_board_at(ctx, 1, 0, model(1203, 500, UPPER)))

    def test_upper_forbids_negative_y(self):
        ctx = make_ctx()
        self.assertFalse(pl.can_place_board_at(ctx, 0, -1, model(300, 500, UPPER)))

    def test_lower_allows_negative_y_overhang(self):
        # overhang_ratio=1.10 → 幅300のボードは y >= -30 まで許容
        ctx = make_ctx(pal_w=1150)
        b = model(300, 500, LOWER)
        self.assertTrue(pl.can_place_board_at(ctx, 0, -30, b))
        self.assertFalse(pl.can_place_board_at(ctx, 0, -31, b))

    def test_lower_caps_y_at_120_percent_of_palette_width(self):
        ctx = make_ctx(pal_w=1000)  # 上限 = 1200
        self.assertTrue(pl.can_place_board_at(ctx, 0, 700, model(500, 400, LOWER)))
        self.assertFalse(pl.can_place_board_at(ctx, 0, 701, model(500, 400, LOWER)))

    def test_rotation_swaps_dimensions_for_boundary_check(self):
        ctx = make_ctx(pal_w=1000)  # 上限 = 1200
        b = model(1300, 400, LOWER)
        self.assertFalse(pl.can_place_board_at(ctx, 0, 0, b, rotate=False))
        self.assertTrue(pl.can_place_board_at(ctx, 0, 0, b, rotate=True))

    def test_overlap_detected_within_same_category(self):
        ctx = make_ctx()
        ctx.placed.append(placed(0, 0, 500, 1000, LOWER))
        self.assertFalse(pl.can_place_board_at(ctx, 999, 499, model(300, 300, LOWER)))
        # 端が接するだけなら重ならない
        self.assertTrue(pl.can_place_board_at(ctx, 1000, 0, model(300, 300, LOWER)))
        self.assertTrue(pl.can_place_board_at(ctx, 0, 500, model(300, 300, LOWER)))

    def test_other_category_never_blocks(self):
        ctx = make_ctx()
        ctx.placed.append(placed(0, 0, 500, 1000, UPPER))
        # 上用が同じ場所にあっても下用は置ける
        self.assertTrue(pl.can_place_board_at(ctx, 0, 0, model(300, 300, LOWER)))


class CanPlaceAtWithYLimitTests(unittest.TestCase):
    def test_y_limit_is_exact_no_tolerance(self):
        ctx = make_ctx()
        b = model(500, 800, UPPER)
        self.assertTrue(pl.can_place_at_with_y_limit(ctx, 0, 500, b, False, 1000))
        self.assertFalse(pl.can_place_at_with_y_limit(ctx, 0, 501, b, False, 1000))

    def test_negative_x_rejected(self):
        ctx = make_ctx()
        self.assertFalse(pl.can_place_at_with_y_limit(ctx, -1, 0, model(100, 100, UPPER), False, 1000))

    def test_x_bound_allows_80mm_overhang(self):
        ctx = make_ctx()
        b = model(100, 500, UPPER)
        # x + 丈500 <= 1000 + 80 まで許容
        self.assertTrue(pl.can_place_at_with_y_limit_and_x_bound(ctx, 580, 0, b, False, 1000, 1000))
        self.assertFalse(pl.can_place_at_with_y_limit_and_x_bound(ctx, 581, 0, b, False, 1000, 1000))


class PlaceBoardAtTests(unittest.TestCase):
    def test_custom_size_overrides_placed_dimensions_but_not_original(self):
        ctx = make_ctx()
        ok = pl.place_board_at(ctx, 10, 20, model(400, 1200, LOWER), rotate=True,
                               bypass_check=True, custom_width=1150, custom_length=300)
        self.assertTrue(ok)
        pb = ctx.placed[0]
        self.assertEqual((pb.width, pb.length), (1150, 300))
        # Original は回転を反映した実寸(rotate=True → 幅=1200 丈=400)
        self.assertEqual((pb.original_width, pb.original_length), (1200, 400))

    def test_custom_size_ignored_unless_both_given(self):
        ctx = make_ctx()
        pl.place_board_at(ctx, 0, 0, model(400, 1200, LOWER),
                          bypass_check=True, custom_width=999)
        self.assertEqual((ctx.placed[0].width, ctx.placed[0].length), (400, 1200))

    def test_failing_check_places_nothing(self):
        ctx = make_ctx()
        self.assertFalse(pl.place_board_at(ctx, -5, 0, model(400, 1200, LOWER)))
        self.assertEqual(ctx.placed, [])

    def test_bypass_check_places_out_of_bounds(self):
        ctx = make_ctx()
        self.assertTrue(pl.place_board_at(ctx, -5, 0, model(400, 1200, LOWER), bypass_check=True))
        self.assertEqual(len(ctx.placed), 1)

    def test_instance_id_is_unique_per_placement(self):
        ctx = make_ctx()
        b = model(400, 1200, LOWER)
        pl.place_board_at(ctx, 0, 0, b, bypass_check=True)
        pl.place_board_at(ctx, 1200, 0, b, bypass_check=True)
        self.assertEqual([p.instance_id for p in ctx.placed], ["t_1", "t_2"])


class TryPlaceSingleOrientationTests(unittest.TestCase):
    def test_finds_origin_when_empty(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        got = pl.try_place_single_orientation(ctx, model(1130, 750, LOWER), False, 1e15)
        self.assertIsNotNone(got)
        self.assertEqual(got[:2], (0, 0))

    def test_returns_none_when_width_never_fits(self):
        ctx = make_ctx(pal_w=1000)  # 下用のY上限は1200
        self.assertIsNone(
            pl.try_place_single_orientation(ctx, model(1300, 400, LOWER), False, 1e15))

    def test_fine_scan_snaps_flush_to_existing_board(self):
        # 粗探索(20mm刻み)だけなら x=760 になるところを、
        # PASS3の1mm刻み細探索で x=750 ちょうどまで詰める。
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1130, 750, LOWER))
        got = pl.try_place_single_orientation(ctx, model(1130, 750, LOWER), False, 1e15)
        self.assertIsNotNone(got)
        self.assertEqual(got[:2], (750, 0))

    def test_min_waste_gate_rejects_worse_positions(self):
        # 既に (0,0) 相当のスコア0が確定していれば何も更新できない
        ctx = make_ctx()
        self.assertIsNone(
            pl.try_place_single_orientation(ctx, model(1130, 750, LOWER), False, 0.0))


class TryPlaceInsidePaletteTests(unittest.TestCase):
    def test_picks_orientation_closest_to_target_width(self):
        # パレット幅1150 → 幅1130の向き(距離20)を、幅750の向き(距離400)より優先
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        self.assertTrue(pl.try_place_inside_palette(ctx, model(750, 1130, LOWER)))
        self.assertEqual(ctx.placed[0].width, 1130)

    def test_falls_back_to_other_orientation(self):
        # 幅1300は下用上限1200を超えるので、回転した幅400が選ばれる
        ctx = make_ctx(pal_w=1000, pal_l=2000)
        self.assertTrue(pl.try_place_inside_palette(ctx, model(1300, 400, LOWER)))
        self.assertEqual(ctx.placed[0].width, 400)

    def test_upper_uses_product_width_as_target(self):
        ctx = make_ctx(pal_w=1150, prod_w=600, prod_l=2000)
        self.assertTrue(pl.try_place_inside_palette(ctx, model(600, 1000, UPPER)))
        self.assertEqual(ctx.placed[0].width, 600)


class TryPlaceInsidePaletteProtecModeTests(unittest.TestCase):
    """VBA `TryPlaceInsidePalette` のプロテック専用向き決定分岐の検証。

    自動選定(`decide_protec_orientation`)を経由しない手動追加ボードでも、
    「製品幅基準・マイナス方向の許容のみ・超過禁止」というプロテックの
    ルールで向きを強制する(距離が近いだけの向きを選ばせない)。
    """

    def test_only_normal_orientation_within_tolerance_is_forced(self):
        # 幅950は許容内(920〜1000)、丈1100は製品幅超過で許容外。
        # 距離だけなら丈1100側(距離50)が幅950側(距離200)より近いが、
        # プロテックルールにより許容内の向き(幅950)が強制される。
        ctx = make_ctx(pal_w=1150, pal_l=2650, prod_w=1000)
        ctx.is_protec_mode = True
        self.assertTrue(pl.try_place_inside_palette(ctx, model(950, 1100, LOWER)))
        self.assertEqual(ctx.placed[0].width, 950)

    def test_only_rotated_orientation_within_tolerance_is_forced(self):
        # 幅850は許容外(920未満)、丈950は許容内 → 回転が強制される。
        # target(パレット幅850)への距離だけなら幅850側(距離0)が丈950側
        # (距離100)より近く、距離基準の従来ロジックなら誤って幅850を選ぶ。
        ctx = make_ctx(pal_w=850, pal_l=2650, prod_w=1000)
        ctx.is_protec_mode = True
        self.assertTrue(pl.try_place_inside_palette(ctx, model(850, 950, LOWER)))
        self.assertEqual(ctx.placed[0].width, 950)

    def test_neither_orientation_within_tolerance_falls_back_to_distance(self):
        # どちらも許容外(警告ログを出しつつ従来の距離比較で続行)
        ctx = make_ctx(pal_w=1150, pal_l=2650, prod_w=1000)
        ctx.is_protec_mode = True
        self.assertTrue(pl.try_place_inside_palette(ctx, model(500, 600, LOWER)))
        # target=パレット幅1150、距離: 幅500→650、丈600→550 → 丈側(600)が近い
        self.assertEqual(ctx.placed[0].width, 600)

    def test_1p1216_uses_tighter_10mm_tolerance(self):
        # 幅950は通常許容(-80mm=920以上)なら通るが、1P1216の-10mm許容
        # (=990以上)では外れる → 丈995側(許容内)が強制される。
        # target(パレット幅950)への距離だけなら幅950側(距離0)が丈995側
        # (距離45)より近く、距離基準の従来ロジックなら誤って幅950を選ぶ。
        ctx = make_ctx(pal_w=950, pal_l=2650, prod_w=1000)
        ctx.is_protec_mode = True
        ctx.is_1p1216 = True
        self.assertTrue(pl.try_place_inside_palette(ctx, model(950, 995, LOWER)))
        self.assertEqual(ctx.placed[0].width, 995)

    def test_does_not_apply_to_upper_category(self):
        # プロテックの向き決定分岐は下用専用(上用は従来の距離比較のまま)
        ctx = make_ctx(pal_w=1150, pal_l=2650, prod_w=1000, prod_l=2000)
        ctx.is_protec_mode = True
        self.assertTrue(pl.try_place_inside_palette(ctx, model(950, 1100, UPPER)))
        # target=製品幅1000、距離: 幅950→50、丈1100→100 → 幅側(950)が近い
        # (協定ルールが無くても結果は同じになる設定だが、分岐に入らないことを
        # ログの有無ではなくカテゴリ条件そのもので保証する)
        self.assertEqual(ctx.placed[0].width, 950)

    def test_normal_mode_unaffected(self):
        # is_protec_mode=False(既定)なら従来どおり距離だけで決まる
        ctx = make_ctx(pal_w=1150, pal_l=2650, prod_w=1000)
        self.assertTrue(pl.try_place_inside_palette(ctx, model(950, 1100, LOWER)))
        # target=パレット幅1150、距離: 幅950→200、丈1100→50 → 丈側が近い
        self.assertEqual(ctx.placed[0].width, 1100)


class AutoPlaceBoardsProtecModeTests(unittest.TestCase):
    def test_protec_flags_reach_placement_context(self):
        lower = [SelectedBoard(950, 1100, 1, "")]
        ctx = pl.auto_place_boards(lower, [], make_palette(1150, 2650),
                                   ProductSize(width=1000, length=2000),
                                   is_protec_mode=True, is_1p1216=False)
        self.assertTrue(ctx.is_protec_mode)
        self.assertFalse(ctx.is_1p1216)
        # プロテックルール(-80mm許容)で幅950(許容内)側が強制される
        self.assertEqual(ctx.placed[0].width, 950)

    def test_defaults_to_normal_mode(self):
        ctx = pl.auto_place_boards([], [], make_palette(1150, 2650),
                                   ProductSize(width=1000, length=2000))
        self.assertFalse(ctx.is_protec_mode)
        self.assertFalse(ctx.is_1p1216)


class TryPlaceUpperWithYOffsetTests(unittest.TestCase):
    def test_places_at_given_y_and_leftmost_x(self):
        ctx = make_ctx(prod_w=1122, prod_l=2502)
        state = pl.RotationState()
        self.assertTrue(
            pl.try_place_upper_with_y_offset(ctx, model(500, 2400, UPPER), 600, state))
        pb = ctx.placed[0]
        self.assertEqual((pb.x, pb.y), (0, 600))

    def test_rejects_when_exceeding_width_tolerance(self):
        # y=600 + 幅700 = 1300 > 1122 + 80
        ctx = make_ctx(prod_w=1122, prod_l=2502)
        state = pl.RotationState()
        self.assertFalse(
            pl.try_place_upper_with_y_offset(ctx, model(700, 2400, UPPER), 600, state))

    def test_first_rotation_is_carried_to_later_pieces(self):
        ctx = make_ctx(prod_w=1122, prod_l=2502)
        state = pl.RotationState()
        pl.try_place_upper_with_y_offset(ctx, model(2400, 500, UPPER), 600, state)
        self.assertTrue(state.first_placed)
        # 残りY(=522)に収まるのは500側 → 回転が選ばれる
        self.assertTrue(state.first_rotation)
        self.assertEqual(ctx.placed[0].width, 500)


class TryPlaceUpperLengthFillTests(unittest.TestCase):
    def test_appends_after_main_row_at_y0(self):
        ctx = make_ctx(prod_w=1122, prod_l=2502)
        ctx.placed.append(placed(0, 0, 1100, 2000, UPPER))
        self.assertTrue(pl.try_place_upper_length_fill(ctx, model(1100, 500, UPPER)))
        pb = ctx.placed[-1]
        self.assertEqual((pb.x, pb.y), (2000, 0))
        # 長辺(1100)がY方向、短辺(500)がX方向
        self.assertEqual((pb.width, pb.length), (1100, 500))

    def test_ignores_non_zero_y_rows_when_computing_max_x(self):
        ctx = make_ctx(prod_w=1122, prod_l=2502)
        ctx.placed.append(placed(0, 0, 1100, 2000, UPPER))
        ctx.placed.append(placed(0, 1100, 50, 2400, UPPER, is_fill=True))  # 幅補填行
        pl.try_place_upper_length_fill(ctx, model(1100, 500, UPPER))
        # Y=0の行だけを見るので x=2400 ではなく x=2000
        self.assertEqual(ctx.placed[-1].x, 2000)


class TryPlaceYCompanionTests(unittest.TestCase):
    def test_first_fill_starts_at_gap_board_edge(self):
        # 主ボードが2枚: y端1080のものと1130のもの → 足りない方(1080)が基準
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1130, 1000, LOWER))
        ctx.placed.append(placed(1000, 0, 1080, 1200, LOWER))
        self.assertTrue(pl.try_place_y_companion(ctx, model(50, 1200, LOWER)))
        pb = ctx.placed[-1]
        self.assertEqual((pb.x, pb.y), (1000, 1080))
        self.assertTrue(pb.is_fill_board)

    def test_length_is_clipped_to_remaining_x(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1080, 1500, LOWER))
        pl.try_place_y_companion(ctx, model(50, 2000, LOWER))
        # 主ボード右端1500 - 起点0 = 1500 までにカット
        self.assertEqual(ctx.placed[-1].length, 1500)

    def test_second_fill_continues_same_row(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1080, 2000, LOWER))
        b = model(50, 800, LOWER)
        pl.try_place_y_companion(ctx, b)
        pl.try_place_y_companion(ctx, b)
        first, second = ctx.placed[-2], ctx.placed[-1]
        self.assertEqual((first.x, first.y), (0, 1080))
        self.assertEqual((second.x, second.y), (800, 1080))

    def test_new_row_when_current_row_is_full(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1080, 800, LOWER))
        b = model(50, 800, LOWER)
        pl.try_place_y_companion(ctx, b)   # 行を埋め切る
        pl.try_place_y_companion(ctx, b)   # 次の行へ
        self.assertEqual((ctx.placed[-1].x, ctx.placed[-1].y), (0, 1130))

    def test_returns_false_without_main_board(self):
        ctx = make_ctx()
        self.assertFalse(pl.try_place_y_companion(ctx, model(50, 800, LOWER)))

    def test_upper_clamps_end_x_to_product_length(self):
        ctx = make_ctx(prod_w=1122, prod_l=1000)
        ctx.placed.append(placed(0, 0, 1000, 1500, UPPER))  # 製品丈より長い主ボード
        pl.try_place_y_companion(ctx, model(50, 1500, UPPER))
        self.assertEqual(ctx.placed[-1].length, 1000)


class GetEffectiveTagTests(unittest.TestCase):
    def test_explicit_tag_wins(self):
        ctx = make_ctx()
        boards = [SelectedBoard(1130, 750, 3, "主"), SelectedBoard(50, 1200, 1, "幅補填")]
        self.assertEqual(pl.get_effective_tag(boards, 1, LOWER, ctx), "幅補填")

    def test_index_zero_never_inferred(self):
        ctx = make_ctx()
        boards = [SelectedBoard(50, 1200, 1, "")]
        self.assertEqual(pl.get_effective_tag(boards, 0, LOWER, ctx), "")

    def test_short_side_over_100_never_inferred(self):
        ctx = make_ctx()
        boards = [SelectedBoard(1130, 750, 3, ""), SelectedBoard(101, 1200, 1, "")]
        self.assertEqual(pl.get_effective_tag(boards, 1, LOWER, ctx), "")

    def test_infers_length_fill_when_main_covers_width(self):
        # パレット幅1150、主ボードの有効幅1150 → 幅は足りている → 丈補填
        ctx = make_ctx(pal_w=1150)
        boards = [SelectedBoard(1150, 750, 3, ""), SelectedBoard(100, 1200, 1, "")]
        self.assertEqual(pl.get_effective_tag(boards, 1, LOWER, ctx), "丈補填")

    def test_infers_width_fill_when_main_is_narrow(self):
        ctx = make_ctx(pal_w=1150)
        boards = [SelectedBoard(1080, 750, 3, ""), SelectedBoard(50, 1200, 1, "")]
        self.assertEqual(pl.get_effective_tag(boards, 1, LOWER, ctx), "幅補填")


class GetEffectiveTagToleranceTests(unittest.TestCase):
    """**選定ロジックと同じ許容値で推し量る**(VBA の修正を移植)。

    上用: 製品幅-主ボード幅 が 80mm(`UPPER_WIDTH_TOLERANCE`)以内なら幅補填しない
    下用: 製品幅-主ボード幅 が 11mm(`PASS1_TOLERANCE`)以内なら幅補填しない

    以前は「目標幅と±3mm以内か」で見ていたので、許容内の小さな幅不足でも
    幅補填と誤判定し、丈方向に置くべき板を幅方向に置いていた。
    """

    def tag(self, category, main_w, fill=(100, 2000), prod_w=1122):
        ctx = make_ctx(pal_w=1150, prod_w=prod_w)
        boards = [SelectedBoard(main_w, 2400, 1, ""),
                  SelectedBoard(fill[0], fill[1], 1, "")]
        return pl.get_effective_tag(boards, 1, category, ctx)

    def test_下用_許容内の幅不足は丈補填(self):
        """製品幅1122-主1115=7 ≦ 11。以前は |1115-1150|=35 > 3 で幅補填だった。"""
        self.assertEqual(self.tag(LOWER, 1115), "丈補填")

    def test_下用_許容ちょうどは丈補填(self):
        self.assertEqual(self.tag(LOWER, 1111), "丈補填")

    def test_下用_許容を超えたら幅補填(self):
        self.assertEqual(self.tag(LOWER, 1110), "幅補填")

    def test_上用_80mm以内の不足は丈補填(self):
        """製品幅1122-主1060=62 ≦ 80。以前は幅補填だった。"""
        self.assertEqual(self.tag(UPPER, 1060), "丈補填")

    def test_上用_80mmちょうどは丈補填(self):
        self.assertEqual(self.tag(UPPER, 1042), "丈補填")

    def test_上用_80mmを超えたら幅補填(self):
        self.assertEqual(self.tag(UPPER, 1041), "幅補填")


class YStackRescueTests(unittest.TestCase):
    """手動ボードのY積み救済は**判定後のタグが空で、1パス目**だけ。

    生のタグ(空)で見ていたので、幅補填と判定された板も2パス目で救済の
    判定に入り、1パス目の古い `eff_l` でY積みに書き換えられることがあった。
    """

    def test_補填と判定された板は救済にかけない(self):
        from unittest import mock
        ctx = make_ctx(pal_w=1150, prod_w=1122)
        boards = [SelectedBoard(1000, 2502, 1, ""),      # 主(タグ空)
                  SelectedBoard(50, 2400, 1, "")]        # 1122-1000=122>80 → 幅補填
        self.assertEqual(pl.get_effective_tag(boards, 1, UPPER, ctx), "幅補填")
        with mock.patch.object(pl, "_try_convert_to_y_stack",
                               return_value=False) as rescue:
            pl.place_boards_from_list(ctx, boards, UPPER)
        checked = [call.args[1].width for call in rescue.call_args_list]
        self.assertEqual(checked, [1000], "補填の板まで救済の判定に入っています")


class ManualOnlyPlacementTests(unittest.TestCase):
    """手動追加だけで配置しても止まらない(VBA `btnAutoPlace_Click` の修正)。

    VBA はカット辞書(`mCutInfo`)が未作成のまま丈補填の幅カットを記録
    しようとして落ちていた。Python版はカット辞書を配置のたびに作るので
    (`PlacementContext.cut_info`)、ここで起きないことを固定する。
    """

    def test_丈補填の幅カットを記録できる(self):
        ctx = pl.auto_place_boards(
            [SelectedBoard(1150, 2000, 1, ""), SelectedBoard(100, 2500, 1, "丈補填")],
            [], make_palette(1150, 2650), ProductSize(width=1122, length=2502))
        self.assertIsInstance(ctx.cut_info, dict)
        self.assertTrue(ctx.placed)


class SortFillBoardsTests(unittest.TestCase):
    def test_width_fills_move_ahead_of_length_fills(self):
        boards = [
            SelectedBoard(1080, 750, 3, "主"),
            SelectedBoard(500, 1200, 1, ""),   # 短辺500 > 70+5 → 丈補填側
            SelectedBoard(50, 1200, 1, ""),    # 短辺50 <= 70+5 → 幅補填側
        ]
        pl.sort_fill_boards(boards, 1, 70)
        self.assertEqual([b.width for b in boards], [1080, 50, 500])

    def test_tolerance_of_5mm(self):
        boards = [SelectedBoard(1080, 750, 3, "主"), SelectedBoard(75, 1200, 1, "")]
        pl.sort_fill_boards(boards, 1, 70)   # 75 <= 70+5 → 幅補填のまま先頭
        self.assertEqual(boards[1].width, 75)

    def test_stable_within_each_group(self):
        boards = [
            SelectedBoard(1080, 750, 3, "主"),
            SelectedBoard(30, 1200, 1, ""),
            SelectedBoard(500, 1200, 1, ""),
            SelectedBoard(50, 1200, 1, ""),
        ]
        pl.sort_fill_boards(boards, 1, 70)
        self.assertEqual([b.width for b in boards], [1080, 30, 50, 500])

    def test_noop_when_start_index_past_end(self):
        boards = [SelectedBoard(1080, 750, 3, "主")]
        pl.sort_fill_boards(boards, 1, 70)
        self.assertEqual(len(boards), 1)


class PlaceLengthFillBoardsTests(unittest.TestCase):
    def test_fills_from_max_x_with_palette_width(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1150, 2500, LOWER))
        boards = [SelectedBoard(1150, 2500, 1, "主"), SelectedBoard(100, 1200, 1, "丈補填")]
        pl.place_length_fill_boards(ctx, boards, LOWER)
        pb = ctx.placed[-1]
        self.assertEqual((pb.x, pb.y), (2500, 0))
        # 幅はパレット幅で固定、丈は短辺
        self.assertEqual((pb.width, pb.length), (1150, 100))
        self.assertTrue(pb.is_fill_board)

    def test_multiple_pieces_stack_along_x(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1150, 2000, LOWER))
        boards = [SelectedBoard(1150, 2000, 1, "主"), SelectedBoard(100, 1200, 2, "丈補填")]
        pl.place_length_fill_boards(ctx, boards, LOWER)
        self.assertEqual([p.x for p in ctx.placed[1:]], [2000, 2100])

    def test_start_x_clears_width_fill_rows_that_stick_out(self):
        # 起点は「同カテゴリ全ボードのX右端の最大値」なので、主ボードより
        # 長く伸びた幅補填行があればその右端から始まる。
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1000, 2000, LOWER))
        ctx.placed.append(placed(0, 1000, 50, 2400, LOWER, is_fill=True))
        boards = [SelectedBoard(1000, 2000, 1, "主"), SelectedBoard(100, 1200, 1, "丈補填")]
        pl.place_length_fill_boards(ctx, boards, LOWER)
        self.assertEqual(ctx.placed[-1].x, 2400)

    def test_uses_actual_long_side_when_smaller_than_limit(self):
        # 【修正】在庫の長辺(1080)がパレット幅(1150)より小さいとき、
        # 無条件でパレット幅まで引き伸ばしていた(=面積の水増し)。
        # 実サイズの長辺のまま配置し、カットは発生しない。
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1150, 2000, LOWER))
        boards = [SelectedBoard(1150, 2000, 1, "主"), SelectedBoard(295, 1080, 1, "丈補填")]
        pl.place_length_fill_boards(ctx, boards, LOWER)
        pb = ctx.placed[-1]
        self.assertEqual((pb.width, pb.length), (1080, 295))
        self.assertEqual(ctx.cut_info, {})

    def test_records_cut_info_when_actual_long_side_exceeds_limit(self):
        # 在庫の長辺(1200)がパレット幅(1150)を超えるときだけ、実際に
        # カットが発生する(ctx.cut_infoに記録)。
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1150, 2000, LOWER))
        boards = [SelectedBoard(1150, 2000, 1, "主"), SelectedBoard(100, 1200, 1, "丈補填")]
        pl.place_length_fill_boards(ctx, boards, LOWER)
        pb = ctx.placed[-1]
        self.assertEqual((pb.width, pb.length), (1150, 100))
        self.assertEqual(ctx.cut_info, {"100x1200": 100})


class CheckLengthFillOverlapTests(unittest.TestCase):
    """`_check_length_fill_overlap` 単体の検証。

    `place_length_fill_boards` は起点に「全ボードのX右端の最大値」を使うため、
    この事前チェックが False になる状況は実際には発生しない
    (VBA版も同じ構造で、防御的なチェックとして置かれている)。
    そのため関数単体で境界を確認する。
    """

    def test_detects_overlap_with_board_extending_past_start(self):
        ctx = make_ctx(pal_w=1150)
        ctx.placed.append(placed(0, 0, 1000, 2400, LOWER))
        self.assertFalse(_check(ctx, 2000, 100, 1150))

    def test_no_overlap_when_flush_to_right_edge(self):
        ctx = make_ctx(pal_w=1150)
        ctx.placed.append(placed(0, 0, 1000, 2400, LOWER))
        self.assertTrue(_check(ctx, 2400, 100, 1150))

    def test_other_category_ignored(self):
        ctx = make_ctx(pal_w=1150)
        ctx.placed.append(placed(0, 0, 1000, 2400, UPPER))
        self.assertTrue(_check(ctx, 2000, 100, 1150))

    def test_upper_uses_product_width(self):
        ctx = make_ctx(prod_w=1122, prod_l=2502)
        ctx.placed.append(placed(0, 0, 1122, 2000, UPPER))
        boards = [SelectedBoard(1122, 2000, 1, "主"), SelectedBoard(100, 1200, 1, "丈補填")]
        pl.place_length_fill_boards(ctx, boards, UPPER)
        self.assertEqual(ctx.placed[-1].width, 1122)


class PlaceBoardsFromListTests(unittest.TestCase):
    def test_main_boards_tile_along_x(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        boards = [SelectedBoard(750, 1130, 3, "主")]
        pl.place_boards_from_list(ctx, boards, LOWER)
        self.assertEqual(len(ctx.placed), 3)
        self.assertEqual([p.y for p in ctx.placed], [0, 0, 0])
        self.assertEqual([p.x for p in ctx.placed], [0, 750, 1500])
        # パレット幅1150に近い1130側がY方向に採用される
        self.assertEqual({p.width for p in ctx.placed}, {1130})

    def test_lower_boards_may_run_past_palette_length(self):
        # 下用の `can_place_board_at` はX方向に上限を持たない(VBA踏襲)。
        # PASS2の「既存ボード右端へのスナップ」もパレット丈で止まらないため、
        # 選定枚数が過剰だとパレットからはみ出して並び続ける。
        # 枚数を適正に決めるのは選定フェーズ側の責務。
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        boards = [SelectedBoard(750, 1130, 10, "主")]
        pl.place_boards_from_list(ctx, boards, LOWER)
        self.assertEqual(len(ctx.placed), 10)
        self.assertEqual([p.x for p in ctx.placed[:5]], [0, 750, 1500, 2250, 3000])
        self.assertGreater(ctx.placed[-1].x, ctx.palette.length)

    def test_width_fill_is_placed_in_pass2_below_main(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        boards = [SelectedBoard(1080, 2000, 1, "主"), SelectedBoard(50, 2000, 1, "幅補填")]
        pl.place_boards_from_list(ctx, boards, LOWER)
        fills = [p for p in ctx.placed if p.is_fill_board]
        self.assertEqual(len(fills), 1)
        self.assertEqual((fills[0].x, fills[0].y), (0, 1080))

    def test_length_fill_is_placed_after_pass1(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        boards = [SelectedBoard(1150, 2000, 1, "主"), SelectedBoard(100, 1150, 1, "丈補填")]
        pl.place_boards_from_list(ctx, boards, LOWER)
        self.assertEqual(len(ctx.placed), 2)
        self.assertEqual((ctx.placed[1].x, ctx.placed[1].y), (2000, 0))

    def test_cut_premise_tiles_short_side_along_x(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650, prod_w=1122, prod_l=2502)
        boards = [SelectedBoard(1000, 300, 3, "カット前提")]
        pl.place_boards_from_list(ctx, boards, LOWER)
        self.assertEqual([p.x for p in ctx.placed], [0, 300, 600])
        # 幅はパレット幅にカット、丈は短辺300
        self.assertEqual({(p.width, p.length) for p in ctx.placed}, {(1150, 300)})

    def test_cut_premise_stops_at_product_length(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650, prod_w=1122, prod_l=500)
        boards = [SelectedBoard(1000, 300, 5, "カット前提")]
        pl.place_boards_from_list(ctx, boards, LOWER)
        self.assertEqual([p.x for p in ctx.placed], [0, 300])

    def test_returns_reordered_list_without_mutating_caller(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        boards = [
            SelectedBoard(1080, 2000, 1, "主"),
            SelectedBoard(500, 2000, 1, "丈補填"),
            SelectedBoard(50, 2000, 1, "幅補填"),
        ]
        original = list(boards)
        result = pl.place_boards_from_list(ctx, boards, LOWER)
        # 呼び出し側のリストは並べ替えられない
        self.assertEqual(boards, original)
        # 戻り値では幅補填が丈補填より前に来る
        self.assertEqual([b.width for b in result], [1080, 50, 500])

    def test_empty_list_is_noop(self):
        ctx = make_ctx()
        self.assertEqual(pl.place_boards_from_list(ctx, [], LOWER), [])
        self.assertEqual(ctx.placed, [])

    def test_upper_second_board_stacks_in_y(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650, prod_w=1100, prod_l=2000)
        boards = [SelectedBoard(600, 2000, 1, "主"), SelectedBoard(450, 2000, 1, "主")]
        pl.place_boards_from_list(ctx, boards, UPPER)
        self.assertEqual(len(ctx.placed), 2)
        self.assertEqual(ctx.placed[0].y, 0)
        self.assertEqual(ctx.placed[1].y, 600)   # 1枚目の幅ぶん下へ


class VbaQuirkTests(unittest.TestCase):
    """VBA側の挙動をあえて踏襲している箇所の固定テスト。"""

    def test_upper_y_offset_reuses_previous_board_width(self):
        # 上用の i>0 は TryPlaceUpperWithYOffset を使うが、VBAはそこで lastB を
        # 更新しないため、3枚目のYオフセットは「2枚目の幅」ではなく
        # 「1枚目の幅」を足した値になる(元ツール再現のため踏襲)。
        ctx = make_ctx(pal_w=1150, pal_l=2650, prod_w=1100, prod_l=2000)
        boards = [
            SelectedBoard(300, 2000, 1, "主"),
            SelectedBoard(200, 2000, 1, "主"),
            SelectedBoard(200, 2000, 1, "主"),
        ]
        pl.place_boards_from_list(ctx, boards, UPPER)
        ys = [p.y for p in ctx.placed]
        # 正しく積むなら [0, 300, 500] だが、1枚目の幅300が再利用され 600 になる
        self.assertEqual(ys, [0, 300, 600])

    def test_pass3_selected_boards_have_no_tag_but_still_place(self):
        # 短辺100mm超・タグ無しは推測対象外 → 主ボード扱いで通常配置される
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        boards = [SelectedBoard(1080, 2000, 1, ""), SelectedBoard(200, 2000, 1, "")]
        pl.place_boards_from_list(ctx, boards, LOWER)
        self.assertEqual(len(ctx.placed), 2)
        self.assertFalse(any(p.is_fill_board for p in ctx.placed))


class PlaceNarrowPaletteBoardsTests(unittest.TestCase):
    def test_centers_and_stacks_along_y(self):
        ctx = make_ctx(pal_w=300, pal_l=2000, prod_w=280, prod_l=1900)
        boards = [SelectedBoard(100, 1900, 1, "主"), SelectedBoard(50, 1900, 1, "幅補填")]
        pl.place_narrow_palette_boards(ctx, boards, LOWER)
        # 合計短辺150 → (300-150)//2 = 75 から積み上げ
        self.assertEqual([p.y for p in ctx.placed], [75, 175])
        self.assertEqual([p.width for p in ctx.placed], [100, 50])

    def test_lower_is_cut_at_product_length_and_centered(self):
        """下用の帯は製品丈で切り、パレット丈の中央に寄せる(VBA の仕様更新)。

        以前は x=0 からパレット丈まで置いていたので、帯ごとに丈がばらついていた。
        """
        ctx = make_ctx(pal_w=300, pal_l=1000, prod_w=280, prod_l=950)
        boards = [SelectedBoard(100, 1900, 1, "主")]
        pl.place_narrow_palette_boards(ctx, boards, LOWER)
        self.assertEqual((ctx.placed[0].x, ctx.placed[0].length), (25, 950))

    def test_upper_clips_to_product_length(self):
        ctx = make_ctx(pal_w=300, pal_l=2000, prod_w=280, prod_l=900)
        boards = [SelectedBoard(100, 1900, 1, "主")]
        pl.place_narrow_palette_boards(ctx, boards, UPPER)
        self.assertEqual(ctx.placed[0].length, 900)

    def test_stops_at_product_length(self):
        ctx = make_ctx(pal_w=300, pal_l=2000, prod_w=280, prod_l=500)
        boards = [SelectedBoard(100, 400, 5, "主")]
        pl.place_narrow_palette_boards(ctx, boards, LOWER)
        # 中央寄せで x=750 から。400 を置き、最後の1枚は製品丈の位置(100)で切って打ち切り
        self.assertEqual([(p.x, p.length) for p in ctx.placed], [(750, 400), (1150, 100)])

    def test_length_fill_sits_at_the_end_of_the_previous_lane(self):
        """丈補填は新しい帯にせず、直前の帯の端に帯の幅に切って置く(厚みが丈方向)。"""
        ctx = make_ctx(pal_w=200, pal_l=2100, prod_w=50, prod_l=1615)
        boards = [SelectedBoard(50, 1600, 1, "主"), SelectedBoard(30, 2500, 1, "丈補填")]
        pl.place_narrow_palette_boards(ctx, boards, UPPER)
        lane, fill = ctx.placed
        self.assertEqual((lane.x, lane.length), (0, 1600))
        self.assertEqual((fill.x, fill.y, fill.width, fill.length),
                         (1600, lane.y, 50, 30))
        self.assertTrue(fill.is_fill_board)
        self.assertFalse(lane.is_fill_board)

    def test_length_fill_is_not_counted_as_a_lane_width(self):
        # 丈補填の短辺30 を帯の幅に数えると中央寄せがずれる
        ctx = make_ctx(pal_w=300, pal_l=2000, prod_w=280, prod_l=1900)
        boards = [SelectedBoard(100, 1800, 1, "主"), SelectedBoard(100, 2500, 1, "丈補填")]
        pl.place_narrow_palette_boards(ctx, boards, LOWER)
        self.assertEqual(ctx.placed[0].y, (300 - 100) // 2)
        # 下用: 帯は x=(2000-1900)//2=50 から 1800、丈補填はその端 x=1850
        self.assertEqual(ctx.placed[1].x, 50 + 1800)

    def test_no_negative_start_when_boards_exceed_width(self):
        ctx = make_ctx(pal_w=300, pal_l=2000, prod_w=280, prod_l=1900)
        boards = [SelectedBoard(200, 1900, 1, "主"), SelectedBoard(200, 1900, 1, "主")]
        pl.place_narrow_palette_boards(ctx, boards, LOWER)
        self.assertEqual(ctx.placed[0].y, 0)


class CountWidthFillStripsTests(unittest.TestCase):
    def test_zero_or_one_strip_yields_no_upper_allocation(self):
        ctx = make_ctx(pal_w=1150)
        boards = [SelectedBoard(1080, 750, 3, "主")]
        self.assertEqual(pl.count_width_fill_strips(boards, LOWER, ctx), (0, 0, 0))
        boards.append(SelectedBoard(50, 1200, 1, "幅補填"))
        self.assertEqual(pl.count_width_fill_strips(boards, LOWER, ctx), (1, 0, 0))

    def test_two_strips_split_one_and_one(self):
        ctx = make_ctx(pal_w=1150)
        boards = [
            SelectedBoard(1080, 750, 3, "主"),
            SelectedBoard(50, 1200, 1, "幅補填"),
            SelectedBoard(30, 1200, 1, "幅補填"),
        ]
        self.assertEqual(pl.count_width_fill_strips(boards, LOWER, ctx), (2, 1, 50))

    def test_three_strips_split_one_and_two(self):
        ctx = make_ctx(pal_w=1150)
        boards = [
            SelectedBoard(1080, 750, 3, "主"),
            SelectedBoard(50, 1200, 1, "幅補填"),
            SelectedBoard(30, 1200, 1, "幅補填"),
            SelectedBoard(20, 1200, 1, "幅補填"),
        ]
        self.assertEqual(pl.count_width_fill_strips(boards, LOWER, ctx), (3, 1, 50))

    def test_four_strips_split_two_and_two(self):
        ctx = make_ctx(pal_w=1150)
        boards = [
            SelectedBoard(1080, 750, 3, "主"),
            SelectedBoard(50, 1200, 1, "幅補填"),
            SelectedBoard(30, 1200, 1, "幅補填"),
            SelectedBoard(20, 1200, 1, "幅補填"),
            SelectedBoard(10, 1200, 1, "幅補填"),
        ]
        self.assertEqual(pl.count_width_fill_strips(boards, LOWER, ctx), (4, 2, 80))

    def test_ignores_non_width_fill_rows(self):
        ctx = make_ctx(pal_w=1150)
        boards = [
            SelectedBoard(1080, 750, 3, "主"),
            SelectedBoard(500, 1200, 1, "丈補填"),
            SelectedBoard(50, 1200, 1, "幅補填"),
            SelectedBoard(30, 1200, 1, "幅補填"),
        ]
        self.assertEqual(pl.count_width_fill_strips(boards, LOWER, ctx), (2, 1, 50))


class TryPlaceYCompanionUpperModeTests(unittest.TestCase):
    def test_first_fill_starts_above_main_top_edge(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        # 主ボードを y=80 から配置(幅補填分のオフセットを事前に確保済み想定)
        ctx.placed.append(placed(0, 80, 1000, 1200, LOWER))
        self.assertTrue(pl.try_place_y_companion(ctx, model(50, 1200, LOWER), True))
        pb = ctx.placed[-1]
        self.assertEqual((pb.x, pb.y), (0, 30))
        self.assertTrue(pb.is_fill_board)

    def test_second_fill_continues_same_upper_row(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 80, 1000, 2000, LOWER))
        b = model(50, 800, LOWER)
        pl.try_place_y_companion(ctx, b, True)
        pl.try_place_y_companion(ctx, b, True)
        first, second = ctx.placed[-2], ctx.placed[-1]
        self.assertEqual((first.x, first.y), (0, 30))
        self.assertEqual((second.x, second.y), (800, 30))

    def test_new_upper_row_when_current_row_is_full(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 150, 1000, 1000, LOWER))
        b = model(50, 1000, LOWER)
        pl.try_place_y_companion(ctx, b, True)   # 1段目(全幅)を埋め切る
        pl.try_place_y_companion(ctx, b, True)   # 2段目(さらに上)へ
        self.assertEqual((ctx.placed[-1].x, ctx.placed[-1].y), (0, 50))

    def test_length_fill_boards_are_not_mistaken_for_an_upper_row(self):
        # 丈補填はY≈0・X=主ボード右端以降に置かれるため、上側モードの
        # 「既存の上段」検出に誤って引っかからないことを確認する。
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 80, 1000, 1200, LOWER))
        ctx.placed.append(placed(1200, 0, 1150, 100, LOWER, is_fill=True))  # 丈補填
        self.assertTrue(pl.try_place_y_companion(ctx, model(50, 1200, LOWER), True))
        pb = ctx.placed[-1]
        self.assertEqual((pb.x, pb.y), (0, 30))

    def test_negative_offset_is_rejected(self):
        # 主ボードのYオフセットが不足(0のまま)だと上側placeYが負になり配置不可
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1000, 1200, LOWER))
        self.assertFalse(pl.try_place_y_companion(ctx, model(50, 1200, LOWER), True))
        self.assertEqual(len(ctx.placed), 1)


class YStartThreadingTests(unittest.TestCase):
    def test_default_y_start_is_unchanged(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        got = pl.try_place_single_orientation(ctx, model(1130, 750, LOWER), False, 1e15)
        self.assertEqual(got[:2], (0, 0))

    def test_y_start_shifts_search_floor(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        got = pl.try_place_single_orientation(ctx, model(1130, 750, LOWER), False, 1e15, y_start=100)
        self.assertIsNotNone(got)
        self.assertEqual(got[1], 100)

    def test_y_start_past_upper_bound_is_unplaceable(self):
        ctx = make_ctx(pal_w=1000)  # 下用のY上限は1200
        self.assertIsNone(
            pl.try_place_single_orientation(ctx, model(500, 400, LOWER), False, 1e15, y_start=1201))

    def test_try_place_inside_palette_honors_y_start(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        self.assertTrue(pl.try_place_inside_palette(ctx, model(750, 1130, LOWER), 100))
        self.assertEqual(ctx.placed[0].y, 100)

    def test_try_place_with_fixed_rotation_honors_y_start(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        self.assertTrue(pl.try_place_with_fixed_rotation(ctx, model(750, 1130, LOWER), False, 100))
        self.assertEqual(ctx.placed[0].y, 100)


class PlaceBoardsFromListUpperDownAllocationTests(unittest.TestCase):
    def test_two_width_fills_split_one_upper_one_lower(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        boards = [
            SelectedBoard(1000, 2000, 1, "主"),
            SelectedBoard(50, 2000, 1, "幅補填"),
            SelectedBoard(30, 2000, 1, "幅補填"),
        ]
        pl.place_boards_from_list(ctx, boards, LOWER)
        main = next(p for p in ctx.placed if not p.is_fill_board)
        fills = [p for p in ctx.placed if p.is_fill_board]
        self.assertEqual(len(fills), 2)
        # 主ボードは上側1本ぶん(短辺50)だけ下にオフセットされる
        self.assertEqual(main.y, 50)
        self.assertEqual({p.y for p in fills}, {0, main.y + main.width})

    def test_single_width_fill_stays_below_only(self):
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        boards = [SelectedBoard(1000, 2000, 1, "主"), SelectedBoard(50, 2000, 1, "幅補填")]
        pl.place_boards_from_list(ctx, boards, LOWER)
        main = next(p for p in ctx.placed if not p.is_fill_board)
        fills = [p for p in ctx.placed if p.is_fill_board]
        self.assertEqual(main.y, 0)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].y, main.width)


class AutoPlaceBoardsTests(unittest.TestCase):
    def test_places_both_categories_independently(self):
        lower = [SelectedBoard(750, 1130, 3, "主")]
        upper = [SelectedBoard(600, 2000, 1, "主")]
        ctx = pl.auto_place_boards(lower, upper, make_palette(1150, 2650),
                                   ProductSize(width=1122, length=2502))
        self.assertEqual(len([p for p in ctx.placed if p.board_category == LOWER]), 3)
        self.assertEqual(len([p for p in ctx.placed if p.board_category == UPPER]), 1)
        # 上用は下用と重なっていてよい(別レイヤ)
        self.assertEqual(ctx.placed[-1].x, 0)

    def test_narrow_flags_switch_strategy(self):
        lower = [SelectedBoard(100, 1900, 2, "主")]
        ctx = pl.auto_place_boards(lower, [], make_palette(300, 2000),
                                   ProductSize(width=280, length=1900),
                                   narrow_lower=True)
        # 狭幅版はセンタリングされたY座標に置く(通常探索ならY=0になる)
        self.assertEqual(ctx.placed[0].y, 100)

    def test_orders_are_exposed_on_context(self):
        lower = [SelectedBoard(1080, 2000, 1, "主"), SelectedBoard(50, 2000, 1, "幅補填")]
        ctx = pl.auto_place_boards(lower, [], make_palette(1150, 2650),
                                   ProductSize(width=1122, length=2502))
        self.assertEqual(len(ctx.lower_order), 2)
        self.assertEqual(ctx.upper_order, [])

    def test_empty_selection_produces_nothing(self):
        ctx = pl.auto_place_boards([], [], make_palette(1150, 2650),
                                   ProductSize(width=1122, length=2502))
        self.assertEqual(ctx.placed, [])


class CenterBoardsInWidthTests(unittest.TestCase):
    """幅が足りないボードは**中央**へ寄せる。

    現場の声:「幅不足のボードも上詰めで配置される。これでは
    マイナス幅を等分にすることが直感的に分からない」。図がそのまま
    作業の指示になるので、上詰めのままだと「下にずらして置く板」に
    読めてしまう。
    """

    def ys(self, ctx, category=LOWER) -> list[tuple[int, int, int]]:
        """(丈の位置, 幅の位置, 幅) の一覧。"""
        return [(p.x, p.y, p.width) for p in ctx.placed
                if p.board_category == category]

    def test_幅不足のボードだけが中央へ寄る(self):
        """現場が出した図と同じ構成(660×1050 が2枚 + 600×600 が1枚)。

        パレット幅660に対し 600 の板は60mm足りない。上下に30mmずつ
        分ける。
        """
        lower = [SelectedBoard(660, 1050, 2, "主"), SelectedBoard(600, 600, 1, "主")]
        ctx = pl.auto_place_boards(lower, [], make_palette(660, 2700),
                                   ProductSize(width=660, length=2700))
        self.assertEqual(self.ys(ctx),
                         [(0, 0, 660), (1050, 0, 660), (2100, 30, 600)])

    def test_ちょうどの幅は動かさない(self):
        lower = [SelectedBoard(660, 1050, 2, "主")]
        ctx = pl.auto_place_boards(lower, [], make_palette(660, 2700),
                                   ProductSize(width=660, length=2700))
        self.assertEqual({y for _x, y, _w in self.ys(ctx)}, {0})

    def test_上用は製品幅が基準(self):
        """下用はパレット幅、上用は製品幅。寄せる先を取り違えない。"""
        upper = [SelectedBoard(1000, 2400, 1, "主")]
        ctx = pl.auto_place_boards([], upper, make_palette(1150, 2650),
                                   ProductSize(width=1100, length=2500))
        # 製品幅1100 - 板1000 = 100 → 上下50ずつ
        self.assertEqual(self.ys(ctx, UPPER), [(0, 50, 1000)])

    def test_丈方向に重なる2枚はまとめて寄せる(self):
        """1枚ずつ寄せると、Y方向に積んだ2枚が重なってしまう。"""
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.extend([placed(0, 0, 500, 2000), placed(0, 500, 500, 2000)])
        pl.center_boards_in_width(ctx, LOWER)
        # 合計1000、パレット幅1150 → 上下75ずつ。**間隔は保つ**
        self.assertEqual(self.ys(ctx), [(0, 75, 500), (0, 575, 500)])

    def test_はみ出しているときは寄せない(self):
        ctx = make_ctx(pal_w=1000, pal_l=2650)
        ctx.placed.append(placed(0, 0, 1100, 2000))
        pl.center_boards_in_width(ctx, LOWER)
        self.assertEqual(self.ys(ctx), [(0, 0, 1100)])

    def test_幅補填があるときは触らない(self):
        """上下振り分け(1本なら下、2本なら上下1本ずつ…)は**既に

        どこに置くかを決めている**配置。そのうえで中央へ寄せ直すと、
        振り分けた意味が消える(現場の指示)。
        """
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        before = [placed(0, 0, 50, 2000, is_fill=True),
                  placed(0, 50, 1000, 2000),
                  placed(0, 1050, 50, 2000, is_fill=True)]
        ctx.placed.extend(before)
        pl.center_boards_in_width(ctx, LOWER)
        self.assertEqual(self.ys(ctx),
                         [(0, 0, 50), (0, 50, 1000), (0, 1050, 50)])

    def test_離れた補填は他のボードを巻き込まない(self):
        """**補填が1枚あるだけで、面ぜんぶが上詰めになっていた。**

        守りたいのは幅補填の上下振り分けだけ。幅補填は主ボードの下端に
        敷くので同じまとまりに入り、これまでどおり触らない。ところが
        丈補填のように**離れたところに1枚ある**だけで、関係のない主
        ボードまで寄らなくなっていた(現場の指摘:「ボードを幅方向の
        センター配置をしていない」)。
        """
        ctx = make_ctx(pal_w=812, pal_l=2650)
        ctx.placed.extend([
            placed(0, 0, 750, 1130),                    # 主。62mm 余る
            placed(1130, 0, 750, 1130),                 # 主。62mm 余る
            placed(2260, 0, 812, 50, is_fill=True),     # 離れた丈補填
        ])
        pl.center_boards_in_width(ctx, LOWER)
        self.assertEqual(self.ys(ctx),
                         [(0, 31, 750), (1130, 31, 750), (2260, 0, 812)])

    def test_補填だけのまとまりは主ボードと揃える(self):
        """丈補填の帯は、主ボードの丈を継ぎ足すもの。

        **幅方向の位置が主ボードと揃っていないとおかしい。** 補填が
        居るまとまりを一律に避けていたときは、主ボードだけが中央へ寄って
        帯が枠の上端に残り、段違いになっていた(実画面で通して見つけた)。

        避けるべきなのは「振り分けが起きているまとまり」── 補填と主
        ボードが同じまとまりに居るときだけ。
        """
        ctx = make_ctx(pal_w=812, pal_l=2650)
        ctx.placed.extend([
            placed(0, 0, 750, 1130),                     # 主
            placed(1130, 0, 750, 50, is_fill=True),      # 丈補填の帯
        ])
        pl.center_boards_in_width(ctx, LOWER)
        self.assertEqual(self.ys(ctx),
                         [(0, 31, 750), (1130, 31, 750)])

    def test_補填と同じまとまりのボードは動かさない(self):
        """幅補填は主ボードの下端に敷く。**そのまとまりは触らない。**"""
        ctx = make_ctx(pal_w=1150, pal_l=2650)
        ctx.placed.extend([
            placed(0, 0, 1000, 2000),                   # 主
            placed(0, 1000, 50, 2000, is_fill=True),    # その下の幅補填
        ])
        pl.center_boards_in_width(ctx, LOWER)
        self.assertEqual(self.ys(ctx), [(0, 0, 1000), (0, 1000, 50)])

    def test_上下振り分けが実際に残ることを通しで確かめる(self):
        """`auto_place_boards` を通しても、振り分けたYは動かない。"""
        # 幅補填は**本数**で振り分けるので、2本は2行として渡す
        # (1行 count=2 は丈方向の継ぎ足しで、1本ぶん)
        lower = [SelectedBoard(1000, 2400, 1, "主"),
                 SelectedBoard(50, 2400, 1, "幅補填"),
                 SelectedBoard(50, 2400, 1, "幅補填")]
        ctx = pl.auto_place_boards(lower, [], make_palette(1150, 2650),
                                   ProductSize(width=1122, length=2502))
        fills = [p.y for p in ctx.placed if p.is_fill_board]
        main = [p.y for p in ctx.placed if not p.is_fill_board]
        self.assertEqual(len(fills), 2)
        # 上に1本・下に1本。主ボードはそのあいだ
        self.assertLess(min(fills), min(main))
        self.assertGreater(max(fills), min(main))
        # **中央へ寄せ直していない。** 寄せていれば主ボードは
        # (1150-1100)//2 + 50 のような別の位置になる
        self.assertEqual(sorted(fills), [0, 1050])
        self.assertEqual(main, [50])

    def test_狭幅パレットは二重に寄せない(self):
        """`place_narrow_palette_boards` が積み上げる時点で中央から

        始めている。あとから寄せ直すと二重に寄る。
        """
        lower = [SelectedBoard(80, 1800, 1, "主")]
        ctx = pl.auto_place_boards(lower, [], make_palette(280, 1900),
                                   ProductSize(width=280, length=1900),
                                   narrow_lower=True)
        self.assertEqual(ctx.placed[0].y, 100)

    def test_重ならない(self):
        """寄せた結果として板が重なってはいけない。"""
        lower = [SelectedBoard(660, 1050, 2, "主"), SelectedBoard(600, 600, 1, "主")]
        ctx = pl.auto_place_boards(lower, [], make_palette(660, 2700),
                                   ProductSize(width=660, length=2700))
        for i, a in enumerate(ctx.placed):
            for b in ctx.placed[i + 1:]:
                with self.subTest(a=a.instance_id, b=b.instance_id):
                    self.assertFalse(
                        a.x < b.x + b.length and a.x + a.length > b.x
                        and a.y < b.y + b.width and a.y + a.width > b.y)


if __name__ == "__main__":
    unittest.main()


class 置けなかった理由(unittest.TestCase):
    """**断るなら理由を言う。**

    置けないボードを黙って飛ばしていた。手で「上用へ追加」したのに図に
    出てこず、選定ログにも何も残らないので、押した人には「このボタンは
    効かない」としか見えない(現場の指摘:「手動でボードを選択して上用へ
    追加 下用へ追加 とあるのに 追加しても配置すらしない この仕様であれば
    このボタンはいらないのでは」)。

    寸法のどこが足りないかで、次にすることが変わる。
    """

    def run_case(self, lower, upper, pal=(550, 985), prod=(482, 967)):
        ctx = pl.auto_place_boards(
            lower, upper, make_palette(*pal),
            ProductSize(width=prod[0], length=prod[1]))
        return ctx, [u.reason for u in ctx.unplaced]

    def test_どちらの向きでも幅が入らない(self) -> None:
        """現場のログにあった 1250x1250(パレット550 / 製品482)。"""
        _ctx, why = self.run_case([SelectedBoard(1250, 1250, 1, "")], [])
        self.assertEqual(len(why), 1)
        self.assertIn("幅がパレット幅", why[0])
        self.assertIn("1250mm", why[0])

    def test_現場が知っている数で言う(self) -> None:
        """**内部の数だけ出さない。**

        下用の幅はパレット幅に はみ出し許容(2割)を乗せた値で判定する。
        その660だけを出すと「うちのパレットは550なのに660とは何のことだ」
        になる。元の数と、許容を入れた数の両方を出す。
        """
        _ctx, why = self.run_case([SelectedBoard(1250, 1250, 1, "")], [])
        self.assertIn("550mm", why[0])
        self.assertIn("660mm", why[0])

    def test_許容が乗らないものは1つだけ言う(self) -> None:
        """丈は許容が乗らない。**同じ数を2度書かない。**"""
        _ctx, why = self.run_case([SelectedBoard(450, 1520, 1, "")], [])
        self.assertIn("パレット丈985mm", why[0])
        self.assertNotIn("はみ出し許容", why[0])

    def test_幅は入るが丈が長い(self) -> None:
        """同じログの 450x1520。幅は足りるので、丈だと言い分ける。"""
        _ctx, why = self.run_case([SelectedBoard(450, 1520, 1, "")], [])
        self.assertEqual(len(why), 1)
        self.assertIn("丈がパレット丈", why[0])
        self.assertIn("1520mm", why[0])

    def test_上用は製品の寸法で言う(self) -> None:
        """境界が違うので、言う相手も違う(上用=製品 / 下用=パレット)。"""
        _ctx, why = self.run_case([], [SelectedBoard(1250, 1250, 1, "")])
        self.assertIn("製品幅", why[0])

    def test_置けたものは理由に出さない(self) -> None:
        """**置けたのに文句だけ出る**、が一番たちが悪い。"""
        ctx, why = self.run_case([SelectedBoard(540, 900, 1, "")], [])
        self.assertTrue(ctx.placed)
        self.assertEqual(why, [])

    def test_場所が埋まっているときはそう言う(self) -> None:
        """寸法は入るのに置けない、は理由が別。"""
        ctx = make_ctx(pal_w=1000, pal_l=1000)
        ctx.placed.append(placed(0, 0, 1000, 1000))
        self.assertIn("置ける場所が残っていません",
                      pl.explain_unplaced(ctx, 400, 400, LOWER))


class 選定が選んだ板は切って置く(unittest.TestCase):
    """別案(候補A・B)がパレット丈より長い下用ボードを選ぶことがある(実データ:
    905×1950 のパレットに 945×2000)。以前は「置けませんでした」と落としていた。

    現場の指示:「ロジック通りなら良いのでは？カット含めて表示してください」。
    """

    def run_case(self, lower, upper=(), pal=(905, 1950), prod=(867, 1834)):
        return pl.auto_place_boards(
            list(lower), list(upper), make_palette(*pal),
            ProductSize(width=prod[0], length=prod[1]))

    def test_はみ出す丈だけ切って置き元の寸法を残す(self) -> None:
        ctx = self.run_case([SelectedBoard(945, 2000, 1, "主")])
        self.assertEqual(ctx.unplaced, [])
        [board] = [p for p in ctx.placed if p.board_category == "下用"]
        self.assertEqual(board.x + board.length, 1950)          # パレット丈まで
        self.assertEqual({board.original_width, board.original_length}, {945, 2000})
        [cut] = ctx.trimmed
        self.assertIn("丈カット50mm", cut.label())
        self.assertNotIn("幅カット", cut.label())                 # 幅ははみ出し許容の内
        # 図はカットとして描く(元の寸法と置いた寸法が違う)
        from packaging_tool.placement_render import detect_board_cut
        self.assertTrue(detect_board_cut(board.original_width, board.original_length,
                                         board.width, board.length).cut_length)

    def test_手で足した板は切らずに理由を言う(self) -> None:
        """どこを切るかを勝手に決めない(手で足した板は今までどおり断る)。"""
        ctx = self.run_case([SelectedBoard(945, 2000, 1, "")])
        self.assertEqual(ctx.trimmed, [])
        self.assertEqual(len(ctx.unplaced), 1)

    def test_ほかの板と重ねない(self) -> None:
        ctx = self.run_case([SelectedBoard(905, 1000, 1, "主"), SelectedBoard(945, 2000, 1, "主")])
        lower = [p for p in ctx.placed if p.board_category == "下用"]
        for a in lower:
            for b in lower:
                if a is b:
                    continue
                self.assertFalse(a.x < b.x + b.length and b.x < a.x + a.length
                                 and a.y < b.y + b.width and b.y < a.y + a.width, (a, b))
