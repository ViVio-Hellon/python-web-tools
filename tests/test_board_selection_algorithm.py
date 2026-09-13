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
from packaging_tool import reports
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


class FixedLengthFill100mmTests(unittest.TestCase):
    """`_apply_fixed_length_fill_100mm`(丈補填の100mm固定ロジック、上用/下用共通)。

    現場の声:「残り丈400以下⇒100補填で丈を埋める(4回まで)、上下とも
    同じにしてほしい」。丈残400mm以下という前提であれば、100mm板を
    Ceiling(丈残/100)枚(最大4枚)追加するだけで必ずカバーできる。
    """

    def test_400_or_less_fills_with_100mm_boards(self):
        main = SelectedBoard(300, 1000, 1, alg.TAG_MAIN)
        boards = [main]
        alg._apply_fixed_length_fill_100mm(
            boards, 350, available=[board(100, 100)], category="下用")
        self.assertEqual(main.count, 1)  # 主ボードは増えない
        fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(fill_rows), 1)
        self.assertEqual((fill_rows[0].width, fill_rows[0].length), (100, 100))
        self.assertEqual(fill_rows[0].count, 4)  # ceil(350/100) = 4

    def test_100mm_fill_count_is_capped_at_4(self):
        main = SelectedBoard(300, 1000, 1, alg.TAG_MAIN)
        boards = [main]
        alg._apply_fixed_length_fill_100mm(
            boards, 400, available=[board(100, 100)], category="下用")
        fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(fill_rows[0].count, alg.LENGTH_FILL_COUNT_CAP)

    def test_over_400_is_deferred_to_force_add(self):
        # 400mm超はここでは扱わない(4枚クランプで埋めきれず、後続の
        # 丈不足強制追加と二重取りになるため)
        main = SelectedBoard(300, 1000, 1, alg.TAG_MAIN)
        boards = [main]
        alg._apply_fixed_length_fill_100mm(
            boards, 500, available=[board(100, 100)], category="下用")
        self.assertEqual(len(boards), 1)

    def test_no_100mm_stock_defers_without_crashing(self):
        main = SelectedBoard(300, 1000, 1, alg.TAG_MAIN)
        boards = [main]
        alg._apply_fixed_length_fill_100mm(
            boards, 200, available=[board(250, 250)], category="下用")
        self.assertEqual(len(boards), 1)  # 100mm在庫が無ければ何も足さない

    def test_existing_length_fill_row_is_merged_not_duplicated(self):
        main = SelectedBoard(300, 1000, 1, alg.TAG_MAIN)
        existing = SelectedBoard(100, 100, 1, alg.TAG_LENGTH_FILL)
        boards = [main, existing]
        alg._apply_fixed_length_fill_100mm(
            boards, 200, available=[board(100, 100)], category="下用")
        fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(fill_rows), 1)
        self.assertEqual(fill_rows[0].count, 1 + 2)  # 既存1枚 + ceil(200/100)=2枚

    def test_works_the_same_way_for_upper_category(self):
        main = SelectedBoard(300, 1000, 1, alg.TAG_MAIN)
        boards = [main]
        alg._apply_fixed_length_fill_100mm(
            boards, 350, available=[board(100, 100)], category="上用")
        fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(fill_rows[0].count, 4)


class LowerLengthFillLoopSameWidthTests(unittest.TestCase):
    """`_run_length_fill_loop`(丈補填反復ループ)の同幅完全一致への変更。

    現場の声:「主サイズ決定⇒残り丈400越えの場合、主と同じサイズの
    ボードを使用してほしいのに、主サイズではないものを使用している」。
    在庫全体から丈残に近い短辺を探す5回ループ自体は維持しつつ、候補を
    「主ボードと同幅(長辺が完全一致)」に限定する(以前は「同幅以上」)。
    """

    def test_picks_a_same_width_candidate_over_400mm_gap(self):
        # 主の実効幅(sel_max_w)=1250。500x1250は長辺1250で主と同幅
        sorted_boards = [board(500, 1250), board(1200, 1250)]
        boards = [SelectedBoard(1250, 2500, 1, alg.TAG_MAIN)]
        remaining = alg._run_length_fill_loop(
            boards, sorted_boards, l_gap=600, sel_max_w=1250,
            fatigue_map=None, fatigue_mode=False)
        fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(fill_rows), 1)
        self.assertEqual((fill_rows[0].width, fill_rows[0].length), (500, 1250))
        self.assertEqual(remaining, 600 - 500)

    def test_rejects_candidates_wider_than_the_main_board(self):
        # 長辺が主の実効幅より大きい(1300)候補は、以前は"同幅以上"で
        # 拾えていたが、いまは完全一致のみなので対象外
        sorted_boards = [board(600, 1300)]
        boards = [SelectedBoard(1250, 2500, 1, alg.TAG_MAIN)]
        remaining = alg._run_length_fill_loop(
            boards, sorted_boards, l_gap=600, sel_max_w=1250,
            fatigue_map=None, fatigue_mode=False)
        self.assertEqual(boards, [SelectedBoard(1250, 2500, 1, alg.TAG_MAIN)])
        self.assertEqual(remaining, 600)


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
            boards, self.palette, self.product, [board(400, 1000)], None, sel_max_w=1000,
        )
        self.assertEqual(len(boards), 2)
        self.assertEqual(boards[1].tag, alg.TAG_LENGTH_FILL)

    def test_tops_up_main_when_no_candidate(self):
        # 在庫が主ボードのみ(短辺1600 > 300*3=900)で候補なし → 主ボードを増やす
        boards = [SelectedBoard(width=1000, length=1600, count=1, tag=alg.TAG_MAIN)]
        alg._force_add_for_length_shortage(
            boards, self.palette, self.product, [board(1000, 1600)], None, sel_max_w=1000,
        )
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0].count, 2)  # ceil(300/1600)=1 追加

    def test_no_change_when_gap_within_3mm(self):
        boards = [SelectedBoard(width=1000, length=1900, count=1, tag=alg.TAG_MAIN)]
        alg._force_add_for_length_shortage(
            boards, self.palette, self.product, [board(400, 1000)], None, sel_max_w=1000,
        )
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0].count, 1)

    def test_ignores_capped_wrong_tag_row_when_matching_candidate(self):
        """タグを見ずにマッチすると、同サイズの"主"行の上限を誤って見てしまう(バグ修正)。

        候補(400x1000)と同じサイズの"主"行が偶然上限(4枚)に達していても、
        丈補填の候補選定には無関係。タグ限定が無いと候補が誤って除外され、
        代わりに主ボードが不要に増やされてしまう。

        finalTotalLは全行(タグ問わず)の合計なので、決め打ちのwrong_tag_row
        (400x1000 x4枚、有効丈換算400 → 合計1600)を加味してproduct.lengthを
        大きめに取り、gapが300mmになるよう調整している。
        """
        main = SelectedBoard(width=1000, length=1600, count=1, tag=alg.TAG_MAIN)
        wrong_tag_row = SelectedBoard(width=400, length=1000, count=alg.LENGTH_FILL_COUNT_CAP,
                                       tag=alg.TAG_MAIN)
        boards = [main, wrong_tag_row]
        product = ProductSize(width=900, length=3500)
        alg._force_add_for_length_shortage(
            boards, self.palette, product, [board(400, 1000)], None, sel_max_w=1000,
        )
        self.assertEqual(main.count, 1)  # フォールバック(主+1枚)は発動しない
        self.assertEqual(wrong_tag_row.count, alg.LENGTH_FILL_COUNT_CAP)  # 汚染されない
        length_fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(length_fill_rows), 1)

    def test_rejects_candidate_wider_than_the_main_board(self):
        """【仕様更新】主ボードと同幅(完全一致)の候補のみ対象。

        400x1050は長辺1050で主(sel_max_w=1000)と一致しないため対象外と
        なり、主ボードを増やすフォールバックへ回る。
        """
        boards = [SelectedBoard(width=1000, length=1600, count=1, tag=alg.TAG_MAIN)]
        alg._force_add_for_length_shortage(
            boards, self.palette, self.product, [board(400, 1050)], None, sel_max_w=1000,
        )
        self.assertEqual(len(boards), 1)
        self.assertEqual(boards[0].count, 2)  # フォールバック(主+1枚)

    def test_excludes_fill_only_sizes_30_50_100(self):
        """【仕様更新】30/50/100mmは幅補填専用サイズなので、丈不足強制追加の

        候補にもしない。ギャップ300mmに対し短辺100mm(同幅1000)は対象外に
        なり、主ボードを増やすフォールバックへ回る。
        """
        boards = [SelectedBoard(width=1000, length=1600, count=1, tag=alg.TAG_MAIN)]
        alg._force_add_for_length_shortage(
            boards, self.palette, self.product, [board(100, 1000)], None, sel_max_w=1000,
        )
        self.assertFalse(any(b.tag == alg.TAG_LENGTH_FILL for b in boards))
        self.assertEqual(boards[0].count, 2)


