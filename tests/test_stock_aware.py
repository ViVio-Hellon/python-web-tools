"""在庫考慮: 在庫薄の板をよけているか / 疲労度と合わせたとき(VER4.8.67)。

現場の声:「在庫加味が正しく動くか(在庫が少ないものをよけているか)、疲労度と在庫を合わせた場合に
正しく動くかチェックしてください」。点検すると、在庫考慮は「在庫のある板を先に並べる」だけで、
在庫薄の板を使う選定が多かった(外せば在庫のある板だけで選べた)。

→「在庫考慮ONのときは、先に在庫薄の板を外して選ぶ」(現場で了承)。外して選べないときだけ在庫薄も
入れる。疲労度もONなら 在庫薄の枚数 → 切る回数 → 合計点 の順で比べる(「両方onの場合在庫がなければ
切るしかないので仕方がない」)。

ボードと置き場は共有マスタの写し(`test_cut_vs_carry.HARD_BOARDS`)。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import board_selection_algorithm as alg  # noqa: E402
from packaging_tool import db, work_context  # noqa: E402
from packaging_tool.board_selection_service import Palette, ProductSize  # noqa: E402
from packaging_tool.models import BoardModel  # noqa: E402
from packaging_tool.presenters.selection import SelectionPresenter  # noqa: E402
from packaging_tool.user_log import UserLog  # noqa: E402

from tests.test_cut_vs_carry import HARD_BOARDS, _RealDataCase  # noqa: E402

FILL = {(30, 2500), (50, 1600), (100, 2000), (100, 2500)}


def boards(low: set) -> list[BoardModel]:
    return [BoardModel(width=w, length=l, stock_low=(w, l) in low) for w, l, _ in HARD_BOARDS]


def used(result, low: set) -> list[tuple[int, int]]:
    return [(b.width, b.length) for b in result.lower + result.upper if (b.width, b.length) in low]


class StockFirstTests(_RealDataCase):

    PAL, PROD = Palette(width=900, length=1500), ProductSize(width=880, length=1480)
    LOW = {(1000, 1500)}

    def test_以前は並べ替えだけで在庫薄を使っていた(self):
        """1枚で覆える板が在庫薄の 1000x1500 しかなく、その段で決まっていた。"""
        before = alg.auto_select_boards(boards(self.LOW), self.PAL, self.PROD, stock_aware=True)
        self.assertEqual(used(before, self.LOW), [(1000, 1500)])

    def test_在庫考慮は在庫薄を外して選ぶ(self):
        sel = alg.select_boards(boards(self.LOW), self.PAL, self.PROD, stock_aware=True)
        self.assertTrue(sel.avoided_low)
        self.assertEqual(used(sel.result, self.LOW), [])
        self.assertTrue(sel.result.lower and sel.result.upper)

    def test_在庫考慮OFFなら在庫薄も使う(self):
        sel = alg.select_boards(boards(self.LOW), self.PAL, self.PROD, stock_aware=False)
        self.assertEqual(used(sel.result, self.LOW), [(1000, 1500)])
        self.assertFalse(sel.low_sizes)

    def test_疲労度と合わせても在庫薄を外す(self):
        lo, up = self.maps(self.PAL, self.PROD)
        sel = alg.select_boards(boards(self.LOW), self.PAL, self.PROD, stock_aware=True,
                                fatigue_map_lower=lo, fatigue_map_upper=up)
        self.assertTrue(sel.avoided_low)
        self.assertIsNotNone(sel.choice)
        self.assertEqual(used(sel.result, self.LOW), [])

    def test_外すと選べないときは在庫薄も入れて_使った板を言う(self):
        low = {(w, l) for w, l, _ in HARD_BOARDS if (w, l) not in FILL}   # 補填の細板しか残らない
        sel = alg.select_boards(boards(low), self.PAL, self.PROD, stock_aware=True)
        self.assertFalse(sel.avoided_low)
        self.assertTrue(sel.low_used)
        self.assertTrue(sel.result.lower and sel.result.upper)

    def test_全部で在庫薄をよける(self):
        """通常の選定がふつうに選ぶ板を在庫薄にしても、外して選べる限り使わない(在庫のみ・両方ON)。"""
        for pw in range(900, 1651, 150):
            for pl in range(1500, 3101, 400):
                pal, prod = Palette(width=pw, length=pl), ProductSize(width=pw - 20, length=pl - 20)
                base = alg.auto_select_boards(boards(set()), pal, prod)
                low = {(b.width, b.length) for b in base.lower + base.upper
                       if (b.width, b.length) not in FILL}
                lo, up = self.maps(pal, prod)
                for kw in ({}, dict(fatigue_map_lower=lo, fatigue_map_upper=up)):
                    sel = alg.select_boards(boards(low), pal, prod, stock_aware=True, **kw)
                    with self.subTest(pallet=(pw, pl), fatigue=bool(kw)):
                        if sel.avoided_low:
                            self.assertEqual(used(sel.result, low), [])
                        else:
                            self.assertTrue(sel.low_used)


class RankTests(unittest.TestCase):

    def test_比べる順は在庫薄_切る回数_合計点(self):
        """両方ONで、切らないが在庫薄を使う選定より、在庫のある板で切る選定を採る。"""
        low_nocut = alg.FatigueTally(distance=0, carry=10, cut=0, sheets=2, cuts=0, low=2)
        stock_cut = alg.FatigueTally(distance=50, carry=40, cut=80, sheets=3, cuts=1, low=0)
        self.assertLess(alg.fatigue_rank(stock_cut), alg.fatigue_rank(low_nocut))
        fewer_cuts = alg.FatigueTally(distance=50, carry=40, cut=0, sheets=3, cuts=0, low=0)
        self.assertLess(alg.fatigue_rank(fewer_cuts), alg.fatigue_rank(stock_cut))


class SessionTests(unittest.TestCase):
    """かんばん(看板_*)の「欲」から在庫薄の印が付き、選定がよけ、選定ログに出る。"""

    def setUp(self) -> None:
        from packaging_tool.selection_session import SelectionSession

        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.conn.executemany(
            "INSERT INTO BoardMaster (ボード幅,ボード丈,ボードタイプ,データラベル) VALUES (?,?,'ハードボード',?)",
            HARD_BOARDS)
        # 1000x1500 は欲しい(品薄)。ほかの看板には在庫ありが無い
        self.conn.execute("INSERT INTO 看板_大板小板 (資材, サイズ, 欲, 不) "
                          "VALUES ('ハードボード', '1000×1500', '〇', '')")
        self.conn.commit()
        work_context.reset_context()
        self.addCleanup(work_context.reset_context)
        self.log = UserLog()
        self.session = SelectionSession(presenter=SelectionPresenter(self.conn, user_log=self.log))
        self.session.apply_pallet("900", "1500")
        self.session.apply_product("880", "1480")
        self.session.board_type = "ハードボード"

    def selected(self) -> list[tuple[int, int]]:
        return [(b.width, b.length) for b in self.session.selected.lower + self.session.selected.upper]

    def test_在庫考慮ONで在庫薄をよけてログに出す(self):
        if not self.session.stock_aware:
            self.session.toggle("stock_aware")
        self.assertTrue(any(c.stock_low for c in self.session.candidates()))
        self.assertTrue(self.session.auto_select_boards().ok)
        self.assertNotIn((1000, 1500), self.selected())
        self.assertIn("在庫薄の板 1種を外して選びました", self.log.text)

    def test_在庫考慮OFFならそのまま(self):
        if self.session.stock_aware:
            self.session.toggle("stock_aware")
        self.assertTrue(self.session.auto_select_boards().ok)
        self.assertIn((1000, 1500), self.selected())


if __name__ == "__main__":
    unittest.main()
