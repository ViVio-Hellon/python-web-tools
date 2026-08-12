"""ボード候補スコアリング・向き判定(board_scoring)のユニットテスト。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import board_scoring as svc
from packaging_tool import db
from packaging_tool.models import BoardModel


def board(width: int, length: int, *, category: str = "", stock_low: bool = False) -> BoardModel:
    return BoardModel(width=width, length=length, board_category=category, stock_low=stock_low)


class SortBoardsTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(svc.sort_boards_by_target_width([], 1000), [])

    def test_wider_board_scores_higher(self):
        boards = [board(500, 1000), board(900, 1000)]
        result = svc.sort_boards_by_target_width(boards, 1000)
        self.assertEqual((result[0].width, result[0].length), (900, 1000))

    def test_strict_excludes_over_target(self):
        # strict=True(上用)は対象幅超過を候補外(スコア-1)にするため最下位に落ちる
        boards = [board(1200, 500), board(900, 500)]
        result = svc.sort_boards_by_target_width(boards, 1000, strict=True)
        self.assertEqual(result[0].width, 900)

    def test_non_strict_allows_20pct_overhang(self):
        # strict=False(下用)は 1000*1.2=1200 まで許容するので1200は候補に残り最上位
        boards = [board(1200, 500), board(900, 500)]
        result = svc.sort_boards_by_target_width(boards, 1000, strict=False)
        self.assertEqual(result[0].width, 1200)

    def test_rotation_considered(self):
        # 幅2000/丈900は回転すれば幅900として使えるため候補になる
        boards = [board(2000, 900)]
        result = svc.sort_boards_by_target_width(boards, 1000, strict=True)
        self.assertEqual(len(result), 1)

    def test_duplicate_sizes_deduplicated(self):
        boards = [board(900, 1000), board(900, 1000), board(800, 1000)]
        result = svc.sort_boards_by_target_width(boards, 1000)
        self.assertEqual(len(result), 2)

    def test_stock_bonus_promotes_in_stock_board(self):
        # 在庫薄の広いボードより、在庫ありの狭いボードが優先される
        boards = [board(950, 1000, stock_low=True), board(600, 1000, stock_low=False)]
        result = svc.sort_boards_by_target_width(boards, 1000, stock_aware=True)
        self.assertEqual(result[0].width, 600)

    def test_stock_bonus_ignored_when_not_stock_aware(self):
        boards = [board(950, 1000, stock_low=True), board(600, 1000, stock_low=False)]
        result = svc.sort_boards_by_target_width(boards, 1000, stock_aware=False)
        self.assertEqual(result[0].width, 950)

    def test_fatigue_penalty_reorders(self):
        # 同幅なら疲労度スコアの低い(=取り出しやすい)方が優先される
        boards = [board(900, 1000), board(900, 1200)]
        fatigue_map = {
            "900x1000": svc.FatigueEntry(dist=50.0, area=50.0, width_cut=0.0, length_cut=0.0),
            "900x1200": svc.FatigueEntry(dist=0.0, area=0.0, width_cut=0.0, length_cut=0.0),
        }
        result = svc.sort_boards_by_target_width(boards, 1000, fatigue_map=fatigue_map)
        self.assertEqual(result[0].length, 1200)  # 疲労度0の方が勝つ

    def test_fatigue_expected_count_scales_area(self):
        # base_lengthを与えると必要枚数(切り上げ)ぶん面積スコアが加算され、
        # 短いボードほど枚数が増えて不利になる
        boards = [board(900, 500)]
        fatigue_map = {"900x500": svc.FatigueEntry(dist=0.0, area=10.0, width_cut=0.0, length_cut=0.0)}
        with_base = svc.sort_boards_by_target_width(
            boards, 1000, fatigue_map=fatigue_map, base_length=2000,
        )
        self.assertEqual(len(with_base), 1)  # クラッシュせず候補として残る


class GetBestOrientationTests(unittest.TestCase):
    def test_lower_allows_overhang(self):
        # 下用: 1000*1.2=1200まで許容。幅1150はそのまま使える
        rotated, w, l = svc.get_best_orientation(board(1150, 800, category="下用"), 1000)
        self.assertFalse(rotated)
        self.assertEqual((w, l), (1150, 800))

    def test_upper_forbids_exceeding_target(self):
        # 上用: 製品幅超過厳禁。幅1150は不可なので回転して800を使う
        rotated, w, l = svc.get_best_orientation(board(1150, 800, category="上用"), 1000)
        self.assertTrue(rotated)
        self.assertEqual((w, l), (800, 1150))

    def test_both_fit_picks_closest_to_target(self):
        rotated, w, _ = svc.get_best_orientation(board(700, 950, category="上用"), 1000)
        self.assertTrue(rotated)
        self.assertEqual(w, 950)  # 950の方が1000に近い

    def test_tie_prefers_no_rotation(self):
        # 両方フィットし対象幅からの距離が同じなら回転しない(VBA: <= 判定)
        rotated, w, l = svc.get_best_orientation(board(900, 1100, category="下用"), 1000)
        self.assertFalse(rotated)
        self.assertEqual((w, l), (900, 1100))

    def test_neither_fits_picks_smaller_width(self):
        rotated, w, l = svc.get_best_orientation(board(3000, 2000, category="上用"), 1000)
        self.assertTrue(rotated)
        self.assertEqual((w, l), (2000, 3000))

    def test_force_category_overrides_board_category(self):
        b = board(1150, 800, category="下用")
        rotated, w, _ = svc.get_best_orientation(b, 1000, force_category="上用")
        self.assertTrue(rotated)  # 上用として扱われるので超過不可
        self.assertEqual(w, 800)


class BuildFatigueMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def test_builds_entry_per_unique_size(self):
        self.conn.execute(
            "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
            " VALUES (500, 1000, 'ハードボード', 'lblItem1')"
        )
        self.conn.commit()

        boards = [board(500, 1000), board(500, 1000), board(600, 1200)]
        result = svc.build_fatigue_map(self.conn, boards, "HVC")
        # VBA `BuildFatigueMap3` は正キーと反転キーの両方を登録する。
        # 同じ板でも向きが変わればカットの有無が変わるので別エントリになる
        self.assertEqual(set(result.keys()),
                         {"500x1000", "1000x500", "600x1200", "1200x600"})
        # 配置図にあるラベルが付いているサイズは距離スコアが付く
        self.assertGreater(result["500x1000"].dist, 0)
        # データラベル未設定のサイズは距離0(=置き場不明扱い、選定を妨げない)
        self.assertEqual(result["600x1200"].dist, 0.0)
        # 面積スコアは常に計算される
        self.assertGreater(result["600x1200"].area, 0)

    def test_no_cut_score_without_a_target_width(self):
        """目標幅を渡さないときはカットの推定をしない(距離と面積だけ)。"""
        result = svc.build_fatigue_map(self.conn, [board(500, 1000)], "HVC")
        self.assertEqual(result["500x1000"].width_cut, 0.0)
        self.assertEqual(result["500x1000"].length_cut, 0.0)


class SelectionFatigueMapsTests(unittest.TestCase):
    """自動選定へ渡す下用・上用のマップ(tkinter版とWeb版で共通)。

    下用と上用では基準になる幅・丈が違う。この幅・丈が「カットが要るか」
    の推定に使われるので、どちらかの基準で両方を作ると別の結果になる。
    """

    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.boards = [board(500, 1000)]

    def build(self, **kwargs):
        return svc.build_selection_fatigue_maps(
            self.conn, self.boards, "HVC",
            pallet_width=1100, pallet_length=2000,
            product_width=1000, product_length=1800, **kwargs)

    def test_下用と上用で別のマップになる(self) -> None:
        lower, upper = self.build()
        self.assertIsNotNone(lower)
        self.assertIsNotNone(upper)
        # 基準丈が違えば丈カットの推定も変わる(パレット丈2000 / 製品丈1800)
        self.assertNotEqual(lower["500x1000"].length_cut,
                            upper["500x1000"].length_cut)

    def test_棚が引けなくても落ちない(self) -> None:
        """VBA版も「マップ取得失敗 → 通常選択で実行」とフォールバックする。"""
        lower, upper = svc.build_selection_fatigue_maps(
            self.conn, self.boards, "存在しない拠点",
            pallet_width=1100, pallet_length=2000,
            product_width=1000, product_length=1800)
        # 拠点が無いだけなら距離不明として作れる。落ちないことが要点
        self.assertIsNotNone(lower)

    def test_片方だけ成功した状態を返さない(self) -> None:
        """半分だけ疲労度が効いた選定は、どちらの理屈でも説明できない。

        上用のぶんだけ落ちても、下用のマップを残して渡してはいけない。
        """
        original = svc.build_fatigue_map
        calls: list[str] = []

        def flaky(*args, **kwargs):
            calls.append(kwargs.get("board_category", ""))
            if kwargs.get("board_category") == "上用":
                raise RuntimeError("棚データが壊れている")
            return original(*args, **kwargs)

        svc.build_fatigue_map = flaky
        self.addCleanup(lambda: setattr(svc, "build_fatigue_map", original))

        lower, upper = self.build()
        self.assertEqual(calls, ["下用", "上用"])
        self.assertIsNone(lower)
        self.assertIsNone(upper)


class EstimateCutFatigueTests(unittest.TestCase):
    """カット疲労の推定 (VBA `AddFatigueEntry3` の後半)。

    「カットするくらいなら遠くから運ぶ」という現場ルールは、
    カットが要る組み合わせをここで不利に評価することで効く。
    """

    def test_a_board_too_wide_in_both_directions_gets_a_width_cut(self):
        # 1200x1100 は目標幅1000に対してどちらの向きでも収まらない → 幅カット
        width_cut, _ = svc.estimate_cut_fatigue(1200, 1100, 1000, 1100, "上用")
        self.assertGreater(width_cut, 0)

    def test_a_board_that_fits_after_turning_gets_no_width_cut(self):
        """回して収まるならカットしない(向き判定が先に効く)。"""
        width_cut, _ = svc.estimate_cut_fatigue(1200, 800, 1000, 800, "上用")
        self.assertEqual(width_cut, 0.0)

    def test_a_board_that_fits_the_width_gets_no_width_cut(self):
        width_cut, _ = svc.estimate_cut_fatigue(900, 800, 1000, 800, "上用")
        self.assertEqual(width_cut, 0.0)

    def test_a_length_that_does_not_divide_evenly_gets_a_length_cut(self):
        # 基準丈3000 を 有効丈800 で敷くと 4枚目に端数が出る → 丈カット
        _, length_cut = svc.estimate_cut_fatigue(900, 800, 1000, 3000, "上用")
        self.assertGreater(length_cut, 0)

    def test_a_length_that_divides_evenly_gets_no_length_cut(self):
        # 基準丈2400 は 有効丈800 でちょうど3枚 → 端数なし
        _, length_cut = svc.estimate_cut_fatigue(900, 800, 1000, 2400, "上用")
        self.assertEqual(length_cut, 0.0)

    def test_the_width_cut_score_uses_the_effective_length(self):
        """切断線の長さは有効丈。長い板ほどカットが重い。"""
        short = svc.estimate_cut_fatigue(1200, 500, 1000, 500, "上用")[0]
        long_ = svc.estimate_cut_fatigue(1200, 2000, 1000, 2000, "上用")[0]
        self.assertGreater(long_, short)

    def test_no_estimate_without_a_base_length(self):
        _, length_cut = svc.estimate_cut_fatigue(900, 800, 1000, 0, "上用")
        self.assertEqual(length_cut, 0.0)


class TotalFatigueTests(unittest.TestCase):
    """合算式 (VBA `CalcTotalFat3`): 距離x1 + 面積x枚数 + 幅Cx枚数 + 丈Cx1。"""

    def _map(self, **kw):
        base = {"dist": 1.0, "area": 2.0, "width_cut": 4.0, "length_cut": 8.0}
        base.update(kw)
        return {"k": svc.FatigueEntry(**base)}

    def test_the_cut_terms_are_part_of_the_total(self):
        # 3枚: 1 + 2*3 + 4*3 + 8 = 27
        self.assertEqual(svc.total_fatigue(self._map(), "k", 3), 27.0)

    def test_the_width_cut_scales_with_the_sheet_count(self):
        one = svc.total_fatigue(self._map(), "k", 1)
        two = svc.total_fatigue(self._map(), "k", 2)
        self.assertEqual(two - one, 2.0 + 4.0)   # 面積1枚分 + 幅カット1枚分

    def test_the_length_cut_is_counted_once_whatever_the_sheet_count(self):
        with_cut = svc.total_fatigue(self._map(length_cut=8.0), "k", 5)
        without = svc.total_fatigue(self._map(length_cut=0.0), "k", 5)
        self.assertEqual(with_cut - without, 8.0)


if __name__ == "__main__":
    unittest.main()