class DecideLengthCountWithCutTests(unittest.TestCase):
    """`decide_length_count_with_cut` の検証。

    判定しているのは「超過量」ではなく「最後の1枚に必要な残りの
    長さ」(lenRemainPL)。100mmを超えて残っていればカットして1枚
    足し、100mm以下なら切り捨てて無視する。
    """

    def test_example1_remainder_over_100mm_adds_a_cut_piece(self):
        # 1枚1000mm、対象丈2910mm: フルサイズ2枚(2000mm)、残り910mm。
        # 910 > 100 → カットして3枚目を910mmで追加
        count, need_cut, cut_eff = alg.decide_length_count_with_cut(1000, 2910)
        self.assertEqual(count, 3)
        self.assertTrue(need_cut)
        self.assertEqual(cut_eff, 910)

    def test_example2_remainder_50mm_is_discarded(self):
        # 1枚1000mm、対象丈2050mm: フルサイズ2枚、残り50mm。
        # 50 <= 100 → 追加しない(2枚のみ)
        count, need_cut, cut_eff = alg.decide_length_count_with_cut(1000, 2050)
        self.assertEqual(count, 2)
        self.assertFalse(need_cut)
        self.assertEqual(cut_eff, 1000)

    def test_example3_remainder_5mm_is_discarded(self):
        count, need_cut, cut_eff = alg.decide_length_count_with_cut(1000, 2005)
        self.assertEqual(count, 2)
        self.assertFalse(need_cut)

    def test_remainder_exactly_100mm_is_discarded(self):
        """境界値: ちょうど100mmは「超える」に含まれないので切り捨てる。"""
        count, need_cut, _ = alg.decide_length_count_with_cut(1000, 2100)
        self.assertEqual(count, 2)
        self.assertFalse(need_cut)

    def test_remainder_101mm_triggers_a_cut(self):
        """境界値: 101mmは閾値を超えるのでカットする。"""
        count, need_cut, cut_eff = alg.decide_length_count_with_cut(1000, 2101)
        self.assertEqual(count, 3)
        self.assertTrue(need_cut)
        self.assertEqual(cut_eff, 101)

    def test_exact_multiple_needs_no_extra_piece(self):
        """対象丈がちょうど割り切れるなら、追加の1枚は要らない。"""
        count, need_cut, _ = alg.decide_length_count_with_cut(1000, 2000)
        self.assertEqual(count, 2)
        self.assertFalse(need_cut)

    def test_board_longer_than_target_still_returns_one(self):
        """在庫1枚が対象丈より長い場合は、その1枚をカットして使う

        (フルサイズの上にもう1枚足す状況ではない)。
        """
        count, need_cut, cut_eff = alg.decide_length_count_with_cut(3000, 2000)
        self.assertEqual(count, 1)
        self.assertTrue(need_cut)
        self.assertEqual(cut_eff, 2000)

    def test_board_exactly_matches_target_needs_no_cut(self):
        count, need_cut, _ = alg.decide_length_count_with_cut(2000, 2000)
        self.assertEqual(count, 1)
        self.assertFalse(need_cut)


class DecideProtecOrientationTests(unittest.TestCase):
    """`decide_protec_orientation` の検証(VBA `DecideProtecOrientation`)。"""

    def test_no_cut_when_within_product_width(self):
        # 有効幅900は製品幅900と一致 → カット不要
        r = alg.decide_protec_orientation(900, 1800, 900, is_1p1216=False)
        self.assertTrue(r.valid)
        self.assertFalse(r.need_cut)
        self.assertEqual(r.cut_eff_width, 900)

    def test_width_cut_uses_product_width_minus_tolerance(self):
        """カットするなら、仕上がり幅は「製品幅」ではなく

        「製品幅-許容」まで削る(通常は-80mm)。
        """
        r = alg.decide_protec_orientation(1100, 1800, 900, is_1p1216=False)
        self.assertTrue(r.need_cut)
        self.assertEqual(r.cut_eff_width, 820)  # 900 - 80

    def test_1p1216_uses_10mm_tolerance(self):
        r = alg.decide_protec_orientation(1100, 1800, 900, is_1p1216=True)
        self.assertTrue(r.need_cut)
        self.assertEqual(r.cut_eff_width, 890)  # 900 - 10

    def test_prefers_orientation_closest_to_product_width(self):
        # 1000x600: 回転しない(幅1000, +100mm)方が、回転する(幅600, -300mm)
        # より製品幅900に近いので、回転しない向きが採用される
        r = alg.decide_protec_orientation(1000, 600, 900, is_1p1216=False)
        self.assertFalse(r.is_rotated)
        self.assertEqual(r.eff_length, 600)

    def test_eff_width_before_cut_is_the_uncut_effective_width(self):
        """比較・選定に使う「カット前の実効幅」は、仕上がり幅

        (`cut_eff_width`、カットが要る候補同士では常に同じ値に揃う)
        とは別に残しておく必要がある。混同すると、選定
        (`select_protec_lower_boards`)が「どちらが無駄が少ないか」を
        比較できなくなる。
        """
        r = alg.decide_protec_orientation(1100, 2000, 900, is_1p1216=False)
        self.assertEqual(r.eff_width_before_cut, 1100)  # カット前の実効幅
        self.assertEqual(r.cut_eff_width, 820)           # カット後の仕上がり幅

    def test_rotates_when_that_orientation_is_closer(self):
        # 600x1000: 回転する(幅1000, +100mm)方が、回転しない(幅600, -300mm)
        # より近いので、回転する向きが採用される
        r = alg.decide_protec_orientation(600, 1000, 900, is_1p1216=False)
        self.assertTrue(r.is_rotated)
        self.assertEqual(r.eff_length, 600)

    def test_invalid_when_neither_orientation_fits(self):
        # 両方向とも 900-80=820 に届かない
        r = alg.decide_protec_orientation(500, 700, 900, is_1p1216=False)
        self.assertFalse(r.valid)

    def test_prefers_no_cut_orientation_even_if_farther_than_cut_orientation(self):
        """カット不要な向きがあれば、絶対距離ではより近いカットが要る

        向きより優先する(設計判断の見直し: 以前は絶対距離が最小の
        向きを無条件で選んでおり、「製品幅よりわずかに超過(カットが
        要る)」が「余裕を持って不足(カット不要)」より僅差で近い
        というだけでカットが要る側を選んでしまうことがあった)。

        920x850(product_width=900, tol=80, min_w=820):
            向き1(回転なし): 幅920(超過20、カット要、距離20)
            向き2(回転あり): 幅850(不足50、カット不要、距離50)
        距離だけなら向き1(20<50)が勝つが、カット不要な向き2を優先する。
        """
        r = alg.decide_protec_orientation(920, 850, 900, is_1p1216=False)
        self.assertFalse(r.need_cut)
        self.assertTrue(r.is_rotated)
        self.assertEqual(r.cut_eff_width, 850)
        self.assertEqual(r.eff_length, 920)

    def test_picks_closest_among_cut_candidates_when_no_no_cut_option(self):
        # 両方とも超過(カット要)しかない場合は、従来どおり最も近い方を選ぶ
        r = alg.decide_protec_orientation(1000, 950, 900, is_1p1216=False)
        self.assertTrue(r.need_cut)
        # 1000(超過100)と950(超過50)なら950の方が近い
        self.assertTrue(r.is_rotated)
        self.assertEqual(r.eff_width_before_cut, 950)


