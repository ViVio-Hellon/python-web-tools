"""「カットするぐらいなら運搬する」が効いているか(VER4.8.65)。

現場の声:「運搬疲労、カット疲労。カットするぐらいなら運搬するという現場の思想は生きていますか？」。
点検すると、疲労度ONのほうが切る選定が多かった(実データ・拠点L1: 切る選定 55 → 116)。
選定の前の見込みで比べていたため。**できあがった選定を数えた本当の合計点**で、通常の選定と
疲労度の選定を比べ、低いほうを採るようにした。アングルはカット1回を配置図の端から端まで
歩くのと同じ重さにした(以前は3.9点で、切っても点がほとんど変わらなかった)。

ボードと置き場は共有マスタ(梱包資材マスタ)のハードボード・アングルの写し(寸法とデータラベル)。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import (angle_service, board_scoring, floor_plan,  # noqa: E402
                            location_service)
from packaging_tool import board_selection_algorithm as alg  # noqa: E402
from packaging_tool.board_selection_service import (Palette, ProductSize,  # noqa: E402
                                                    SelectedBoard)
from packaging_tool.models import BoardModel  # noqa: E402

from tests import _web  # noqa: E402

HARD_BOARDS = (
    (30, 2500, "lblItem4,lblItem13"), (50, 1600, "lblItem4"), (100, 2000, "lblItem4"),
    (100, 2500, "lblItem4"), (295, 985, "lblItem10"), (295, 1080, "lblItem10"),
    (320, 985, "lblItem10"), (400, 1250, "lblItem2"), (420, 1250, "lblItem21"),
    (450, 1520, "lblItem2"), (500, 1250, "lblItem1,lblItem22"), (510, 770, "lblItem12"),
    (550, 1130, "lblItem1,lblItem21"), (580, 880, "lblItem12"), (600, 600, "lblItem12"),
    (650, 650, "lblItem21"), (660, 1050, "lblItem9,lblItem15"), (660, 1310, "lblItem21"),
    (750, 750, "lblItem21"), (750, 1130, "lblItem11,lblItem15"), (840, 870, "lblItem12"),
    (900, 900, "lblItem12"), (945, 2000, "lblItem20"), (945, 2500, "lblItem20"),
    (950, 950, "lblItem12"), (1000, 1000, "lblItem1"), (1000, 1200, "lblItem15"),
    (1000, 1250, "lblItem22"), (1000, 1400, "lblItem1"), (1000, 1500, "lblItem23"),
    (1000, 1600, "lblItem23"), (1000, 1700, "lblItem15"), (1000, 2000, "lblItem3,lblItem14"),
    (1030, 1520, "lblItem1,lblItem7,lblItem19,lblItem23"), (1250, 1250, "lblItem1,lblItem7"),
    (1250, 2500, "lblItem3,lblItem14"),
)
ANGLES = (
    (600, "lblItem34"), (700, "lblItem34"), (800, "lblItem34"), (900, "lblItem34"),
    (1000, "lblItem34"), (1100, "lblItem34"), (1200, "lblItem34"), (1300, "lblItem34"),
    (1450, "lblItem34"), (1550, "lblItem34"), (1250, "lblItem34"), (1350, "lblItem34"),
    (1550, "lblItem31,lblItem32,lblItem33,lblItem34,lblItem35"),
    (2010, "lblItem30,lblItem31,lblItem32"), (2510, "lblItem30,lblItem31,lblItem32,lblItem35"),
)


class _RealDataCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = floor_plan.USER_PATH
        floor_plan.USER_PATH = Path(tmp.name) / "floor_plan.json"      # 出荷時の配置図で測る
        self.addCleanup(lambda: setattr(floor_plan, "USER_PATH", original))
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        self.conn.executemany(
            "INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル) VALUES (?,?,'ハードボード',?)",
            HARD_BOARDS)
        self.conn.executemany(
            "INSERT INTO CornerboardMaster (アングル丈, データラベル) VALUES (?,?)", ANGLES)
        self.conn.commit()
        self.boards = [BoardModel(width=w, length=l) for w, l, _ in HARD_BOARDS]

    def maps(self, pallet, product, base="L1"):
        return board_scoring.build_selection_fatigue_maps(
            self.conn, self.boards, base, board_type="ハードボード",
            pallet_width=pallet.width, pallet_length=pallet.length,
            product_width=product.width, product_length=product.length)


class BoardTests(_RealDataCase):

    def test_上用_950x950を2枚並べても切る(self):
        """製品 980x1480。945x2000 を1枚(520mm切る)と 950x950 を2枚(主+丈補填で 1900mm)。
        VER4.8.65 は 950x950 の2枚を「切らない」と数えていたが、1900 - 1480 = 420mm はみ出すので切る
        (ボードMAPも「はみ出し丈カット」と描く。VER4.8.70)。どちらも1回切るので、合計点の低いほう。"""
        pal, prod = Palette(width=1000, length=1500), ProductSize(width=980, length=1480)
        lo, up = self.maps(pal, prod)
        choice = alg.auto_select_lowest_fatigue(self.boards, pal, prod,
                                                fatigue_map_lower=lo, fatigue_map_upper=up)
        self.assertEqual(choice.normal.cuts, choice.fatigue.cuts)
        self.assertEqual([(b.width, b.length) for b in choice.result.upper], [(945, 2000)])
        self.assertLessEqual(alg.actual_fatigue(choice.result, lo, up).total,
                             min(choice.normal.total, choice.fatigue.total))

    def test_下用_あとから足す1枚の切りも数える(self):
        """パレット 950x2600。疲労度モードの下用は、点を数えたあとで主ボードを1枚足す
        (945x2000 を2枚で 1400mm 切る)。足した1枚とカットを、できあがった選定で数える。
        通常の選定も丈補填のはみ出し(上用 900x900 を3枚で 2700mm)で切るので、切る回数は同じ。"""
        pal, prod = Palette(width=950, length=2600), ProductSize(width=930, length=2580)
        lo, up = self.maps(pal, prod)
        guess = alg.auto_select_boards(self.boards, pal, prod, fatigue_map_lower=lo,
                                       fatigue_map_upper=up, fatigue_mode=True)
        self.assertIn("L_945x2000", guess.length_cut_info)
        self.assertGreaterEqual(alg.actual_fatigue(guess, lo, up).cuts, 1)
        choice = alg.auto_select_lowest_fatigue(self.boards, pal, prod,
                                                fatigue_map_lower=lo, fatigue_map_upper=up)
        self.assertEqual(choice.normal.cuts, choice.fatigue.cuts)
        self.assertLessEqual(alg.actual_fatigue(choice.result, lo, up).total,
                             min(choice.normal.total, choice.fatigue.total))

    def test_カットは最終手段_切る回数は通常以下_同じなら合計点も通常以下(self):
        """パレット幅・丈を振った全部で確かめる(実データで直す前は切る選定が倍になっていた)。

        **カットは最終手段**(VER4.8.66): 切る回数が少ない選定を先に採る。そのぶん歩く・運ぶ点が
        通常より増えることはある(切らずに済むなら運ぶ)。切る回数が同じなら合計点は通常以下。
        """
        cuts_off = cuts_on = 0
        for pw in range(900, 1651, 150):
            for pl in range(1500, 3101, 400):
                pal, prod = Palette(width=pw, length=pl), ProductSize(width=pw - 20, length=pl - 20)
                lo, up = self.maps(pal, prod)
                off = alg.auto_select_boards(self.boards, pal, prod, fatigue_mode=False)
                choice = alg.auto_select_lowest_fatigue(self.boards, pal, prod,
                                                        fatigue_map_lower=lo, fatigue_map_upper=up)
                t_off = alg.actual_fatigue(off, lo, up)
                t_on = alg.actual_fatigue(choice.result, lo, up)
                with self.subTest(pallet=(pw, pl)):
                    self.assertLessEqual(t_on.cuts, t_off.cuts)
                    if t_on.cuts == t_off.cuts:
                        self.assertLessEqual(t_on.total, t_off.total + 1e-9)
                cuts_off += t_off.cuts
                cuts_on += t_on.cuts
        self.assertLessEqual(cuts_on, cuts_off)

    def test_切らずに済むなら_歩く点が増えても切らないほうを採る(self):
        """合計点では切るほうが低くても、切らない選定があればそちらを採る(最終手段)。"""
        from packaging_tool.board_selection_types import AutoSelectResult
        cut = AutoSelectResult(lower=[SelectedBoard(1000, 2000, 1, "主")], upper=[],
                               lower_result=None, upper_result=None,
                               length_cut_info={"L_1000x2000": 300}, length_cut_count={"L_1000x2000": 1})
        nocut = AutoSelectResult(lower=[SelectedBoard(1000, 1000, 2, "主")], upper=[],
                                 lower_result=None, upper_result=None)
        far = {"1000x1000": board_scoring.FatigueEntry(dist=50.0, area=0.0, width_cut=0.0, length_cut=0.0)}
        near = {"1000x2000": board_scoring.FatigueEntry(dist=0.0, area=0.0, width_cut=0.0, length_cut=0.0)}
        t_cut, t_nocut = alg.actual_fatigue(cut, near, {}), alg.actual_fatigue(nocut, far, {})
        self.assertLess(t_cut.total, t_nocut.total)                    # 点だけなら切るほうが低い
        self.assertLess(alg.fatigue_rank(t_nocut), alg.fatigue_rank(t_cut))   # それでも切らないほうが先

    def test_プロテックは比べない_カットは決まり(self):
        """プロテックは製品幅に合わせて切るのが決まり(カット前提とは別の考え方)。比べずにそのまま。"""
        pal, prod = Palette(width=1100, length=2500), ProductSize(width=1050, length=2400)
        lo, up = self.maps(pal, prod)
        calls = []
        original = alg.auto_select_boards

        def spy(*args, **kwargs):
            calls.append(kwargs.get("fatigue_mode"))
            return original(*args, **kwargs)
        alg.auto_select_boards = spy
        self.addCleanup(lambda: setattr(alg, "auto_select_boards", original))
        choice = alg.auto_select_lowest_fatigue(self.boards, pal, prod, fatigue_map_lower=lo,
                                                fatigue_map_upper=up, is_protec_mode=True)
        self.assertFalse(choice.compared)
        self.assertEqual(calls, [True])
        self.assertIn("プロテック", choice.why)

    def test_本当の合計点は補填板と実際のカットを数える(self):
        result = alg.AutoSelectResult(
            lower=[SelectedBoard(1000, 2000, 1, "主"), SelectedBoard(100, 2000, 2, "丈補填")],
            upper=[SelectedBoard(420, 1250, 3, "カット前提")],
            lower_result=None, upper_result=None, cut_info={},
            length_cut_info={"U_420x1250": 980, "945x2000": 900},   # 印の無いキーは数えない
            length_cut_count={"U_420x1250": 1})
        t = alg.actual_fatigue(result, {}, {})
        a = angle_service
        self.assertEqual(t.sheets, 6)
        self.assertAlmostEqual(t.distance, a.FAT_DIST_DEFAULT * 3)          # サイズごとに1往復
        self.assertAlmostEqual(t.carry, sum((a.fat_area_score1(w, l) + a.FAT_SHEET_SCORE) * n
                                            for w, l, n in ((1000, 2000, 1), (100, 2000, 2), (420, 1250, 3))))
        self.assertEqual(t.cuts, 3 + 1)                                       # カット前提3枚 + 丈1回
        self.assertAlmostEqual(t.cut, a.fat_cut_score1(420) * 3 + a.fat_cut_score1(980))

    def test_上用の丈カットは名前が1つ(self):
        """以前は「945x2000」と「U_945x2000」の2つで記録され、実績保存のカット行に余分な1行が
        入っていた(切断依頼は配置とカット前提の印から作るので効いていなかった)。"""
        pal, prod = Palette(width=1000, length=1500), ProductSize(width=980, length=1480)
        lo, up = self.maps(pal, prod)
        result = alg.auto_select_boards(self.boards, pal, prod, fatigue_map_lower=lo,
                                        fatigue_map_upper=up, fatigue_mode=True)
        self.assertTrue(result.length_cut_info)
        for key in result.length_cut_info:
            self.assertTrue(key.startswith(("L_", "U_")), key)


class AngleTests(_RealDataCase):

    def test_カット1回は配置図の端から端まで歩くのと同じ重さ(self):
        self.assertEqual(angle_service.ANGLE_CUT_SCORE, angle_service.FAT_SCORE_CAP)
        self.assertGreater(angle_service.ANGLE_CUT_SCORE,
                           angle_service.fat_cut_score1(angle_service.ANGLE_CUT_LINE))  # 以前の 3.9点

    def test_疲労度ONで切る選定が減り_通常より疲れず_製品丈に足りる(self):
        angles = sorted({a for a, _ in ANGLES})
        fm = location_service.build_angle_fatigue_map(self.conn, angles, "L1")
        self.assertTrue(fm)
        cut_off = cut_on = 0
        for plen in range(800, 6001, 200):
            for pal in (plen + 100, plen + 300):
                off = angle_service.select_angles(plen, angles, pal, 0)
                on, n_score, f_score = angle_service.select_angles_lowest_fatigue(
                    plen, angles, pal, 0, fm)
                with self.subTest(product=plen, pallet=pal):
                    self.assertLessEqual(on.need_cut, off.need_cut)       # カットは最終手段
                    if on.need_cut == off.need_cut:
                        self.assertLessEqual(angle_service.angle_result_fatigue(on, fm),
                                             angle_service.angle_result_fatigue(off, fm) + 1e-9)
                    self.assertGreaterEqual(on.total_len, plen)
                cut_off += off.need_cut
                cut_on += on.need_cut
        self.assertGreater(cut_off, 0)
        self.assertLess(cut_on, cut_off)


if __name__ == "__main__":
    unittest.main()
