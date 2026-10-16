"""疲労度の点検で見つかった食い違いの試験(VER4.8.61)。

現場の声:「疲労度計算をチェックしてください。枚数・カット・運搬 正しく加味されてますか」。
点検で見つけた6つの食い違いと、足した「1枚ごとの手間」を、点検で使った例のまま確かめる。
考え方は `docs/疲労度の計算.md`。

距離は配置図の上の点(図の幅が1000)。距離100 → 5点、距離1000 → 50点。
"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import (angle_service, board_scoring, floor_plan,  # noqa: E402
                            layout_session, location_service, user_settings)
from packaging_tool import board_selection_algorithm as alg  # noqa: E402
from packaging_tool.board_scoring import FatigueEntry  # noqa: E402
from packaging_tool.board_selection_narrow import wide_cut_fatigue  # noqa: E402
from packaging_tool.board_selection_service import Palette, ProductSize  # noqa: E402
from packaging_tool.models import BoardModel  # noqa: E402

from tests import _web  # noqa: E402
from tests.test_handoff import insert_angle, insert_board  # noqa: E402

NEAR, FAR = 100, 1000           # 配置図の上の距離


def lower_map(specs, pallet_w, pallet_l):
    """下用マップを手で作る(棚の代わりに距離を直接与える)。中身は `build_fatigue_map` と同じ。"""
    out = {}
    for w, l, dist in specs:
        for a, b in ((w, l), (l, w)):
            wc, lc = board_scoring.estimate_cut_fatigue(a, b, pallet_w, pallet_l, "下用")
            out[f"{a}x{b}"] = FatigueEntry(
                dist=angle_service.fat_dist_score(dist),
                area=angle_service.fat_area_score1(a, b),
                width_cut=wc, length_cut=lc)
    return out


def lower_pick(specs, pallet, product, *, fatigue: bool):
    available = [BoardModel(width=w, length=l) for w, l, _ in specs]
    fmap = lower_map(specs, *pallet) if fatigue else None
    result = alg.auto_select_boards(
        available, Palette(width=pallet[0], length=pallet[1]),
        ProductSize(width=product[0], length=product[1]),
        fatigue_map_lower=fmap, fatigue_mode=fatigue)
    return [(b.width, b.length, b.count) for b in result.lower], result


class AngleSingleCoversTheProductTests(unittest.TestCase):
    """1. アングル「1本そのまま」は製品丈以上(以前は製品丈の半分で通っていた)。"""

    def test_a_single_angle_must_cover_the_product(self):
        fat_map = {"2500": angle_service.fat_dist_score(NEAR),
                   "1300": angle_service.fat_dist_score(NEAR)}
        result = angle_service.select_angles(2400, [2500, 1300], 2600, 0,
                                             fatigue_mode=True, fat_map=fat_map)
        # 以前: 1300x1本(1100mm足りない)
        self.assertEqual((result.count, result.angles), (1, [2500]))
        self.assertGreaterEqual(result.total_len, 2400)

    def test_the_fatigue_result_never_leaves_a_gap(self):
        fat_map = {str(a): 0.0 for a in (600, 1040)}
        result = angle_service.select_angles(1000, [1040, 600], 2000, 0,
                                             fatigue_mode=True, fat_map=fat_map)
        self.assertGreaterEqual(result.total_len, 1000)


class AngleLengthCountedOnceTests(unittest.TestCase):
    """2. アングルの長さは本数ぶんだけ(以前はマップにも入っていて本数+1回)。"""

    def test_the_map_holds_the_distance_only(self):
        conn = _web.memory_db()
        self.addCleanup(conn.close)
        insert_angle(conn, length=2500, label="lblItem34")
        fat_map = location_service.build_angle_fatigue_map(conn, [2500], "L1")
        nearest = location_service.find_nearest_shelf_for_angle(conn, 2500, "L1")
        self.assertEqual(fat_map["2500"], angle_service.fat_dist_score(nearest.distance))

    def test_one_long_angle_beats_two_short_ones_from_the_same_shelf(self):
        fat_map = {"2500": angle_service.fat_dist_score(NEAR),
                   "1300": angle_service.fat_dist_score(NEAR)}
        one = angle_service._agl_fat_score(2500, 1, False, 0, fat_map)
        two = angle_service._agl_fat_score(1300, 2, False, 0, fat_map)
        h = angle_service.FAT_SHEET_SCORE
        self.assertAlmostEqual(one, 5.0 + 2500 / 3000 * 50 + h)
        self.assertAlmostEqual(two, 5.0 + (1300 / 3000 * 50 + h) * 2)
        self.assertLess(one, two)          # 以前は 88.3 対 70.0 で逆転していた


class LowerOverhangIsNotCutTests(unittest.TestCase):
    """3. 下用の切らないはみ出しを幅カットと数えない。"""

    SPECS = [(1105, 1000, NEAR), (1095, 1000, FAR)]

    def test_the_near_board_wins_when_neither_is_cut(self):
        off, _ = lower_pick(self.SPECS, (1100, 2000), (1000, 1950), fatigue=False)
        on, result = lower_pick(self.SPECS, (1100, 2000), (1000, 1950), fatigue=True)
        self.assertEqual(off, [(1105, 1000, 2)])
        # 以前: 疲労度ONだと遠い 1095 を選んでいた(幅カット 77.7x2枚 が付いていた)
        self.assertEqual(on, [(1105, 1000, 2)])
        # 実際にも切らない(切断依頼の元になる記録が無い)
        self.assertEqual(result.cut_info, {})


class SinglePieceIsNotLengthCutTests(unittest.TestCase):
    """4. 1枚物(パレット丈に収まる板)に丈カットを数えない。"""

    SPECS = [(1100, 2000, NEAR), (1100, 2100, FAR)]

    def test_the_near_single_piece_wins(self):
        on, result = lower_pick(self.SPECS, (1100, 2100), (1050, 1950), fatigue=True)
        # 以前: 2100÷2000 が割り切れないので丈カット85.4が付き、遠い 2100 を選んでいた
        self.assertEqual(on, [(1100, 2000, 1)])
        self.assertFalse(any(k.startswith("L_") for k in result.length_cut_info))


class RemainderStillCountsTests(unittest.TestCase):
    """4の直しは1枚物だけ。**端数のある板の丈カット見込みは残す。**

    補填板の枚数・運搬は疲労度に入っていないので、丈カットの見込みが「端数を埋める
    手間」の代わりになっている。これを外すと、補填が要る板ほど安く見えた
    (実データ: パレット900x1800 で 900x900x2枚 より 750x1130 + 補填3枚 を選んだ)。
    """

    SPECS = [(900, 900, FAR), (750, 1130, NEAR), (750, 750, NEAR),
             (100, 2000, NEAR), (50, 1600, NEAR)]

    def test_the_exact_fit_wins_over_a_board_that_needs_fillers(self):
        on, _ = lower_pick(self.SPECS, (900, 1800), (880, 1780), fatigue=True)
        self.assertEqual(on, [(900, 900, 2)])


class WideCutLengthCutTests(unittest.TestCase):
    """5. カット前提(幅広)は最後の1枚の丈カットも数える。"""

    def test_the_last_sheet_cut_is_counted(self):
        entry = FatigueEntry(dist=5.0, area=10.0, width_cut=0.0, length_cut=0.0)
        # 短辺1000を3枚で3000 > 基準丈2500+3 → 最後の1枚を切る(切断線=対象幅1200)
        with_cut = wide_cut_fatigue(entry, 1000, 3, 1200, 2500)
        exact = wide_cut_fatigue(entry, 1000, 3, 1200, 3000)
        self.assertAlmostEqual(with_cut - exact, angle_service.fat_cut_score1(1200))

    def test_the_width_cut_is_counted_on_every_sheet(self):
        one = wide_cut_fatigue(None, 1000, 1, 1200, 0)
        three = wide_cut_fatigue(None, 1000, 3, 1200, 0)
        per_sheet = angle_service.FAT_SHEET_SCORE + angle_service.fat_cut_score1(1000)
        self.assertAlmostEqual(three - one, per_sheet * 2)


class ShelfVisitedOnceTests(unittest.TestCase):
    """6. 棚配置図の合計疲労度: 同じ置き場の距離は1回。"""

    def setUp(self) -> None:
        layout_session.reset_session()
        self.addCleanup(layout_session.reset_session)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = floor_plan.USER_PATH
        floor_plan.USER_PATH = Path(tmp.name) / "floor_plan.json"
        self.addCleanup(lambda: setattr(floor_plan, "USER_PATH", original))
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        self.session = layout_session.get_session()
        self.base = floor_plan.load().base_point_names[0]
        user_settings.set_position(self.base)

    def total(self, items) -> float:
        result = self.session.show_selection(self.conn, items)
        self.assertTrue(result.ok, result.message)
        return float(re.search(r"合計疲労度=([\d.]+)", result.message).group(1))

    def test_two_symmetric_angles_are_one_trip(self):
        insert_angle(self.conn, length=1300, label="lblItem34")
        dist = location_service.find_nearest_shelf_for_angle(self.conn, 1300, self.base).distance
        got = self.total([{"kind": "angle", "width": 0, "length": 1300, "count": 1},
                          {"kind": "angle", "width": 0, "length": 1300, "count": 1}])
        want = location_service.score_angle_pick(dist, 1300, 2)
        self.assertAlmostEqual(got, want, places=1)

    def test_upper_and_lower_of_the_same_size_are_one_trip(self):
        insert_board(self.conn, width=1100, length=2000, label="lblItem1")
        dist = location_service.find_nearest_shelf_for_board(self.conn, 1100, 2000, self.base).distance
        got = self.total([
            {"kind": "board", "category": "下用", "width": 1100, "length": 2000, "count": 2},
            {"kind": "board", "category": "上用", "width": 1100, "length": 2000, "count": 2}])
        self.assertAlmostEqual(got, location_service.score_board_pick(dist, 1100, 2000, 4), places=1)

    def test_different_shelves_are_separate_trips(self):
        insert_board(self.conn, width=750, length=1130, label="lblItem1")
        insert_board(self.conn, width=600, length=900, label="lblItem8")
        d1 = location_service.find_nearest_shelf_for_board(self.conn, 750, 1130, self.base).distance
        d2 = location_service.find_nearest_shelf_for_board(self.conn, 600, 900, self.base).distance
        got = self.total([{"kind": "board", "width": 750, "length": 1130, "count": 1},
                          {"kind": "board", "width": 600, "length": 900, "count": 1}])
        want = (location_service.score_board_pick(d1, 750, 1130)
                + location_service.score_board_pick(d2, 600, 900))
        self.assertAlmostEqual(got, want, places=1)


class HandlingPerSheetTests(unittest.TestCase):
    """7. 1枚ごとの手間を数える(面積が同じでも枚数が多いほど重い)。"""

    def test_six_small_sheets_cost_more_than_two_large_ones_of_the_same_area(self):
        small = location_service.carry_board_score(400, 1100, 6)     # 面積 2.64m2
        large = location_service.carry_board_score(1100, 1200, 2)    # 面積 2.64m2
        self.assertAlmostEqual(small - large, angle_service.FAT_SHEET_SCORE * 4)

    def test_the_formula_is_the_same_everywhere(self):
        """ボード選定・棚配置図で、運ぶぶんの点が一致する。"""
        entry = {"k": FatigueEntry(dist=0.0, area=angle_service.fat_area_score1(1100, 2000),
                                   width_cut=0.0, length_cut=0.0)}
        self.assertAlmostEqual(board_scoring.total_fatigue(entry, "k", 3),
                               location_service.carry_board_score(1100, 2000, 3))


if __name__ == "__main__":
    unittest.main()


class DocTests(unittest.TestCase):
    """`docs/疲労度の計算.md` の値と例が、プログラムと食い違っていないか。"""

    DOC = Path(__file__).resolve().parent.parent / "docs" / "疲労度の計算.md"

    def test_the_handling_value_in_the_doc_is_the_one_in_the_code(self):
        text = self.DOC.read_text(encoding="utf-8")
        self.assertIn(f"**{angle_service.FAT_SHEET_SCORE:g}点(仮の値)**", text)
        self.assertIn("FAT_SHEET_SCORE", text)

    def test_the_angle_example_matches(self):
        text = self.DOC.read_text(encoding="utf-8")
        fat_map = {"2500": angle_service.fat_dist_score(NEAR),
                   "1300": angle_service.fat_dist_score(NEAR)}
        one = angle_service._agl_fat_score(2500, 1, False, 0, fat_map)
        two = angle_service._agl_fat_score(1300, 2, False, 0, fat_map)
        self.assertIn(f"**{one:.1f}**(勝ち)", text)
        self.assertIn(f"= {two:.1f}", text)