class SelectProtecLowerBoardsTests(unittest.TestCase):
    """`select_protec_lower_boards` の検証(VBA `SelectProtecLowerBoards`)。

    パレット幅ではなく**製品幅**を基準に在庫全件を評価し、最も条件に
    近い1件を採用する。丈方向は枚数を増やすだけでカバーする。
    """

    def setUp(self) -> None:
        self.product = ProductSize(width=900, length=1800)

    def test_picks_the_board_closest_to_product_width(self):
        """比較は `eff_width_before_cut`(カット前の実効幅)で行う。

        `cut_eff_width`(カット後の仕上がり幅)で比べると、両方とも
        カットが必要な候補では同じ値(製品幅-許容)に揃ってしまい、
        1100幅(製品幅900に対して+200mm、無駄が大きい)と950幅
        (+50mm、無駄が少ない)の差が見えなくなる。
        """
        available = [board(1100, 2000), board(950, 2000)]
        boards, result = alg.select_protec_lower_boards(
            available, self.product, make_palette(1000, 2000), is_1p1216=False)
        self.assertEqual(len(boards), 1)
        self.assertEqual((boards[0].width, boards[0].length), (950, 2000))
        self.assertTrue(result.valid)

    def test_細い板は主ボードにしない(self):
        """VBA冒頭の `ab.width <= 100 Or ab.length <= 100` が抜けていた。

        補填に使う30/50/100の帯は、寝かせれば幅の条件を満たしてしまう
        (100x3000 を回すと有効幅3000)。主ボードに選ぶと丈方向が帯の
        厚みぶんずつになり、何十枚も並べる解になる。他のPASSでは元から
        外していたので、ここだけ抜けていた。
        """
        thin = [board(100, 3000), board(50, 3000)]
        boards, result = alg.select_protec_lower_boards(
            thin, self.product, make_palette(1000, 2000), is_1p1216=False)
        self.assertEqual(boards, [])
        self.assertFalse(result.valid)

    def test_細い板があっても普通の板は選べる(self):
        """外すのは細い板だけ。**選べるものまで道連れにしない。**"""
        available = [board(100, 3000), board(950, 2000)]
        boards, _result = alg.select_protec_lower_boards(
            available, self.product, make_palette(1000, 2000), is_1p1216=False)
        self.assertEqual([(b.width, b.length) for b in boards], [(950, 2000)])

    def test_ちょうど100は外す(self):
        """VBAは `<= 100`。101から使う。"""
        for width, expect in ((100, []), (101, [(101, 3000)])):
            with self.subTest(width=width):
                boards, _r = alg.select_protec_lower_boards(
                    [board(width, 3000)], ProductSize(width=3000, length=1800),
                    make_palette(3100, 2000), is_1p1216=False)
                self.assertEqual([(b.width, b.length) for b in boards], expect)

    def test_count_covers_product_length_by_stacking(self):
        """**枚数を決める基準は製品丈。** 端数が100mm以下なら切り捨てる。

        製品丈1800・1枚900mmなら、ちょうど2枚で端数0。
        """
        available = [board(900, 900)]
        boards, result = alg.select_protec_lower_boards(
            available, self.product, make_palette(1000, 2000), is_1p1216=False)
        self.assertEqual(boards[0].count, 2)
        self.assertEqual(result.count, 2)
        self.assertFalse(result.need_length_cut)
        self.assertFalse(result.len_cut_optional)

    def test_length_cut_is_required_when_it_would_stick_out(self):
        """もう1枚足すとパレットからはみ出す ── **切るしかない。**

        製品丈1800・1枚1000mmなら、フル1枚で残り800mm。もう1枚
        足すと2000mmで、パレット丈1700(許容後1870)を超える。
        はみ出したままにはできないので、最後の1枚を800mmへ切る。

        幅はちょうど(900)でカット不要なので、「カット前提」タグが
        付くのは**丈カットのため**だと分かる。
        """
        available = [board(900, 1000)]
        boards, result = alg.select_protec_lower_boards(
            available, self.product, make_palette(1000, 1700), is_1p1216=False)
        self.assertEqual(boards[0].count, 2)
        self.assertTrue(result.need_length_cut)
        self.assertFalse(result.len_cut_optional)
        self.assertEqual(result.length_cut_eff, 800)
        self.assertEqual((result.len_normal_cnt, result.len_cut_cnt), (1, 1))
        self.assertEqual(boards[0].tag, alg.TAG_CUT_PREMISE)

    def test_length_cut_is_optional_when_it_still_fits_the_pallet(self):
        """もう1枚足してもパレットに収まる ── **切るかどうかは現場が選ぶ。**

        製品丈は超えるが、はみ出してはいない。配置図は「切らない姿」
        (`need_length_cut=False`)で描き、切断依頼書だけが「もし切るなら」
        の内訳(`len_opt_*`)を出す。
        """
        available = [board(900, 1000)]
        boards, result = alg.select_protec_lower_boards(
            available, self.product, make_palette(1000, 2000), is_1p1216=False)
        self.assertEqual(boards[0].count, 2)
        self.assertFalse(result.need_length_cut, "図にカット線は出さない")
        self.assertTrue(result.len_cut_optional)
        self.assertEqual((result.len_normal_cnt, result.len_cut_cnt), (2, 0))
        self.assertEqual((result.len_opt_normal_cnt, result.len_opt_cut_cnt,
                          result.len_opt_cut_eff), (1, 1, 800))
        self.assertEqual(boards[0].tag, alg.TAG_MAIN,
                         "任意カットで「カット前提」にはしない")

    def test_prefers_a_board_that_fits_the_pallet_length(self):
        """**パレット丈に収まるかが第1の評価軸。**

        カットの向きは1つに減らしたい ── 幅カットだけで済む候補が
        あるのに、幅も丈も切る候補を選ぶ理由は無い(現場の指示)。
        900×600 は製品幅ちょうどで幅カット不要、3枚(1800)でも
        パレット内。950×1000 は幅の近さでは勝つが、丈が収まらない。
        """
        available = [board(950, 1000), board(900, 600)]
        boards, result = alg.select_protec_lower_boards(
            available, self.product, make_palette(1000, 1700), is_1p1216=False)
        self.assertEqual((boards[0].width, boards[0].length), (900, 600))
        self.assertFalse(result.need_length_cut)

    def test_width_cut_needed_sets_cut_premise_tag(self):
        """幅カットが必要なときは「カット前提」タグにする(早期にカット

        前提へ倒す設計。現場の声:「プロテックボードは通常ボードより
        種類が少ないので、通常ボードよりもカット前提のフラグをはやく
        立てるべき」)。
        """
        available = [board(1100, 2000)]  # 有効幅1100 > 製品幅900 → カット要
        boards, result = alg.select_protec_lower_boards(
            available, self.product, make_palette(1000, 2000), is_1p1216=False)
        self.assertTrue(result.need_cut)
        self.assertEqual(boards[0].tag, alg.TAG_CUT_PREMISE)

    def test_no_cut_needed_keeps_main_tag(self):
        available = [board(900, 2000)]  # 有効幅900 = 製品幅900 → カット不要
        boards, result = alg.select_protec_lower_boards(
            available, self.product, make_palette(1000, 2000), is_1p1216=False)
        self.assertFalse(result.need_cut)
        self.assertEqual(boards[0].tag, alg.TAG_MAIN)

    def test_no_matching_stock_returns_invalid(self):
        available = [board(300, 400)]
        boards, result = alg.select_protec_lower_boards(
            available, self.product, make_palette(1000, 2000), is_1p1216=False)
        self.assertEqual(boards, [])
        self.assertFalse(result.valid)


