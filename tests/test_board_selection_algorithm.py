"""ボード自動選定アルゴリズム(board_selection_algorithm)のユニットテスト。

VBAの各PASSの優先順位・許容値・補填ロジックの境界を検証する。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import board_selection_algorithm as alg
from packaging_tool.board_scoring import FatigueEntry
from packaging_tool.board_selection_service import Palette, ProductSize, SelectedBoard
from packaging_tool.models import BoardModel


def board(w: int, l: int, *, stock_low: bool = False) -> BoardModel:
    return BoardModel(width=w, length=l, board_category="下用", stock_low=stock_low)


def make_palette(width: int, length: int) -> Palette:
    return Palette(width=width, length=length, overhang_ratio=1.10,
                   max_width=width * 1.10, max_length=length * 1.10)


class AdjustOrientationTests(unittest.TestCase):
    def test_no_change_when_no_width_cut(self):
        # eff_w <= target_w なら何もしない
        rot, w, l = alg.adjust_orientation_for_coverage(
            500, 1000, target_w=600, target_l=900, rot=False, eff_w=500, eff_l=1000,
        )
        self.assertEqual((rot, w, l), (False, 500, 1000))

    def test_swaps_to_avoid_width_cut(self):
        # 現在向き 1000幅(target 600超過) → 逆向き 500幅で丈1000は製品丈900をカバー
        rot, w, l = alg.adjust_orientation_for_coverage(
            1000, 500, target_w=600, target_l=400, rot=False, eff_w=1000, eff_l=500,
        )
        self.assertTrue(rot)
        self.assertEqual((w, l), (500, 1000))

    def test_no_swap_when_alt_cannot_cover_length(self):
        # 逆向き(幅500/丈1000)にしても製品丈1100に届かない → 変更しない
        rot, w, l = alg.adjust_orientation_for_coverage(
            1000, 500, target_w=600, target_l=1100, rot=False, eff_w=1000, eff_l=500,
        )
        self.assertEqual((rot, w, l), (False, 1000, 500))

    def test_upper_category_has_no_overhang_allowance(self):
        # 上用は fit_limit = target_w (はみ出し許容なし)。alt_w=650 > 600 なので変更しない
        rot, w, l = alg.adjust_orientation_for_coverage(
            1000, 650, target_w=600, target_l=400, rot=False, eff_w=1000, eff_l=650, category="上用",
        )
        self.assertEqual((rot, w, l), (False, 1000, 650))


class ForceShortSideIfOvershootTests(unittest.TestCase):
    """VBA修正: 短辺強制の閾値は `pallet_w + 3mm` ではなく
    `pallet_w * LOWER_OVERHANG_Y`(20%許容)であるべき、というバグ修正の検証。

    旧実装は `pallet_w + WIDTH_OVERSHOOT_LIMIT`(3mm)を閾値にしていたため、
    GetBestOrientation/adjust_orientation_for_coverage が正しく選んだ
    「はみ出し許容(20%)内の長辺側」まで問答無用で短辺側へ書き換えて
    しまい、本来カット不要な組み合わせを誤却下していた。
    """

    def test_within_20_percent_allowance_keeps_the_chosen_orientation(self):
        # pallet_w=1000, fit_limit=1200。eff_w=1150は旧閾値(1003)は超えるが
        # 新閾値(1200)以内 → 短辺へ強制されず、選ばれた向きのまま
        rot, w, l = alg._force_short_side_if_overshoot(
            board_w=1150, board_l=950, pallet_w=1000, rot=False, eff_w=1150, eff_l=950)
        self.assertEqual((rot, w, l), (False, 1150, 950))

    def test_beyond_20_percent_allowance_still_forces_the_short_side(self):
        # eff_w=1250は新閾値(1200)も超える → 短辺(900)へ強制
        rot, w, l = alg._force_short_side_if_overshoot(
            board_w=1250, board_l=900, pallet_w=1000, rot=False, eff_w=1250, eff_l=900)
        self.assertEqual((rot, w, l), (True, 900, 1250))

    def test_within_the_old_3mm_threshold_is_unaffected(self):
        rot, w, l = alg._force_short_side_if_overshoot(
            board_w=1002, board_l=900, pallet_w=1000, rot=False, eff_w=1002, eff_l=900)
        self.assertEqual((rot, w, l), (False, 1002, 900))

    def test_no_flip_possible_when_the_short_side_also_exceeds_pallet_width(self):
        # 短辺もパレット幅を超えるなら、どちらの閾値でも変更しない
        rot, w, l = alg._force_short_side_if_overshoot(
            board_w=1250, board_l=1100, pallet_w=1000, rot=False, eff_w=1250, eff_l=1100)
        self.assertEqual((rot, w, l), (False, 1250, 1100))

    def test_orient_lower_end_to_end_keeps_the_long_side_within_allowance(self):
        """`_orient_lower`(PASS1〜3共通の向き決定)を通しても同じ結果になること。

        board=1180x700, pallet幅=1000: get_best_orientation は
        (1000との差)1180の方が700より小さい(180<300)ため1180幅を選び、
        adjust_orientation_for_coverage も「逆向き(700幅/1180丈)では
        製品丈1300をカバーできない」ため据え置く。旧実装はここで
        pallet幅+3mmという生の閾値に引っかけて700幅へ強制的に
        書き換えてしまっていたが、1180は20%許容(1200)以内なので
        書き換えないのが正しい。
        """
        palette = make_palette(1000, 3000)
        product = ProductSize(width=900, length=1300)
        rot, eff_w, eff_l = alg._orient_lower(1180, 700, palette, product)
        self.assertEqual((rot, eff_w, eff_l), (False, 1180, 700))


class CanCoverWithFillTests(unittest.TestCase):
    def test_small_gap_always_true(self):
        # gap <= PASS1_TOLERANCE(11mm) は在庫を見ずにTrue
        self.assertTrue(alg.can_cover_with_fill(11, 2, []))
        self.assertTrue(alg.can_cover_with_fill(0, 2, []))

    def test_exact_cover_with_available_size(self):
        available = [board(100, 1000)]  # 短辺100mm
        self.assertTrue(alg.can_cover_with_fill(100, 2, available))

    def test_false_when_size_not_in_stock(self):
        available = [board(200, 1000)]  # 30/50/100 のいずれでもない
        self.assertFalse(alg.can_cover_with_fill(100, 2, available))

    def test_combination_of_sizes(self):
        available = [board(50, 1000), board(30, 1000)]
        # 50+30=80 が gap=80 に一致
        self.assertTrue(alg.can_cover_with_fill(80, 2, available))

    def test_respects_max_slots(self):
        available = [board(30, 1000)]
        # gap=90 は 30x3 で埋まるが max_slots=2 なら不可
        self.assertFalse(alg.can_cover_with_fill(90, 2, available))
        self.assertTrue(alg.can_cover_with_fill(90, 3, available))

    def test_size_larger_than_gap_is_never_used(self):
        # 1サイズあたりの上限が floor(gap / size) のため、
        # gap(80mm) より大きい 100mm は候補にすらならない
        available = [board(100, 1000)]
        self.assertFalse(alg.can_cover_with_fill(80, 2, available))

    def test_over_margin_allows_overshoot_up_to_30mm(self):
        available = [board(100, 1000), board(50, 1000)]
        # gap=120 → 100+50=150 (超過30mm = FILL_OVER_MARGIN上限) なので許容
        self.assertTrue(alg.can_cover_with_fill(120, 2, available))
        # gap=115 → 150 は超過35mm で許容外
        self.assertFalse(alg.can_cover_with_fill(115, 2, available))


class SinglePieceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.palette = make_palette(1100, 2600)
        self.product = ProductSize(width=1000, length=2400)

    def test_adopts_board_covering_whole_product(self):
        # 1枚で製品(1000x2400)をカバーでき、パレット丈2600に収まる
        available = [board(1050, 2500)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertEqual(len(result.boards), 1)
        self.assertEqual((result.boards[0].width, result.boards[0].length), (1050, 2500))
        self.assertEqual(result.boards[0].count, 1)
        self.assertEqual(result.boards[0].tag, alg.TAG_MAIN)

    def test_rejects_board_exceeding_pallet_length(self):
        # 丈2700 > パレット丈2600 なので1枚物としては採用されない
        available = [board(1050, 2700)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        if result.boards:
            self.assertNotEqual((result.boards[0].width, result.boards[0].length), (1050, 2700))

    def test_rejects_too_narrow_board(self):
        # 幅900 < product.width - PASS1_TOLERANCE(=989) なので1枚物にはならない。
        # PASS2で拾われるが補填在庫が無いためカット前提選定へ回る
        available = [board(900, 2500)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertTrue(result.needs_wide_cut)
        # カット前提選定に回り、最大辺がパレット幅以上の900x2500が採用される
        self.assertEqual(len(result.boards), 1)
        self.assertEqual(result.boards[0].tag, alg.TAG_CUT_PREMISE)

    def test_ignores_boards_at_or_below_100mm(self):
        available = [board(100, 2500), board(1050, 2500)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertEqual((result.boards[0].width, result.boards[0].length), (1050, 2500))

    def test_pass1_is_not_invoked_after_single_piece_selection(self):
        """VBA修正: 1枚物優先で決定済み(pass1_done=True)なら、PASS2/PASS3と
        同様にPASS1/1.5を続けて実行しない。

        `_current_covered_length` による「製品丈カバー済み」ガードは
        今回の修正対象とは別物なので、そちらを無効化した状態で
        「PASS1が呼ばれるかどうか」自体を切り分けて検証する。
        """
        palette = make_palette(1000, 3000)
        product = ProductSize(width=900, length=2000)
        single_piece_board = board(1000, 2500)
        # 1枚物優先では拾われないが、ガードが無ければPASS1で拾われてしまう候補
        extra_board = board(1000, 400)

        with mock.patch.object(alg, "_current_covered_length", return_value=0):
            result = alg.select_lower_boards(
                [single_piece_board, extra_board], palette, product)

        self.assertEqual(len(result.boards), 1)
        self.assertEqual(
            (result.boards[0].width, result.boards[0].length), (1000, 2500))


class Pass1NormalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.palette = make_palette(1000, 2000)
        self.product = ProductSize(width=900, length=1900)

    def test_pass1_exact_width_match(self):
        # 幅1000はパレット幅と一致 → PASS1で採用。1枚物条件(丈>=1900)は満たさない丈にする
        available = [board(1000, 600)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertTrue(result.state.pass1_done)
        self.assertEqual(result.boards[0].tag, alg.TAG_MAIN)

    def test_pass1_tolerance_boundary(self):
        # |eff_w - pallet_w| <= PASS1_TOLERANCE(11) → 989 は採用範囲
        available = [board(989, 600)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertTrue(result.state.pass1_done)

    def test_pass1_rejects_beyond_tolerance(self):
        # 差が12mm(>11) かつ 幅超過側でもない → PASS1却下 → PASS2以降で拾われる
        available = [board(988, 600)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        # PASS1では通らない(PASS2で採用されるためboards自体は空でない)
        self.assertTrue(result.boards)

    def test_rejects_board_under_half_pallet_width(self):
        # 400x450 はどちらの向きでも有効幅が pallet_w*0.5(=500) 未満(採用向きは450)
        # → 全PASSで除外され、狭幅パレット経路に回る
        available = [board(400, 450)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertTrue(result.needs_narrow)
        self.assertEqual(len(result.boards), 1)
        self.assertEqual(result.boards[0].tag, alg.TAG_MAIN)

    def test_board_passing_half_width_check_goes_to_normal_pass(self):
        # 400x600 は回転向き(600)が採用され 500 以上なのでPASS2で拾われる
        # (その後カバー不足でカット前提のフォールバックに置き換わる)
        available = [board(400, 600)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertFalse(result.needs_narrow)
        self.assertTrue(result.needs_wide_cut)

    def test_count_is_floor_then_topped_up_for_length_shortage(self):
        # PASS1: パレット丈2000 / 丈600 = 3枚 (合計1800mm)
        # → 製品丈1900に100mm不足 → 丈不足強制追加で ceil(100/600)=1枚 追加され4枚
        available = [board(1000, 600)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertEqual(result.boards[0].count, 4)


class Pass2Pass3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.palette = make_palette(1000, 2000)
        self.product = ProductSize(width=900, length=1900)

    def test_pass2_used_when_pass1_fails(self):
        # 幅800: PASS1の許容外(差200mm)だが パレット幅+3 以内なのでPASS2で採用。
        # 100mm補填在庫(100x300)も置いて、幅補填で製品幅に届くようにする
        available = [board(800, 600), board(100, 300)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertTrue(result.boards)
        self.assertEqual(result.boards[0].width, 800)
        self.assertEqual(result.boards[0].tag, alg.TAG_MAIN)
        self.assertFalse(result.needs_wide_cut)
        self.assertEqual(len([b for b in result.boards if b.tag == alg.TAG_WIDTH_FILL]), 1)

    def test_pass2_selection_replaced_by_wide_cut_when_no_fill_stock(self):
        # 補填在庫が無いと幅がカバーできず、カット前提選定に置き換わる。
        # 800x600 は最大辺800 < パレット幅1000 なのでフォールバック採用になる
        result = alg.select_lower_boards([board(800, 600)], self.palette, self.product)
        self.assertTrue(result.needs_wide_cut)
        self.assertEqual(len(result.boards), 1)
        self.assertEqual(result.boards[0].tag, alg.TAG_MAIN)  # フォールバックのタグ

    def test_pass3_allows_overhang_and_leaves_tag_empty(self):
        # 幅1100: パレット幅+3を超えるためPASS2却下。
        # 短辺強制も効かない(短辺=1100>パレット幅1000)ので
        # LOWER_OVERHANG_Y(1200)以内のPASS3で拾われ、タグは空のまま
        available = [board(1100, 1100)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertTrue(result.boards)
        self.assertEqual(result.boards[0].tag, "")
        self.assertIn("1100x1100", result.state.cut_info)

    def test_pass3_rejects_beyond_overhang_limit(self):
        # 幅1300 > 1000*1.2=1200 → PASS3でも却下 → 何も選定されない
        available = [board(1300, 1300)]
        result = alg.select_lower_boards(available, self.palette, self.product)
        self.assertEqual(len(result.boards), 0)


class MeasureSelectionTests(unittest.TestCase):
    """VBA修正: 補填フェーズ入力(selMaxW/selMinW/selTotalL)の向き判定を、
    選定本体(PASS2/PASS3)と同じ GetBestOrientation → AdjustOrientationForCoverage
    の2段階に統一。
    """

    def setUp(self) -> None:
        self.product = ProductSize(width=900, length=2000)

    def test_uses_get_best_orientation_not_the_closest_distance_heuristic(self):
        # 200x1400: 「パレット幅(1000)に近い辺を幅方向にする」という旧
        # ヒューリスティックは、1000との差だけを見て1400を幅方向に選んで
        # しまう(1000mmパレットに対し1400mm幅という現実にはあり得ない
        # 解釈)。GetBestOrientationは20%許容(1200mm)を超える1400側を
        # 除外するので、200を幅方向として正しく選ぶ。
        palette = make_palette(1000, 3000)
        boards = [SelectedBoard(width=200, length=1400, count=1, tag=alg.TAG_MAIN)]
        sel_max_w, sel_min_w, sel_total_l = alg._measure_selection(boards, palette, self.product)
        self.assertEqual(sel_max_w, 200)
        self.assertEqual(sel_min_w, 200)
        self.assertEqual(sel_total_l, 1400)

    def test_matches_the_orientation_actually_chosen_during_selection(self):
        """選定時(_orient_lower)と補填計算(_measure_selection)の向きが
        一致すること(食い違いの回帰防止)。"""
        palette = make_palette(1000, 3000)
        selected_rot, selected_eff_w, selected_eff_l = alg._orient_lower(
            200, 1400, palette, self.product)
        boards = [SelectedBoard(width=200, length=1400, count=1, tag=alg.TAG_MAIN)]
        sel_max_w, _, sel_total_l = alg._measure_selection(boards, palette, self.product)
        self.assertEqual(sel_max_w, selected_eff_w)
        self.assertEqual(sel_total_l, selected_eff_l)

    def test_applies_adjust_orientation_for_coverage_like_the_selection_body_does(self):
        """VBA修正: PASS2/PASS3の選定本体はGetBestOrientationの直後に
        AdjustOrientationForCoverageも呼んで幅カット回避の向き補正を
        かけているが、この算出はGetBestOrientationだけで済ませていた。

        board=1180x700, パレット幅=1000, 製品丈=1000:
        GetBestOrientationだけだと(1000との差が近い)1180を幅方向に
        選ぶが、それだと幅カットが出る。AdjustOrientationForCoverageは
        逆向き(700幅/1180丈)が20%許容内かつ製品丈1000をカバーできる
        ことを確認して逆向きへ補正する。選定本体はこの補正込みで
        board.width/lengthを記録しているので、補填計算も同じ補正を
        経ないと向きが食い違う。
        """
        palette = make_palette(1000, 3000)
        product = ProductSize(width=900, length=1000)
        boards = [SelectedBoard(width=1180, length=700, count=1, tag=alg.TAG_MAIN)]
        sel_max_w, sel_min_w, sel_total_l = alg._measure_selection(boards, palette, product)
        self.assertEqual(sel_max_w, 700)
        self.assertEqual(sel_min_w, 700)
        self.assertEqual(sel_total_l, 1180)

        # 選定本体(_orient_lower)と一致すること
        _, selected_eff_w, selected_eff_l = alg._orient_lower(1180, 700, palette, product)
        self.assertEqual(sel_max_w, selected_eff_w)
        self.assertEqual(sel_total_l, selected_eff_l)


class WidthFillTests(unittest.TestCase):
    def test_fills_gap_with_largest_size_first(self):
        boards = [SelectedBoard(width=800, length=1000, count=1, tag=alg.TAG_MAIN)]
        available = [board(100, 1000), board(50, 1000), board(30, 1000)]
        # 主800 → 製品900 まで gap=100 → 100mmを1ストリップ
        result = alg.run_width_fill_phase(
            boards, main_min_w=800, product_w=900, pallet_w=1000, target_len=1000, available=available,
        )
        fills = [b for b in boards if b.tag == alg.TAG_WIDTH_FILL]
        self.assertEqual(len(fills), 1)
        self.assertEqual(min(fills[0].width, fills[0].length), 100)
        self.assertEqual(result, 900)

    def test_skips_when_gap_within_tolerance(self):
        boards = [SelectedBoard(width=895, length=1000, count=1, tag=alg.TAG_MAIN)]
        available = [board(100, 1000)]
        result = alg.run_width_fill_phase(
            boards, main_min_w=895, product_w=900, pallet_w=1000, target_len=1000, available=available,
        )
        self.assertEqual(len([b for b in boards if b.tag == alg.TAG_WIDTH_FILL]), 0)
        self.assertEqual(result, 895)

    def test_respects_pallet_width_brake(self):
        # 主950 + 100mm補填 = 1050 > パレット幅1000 なので補填を削る
        boards = [SelectedBoard(width=950, length=1000, count=1, tag=alg.TAG_MAIN)]
        available = [board(100, 1000)]
        alg.run_width_fill_phase(
            boards, main_min_w=950, product_w=1100, pallet_w=1000, target_len=1000, available=available,
        )
        self.assertEqual(len([b for b in boards if b.tag == alg.TAG_WIDTH_FILL]), 0)

    def test_respects_max_strips(self):
        # 既に3ストリップある状態では補填を追加できない
        boards = [
            SelectedBoard(width=300, length=1000, count=1, tag=alg.TAG_MAIN),
            SelectedBoard(width=300, length=1000, count=1, tag=alg.TAG_MAIN),
            SelectedBoard(width=300, length=1000, count=1, tag=alg.TAG_MAIN),
        ]
        available = [board(100, 1000)]
        alg.run_width_fill_phase(
            boards, main_min_w=300, product_w=1000, pallet_w=1200, target_len=1000, available=available,
        )
        self.assertEqual(len([b for b in boards if b.tag == alg.TAG_WIDTH_FILL]), 0)

    def test_length_fill_boards_excluded_from_strip_count(self):
        boards = [
            SelectedBoard(width=800, length=1000, count=1, tag=alg.TAG_MAIN),
            SelectedBoard(width=200, length=900, count=1, tag=alg.TAG_LENGTH_FILL),
        ]
        available = [board(100, 1000)]
        alg.run_width_fill_phase(
            boards, main_min_w=800, product_w=900, pallet_w=1000, target_len=1000, available=available,
        )
        # 丈補填はストリップ数に数えないので幅補填が入る
        self.assertEqual(len([b for b in boards if b.tag == alg.TAG_WIDTH_FILL]), 1)


class SimulateWidthFillTests(unittest.TestCase):
    def test_returns_input_when_no_gap(self):
        self.assertEqual(alg.simulate_lower_width_fill(900, 900, []), 900)

    def test_accumulates_short_sides(self):
        sorted_boards = [board(100, 1000)]
        # gap=100 → 100mmで1回埋めて到達900
        self.assertEqual(alg.simulate_lower_width_fill(800, 900, sorted_boards), 900)

    def test_does_not_mutate_inputs(self):
        sorted_boards = [board(100, 1000)]
        before = [(b.width, b.length) for b in sorted_boards]
        alg.simulate_lower_width_fill(800, 900, sorted_boards)
        self.assertEqual([(b.width, b.length) for b in sorted_boards], before)


class FatigueModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.palette = make_palette(1000, 2000)
        self.product = ProductSize(width=900, length=1900)

    def test_competitive_prefers_lower_fatigue(self):
        # どちらもPASS1許容内。疲労度の小さい 995x600 が選ばれるべき
        available = [board(1000, 600), board(995, 600)]
        fatigue_map = {
            "1000x600": FatigueEntry(dist=40.0, area=10.0, width_cut=0.0, length_cut=0.0),
            "995x600": FatigueEntry(dist=1.0, area=1.0, width_cut=0.0, length_cut=0.0),
        }
        result = alg.select_lower_boards(
            available, self.palette, self.product, fatigue_map=fatigue_map, fatigue_mode=True,
        )
        self.assertTrue(result.boards)
        self.assertEqual(result.boards[0].width, 995)

    def test_pass15_used_flag_stays_false_reproducing_vba_bug(self):
        # VBAの既知の不具合(常にFalse)をそのまま踏襲していることを固定する
        available = [board(1000, 600)]
        result = alg.select_lower_boards(
            available, self.palette, self.product, fatigue_map={}, fatigue_mode=True,
        )
        self.assertFalse(result.state.pass15_used)


class NarrowAndWideCutTests(unittest.TestCase):
    def test_narrow_detected_when_nothing_selected(self):
        palette = make_palette(1000, 2000)
        product = ProductSize(width=900, length=1900)
        result = alg.select_lower_boards([board(400, 400)], palette, product)
        self.assertTrue(result.needs_narrow)
        self.assertTrue(result.state.narrow_pallet)

    def test_wide_cut_needed_when_fill_cannot_cover(self):
        # 主600(パレット幅1000の半分以上)だが製品幅900に届かず、
        # 補填サイズの在庫も無いためカット前提選定に置き換わる
        palette = make_palette(1000, 2000)
        product = ProductSize(width=900, length=1900)
        result = alg.select_lower_boards([board(600, 600)], palette, product)
        self.assertTrue(result.needs_wide_cut)
        self.assertEqual(len(result.boards), 1)


if __name__ == "__main__":
    unittest.main()


class LengthFillWidthCorrectionTests(unittest.TestCase):
    """VBA `ApplyLengthFillWidthCorrection` の移植の検証。"""

    def setUp(self) -> None:
        self.palette = make_palette(1000, 2000)
        self.product = ProductSize(width=900, length=1900)

    def test_skips_main_and_length_fill_rows(self):
        boards = [
            SelectedBoard(width=1000, length=600, count=1, tag=alg.TAG_MAIN),
            SelectedBoard(width=400, length=600, count=1, tag=alg.TAG_LENGTH_FILL),
        ]
        alg.apply_length_fill_width_correction(
            boards, sel_max_w=1000, palette=self.palette, product=self.product,
            available=[board(50, 600)],
        )
        self.assertEqual(len(boards), 2)  # 何も追加・削除されない

    def test_skips_small_filler_rows(self):
        boards = [
            SelectedBoard(width=1000, length=600, count=1, tag=alg.TAG_MAIN),
            SelectedBoard(width=50, length=600, count=1, tag=""),  # 短辺50mm = 補填サイズ
        ]
        alg.apply_length_fill_width_correction(
            boards, sel_max_w=1000, palette=self.palette, product=self.product,
            available=[board(50, 600)],
        )
        self.assertEqual(len(boards), 2)

    def test_adds_small_filler_for_untagged_narrow_row(self):
        # 未タグ(PASS3経由)の400mm行は製品幅900に対し500mm不足 →
        # 500mm以上の補填サイズは無いので主ボード+1して行ごと削除される
        boards = [
            SelectedBoard(width=1000, length=600, count=1, tag=alg.TAG_MAIN),
            SelectedBoard(width=400, length=600, count=1, tag=""),
        ]
        alg.apply_length_fill_width_correction(
            boards, sel_max_w=1000, palette=self.palette, product=self.product,
            available=[board(50, 600)],
        )
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0].count, 2)

    def test_adds_filler_when_gap_fits_available_size(self):
        # 幅ギャップ50mm、50mm在庫あり → 幅補填として追加される
        boards = [
            SelectedBoard(width=1000, length=600, count=1, tag=alg.TAG_MAIN),
            SelectedBoard(width=850, length=600, count=1, tag=""),
        ]
        alg.apply_length_fill_width_correction(
            boards, sel_max_w=1000, palette=self.palette, product=self.product,
            available=[board(50, 600)],
        )
        fills = [b for b in boards if b.tag == alg.TAG_WIDTH_FILL]
        self.assertEqual(len(fills), 1)
        self.assertEqual(min(fills[0].width, fills[0].length), 50)

    def test_consecutive_deleted_rows_are_not_skipped(self):
        """VBA修正: RemoveItem後の余分なデクリメントで隣の行が読み飛ばされ
        ないこと(1行おきにしか処理されない、という壊れ方をしないこと)。
        """
        boards = [
            SelectedBoard(width=1000, length=600, count=1, tag=alg.TAG_MAIN),
            SelectedBoard(width=400, length=600, count=1, tag=""),   # 削除対象
            SelectedBoard(width=400, length=600, count=1, tag=""),   # これも削除対象
        ]
        alg.apply_length_fill_width_correction(
            boards, sel_max_w=1000, palette=self.palette, product=self.product,
            available=[board(50, 600)],
        )
        # 両方とも処理されて削除され、主ボードの枚数が2回分増えていること
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0].count, 3)


class DropLengthFillTests(unittest.TestCase):
    def test_removes_non_width_fill_rows_when_main_covers_length(self):
        palette = make_palette(1000, 2000)
        product = ProductSize(width=900, length=1000)
        boards = [
            SelectedBoard(width=1000, length=1200, count=1, tag=alg.TAG_MAIN),  # 1200 >= 1000
            SelectedBoard(width=300, length=800, count=1, tag=alg.TAG_LENGTH_FILL),
            SelectedBoard(width=50, length=800, count=1, tag=alg.TAG_WIDTH_FILL),
        ]
        alg._drop_length_fill_when_covered(boards, palette, product)
        tags = [b.tag for b in boards]
        self.assertIn(alg.TAG_MAIN, tags)
        self.assertIn(alg.TAG_WIDTH_FILL, tags)   # 幅補填は残る
        self.assertNotIn(alg.TAG_LENGTH_FILL, tags)  # 丈補填は削除

    def test_keeps_rows_when_main_does_not_cover(self):
        palette = make_palette(1000, 2000)
        product = ProductSize(width=900, length=1900)
        boards = [
            SelectedBoard(width=1000, length=600, count=1, tag=alg.TAG_MAIN),
            SelectedBoard(width=300, length=800, count=1, tag=alg.TAG_LENGTH_FILL),
        ]
        alg._drop_length_fill_when_covered(boards, palette, product)
        self.assertEqual(len(boards), 2)


class ForceAddForLengthShortageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.palette = make_palette(1000, 2000)
        self.product = ProductSize(width=900, length=1900)

    def test_adds_length_fill_board_within_3x_gap(self):
        # ギャップ300mm、短辺400mm(<=900=300*3)の在庫あり → 丈補填として追加
        boards = [SelectedBoard(width=1000, length=1600, count=1, tag=alg.TAG_MAIN)]
        alg._force_add_for_length_shortage(
            boards, self.palette, self.product, [board(400, 1000)], None,
        )
        self.assertEqual(len(boards), 2)
        self.assertEqual(boards[1].tag, alg.TAG_LENGTH_FILL)

    def test_tops_up_main_when_no_candidate(self):
        # 在庫が主ボードのみ(短辺1600 > 300*3=900)で候補なし → 主ボードを増やす
        boards = [SelectedBoard(width=1000, length=1600, count=1, tag=alg.TAG_MAIN)]
        alg._force_add_for_length_shortage(
            boards, self.palette, self.product, [board(1000, 1600)], None,
        )
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0].count, 2)  # ceil(300/1600)=1 追加

    def test_no_change_when_gap_within_3mm(self):
        boards = [SelectedBoard(width=1000, length=1900, count=1, tag=alg.TAG_MAIN)]
        alg._force_add_for_length_shortage(
            boards, self.palette, self.product, [board(400, 1000)], None,
        )
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0].count, 1)


class SelectUpperBoardsTests(unittest.TestCase):
    """VBA `SelectUpperBoards` の移植の検証。"""

    def setUp(self) -> None:
        self.palette = make_palette(1000, 2000)
        self.product = ProductSize(width=900, length=1900)
        self.lower = [SelectedBoard(width=1000, length=600, count=3, tag=alg.TAG_MAIN)]

    def test_shared_mode_copies_lower_main(self):
        # 保護材が確定していてアングル/一致なしでもない → 上下共用
        r = alg.select_upper_boards(
            self.lower, [board(900, 1900)], self.palette, self.product, last_hosozai="厚紙",
        )
        self.assertEqual(r.mode, "共用")
        self.assertEqual(len(r.boards), 1)
        self.assertEqual((r.boards[0].width, r.boards[0].length, r.boards[0].count), (1000, 600, 3))
        self.assertEqual(r.boards[0].tag, "")

    def test_shared_mode_not_applied_for_angle_or_no_match(self):
        for hosozai in ("アングル", "一致なし", ""):
            r = alg.select_upper_boards(
                self.lower, [board(900, 1900)], self.palette, self.product, last_hosozai=hosozai,
            )
            self.assertNotEqual(r.mode, "共用", f"hosozai={hosozai}")

    def test_protec_mode_copies_lower_when_within_tolerance(self):
        # 下用1000x600 の上用有効幅は 900(製品幅で頭打ち)ではなく…
        # GetBestOrientationは上用ではfit_limit=製品幅900。1000も600も比較され600が採用される
        # 600 >= 900-80=820 は不成立 → 通常選定に落ちる
        r = alg.select_upper_boards(
            self.lower, [board(880, 1900)], self.palette, self.product, is_protec_mode=True,
        )
        self.assertNotEqual(r.mode, "プロテック")

    def test_protec_mode_applies_when_lower_width_fits(self):
        lower = [SelectedBoard(width=880, length=600, count=2, tag=alg.TAG_MAIN)]
        # 880 >= 900-80=820 なのでプロテックモード成立
        r = alg.select_upper_boards(
            lower, [board(880, 1900)], self.palette, self.product, is_protec_mode=True,
        )
        self.assertEqual(r.mode, "プロテック")
        self.assertEqual((r.boards[0].width, r.boards[0].count), (880, 2))

    def test_protec_1p1216_uses_tighter_tolerance(self):
        lower = [SelectedBoard(width=880, length=600, count=2, tag=alg.TAG_MAIN)]
        # 1P1216 は許容-5mm → 880 >= 900-5=895 は不成立 → 通常選定へ
        r = alg.select_upper_boards(
            lower, [board(880, 1900)], self.palette, self.product,
            is_protec_mode=True, is_protec_1p1216=True,
        )
        self.assertNotEqual(r.mode, "プロテック")

    def test_normal_selection_picks_first_qualifying_board(self):
        r = alg.select_upper_boards(self.lower, [board(900, 600)], self.palette, self.product)
        self.assertEqual(r.mode, "normal")
        self.assertTrue(r.boards)
        self.assertEqual(r.boards[0].width, 900)

    def test_rejects_boards_at_or_below_100mm(self):
        r = alg.select_upper_boards(self.lower, [board(100, 600)], self.palette, self.product)
        self.assertTrue(r.narrow_pallet)  # 候補なし → 狭幅扱い

    def test_rejects_board_under_half_product_width(self):
        # 400x440 は両向きとも 900*0.5=450 未満(採用される向きは440) → 却下 → 候補なし
        r = alg.select_upper_boards(self.lower, [board(400, 440)], self.palette, self.product)
        self.assertTrue(r.narrow_pallet)

    def test_rotated_orientation_can_pass_half_width_check(self):
        # 400x600 は回転向き(600)が採用されるため幅不足チェックは通過し、
        # 主ボードとして選ばれる(その後、幅補填できずカット前提選定へ回る)
        r = alg.select_upper_boards(self.lower, [board(400, 600)], self.palette, self.product)
        self.assertFalse(r.narrow_pallet)
        self.assertTrue(r.needs_wide_cut)

    def test_count_uses_product_length_minus_5(self):
        # 主ボードの枚数は (1900-5)//600 = 3枚。ただし 3枚では丈1800で
        # 製品丈1900に100mm足りず、補填できる小型在庫も無いため
        # 「丈不足強制追加」で主ボードが1枚増えて4枚になる
        r = alg.select_upper_boards(self.lower, [board(900, 600)], self.palette, self.product)
        self.assertEqual(r.boards[0].count, 4)

    def test_width_fill_skipped_within_tolerance(self):
        # 850 は製品幅900に対し50mm不足だが UPPER_WIDTH_TOLERANCE(80) 以内 → 補填しない
        r = alg.select_upper_boards(
            self.lower, [board(850, 600), board(50, 600)], self.palette, self.product,
        )
        self.assertEqual(len([b for b in r.boards if b.tag == alg.TAG_WIDTH_FILL]), 0)

    def test_wide_cut_needed_when_fill_cannot_cover(self):
        # 500 は製品幅900に対し400mm不足、補填在庫も無い → カット前提選定に置き換わる
        r = alg.select_upper_boards(self.lower, [board(500, 600)], self.palette, self.product)
        self.assertTrue(r.needs_wide_cut)
        self.assertEqual(len(r.boards), 1)

    def test_length_fill_adds_main_when_gap_over_400(self):
        # 主900x600 x3 = 1800、製品丈1900に対し100mm不足(400以下)なので
        # 主ボード追加ではなく小型補填が優先される
        r = alg.select_upper_boards(
            self.lower, [board(900, 600), board(100, 800)], self.palette, self.product,
        )
        main = r.boards[0]
        self.assertEqual(main.count, 3)  # 主ボードは増えない
        self.assertTrue(any(b.tag == alg.TAG_LENGTH_FILL for b in r.boards))

    def test_length_shortage_forces_extra_main_board(self):
        # (1900-5)//300 = 6枚 = 1800 → 100mm不足。丈残100mmは400以下なので
        # 丈補填フェーズでは主ボードを増やさないが、補填できる在庫が無いため
        # 最後の「丈不足強制追加」で ceil(100/300)=1枚 が足される
        product = ProductSize(width=900, length=1900)
        r = alg.select_upper_boards(
            self.lower, [board(900, 300)], self.palette, product,
        )
        self.assertEqual(r.boards[0].count, 7)

    def test_length_shortage_prefers_a_small_board_over_bumping_main(self):
        # 丈ギャップ100mmを埋められる小型在庫(100x800)があれば、
        # 主ボードを増やさずそれを足す
        product = ProductSize(width=900, length=1900)
        r = alg.select_upper_boards(
            self.lower, [board(900, 300), board(100, 800)], self.palette, product,
        )
        self.assertEqual(r.boards[0].count, 6)
        self.assertTrue(any(b.tag == alg.TAG_LENGTH_FILL for b in r.boards))


class UpperLengthFillMergeTests(unittest.TestCase):
    """`_run_upper_length_fill` の「既存行への丈補填マージ」バグ修正の検証。

    バグの内容(2026年 実機ログから発覚):
    既存の "丈補填" 行へ+1するとき、丈残の減算量を `_orient()` の
    再計算結果(幅フィット優先の向き)から取っていた。丈補填ボードは
    短辺だけを丈方向に使う配置なので、実際には短辺(例:30mm)しか
    埋めていないのに、`_orient()` が返す長辺(例:1600mm)を使って
    「1600mm埋めた」と誤認していた。
    """

    def setUp(self) -> None:
        self.product = ProductSize(width=900, length=2000)
        # 主900x190 x10 → 丈カバー1900。丈残は100(400以下なので主は増えない)
        self.main = SelectedBoard(900, 190, 10, alg.TAG_MAIN)

    def test_existing_merge_uses_short_side_not_orientation_recalc(self):
        """既存"丈補填"行への+1は短辺(30)ずつ減らす。長辺(1600)ではない。

        30x1600 は在庫1種類のみなので、毎回同じ行への merge を繰り返す。
        短辺(30)ずつ正しく減らせば丈残100mmを埋めるのに複数回のmergeが
        起き、上限4枚まで積み増される。誤って長辺(1600)を使うと、
        1回のmergeで丈残が大きく負の値になり、2回目のループ以降が
        起きないまま止まる(=枚数が少ないまま終わる)。
        """
        boards = [
            self.main,
            SelectedBoard(30, 1600, 1, alg.TAG_LENGTH_FILL),
        ]
        available = [board(30, 1600)]
        alg._run_upper_length_fill(boards, self.product, available, None)

        length_fill = next(b for b in boards if b.tag == alg.TAG_LENGTH_FILL)
        self.assertEqual((length_fill.width, length_fill.length), (30, 1600))
        # 短辺(30mm)ずつ正しく減算されていれば、100mmの丈残を埋めるのに
        # 複数回mergeが起き、上限の4枚まで積み増される
        self.assertEqual(length_fill.count, alg.LENGTH_FILL_COUNT_CAP)

    def test_does_not_merge_into_width_fill_row_of_same_size(self):
        """"幅補填" タグの行は丈カバーに寄与しない専用枠。マージ対象にしない。

        同じ(30,1600)が既に"幅補填"として1枚あっても、丈補填はそこへ
        +1するのではなく、別の"丈補填"行を新規に作らなければならない。
        タグを問わずマージすると、幅補填の専用枠に丈補填の枚数が
        紛れ込み、実際の丈カバーは伸びないまま丈残だけ消費した扱いに
        なってしまう。
        """
        width_fill = SelectedBoard(30, 1600, 1, alg.TAG_WIDTH_FILL)
        boards = [self.main, width_fill]
        available = [board(30, 1600)]
        alg._run_upper_length_fill(boards, self.product, available, None)

        # 幅補填行はそのまま(丈補填の+1が紛れ込んでいない)
        self.assertEqual(width_fill.count, 1)
        # 丈補填は別行として新規に作られている
        length_fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(length_fill_rows), 1)
        self.assertEqual((length_fill_rows[0].width, length_fill_rows[0].length), (30, 1600))


class NarrowPaletteSelectionTests(unittest.TestCase):
    """VBA `SelectBoardsForNarrowPalette` 系の検証。"""

    def setUp(self) -> None:
        self.palette = make_palette(600, 2000)
        self.product = ProductSize(width=500, length=1800)

    def test_stacks_boards_widthwise_until_covered(self):
        # 短辺300を2枚積んで製品幅500をカバー(2枚目は残り200以下の候補)
        available = [board(300, 900), board(200, 900)]
        boards = alg.select_boards_for_narrow_palette(available, self.palette, self.product)
        self.assertEqual(len(boards), 2)
        self.assertEqual(sum(min(b.width, b.length) for b in boards), 500)
        self.assertTrue(all(b.tag == alg.TAG_MAIN for b in boards))

    def test_count_covers_product_length(self):
        available = [board(500, 900)]
        boards = alg.select_boards_for_narrow_palette(available, self.palette, self.product)
        # ceil(1800/900) = 2枚
        self.assertEqual(boards[0].count, 2)

    def test_excludes_boards_wider_than_pallet(self):
        available = [board(700, 900)]  # 短辺700 > パレット幅600
        boards = alg.select_boards_for_narrow_palette(available, self.palette, self.product)
        self.assertEqual(len(boards), 0)

    def test_excludes_boards_exceeding_remaining_width(self):
        # 短辺550 は残り幅500+3 を超えるので選ばれない
        available = [board(550, 900)]
        boards = alg.select_boards_for_narrow_palette(available, self.palette, self.product)
        self.assertEqual(len(boards), 0)

    def test_upper_variant_caps_length_by_pallet(self):
        # 上用は有効長を Min(長辺, パレット丈) で頭打ちにする
        palette = make_palette(600, 500)   # パレット丈500
        available = [board(500, 900)]      # 長辺900 → 500で頭打ち
        boards = alg.select_boards_for_narrow_palette_upper(available, palette, self.product)
        # ceil(1800/500) = 4枚 (頭打ちしなければ ceil(1800/900)=2枚)
        self.assertEqual(boards[0].count, 4)


class NarrowPaletteFillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.palette = make_palette(600, 2000)
        self.product = ProductSize(width=500, length=1800)

    def test_fills_y_gap_with_small_sizes(self):
        boards = [SelectedBoard(width=500, length=900, count=2, tag=alg.TAG_MAIN)]
        available = [board(100, 900), board(50, 900)]
        alg.run_narrow_palette_lower_fill(boards, self.palette, self.product, available)
        fills = [b for b in boards if b.tag == alg.TAG_WIDTH_FILL]
        # Y方向gap = 600-500 = 100 → 30/50/100の順に試し、50と100が入りうる
        self.assertTrue(fills)
        self.assertTrue(all(min(b.width, b.length) in (30, 50, 100) for b in fills))

    def test_skips_y_fill_within_tolerance(self):
        boards = [SelectedBoard(width=595, length=900, count=2, tag=alg.TAG_MAIN)]
        available = [board(100, 900)]
        alg.run_narrow_palette_lower_fill(boards, self.palette, self.product, available)
        self.assertEqual(len([b for b in boards if b.tag == alg.TAG_WIDTH_FILL]), 0)

    def test_tops_up_x_direction_count(self):
        # 長辺900 x 1枚 = 900 だが製品丈1800 → +1枚
        boards = [SelectedBoard(width=500, length=900, count=1, tag=alg.TAG_MAIN)]
        alg.run_narrow_palette_lower_fill(boards, self.palette, self.product, [])
        self.assertEqual(boards[0].count, 2)

    def test_width_fill_rows_excluded_from_x_topup(self):
        boards = [
            SelectedBoard(width=500, length=900, count=2, tag=alg.TAG_MAIN),
            SelectedBoard(width=50, length=100, count=1, tag=alg.TAG_WIDTH_FILL),
        ]
        alg.run_narrow_palette_lower_fill(boards, self.palette, self.product, [])
        self.assertEqual(boards[1].count, 1)  # 幅補填は枚数追加されない


class WideCutSelectionTests(unittest.TestCase):
    """VBA `SelectBoardsForWideLower` / `SelectBoardsForWideProduct` の検証。"""

    def setUp(self) -> None:
        self.palette = make_palette(1000, 2000)
        self.product = ProductSize(width=900, length=1800)

    def test_picks_min_overshoot_board(self):
        # 最大辺がパレット幅1000以上のうち、差が最小の1100が選ばれる
        available = [board(300, 1500), board(300, 1100)]
        r = alg.select_boards_for_wide_lower(available, self.palette, self.product)
        self.assertEqual(r.boards[0].length, 1100)
        self.assertEqual(r.boards[0].tag, alg.TAG_CUT_PREMISE)

    def test_tie_break_prefers_larger_short_side(self):
        available = [board(250, 1100), board(400, 1100)]
        r = alg.select_boards_for_wide_lower(available, self.palette, self.product)
        self.assertEqual(min(r.boards[0].width, r.boards[0].length), 400)

    def test_excludes_short_side_under_200(self):
        # 短辺100は補填専用サイズなので候補外 → フォールバックに回る
        available = [board(100, 1100)]
        r = alg.select_boards_for_wide_lower(available, self.palette, self.product)
        self.assertTrue(r.used_fallback)

    def test_records_cut_line_as_short_side(self):
        available = [board(400, 1100)]
        r = alg.select_boards_for_wide_lower(available, self.palette, self.product)
        self.assertEqual(r.cut_info["400x1100"], 400)

    def test_count_ceils_product_length_over_short_side(self):
        available = [board(400, 1100)]
        r = alg.select_boards_for_wide_lower(available, self.palette, self.product)
        # ceil(1800/400) = 5枚(丈補填で更に増える可能性はない: 400*5=2000 >= 1800)
        self.assertGreaterEqual(r.boards[0].count, 5)

    def test_fallback_when_no_board_reaches_target_width(self):
        available = [board(300, 500)]  # 最大辺500 < パレット幅1000
        r = alg.select_boards_for_wide_lower(available, self.palette, self.product)
        self.assertTrue(r.used_fallback)
        self.assertEqual(r.boards[0].tag, alg.TAG_MAIN)  # 下用のフォールバックタグ

    def test_upper_variant_uses_product_width_and_empty_fallback_tag(self):
        available = [board(300, 500)]
        r = alg.select_boards_for_wide_product(available, self.product)
        self.assertTrue(r.used_fallback)
        self.assertEqual(r.boards[0].tag, "")  # 上用のフォールバックタグは空

    def test_fatigue_mode_prefers_lowest_total(self):
        available = [board(400, 1100), board(450, 1100)]
        fatigue_map = {
            "400x1100": FatigueEntry(dist=90.0, area=40.0, width_cut=0.0, length_cut=0.0),
            "450x1100": FatigueEntry(dist=1.0, area=1.0, width_cut=0.0, length_cut=0.0),
        }
        r = alg.select_boards_for_wide_lower(
            available, self.palette, self.product, fatigue_map=fatigue_map, fatigue_mode=True,
        )
        self.assertEqual(min(r.boards[0].width, r.boards[0].length), 450)


class UpperWideCutTests(unittest.TestCase):
    def test_tops_up_count_for_length_shortage(self):
        product = ProductSize(width=900, length=1800)
        available = [board(400, 1000)]
        r = alg.select_upper_boards_wide_cut(available, product)
        # 短辺400 → ceil(1800/400)=5枚で1800ちょうど
        self.assertEqual(min(r.boards[0].width, r.boards[0].length), 400)
        self.assertGreaterEqual(r.boards[0].count * 400, 1800)

    def test_records_length_cut_when_overshooting(self):
        product = ProductSize(width=900, length=1750)
        available = [board(400, 1000)]
        r = alg.select_upper_boards_wide_cut(available, product)
        # 400*5=2000 > 1750+3 → 丈カット記録。
        # VBAも `mLengthCutInfo.Add "U_" & w & "x" & l` なので丈カット側に入る
        # (以前は幅カット側の cut_info に入れており、切断依頼書が
        #  丈カットを検出できなかった)
        self.assertIn("U_400x1000", r.length_cut_info)
        self.assertNotIn("U_400x1000", r.cut_info)

    def test_length_cut_reaches_auto_select_result(self):
        """カット前提の丈カットが最終結果まで残ること(切断依頼書が読む)。"""
        product = ProductSize(width=900, length=1750)
        palette = make_palette(1000, 2000)
        available = [board(400, 1000)]
        r = alg.auto_select_boards(available, palette, product)
        self.assertEqual(r.length_cut_info.get("U_400x1000"), 400)
        self.assertEqual(r.length_cut_count.get("U_400x1000"), 1)


class RecalcLengthCutInfoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.palette = make_palette(1000, 2000)
        self.product = ProductSize(width=900, length=1800)

    def test_records_lower_length_cut_when_exceeding_pallet(self):
        lower = [SelectedBoard(width=1000, length=700, count=3, tag=alg.TAG_MAIN)]
        info, count = alg.recalc_length_cut_info(lower, [], self.palette, self.product)
        # 700*3 = 2100 > パレット丈2000 → 記録
        self.assertIn("L_1000x700", info)
        self.assertEqual(count["L_1000x700"], 1)  # 常に1枚(業務ルール)

    def test_skips_fill_rows(self):
        lower = [
            SelectedBoard(width=1000, length=700, count=3, tag=alg.TAG_WIDTH_FILL),
            SelectedBoard(width=1000, length=700, count=3, tag=alg.TAG_LENGTH_FILL),
        ]
        info, _ = alg.recalc_length_cut_info(lower, [], self.palette, self.product)
        self.assertEqual(info, {})

    def test_skips_cut_premise_rows_in_upper(self):
        upper = [SelectedBoard(width=900, length=700, count=3, tag=alg.TAG_CUT_PREMISE)]
        info, _ = alg.recalc_length_cut_info([], upper, self.palette, self.product)
        self.assertEqual(info, {})

    def test_no_record_when_within_limit(self):
        lower = [SelectedBoard(width=1000, length=600, count=3, tag=alg.TAG_MAIN)]
        info, _ = alg.recalc_length_cut_info(lower, [], self.palette, self.product)
        self.assertEqual(info, {})  # 600*3=1800 <= 2000


class AutoSelectBoardsHubTests(unittest.TestCase):
    def test_runs_lower_then_upper_then_length_cut(self):
        palette = make_palette(1150, 2650)
        product = ProductSize(width=1122, length=2502)
        available = [board(750, 1130), board(660, 1050), board(50, 1600), board(100, 2000)]
        r = alg.auto_select_boards(available, palette, product)
        self.assertTrue(r.lower)
        self.assertTrue(r.upper)
        self.assertIsInstance(r.cut_info, dict)
        self.assertIsInstance(r.length_cut_info, dict)
        self.assertIsInstance(r.length_cut_count, dict)


class WidthFillLengthRecheckTests(unittest.TestCase):
    """VBA `RunLowerFillPhase` 最終段「幅補填行の丈カバレッジ再確認」。"""

    def setUp(self) -> None:
        self.palette = make_palette(1000, 3000)
        self.product = ProductSize(width=980, length=2900)

    def test_short_width_fill_row_gets_more_pieces(self):
        # 主ボード 900x1000 x2 → X方向カバー2000
        # 幅補填 50x1000 が1枚(=1000)しかなければ、丈方向に足りないので増える
        boards = [
            SelectedBoard(900, 1000, 2, alg.TAG_MAIN),
            SelectedBoard(50, 1000, 1, alg.TAG_WIDTH_FILL),
        ]
        alg._recheck_width_fill_length(
            boards, 1, 900, self.palette, self.product, [board(50, 1000)])
        self.assertGreater(boards[1].count, 1)

    def test_row_wider_than_main_is_skipped(self):
        # 短辺が主ボードの有効幅以上なら幅補填ではないので対象外
        boards = [
            SelectedBoard(900, 1000, 2, alg.TAG_MAIN),
            SelectedBoard(900, 1000, 1, alg.TAG_MAIN),
        ]
        alg._recheck_width_fill_length(
            boards, 1, 900, self.palette, self.product, [board(50, 1000)])
        self.assertEqual(boards[1].count, 1)

    def test_already_covered_row_is_left_alone(self):
        boards = [
            SelectedBoard(900, 1000, 2, alg.TAG_MAIN),
            SelectedBoard(50, 1000, 2, alg.TAG_WIDTH_FILL),
        ]
        alg._recheck_width_fill_length(
            boards, 1, 900, self.palette, self.product, [board(50, 1000)])
        self.assertEqual(boards[1].count, 2)

    def test_added_count_is_capped_at_four(self):
        # 主ボードのカバーが非常に長くても、同種の追加は4枚まで
        boards = [
            SelectedBoard(900, 1000, 3, alg.TAG_MAIN),
            SelectedBoard(50, 100, 1, alg.TAG_WIDTH_FILL),
        ]
        alg._recheck_width_fill_length(
            boards, 1, 900, self.palette, self.product, [])
        self.assertLessEqual(boards[1].count, 1 + alg.LENGTH_FILL_COUNT_CAP)

    def test_target_is_capped_by_product_length(self):
        # 主ボードが製品丈を超えてカバーしていても、目標は製品丈まで
        product = ProductSize(width=980, length=1500)
        boards = [
            SelectedBoard(900, 1000, 3, alg.TAG_MAIN),   # カバー3000
            SelectedBoard(50, 1500, 1, alg.TAG_WIDTH_FILL),
        ]
        alg._recheck_width_fill_length(
            boards, 1, 900, self.palette, product, [])
        self.assertEqual(boards[1].count, 1)   # 1500で製品丈を満たすので増えない

    def test_no_boards_is_safe(self):
        boards: list[SelectedBoard] = []
        alg._recheck_width_fill_length(boards, 0, 900, self.palette, self.product, [])
        self.assertEqual(boards, [])

    def test_length_fill_row_is_excluded(self):
        """"丈補填" タグの行は幅方向ストリップではないので対象外(バグ修正)。

        丈補填ボード(例: 30x800)は「丈方向の穴埋め専用」で、幅方向の
        カバー量を判定するこのチェックの対象ではない。タグ除外が無いと
        誤ってこのチェックにかけられ、無関係な丈補填の枚数を芋づる式に
        水増ししてしまう(過剰選定の直接原因)。

        主900x1000 x2 → X方向カバー1800。丈補填 30x800 が1枚(=800)だけ
        なので、タグ除外が無いと「幅補填ストリップとして丈カバーが
        1000mm足りない」と誤判定され、無関係に枚数が増やされる。
        """
        boards = [
            SelectedBoard(900, 1000, 2, alg.TAG_MAIN),
            SelectedBoard(30, 800, 1, alg.TAG_LENGTH_FILL),
        ]
        alg._recheck_width_fill_length(
            boards, 1, 900, self.palette, self.product, [board(30, 800)])
        self.assertEqual(boards[1].count, 1)


class AddOrIncrementTests(unittest.TestCase):
    def test_new_size_is_appended(self):
        boards: list[SelectedBoard] = []
        alg._add_or_increment(boards, 50, 1000, alg.TAG_LENGTH_FILL)
        self.assertEqual((boards[0].width, boards[0].count, boards[0].tag),
                         (50, 1, alg.TAG_LENGTH_FILL))

    def test_existing_size_is_incremented(self):
        boards = [SelectedBoard(50, 1000, 1, alg.TAG_LENGTH_FILL)]
        alg._add_or_increment(boards, 50, 1000, alg.TAG_LENGTH_FILL)
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0].count, 2)

    def test_count_is_capped(self):
        boards = [SelectedBoard(50, 1000, alg.LENGTH_FILL_COUNT_CAP, alg.TAG_LENGTH_FILL)]
        alg._add_or_increment(boards, 50, 1000, alg.TAG_LENGTH_FILL)
        self.assertEqual(boards[0].count, alg.LENGTH_FILL_COUNT_CAP)


class UpperFillWidthCorrectionTests(unittest.TestCase):
    """VBA「上用: 補填ボードの幅不足を30/50/100mmで補填」。"""

    def setUp(self) -> None:
        self.product = ProductSize(width=900, length=1900)

    def test_narrow_row_gets_a_small_board(self):
        boards = [
            SelectedBoard(900, 600, 3, ""),
            SelectedBoard(850, 300, 1, alg.TAG_LENGTH_FILL),   # 幅50mm不足
        ]
        alg._apply_upper_fill_width_correction(boards, self.product, [board(50, 600)])
        self.assertTrue(any(b.width == 50 and b.tag == alg.TAG_WIDTH_FILL for b in boards))

    def test_row_is_dropped_when_no_small_board(self):
        boards = [
            SelectedBoard(900, 600, 3, ""),
            SelectedBoard(850, 300, 1, alg.TAG_LENGTH_FILL),
        ]
        alg._apply_upper_fill_width_correction(boards, self.product, [])
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0].count, 4)   # 主ボードが1枚増える

    def test_full_width_row_is_untouched(self):
        boards = [
            SelectedBoard(900, 600, 3, ""),
            SelectedBoard(900, 300, 1, alg.TAG_LENGTH_FILL),
        ]
        alg._apply_upper_fill_width_correction(boards, self.product, [board(50, 600)])
        self.assertEqual(len(boards), 2)
        self.assertEqual(boards[1].count, 1)

    def test_width_fill_rows_are_skipped(self):
        boards = [
            SelectedBoard(900, 600, 3, ""),
            SelectedBoard(50, 600, 1, alg.TAG_WIDTH_FILL),
        ]
        alg._apply_upper_fill_width_correction(boards, self.product, [board(50, 600)])
        self.assertEqual(boards[1].count, 1)


class FindSmallBoardTests(unittest.TestCase):
    SIZES = (alg.FILL_SIZE_50, alg.FILL_SIZE_30, alg.FILL_SIZE_100)

    def test_picks_the_smallest_that_covers_the_gap(self):
        avail = [board(30, 600), board(50, 600), board(100, 600)]
        got = alg._find_small_board(avail, self.SIZES, 40)
        self.assertEqual(min(got.width, got.length), 50)

    def test_uses_30_when_the_gap_is_tiny(self):
        avail = [board(30, 600), board(50, 600), board(100, 600)]
        got = alg._find_small_board(avail, self.SIZES, 20)
        self.assertEqual(min(got.width, got.length), 30)

    def test_returns_none_when_gap_exceeds_every_size(self):
        avail = [board(30, 600), board(50, 600), board(100, 600)]
        self.assertIsNone(alg._find_small_board(avail, self.SIZES, 200))

    def test_returns_none_when_not_in_stock(self):
        self.assertIsNone(alg._find_small_board([], self.SIZES, 40))