class ApplyProtecRulesToLowerListTests(unittest.TestCase):
    """`apply_protec_rules_to_lower_list` の検証(VBA

    `ApplyProtecRulesToLowerList`)。手動で増減した下用行に、配置直前で
    `ProtecCutResult` を後付けする。以前は丈カット判定
    (`decide_length_count_with_cut`)を一切行わず、手動で入れた枚数を
    そのまま `count` に写すだけだった。
    """

    def setUp(self) -> None:
        self.product = ProductSize(width=1090, length=1900)

    def test_recomputes_length_cut_for_manual_row(self):
        """手動追加行にも丈カット判定が効く。

        製品丈1900・1枚1000mmなら、フル1枚で残り900mm。もう1枚
        足すと2000mmで、パレット丈1800(許容後1980)を超えるので
        切るしかない ── 最後の1枚を900mmへ。
        """
        lower = [SelectedBoard(width=1090, length=1000, count=2, tag="")]
        result = alg.apply_protec_rules_to_lower_list(
            lower, self.product, make_palette(1150, 1800), is_1p1216=True)
        self.assertTrue(result.valid)
        self.assertEqual(result.count, 2)
        self.assertTrue(result.need_length_cut)
        self.assertEqual(result.length_cut_eff, 900)
        # 行自体の枚数も確定値に合わせて更新される(表示上の枚数と
        # 確定値が食い違わないようにするため)
        self.assertEqual(lower[0].count, 2)

    def test_no_length_cut_when_remainder_within_threshold(self):
        lower = [SelectedBoard(width=1090, length=1000, count=2, tag="")]
        result = alg.apply_protec_rules_to_lower_list(
            lower, self.product, make_palette(1150, 2050), is_1p1216=True)
        self.assertFalse(result.need_length_cut)
        self.assertEqual(result.count, 2)

    def test_auto_selected_rows_are_skipped(self):
        """自動選定済みの行(タグ"主"/"カット前提")は対象外。

        そちらは既に `select_protec_lower_boards` が確定させているので、
        ここで上書きすると選定結果と食い違う。
        """
        lower = [SelectedBoard(width=1090, length=1000, count=5, tag=alg.TAG_MAIN)]
        result = alg.apply_protec_rules_to_lower_list(
            lower, self.product, make_palette(1150, 2910), is_1p1216=True)
        self.assertFalse(result.valid)
        self.assertEqual(lower[0].count, 5)   # 触られていない

    def test_empty_list_returns_invalid(self):
        result = alg.apply_protec_rules_to_lower_list(
            [], self.product, make_palette(1000, 2000), is_1p1216=False)
        self.assertFalse(result.valid)

    def test_uses_the_same_threshold_as_select_protec_lower_boards(self):
        """選定(`select_protec_lower_boards`)と手動追加後の後付け

        (`apply_protec_rules_to_lower_list`)が、同じ在庫・同じパレット丈
        なら同じ結果になることを確かめる(計算式が2か所で別々に
        実装されて食い違う、という事故を防ぐ)。
        """
        available = [board(1090, 1000)]
        palette = make_palette(1150, 2910)
        auto_boards, auto_result = alg.select_protec_lower_boards(
            available, self.product, palette, is_1p1216=True)

        manual = [SelectedBoard(width=1090, length=1000, count=1, tag="")]
        manual_result = alg.apply_protec_rules_to_lower_list(
            manual, self.product, palette, is_1p1216=True)

        self.assertEqual(auto_result.count, manual_result.count)
        self.assertEqual(auto_result.need_length_cut, manual_result.need_length_cut)
        self.assertEqual(auto_result.length_cut_eff, manual_result.length_cut_eff)


class IndustryStandardShortcutTests(unittest.TestCase):
    """業界標準サイズ("1×2"/"4×8")のショートカット選定。

    発端: 製品1002×2002で、業界標準サイズ"1×2"(1000×2000、カット
    不要)ではなく"1030×1520"(差0mmだが継ぎ足しカットが必要)が
    選ばれていた。原因はPASS1が「幅の一致度が最も高い候補を、
    1件見つけた時点で即採用」という設計だったため、"1030×1520"が
    候補リストの先頭に来るとそこで打ち切られ、後方にある
    "1000×2000"は評価すらされなかった。
    """

    def setUp(self) -> None:
        self.palette = make_palette(1150, 2200)

    def test_1x2_range_picks_standard_board_over_first_candidate(self):
        product = ProductSize(width=1002, length=2002)
        available = [
            board(1030, 1520),   # 候補リストの先頭。継ぎ足しカットが必要
            board(1000, 2000),   # 業界標準"1×2"。カット不要
        ]
        r = alg.select_lower_boards(available, self.palette, product)
        self.assertEqual((r.boards[0].width, r.boards[0].length), (1000, 2000))
        self.assertEqual(r.boards[0].tag, alg.TAG_MAIN)

    def test_4x8_range_picks_standard_board(self):
        product = ProductSize(width=1251, length=2501)
        palette = make_palette(1300, 2700)
        available = [board(1280, 1900), board(1250, 2500)]
        r = alg.select_lower_boards(available, palette, product)
        self.assertEqual((r.boards[0].width, r.boards[0].length), (1250, 2500))

    def test_board_can_be_stored_rotated(self):
        """在庫の向き(幅/丈が入れ替わっていても)関わらず見つける。"""
        product = ProductSize(width=1002, length=2002)
        available = [board(2000, 1000)]   # 1000x2000の逆向き
        r = alg.select_lower_boards(available, self.palette, product)
        self.assertEqual({r.boards[0].width, r.boards[0].length}, {1000, 2000})

    def test_outside_range_does_not_trigger_shortcut(self):
        """製品サイズが範囲外なら、通常のPASS1がそのまま動く

        (ショートカットは発動しない)。
        """
        product = ProductSize(width=1010, length=2010)   # "1×2"の範囲外
        available = [board(1000, 2000), board(1030, 1520)]
        r = alg.select_lower_boards(available, self.palette, product)
        # 通常のPASS1(幅一致優先、先頭優先)が動くので、標準サイズが
        # 自動的に選ばれるとは限らない。ここでは「ショートカット専用の
        # 決め打ちロジック(枚数計算式)を通っていない」ことだけを確かめる
        self.assertNotEqual(r.state.remaining_len, 0)

    def test_stock_not_available_falls_back_to_normal_pass1(self):
        """範囲内でも、対応するボードサイズの在庫が無ければ

        通常のPASS1にフォールバックする。
        """
        product = ProductSize(width=1002, length=2002)
        available = [board(1030, 1520)]   # 標準サイズの在庫が無い
        r = alg.select_lower_boards(available, self.palette, product)
        self.assertEqual((r.boards[0].width, r.boards[0].length), (1030, 1520))

    def test_protec_mode_takes_priority_over_shortcut_even_if_protec_search_fails(self):
        """プロテックモードでは、SelectProtecLowerBoardsが適合する在庫を

        見つけられずに通常フローへフォールバックしても、ショートカットを
        使ってはいけない(上用側は元から `is_protec_mode` でガードして
        いたが、下用側だけそのガードが抜けていたバグ修正)。

        ガードが無いと、下用だけプロテックの業務ルールを一切通さずに
        標準サイズを即採用し、上用は(protec_resultが無効なので)独自に
        通常選定へ進むため、下用と上用が無関係な別ボードになる
        (「プロテックなのに上下が一致しない」不具合)。

        パレット幅(200mm)では標準サイズ(1000×2000)は物理的に置けない
        ため、ガードが効いていれば通常選定(PASS1-3→狭幅パレット)に
        回り、この板は採用されない。ガードが無ければ、パレット幅を
        一切見ないショートカットがそのまま採用してしまう。
        """
        product = ProductSize(width=1000, length=2000)   # "1×2"の範囲内
        palette = make_palette(200, 2600)                # 標準サイズが物理的に収まらない
        available = [board(1000, 2000)]

        with mock.patch.object(
            alg, "select_protec_lower_boards",
            return_value=([], alg.ProtecCutResult(valid=False)),
        ):
            r = alg.select_lower_boards(
                available, palette, product,
                is_protec_mode=True, is_protec_1p1216=True,
            )

        self.assertEqual(r.boards, [])
        self.assertTrue(r.needs_narrow)

    def test_upper_boards_also_use_the_shortcut(self):
        """上用(`select_upper_boards`)にも同じ判定を適用する。"""
        product = ProductSize(width=1002, length=2002)
        available = [board(1030, 1520), board(1000, 2000)]
        lower = [SelectedBoard(width=1030, length=1520, count=1, tag=alg.TAG_MAIN)]
        r = alg.select_upper_boards(lower, available, self.palette, product)
        self.assertEqual((r.boards[0].width, r.boards[0].length), (1000, 2000))

    def test_shared_mode_takes_priority_over_shortcut(self):
        """上下共用モードは、ショートカットより優先される

        (通常モードでのみ発動する、という指定どおり)。
        """
        product = ProductSize(width=1002, length=2002)
        available = [board(1000, 2000)]
        lower = [SelectedBoard(width=1030, length=1520, count=1, tag=alg.TAG_MAIN)]
        r = alg.select_upper_boards(
            lower, available, self.palette, product, last_hosozai="ザラ板")
        self.assertEqual(r.mode, "共用")
        self.assertEqual((r.boards[0].width, r.boards[0].length), (1030, 1520))

    def test_protec_mode_takes_priority_over_shortcut(self):
        """プロテックモードも、ショートカットより優先される。"""
        product = ProductSize(width=1002, length=2002)
        available = [board(1000, 2000)]
        lower_boards, protec_result = alg.select_protec_lower_boards(
            [board(1030, 1520)], product, self.palette, is_1p1216=False)
        r = alg.select_upper_boards(
            lower_boards, available, self.palette, product,
            is_protec_mode=True, protec_result=protec_result)
        self.assertEqual(r.mode, "プロテック")

    def test_length_cut_is_recorded_when_needed(self):
        """ショートカットで選んだボードでも、100mm閾値による丈カット

        判定は行われる(パレット丈を短くして丈カットを誘発する)。
        """
        product = ProductSize(width=1002, length=2002)
        palette = make_palette(1150, 2050)   # 1000mm×2枚=2000、残り50→切り捨て
        available = [board(1000, 2000)]
        r = alg.select_lower_boards(available, palette, product)
        self.assertEqual(r.length_cut_info, {})

        palette2 = make_palette(1150, 2101)  # 残り101→カット
        r2 = alg.select_lower_boards(available, palette2, product)
        self.assertIn("L_1000x2000", r2.length_cut_info)


class Shortcut5x8Tests(unittest.TestCase):
    """"5×8"専用サイズショートカット(上用・下用)。

    "1×2"/"4×8"と違い1枚では収まらないため、主+補填の複数枚構成を
    直接組み立てる。使用する全サイズが在庫にあるときだけ発動し、
    1つでも欠品していれば一般ロジックへ落とす。
    """

    def setUp(self) -> None:
        self.palette = make_palette(1600, 2600)
        self.product = ProductSize(width=1520, length=2502)  # 5×8の範囲内

    def test_lower_shortcut_builds_main_plus_length_fill(self):
        available = [board(1030, 1520), board(450, 1520)]
        r = alg.select_lower_boards(available, self.palette, self.product)
        self.assertEqual(len(r.boards), 2)
        main, fill = r.boards
        self.assertEqual((main.width, main.length, main.count, main.tag),
                         (1030, 1520, 2, alg.TAG_MAIN))
        self.assertEqual((fill.width, fill.length, fill.count, fill.tag),
                         (450, 1520, 1, alg.TAG_LENGTH_FILL))

    def test_lower_falls_back_when_fill_size_missing(self):
        available = [board(1030, 1520)]  # 450x1520が欠品
        r = alg.select_lower_boards(available, self.palette, self.product)
        self.assertNotEqual(
            [(b.width, b.length) for b in r.boards], [(1030, 1520), (450, 1520)])

    def test_upper_shortcut_builds_main_plus_two_width_fills(self):
        available = [board(1250, 2500), board(100, 2500), board(30, 2500)]
        lower = [SelectedBoard(width=1030, length=1520, count=2, tag=alg.TAG_MAIN)]
        r = alg.select_upper_boards(lower, available, self.palette, self.product)
        self.assertEqual(len(r.boards), 3)
        main, fill1, fill2 = r.boards
        self.assertEqual((main.width, main.length, main.count), (1250, 2500, 1))
        self.assertEqual((fill1.width, fill1.length, fill1.count, fill1.tag),
                         (100, 2500, 2, alg.TAG_WIDTH_FILL))
        self.assertEqual((fill2.width, fill2.length, fill2.count, fill2.tag),
                         (30, 2500, 1, alg.TAG_WIDTH_FILL))

    def test_upper_falls_back_when_any_fill_size_missing(self):
        available = [board(1250, 2500), board(100, 2500)]  # 30x2500が欠品
        lower = [SelectedBoard(width=1030, length=1520, count=2, tag=alg.TAG_MAIN)]
        r = alg.select_upper_boards(lower, available, self.palette, self.product)
        self.assertNotEqual(
            [(b.width, b.length) for b in r.boards],
            [(1250, 2500), (100, 2500), (30, 2500)])

    def test_outside_5x8_range_does_not_trigger_shortcut(self):
        # 幅が範囲外。ショートカット専用サイズが在庫に無いので、
        # 発動していれば候補ゼロで狭幅パレット扱いになるはず
        product = ProductSize(width=1490, length=2502)
        available = [board(900, 1600)]
        r = alg.select_lower_boards(available, self.palette, product)
        self.assertFalse(r.needs_narrow)
        self.assertEqual((r.boards[0].width, r.boards[0].length), (900, 1600))


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

    def test_protec_mode_prefers_orientation_closest_to_product_width(self):
        """VBA `DecideProtecOrientation` の判定: 製品幅に最も近い向きを

        選び、超過分はカットする(超過を避けて大きく不足する向きを
        選ぶのではない)。下用1000x600 は、回転しない向き(幅1000。
        製品幅900に対して+100mm、カットで丸める)の方が、回転する
        向き(幅600。-300mmで大きく不足)より製品幅に近いので、
        1000の向きが採用され、プロテックとして成立する。
        カット後の仕上がり幅は「製品幅そのもの」ではなく
        「製品幅-許容」(通常80mm)まで削る: 900-80=820。
        """
        r = alg.select_upper_boards(
            self.lower, [board(880, 1900)], self.palette, self.product, is_protec_mode=True,
        )
        self.assertEqual(r.mode, "プロテック")
        self.assertTrue(r.protec_result.need_cut)
        self.assertEqual(r.protec_result.cut_eff_width, 820)

    def test_protec_mode_applies_when_lower_width_fits(self):
        lower = [SelectedBoard(width=880, length=600, count=2, tag=alg.TAG_MAIN)]
        # 880 >= 900-80=820 なのでプロテックモード成立。
        # 丈カウントは上用専用に製品丈基準で決め直す(下用の`count`を
        # そのまま引き継がない): 製品丈1900 // 600 = 3枚、残り100mmは
        # 閾値ちょうどなので切り捨てて3枚のまま
        r = alg.select_upper_boards(
            lower, [board(880, 1900)], self.palette, self.product, is_protec_mode=True,
        )
        self.assertEqual(r.mode, "プロテック")
        self.assertEqual((r.boards[0].width, r.boards[0].count), (880, 3))
        self.assertFalse(r.protec_result.need_length_cut)

    def test_protec_1p1216_uses_tighter_tolerance(self):
        lower = [SelectedBoard(width=880, length=600, count=2, tag=alg.TAG_MAIN)]
        # 1P1216 は許容-10mm → 880 >= 900-10=890 は不成立 → 通常選定へ
        r = alg.select_upper_boards(
            lower, [board(880, 1900)], self.palette, self.product,
            is_protec_mode=True, is_protec_1p1216=True,
        )
        self.assertNotEqual(r.mode, "プロテック")

    def test_protec_1p1216_applies_within_10mm(self):
        """1P1216 の許容は10mm(精度の都合で本来の-20mmより厳しめ)。"""
        lower = [SelectedBoard(width=892, length=600, count=2, tag=alg.TAG_MAIN)]
        # 892 >= 900-10=890 は成立 → プロテック成立
        r = alg.select_upper_boards(
            lower, [board(892, 1900)], self.palette, self.product,
            is_protec_mode=True, is_protec_1p1216=True,
        )
        self.assertEqual(r.mode, "プロテック")

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
        # 丈補填フェーズでは主ボードを増やさない。在庫は主と同じ900x300のみだが、
        # 【VBAからの修正】タグ限定(sfSkipCap/sfExists)により主ボードの枚数を
        # 汚染せず、最後の「丈不足強制追加」で "丈補填" タグの別行として
        # 追加される(主ボードの上限枚数を誤って見て「候補なし」と
        # 誤認し、主ボードを不要に増やしてしまうことを防ぐ)。
        product = ProductSize(width=900, length=1900)
        r = alg.select_upper_boards(
            self.lower, [board(900, 300)], self.palette, product,
        )
        self.assertEqual(r.boards[0].count, 6)  # 主ボードは汚染されない
        length_fill_rows = [b for b in r.boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(length_fill_rows), 1)
        self.assertEqual(length_fill_rows[0].count, 1)

    def test_length_shortage_prefers_a_small_board_over_bumping_main(self):
        # 丈ギャップ100mmを埋められる小型在庫(100x800)があれば、
        # 主ボードを増やさずそれを足す
        product = ProductSize(width=900, length=1900)
        r = alg.select_upper_boards(
            self.lower, [board(900, 300), board(100, 800)], self.palette, product,
        )
        self.assertEqual(r.boards[0].count, 6)
        self.assertTrue(any(b.tag == alg.TAG_LENGTH_FILL for b in r.boards))


class UpperLengthFillFixedCalcTests(unittest.TestCase):
    """`_run_upper_length_fill` の「100mm板をCeiling(丈残/100)枚、最大4枚」

    という単純計算への置き換えの検証(VBA仕様更新)。旧来の「在庫全体から
    短辺の近さで探す」5回ループは廃止され、100mm在庫の有無だけで決まる。
    """

    def setUp(self) -> None:
        self.product = ProductSize(width=900, length=2000)
        # 主900x190 x10 → 丈カバー1900。丈残は100(400以下なので主は増えない)
        self.main = SelectedBoard(900, 190, 10, alg.TAG_MAIN)

    def test_needed_count_is_ceiling_of_remaining_over_100(self):
        # 丈残100mm → Ceiling(100/100)=1枚
        boards = [self.main]
        available = [board(100, 800)]
        alg._run_upper_length_fill(boards, self.product, available)

        length_fill = next(b for b in boards if b.tag == alg.TAG_LENGTH_FILL)
        self.assertEqual((length_fill.width, length_fill.length), (100, 800))
        self.assertEqual(length_fill.count, 1)

    def test_same_width_loop_runs_first_when_gap_exceeds_400(self):
        # 主900x190 x9 → 丈カバー1710、製品丈2390に対し丈残680(400超)。
        # 【仕様更新】丈残400超のときはもう主ボードを増やさず、同幅(900)
        # の候補(500x900)を探して丈残を180まで縮めてから、100mm固定で
        # 残りを埋める(下用の丈補填反復ループと同じ規則)
        product = ProductSize(width=900, length=2390)
        boards = [SelectedBoard(900, 190, 9, alg.TAG_MAIN)]
        available = [board(100, 800), board(500, 900)]
        alg._run_upper_length_fill(boards, product, available)

        self.assertEqual(boards[0].count, 9)  # 主ボードは増えない
        same_width_row = next(b for b in boards if (b.width, b.length) == (500, 900))
        self.assertEqual(same_width_row.tag, alg.TAG_LENGTH_FILL)
        fixed_100_row = next(b for b in boards if (b.width, b.length) == (100, 800))
        self.assertEqual(fixed_100_row.count, 2)  # ceil(180/100)=2

    def test_same_width_loop_excludes_fill_only_sizes(self):
        """【仕様更新】30/50/100mmは丈残400以下用の補填専用サイズであり、

        同幅探索ループ(丈残400超が対象)の候補にしてはならない。除外しないと
        同じ板が5回連続で選ばれ続け、上限まで使い切ってしまう
        (現場の声:「ループが同じ50mm板を何度でも選べてしまう」)。
        同幅(900)の50x900しか無い場合、ループは何も採用できずに終わり、
        丈残は400超のまま丈不足強制追加ブロックへ引き継がれる。
        """
        product = ProductSize(width=900, length=2390)
        boards = [SelectedBoard(900, 190, 9, alg.TAG_MAIN)]  # 丈カバー1710、丈残680
        available = [board(50, 900)]
        alg._run_upper_length_fill(boards, product, available)

        self.assertFalse(any(b.tag == alg.TAG_LENGTH_FILL for b in boards))

    def test_merges_into_existing_length_fill_row_of_same_size(self):
        # 主900x190 x9 → 丈カバー1710。既存"丈補填"100x800 x1が短辺(100)分
        # 寄与して1810、丈残190mm → Ceiling(190/100)=2枚を既存行に積む
        main = SelectedBoard(900, 190, 9, alg.TAG_MAIN)
        existing = SelectedBoard(100, 800, 1, alg.TAG_LENGTH_FILL)
        boards = [main, existing]
        available = [board(100, 800)]
        alg._run_upper_length_fill(boards, self.product, available)

        # 別行を新規に作らず、既存の"丈補填"行にneeded(=2)を積む
        length_fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(length_fill_rows), 1)
        self.assertEqual(length_fill_rows[0].count, 3)

    def test_no_100mm_stock_defers_to_force_add_block(self):
        # 幅・丈のどちらにも100mmを持つ在庫が無ければ何も追加しない
        # (後続の「丈不足強制追加」ブロックに委ねる)
        boards = [self.main]
        available = [board(30, 1600), board(900, 300)]
        alg._run_upper_length_fill(boards, self.product, available)

        self.assertFalse(any(b.tag == alg.TAG_LENGTH_FILL for b in boards))


class ForceAddForUpperLengthShortageTests(unittest.TestCase):
    """`_force_add_for_upper_length_shortage`(sfSkipCap/sfExists)のタグ限定バグ修正の検証。

    バグの内容: タグを見ずに幅・丈だけでマッチしていたため、同サイズの
    「主」や「幅補填」ボードが存在すると、その上限判定やマージ対象を
    誤って見てしまっていた。
    """

    def setUp(self) -> None:
        self.product = ProductSize(width=900, length=2000)
        # 主900x190 x10 → 丈カバー1900。丈残は100
        self.main = SelectedBoard(900, 190, 10, alg.TAG_MAIN)

    def test_skip_cap_check_ignores_wrong_tag_row(self):
        """候補と同サイズの"幅補填"行が上限でも、丈不足強制追加には無関係(sfSkipCap)。

        候補は主ボード(900x190、effW=900)と同幅(長辺900)の150x900。
        """
        decoy = SelectedBoard(150, 900, alg.LENGTH_FILL_COUNT_CAP, alg.TAG_WIDTH_FILL)
        boards = [self.main, decoy]
        alg._force_add_for_upper_length_shortage(boards, self.product, [board(150, 900)], None)

        self.assertEqual(self.main.count, 10)  # フォールバック(主+1枚)は発動しない
        self.assertEqual(decoy.count, alg.LENGTH_FILL_COUNT_CAP)  # 汚染されない
        length_fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(length_fill_rows), 1)

    def test_merge_ignores_wrong_tag_row(self):
        """マージ(sfExists)も"丈補填"タグの行だけを対象にする。"""
        decoy = SelectedBoard(150, 900, 1, alg.TAG_WIDTH_FILL)
        boards = [self.main, decoy]
        alg._force_add_for_upper_length_shortage(boards, self.product, [board(150, 900)], None)

        self.assertEqual(decoy.count, 1)  # "幅補填"行は汚染されない
        length_fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(length_fill_rows), 1)
        self.assertEqual(length_fill_rows[0].count, 1)

    def test_candidate_must_match_main_boards_effective_width(self):
        """【仕様更新】候補は主ボードと同幅(長辺完全一致)のみ対象。

        150x1600は長辺1600で主(effW=900)と一致しないため対象外となり、
        主ボードを増やすフォールバックへ回る。
        """
        boards = [self.main]
        alg._force_add_for_upper_length_shortage(boards, self.product, [board(150, 1600)], None)

        self.assertFalse(any(b.tag == alg.TAG_LENGTH_FILL for b in boards))
        self.assertEqual(self.main.count, 11)  # フォールバック(主+1枚)が発動する

    def test_excludes_fill_only_sizes_30_50_100(self):
        """【仕様更新】30/50/100mmは幅補填専用サイズなので、丈不足強制追加の

        候補にもしない。同幅(900)の100x900しか在庫が無い場合は対象外になり、
        主ボードを増やすフォールバックへ回る。
        """
        boards = [self.main]
        alg._force_add_for_upper_length_shortage(boards, self.product, [board(100, 900)], None)

        self.assertFalse(any(b.tag == alg.TAG_LENGTH_FILL for b in boards))
        self.assertEqual(self.main.count, 11)


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


class WideCutLengthFillTests(unittest.TestCase):
    """`_wide_cut_length_fill`(カット前提選定後の丈補填)の検証。

    **この関数は公開経路からは到達しない。** `_pick_wide_cut_board` の
    枚数が `ceil(製品丈 / 短辺)` なので `短辺 × 枚数 >= 製品丈` が常に
    成り立ち、丈残は0以下にしかならない(VBA側の `cntL` も同じ天井計算)。
    枚数の決め方が変わったときに仕様どおり動くことを保証するため、
    ここでは関数を直接呼んで検証する。
    """

    def _fill(self, boards, available, *, gap, short_side=300, target_width=1000):
        """丈残がちょうど `gap` になる条件で呼ぶ(枚数1・製品丈=短辺+gap)。"""
        product = ProductSize(width=900, length=short_side + gap)
        alg._wide_cut_length_fill(
            boards, short_side, 1, product, available, target_width)

    def test_gap_over_400_now_searches_a_fill_board_first(self):
        """撤去した「丈残400超 → 主+1枚」が主ボードを増やさないこと。

        丈残500。以前は400超なので問答無用で主ボードが1枚増えていたが、
        いまは丈残に収まる補填ボードの探索が先に走る。
        """
        main = SelectedBoard(width=300, length=1200, count=1, tag=alg.TAG_CUT_PREMISE)
        boards = [main]
        # 短辺500がちょうど丈残を埋める
        self._fill(boards, [board(500, 1200)], gap=500)
        self.assertEqual(main.count, 1)          # 主ボードは増えない
        self.assertEqual(len(boards), 2)
        self.assertEqual(boards[1].tag, alg.TAG_LENGTH_FILL)
        self.assertEqual(min(boards[1].width, boards[1].length), 500)

    def test_picks_the_largest_short_side_that_fits(self):
        """丈残に収まる中で最も短辺が大きいものを採る(VBA `lfS > lfBestShort`)。"""
        main = SelectedBoard(width=300, length=1200, count=1, tag=alg.TAG_CUT_PREMISE)
        boards = [main]
        # 丈残500。100/300/500 のうち500が採られる(最初に当たる100ではない)
        self._fill(boards, [board(100, 1200), board(300, 1200), board(500, 1200)], gap=500)
        self.assertEqual(min(boards[1].width, boards[1].length), 500)
        self.assertEqual(main.count, 1)

    def test_candidate_must_fit_within_three_mm_tolerance(self):
        """丈残+3mmを超える短辺は候補にしない(VBA `lfS <= lLenGap + 3`)。"""
        main = SelectedBoard(width=600, length=1200, count=1, tag=alg.TAG_CUT_PREMISE)
        boards = [main]
        # 丈残500に対し短辺504は +3 を超えるので不採用 → 主ボード+1枚に倒れる
        self._fill(boards, [board(504, 1200)], gap=500, short_side=600)
        self.assertEqual(main.count, 2)
        self.assertEqual(len(boards), 1)

    def test_falls_back_to_main_when_no_candidate_fits(self):
        main = SelectedBoard(width=600, length=1200, count=1, tag=alg.TAG_CUT_PREMISE)
        boards = [main]
        # 短辺10未満は候補外(NARROW_MIN_SHORT_SIDE)
        self._fill(boards, [board(5, 1200)], gap=500, short_side=600)
        self.assertEqual(main.count, 2)
        self.assertEqual(len(boards), 1)

    def test_merges_into_an_existing_row_of_the_same_size(self):
        main = SelectedBoard(width=300, length=1200, count=1, tag=alg.TAG_CUT_PREMISE)
        fill = SelectedBoard(width=250, length=1200, count=1, tag=alg.TAG_LENGTH_FILL)
        boards = [main, fill]
        # 丈残500 → 250を2回採って埋める(新しい行は増えない)
        self._fill(boards, [board(250, 1200)], gap=500)
        self.assertEqual(len(boards), 2)
        self.assertEqual(fill.count, 3)
        self.assertEqual(main.count, 1)

    def test_no_fill_when_the_gap_is_already_closed(self):
        main = SelectedBoard(width=300, length=1200, count=1, tag=alg.TAG_CUT_PREMISE)
        boards = [main]
        self._fill(boards, [board(500, 1200)], gap=0)
        self.assertEqual(boards, [main])
        self.assertEqual(main.count, 1)


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


class LowerWideCutTests(unittest.TestCase):
    """下用のカット前提選定(`select_boards_for_wide_lower`)。上用の

    `UpperWideCutTests` と対称。以前は下用側だけ丈カットの記録漏れ
    があり、切断依頼書に反映されなかった(現場の声:「3枚出るはずが
    分割されずに出力される」)。
    """

    def test_records_length_cut_when_overshooting(self):
        palette = make_palette(1150, 2650)
        product = ProductSize(width=1122, length=2502)
        available = [board(1030, 1520)]
        r = alg.select_boards_for_wide_lower(available, palette, product)
        # 1030x1520(最大辺1520 >= パレット幅1150)がヒットしてカット前提に
        # なる。丈補填で3枚(1030×3=3090)になり、パレット丈2650+3を
        # 超えるので丈カットが記録される
        self.assertEqual(r.boards[0].tag, alg.TAG_CUT_PREMISE)
        self.assertIn("L_1030x1520", r.length_cut_info)

    def test_no_record_when_within_pallet_length(self):
        palette = make_palette(1150, 4000)
        product = ProductSize(width=1122, length=2502)
        available = [board(1030, 1520)]
        r = alg.select_boards_for_wide_lower(available, palette, product)
        self.assertEqual(r.length_cut_info, {})

    def test_length_cut_reaches_auto_select_result(self):
        """カット前提の丈カットが最終結果まで残ること(切断依頼書が読む)。

        以前はここが漏れていた: `LowerSelectionResult` に
        `length_cut_info` 自体が無く、`auto_select_boards` も
        マージしていなかった。パレット丈を短くして、確実に丈カットが
        発生する構成にする。
        """
        palette = make_palette(1150, 1750)
        product = ProductSize(width=1122, length=1800)
        available = [board(1030, 1520)]
        r = alg.auto_select_boards(available, palette, product)
        self.assertEqual(r.length_cut_info.get("L_1030x1520"), 1030)
        self.assertEqual(r.length_cut_count.get("L_1030x1520"), 1)

    def test_fallback_board_is_not_double_counted(self):
        """在庫が対象幅に届かずフォールバック(タグ"主")になったときは、

        ここで丈カットを記録しない。`recalc_length_cut_info` の通常
        ループがこのボードを対象にする(除外されない)ので、ここでも
        記録すると `_orient` の向き判断の違いで別の値になり、
        後からマージしても食い違ったまま残ってしまう。
        """
        palette = make_palette(1150, 1750)
        product = ProductSize(width=1122, length=1800)
        available = [board(400, 1000)]   # 最大辺400 < パレット幅1150 → フォールバック
        r = alg.select_boards_for_wide_lower(available, palette, product)
        self.assertEqual(r.boards[0].tag, alg.TAG_MAIN)
        self.assertEqual(r.length_cut_info, {})

    def test_reported_scenario_1030x1520_splits_into_two_cut_groups(self):
        """発端の症状: 1030x1520(カット前提)が丈カットで分割されず、

        3枚が「幅カットのみ」の1本にまとめられてしまっていた。
        """
        palette = make_palette(1150, 2650)
        product = ProductSize(width=1122, length=2502)
        available = [board(1030, 1520)]
        r = alg.auto_select_boards(available, palette, product)
        cut_info = reports.get_cut_size_info(
            r.lower, "下用", palette.width, palette.length,
            r.cut_info, r.length_cut_info, r.length_cut_count)
        # 丈カットが検出されていれば size_both が埋まり、
        # 「幅カットのみ」の1本にまとめられない
        self.assertNotEqual(cut_info.size_both, "")


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

    def test_protec_uses_confirmed_result_without_recalculating(self):
        """プロテックのときは選定の確定値(`need_length_cut`)をそのまま

        使い、独自に「count * eff_length > limit」を再計算しない
        (再計算すると、選定[100mm閾値]と食い違う結果になりうる)。
        """
        lower = [SelectedBoard(width=1090, length=1000, count=3, tag=alg.TAG_CUT_PREMISE)]
        pr = alg.ProtecCutResult(
            valid=True, orig_width=1090, orig_length=1000, cut_eff_width=1090,
            eff_length=1000, need_cut=False, need_length_cut=True,
            length_cut_eff=910, count=3)
        info, count = alg.recalc_length_cut_info(
            lower, [], self.palette, self.product, lower_protec_result=pr)
        self.assertIn("L_1090x1000", info)
        self.assertEqual(count["L_1090x1000"], 1)

    def test_protec_records_the_lower_only(self):
        """**プロテックは上用を判定しない。**

        上下共用・同サイズが確定ルールなので、上用を製品丈基準で
        独自に判定すると下用(確定値)と食い違う。画面でも上用は
        下用と同じ図の重ね描きになり、上用のラベルだけが下用の図に
        残る(現場の声:「上用ラベルが重なる」)。
        """
        lower = [SelectedBoard(width=1090, length=1000, count=2, tag=alg.TAG_MAIN)]
        upper = [SelectedBoard(width=1090, length=1000, count=3, tag=alg.TAG_CUT_PREMISE)]
        lower_pr = alg.ProtecCutResult(
            valid=True, orig_width=1090, orig_length=1000, cut_eff_width=1090,
            eff_length=1000, need_cut=False, need_length_cut=True,
            length_cut_eff=900, count=2, len_normal_cnt=1, len_cut_cnt=1)
        upper_pr = alg.ProtecCutResult(
            valid=True, orig_width=1090, orig_length=1000, cut_eff_width=1090,
            eff_length=1000, need_cut=False, need_length_cut=True,
            length_cut_eff=910, count=3)
        info, count = alg.recalc_length_cut_info(
            lower, upper, self.palette, self.product,
            lower_protec_result=lower_pr, upper_protec_result=upper_pr)
        self.assertEqual(list(info), ["L_1090x1000"])
        self.assertEqual(count["L_1090x1000"], 1)

    def test_protec_records_nothing_when_the_lower_needs_no_cut(self):
        lower = [SelectedBoard(width=1090, length=1000, count=2, tag=alg.TAG_MAIN)]
        lower_pr = alg.ProtecCutResult(
            valid=True, orig_width=1090, orig_length=1000, cut_eff_width=1090,
            eff_length=1000, need_cut=False, need_length_cut=False, count=2,
            len_normal_cnt=2)
        info, _count = alg.recalc_length_cut_info(
            lower, [], self.palette, self.product, lower_protec_result=lower_pr)
        self.assertEqual(info, {})


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
        # 主ボード 900x1000 x2 → X方向カバー1800
        # 幅補填 50x1000 が1枚(=1000)しかなければ、丈方向に足りないので増える。
        # 【VBAからの修正】残りを別の小型ボードで埋める段(wfAlready)は
        # "丈補填" タグの行だけをマージ対象にするため、候補が幅補填行自身と
        # 同サイズ(50x1000)でも、幅補填行そのものではなく別行として
        # "丈補填" タグで追加される。
        boards = [
            SelectedBoard(900, 1000, 2, alg.TAG_MAIN),
            SelectedBoard(50, 1000, 1, alg.TAG_WIDTH_FILL),
        ]
        alg._recheck_width_fill_length(
            boards, 1, 900, self.palette, self.product, [board(50, 1000)])
        self.assertEqual(boards[1].count, 1)  # 幅補填行そのものは汚染されない
        length_fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(length_fill_rows), 1)
        self.assertGreater(length_fill_rows[0].count, 0)

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

    def test_gap_filler_merge_ignores_wrong_tag_row(self):
        """残gapを別の小型ボードで埋めるとき(wfAlready)もタグ限定が必要(バグ修正)。

        タグを問わず(width, length)一致だけでマージすると、"主"や"幅補填"の
        行にこの残gap埋め分が誤って合算されていた。丈補填タグの行だけを
        対象にし、無ければ新規行を作る必要がある。

        主900x1000 x2(カバー1800)、幅補填50x1700(長辺1700で残gap=100)。
        埋め穴候補と同サイズ(150x900)の"主"行を意図的にstart_idxより前に
        置き、丈カバレッジ再確認の対象外にしつつ、マージ検索には掛かる
        ようにして汚染を検出する。
        """
        main = SelectedBoard(900, 1000, 2, alg.TAG_MAIN)
        decoy = SelectedBoard(150, 900, 1, alg.TAG_MAIN)
        width_fill = SelectedBoard(50, 1700, 1, alg.TAG_WIDTH_FILL)
        boards = [main, decoy, width_fill]
        alg._recheck_width_fill_length(
            boards, 2, 900, self.palette, self.product, [board(150, 900)])
        self.assertEqual(decoy.count, 1)  # "主"行は汚染されない
        length_fill_rows = [b for b in boards if b.tag == alg.TAG_LENGTH_FILL]
        self.assertEqual(len(length_fill_rows), 1)
        self.assertEqual(
            (length_fill_rows[0].width, length_fill_rows[0].length, length_fill_rows[0].count),
            (150, 900, 1),
        )


class PickGapFillerTagTests(unittest.TestCase):
    """`_pick_gap_filler`(wfSkipCap)のタグ限定バグ修正の検証。"""

    def test_capped_wrong_tag_row_does_not_block_candidate(self):
        # 同サイズの"主"行が上限(4枚)でも、丈補填の候補選定には無関係
        boards = [SelectedBoard(50, 1000, alg.LENGTH_FILL_COUNT_CAP, alg.TAG_MAIN)]
        best = alg._pick_gap_filler(boards, [board(50, 1000)], gap_x=500, sel_max_w=900)
        self.assertIsNotNone(best)
        self.assertEqual((best.width, best.length), (50, 1000))

    def test_capped_length_fill_row_is_excluded(self):
        boards = [SelectedBoard(50, 1000, alg.LENGTH_FILL_COUNT_CAP, alg.TAG_LENGTH_FILL)]
        best = alg._pick_gap_filler(boards, [board(50, 1000)], gap_x=500, sel_max_w=900)
        self.assertIsNone(best)


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
