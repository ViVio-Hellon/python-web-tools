"""敷き詰め方式(「候補変更」)の選定と配置

現行の選定(`board_selection_algorithm`)は**そのまま**で、こちらは
「候補変更」を押したときだけ動く補助です。ここで確かめるのは、

  1. 現行では原理的に出ない解(丈から入る解)が出ること ── 動機そのもの
  2. 決めた制約(枚数・行数・許容範囲)を守ること
  3. 評価の順序どおりにA/B/Cが選ばれること
  4. 行モデルがそのまま座標になること(重ならない・はみ出さない)

の4つです。
"""
from __future__ import annotations

import unittest

from packaging_tool import board_selection_algorithm as alg
from packaging_tool import placement_algorithm as place
from packaging_tool import tiling_algorithm as T
from packaging_tool.board_selection_service import (BoardRow, Palette,
                                                    ProductSize)

# 検証に使う在庫。細ボード4種は必ず入れる ── 幅と丈の端数を埋めるのは
# この4種だけで、抜くと成立率がまるごと変わる
STOCK_DIMS = ((1030, 1520), (1250, 2500), (910, 1820), (1000, 2000),
              (1220, 2440), (600, 1800),
              (30, 2500), (50, 1600), (100, 2000), (100, 2500))


def stock() -> list[T.TileSku]:
    return T.build_stock([BoardRow(i, w, l, "ハードボード")
                          for i, (w, l) in enumerate(STOCK_DIMS)])


def solve(pal_w: int, pal_l: int, prod_w: int, prod_l: int,
          *, upper: bool = False, share: bool = False):
    """(候補すべて, (A,B,C)) を返す。"""
    palette = Palette(width=pal_w, length=pal_l)
    product = ProductSize(width=prod_w, length=prod_l)
    bounds = T.get_bounds(upper, share, product, palette)
    cands = T.solve_tiling(stock(), bounds)
    return cands, T.pick_axes(cands), bounds


def dims_of(cand: T.TileCand) -> set[tuple[int, int]]:
    return {(b.width, b.length) for b in T.cand_to_selected(cand)}


# ==================================================================
class 動機(unittest.TestCase):
    """**現行では出ない解が出る**こと。これが無いなら作る意味が無い。"""

    def test_丈から入れば丈カットゼロの解に届く(self) -> None:
        """メモの例: パレット1540×2550 / 製品1505×2502。

        現行は幅の近さだけで主ボードを決めるので `1030×1520` を選び、
        丈方向に継ぎ足して 2510 を作る(丈カットが要る)。
        丈から入れば `1250×2500` が選べて丈カットが要らない。
        """
        rows = [BoardRow(i, w, l, "ハードボード")
                for i, (w, l) in enumerate(STOCK_DIMS)]
        current = alg.auto_select_boards(
            rows, Palette(width=1540, length=2550),
            ProductSize(width=1505, length=2502))
        self.assertNotIn((1250, 2500),
                         {(b.width, b.length) for b in current.lower},
                         "現行が既に丈から入れているなら、この動機は成り立たない")

        _, (axis_a, _, _), _ = solve(1540, 2550, 1505, 2502)
        self.assertIsNotNone(axis_a)
        self.assertIn((1250, 2500), dims_of(axis_a))
        self.assertEqual(axis_a.cuts, 0)
        # 主ボードの丈がそのまま製品丈を覆う ── 継ぎ足していない
        self.assertEqual(axis_a.n1, 1)


class 許容範囲(unittest.TestCase):
    """上用はプラス絶対禁止。下用はパレットに対して少しはみ出せる。"""

    def test_上用は製品幅を超えない(self) -> None:
        cands, _, bounds = solve(1540, 2550, 1505, 2502, upper=True)
        self.assertEqual(bounds.w_hi, 1504)  # 製品幅-1。**同値も禁止**
        self.assertTrue(cands)
        for cand in cands:
            self.assertLess(cand.tot_w, 1505,
                            "上用は保護材なので製品幅より必ずマイナス")

    def test_上用の丈は業界シリーズと同じ5mmまでしか短くならない(self) -> None:
        """上用は丈のマイナスがNG。許容は1×2・4×8・5×8 と同じ(製品丈2005 にボード2000)。"""
        cands, _, bounds = solve(1540, 2550, 1505, 2502, upper=True)
        self.assertEqual(bounds.l_lo, 2497)   # 製品丈-5
        self.assertTrue(cands)
        for cand in cands:
            self.assertGreaterEqual(cand.tot_l, 2497)

    def test_上用の丈の許容はシリーズの定数から決まる(self) -> None:
        from packaging_tool import board_selection_common as C
        self.assertEqual(T.UPPER_L_MINUS, C.SC_1X2_L_MAX - C.SC_1X2_BOARD_L)
        self.assertEqual(T.UPPER_L_MINUS, C.SC_4X8_L_MAX - C.SC_4X8_BOARD_L)

    def test_上下共用の上蓋は下用と同じ扱い(self) -> None:
        """保護材ではないのでプラス禁止が外れる。"""
        product = ProductSize(width=1505, length=2502)
        palette = Palette(width=1540, length=2550)
        shared = T.get_bounds(True, True, product, palette)
        lower = T.get_bounds(False, False, product, palette)
        self.assertEqual(shared, lower)

    def test_下用はパレット幅を50までは超えられる(self) -> None:
        _, _, bounds = solve(1540, 2550, 1505, 2502)
        self.assertEqual(bounds.w_lo, 1485)   # 製品幅-20
        self.assertEqual(bounds.w_hi, 1590)   # パレット幅+50
        self.assertEqual(bounds.l_lo, 2482)   # 製品丈-20
        self.assertEqual(bounds.l_hi, 3060)   # パレット丈×1.2

    def test_20mmの根拠は補填板の最小幅(self) -> None:
        """30mm未満の不足は埋めても無駄なので、下限は30未満に取る。"""
        self.assertLess(T.LOWER_W_MINUS, 30)
        self.assertLess(T.UPPER_L_MINUS, 30)


class 制約(unittest.TestCase):
    """守らなければ現物にならない上限。"""

    def test_候補はすべて上限を守る(self) -> None:
        for upper in (False, True):
            cands, _, _ = solve(2000, 3150, 1950, 3100, upper=upper)
            self.assertTrue(cands)
            for cand in cands:
                with self.subTest(upper=upper, cand=cand):
                    self.assertLessEqual(cand.board_count, T.MAX_BOARDS)
                    self.assertLessEqual(cand.n1 + cand.n2, T.MAX_ROWS)
                    self.assertLessEqual(len(cand.thin_rows), T.MAX_THIN_ROW)
                    for comp in (cand.c1, cand.c2):
                        if comp is None:
                            continue
                        self.assertLessEqual(sum(comp.qty), T.MAX_PER_ROW)
                        self.assertLessEqual(sum(comp.thin_qty), T.MAX_THIN_PER)

    def test_枚数の上限は10のまま(self) -> None:
        """14に上げてもA軸は変わらず、B軸だけが悪くなる(全件検証)。"""
        self.assertEqual(T.MAX_BOARDS, 10)

    def test_行高は2種まで(self) -> None:
        cands, _, _ = solve(1540, 2550, 1505, 2502)
        self.assertTrue(any(c.c2 is not None for c in cands),
                        "2種混在が1件も出ないなら、この検証は何も見ていない")


class 評価順序(unittest.TestCase):
    """①要カット ②枚数 ③超過の段 ④種類数。"""

    def test_Aは枚数最小でBは種類最小(self) -> None:
        _, (a, b, _), _ = solve(2000, 3150, 1950, 3100, upper=True)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertLess(a.board_count, b.board_count)
        self.assertLess(b.type_count, a.type_count)
        self.assertEqual(a.cuts, 0)
        self.assertEqual(b.cuts, 0)

    def test_Cはカットを1枚まで許して枚数を減らす(self) -> None:
        _, (a, _, c), _ = solve(1540, 2550, 1505, 2502)
        self.assertEqual(c.cuts, 1)
        self.assertLess(c.board_count, a.board_count,
                        "カットを許して枚数が減らないなら、Cを出す意味が無い")

    def test_Bが枚数で離れすぎたら出さない(self) -> None:
        """疲労度の式に枚数の項が無いので、ここで切っておく。"""
        a = T.TileCand(board_count=3, type_count=2, cuts=0)
        near = T.TileCand(board_count=3 + T.MAX_B_EXTRA, type_count=1, cuts=0)
        far = T.TileCand(board_count=3 + T.MAX_B_EXTRA + 1, type_count=1, cuts=0)
        self.assertIs(T.pick_axes([a, near])[T.AXIS_B], near)
        self.assertIsNone(T.pick_axes([a, far])[T.AXIS_B])

    def test_段は超過だけを数える(self) -> None:
        """不足は保護材の内側に収まるだけで、はみ出しとは性質が違う。"""
        self.assertEqual(T.tile_tier(0), 0)
        self.assertEqual(T.tile_tier(T.TIER1), 0)
        self.assertEqual(T.tile_tier(T.TIER1 + 1), 1)
        self.assertEqual(T.tile_tier(T.TIER2), 1)
        self.assertEqual(T.tile_tier(T.TIER2 + 1), 2)
        # 不足(パレットより小さい)は 0mm 扱い ── 段が上がらない
        cands, _, _ = solve(1540, 2550, 1505, 2502)
        small = [c for c in cands if c.tot_w < 1540 and c.tot_l < 2550]
        self.assertTrue(small)
        for cand in small:
            self.assertEqual((cand.over_w, cand.over_l), (0, 0))

    def test_段は3段のまま(self) -> None:
        """2段にすると、はみ出しが中央値で480mm増える(全件検証)。"""
        self.assertEqual(len({T.tile_tier(0), T.tile_tier(100),
                              T.tile_tier(1000)}), 3)


class 軸の重なり(unittest.TestCase):
    """AとBは**しばしば同じ候補**になる。押しても変わらないのは困る。"""

    def test_枚数でも種類でも最適なものが1つならAとBは一致する(self) -> None:
        _, (a, b, _), _ = solve(1540, 2550, 1505, 2502)
        self.assertIs(a, b, "この在庫では3枚1種が両方の1位になる")
        self.assertEqual(T.cand_signature(a), T.cand_signature(b))

    def test_中身が同じなら鍵も同じ(self) -> None:
        def comp():
            return T.TileRowComp(height=2500, dims=[1250], qty=[1],
                                 thin_qty=(0, 0, 3), thin_th=(30, 50, 100),
                                 width=1550, boards=4)
        # 別のオブジェクトでも、出るものが同じなら同じ鍵
        self.assertEqual(T.cand_signature(T.TileCand(h1=2500, n1=1, c1=comp())),
                         T.cand_signature(T.TileCand(h1=2500, n1=1, c1=comp())))

    def test_使うSKUが同じでも積み方が違えば別の鍵(self) -> None:
        """SKUだけを比べると、**図だけが変わる候補**を取り逃がす。"""
        one_row = T.TileRowComp(height=1000, dims=[500], qty=[2], width=1000,
                                boards=2)
        two_rows = T.TileRowComp(height=1000, dims=[500], qty=[1], width=500,
                                 boards=1)
        flat = T.TileCand(h1=1000, n1=1, c1=one_row)
        tall = T.TileCand(h1=1000, n1=2, c1=two_rows)
        # 選定リストは同じ(500×1000 が2枚)
        self.assertEqual([(r.width, r.length, r.count)
                          for r in T.cand_to_selected(flat)],
                         [(r.width, r.length, r.count)
                          for r in T.cand_to_selected(tall)])
        # それでも並べ方が違うので、別の候補として扱う
        self.assertNotEqual(T.cand_signature(flat), T.cand_signature(tall))

    def test_丈補填の行が違えば別の鍵(self) -> None:
        comp = T.TileRowComp(height=2500, dims=[1250], qty=[1], width=1250,
                             boards=1)
        self.assertNotEqual(
            T.cand_signature(T.TileCand(h1=2500, n1=1, c1=comp, thin_rows=[30])),
            T.cand_signature(T.TileCand(h1=2500, n1=1, c1=comp, thin_rows=[50])))

    def test_空の軸の鍵はNone(self) -> None:
        self.assertIsNone(T.cand_signature(None))


class 行構成(unittest.TestCase):
    def test_パレート絞り込みが効いている(self) -> None:
        """これが無いと下用1件に1.7秒かかって実用にならない。"""
        raw = T._RowBuilder(  # noqa: SLF001 - 絞り込み前を見るため
            [1250, 1030, 600], [30, 50, 100], 2500, 1485, 1590, 1540,
            1590 + 2500)
        raw._rec(0, 0, 0)  # noqa: SLF001
        filtered = T._pareto_filter(raw.out)  # noqa: SLF001
        self.assertLess(len(filtered), len(raw.out))

    def test_劣る構成だけが落ちる(self) -> None:
        keep = T.TileRowComp(boards=2, n_sku=1, w_cut=0, over_w=0, width=100)
        worse = T.TileRowComp(boards=3, n_sku=2, w_cut=0, over_w=10, width=100)
        # 枚数では負けるが種類で勝つものは残る(どちらが良いか決まらない)
        trade = T.TileRowComp(boards=5, n_sku=0, w_cut=0, over_w=0, width=100)
        result = T._pareto_filter([keep, worse, trade])  # noqa: SLF001
        self.assertEqual(result, [keep, trade])

    def test_許容幅を超えたら最後の1枚を幅カットする前提になる(self) -> None:
        comps = T.build_row_comps(stock(), 2500, 1485, 1590, 1540)
        cut = [c for c in comps if c.w_cut > 0]
        self.assertTrue(cut)
        for comp in cut:
            self.assertEqual(comp.width, 1590, "カット後は許容幅ちょうどに着地")

    def test_細ボードは長辺が行高以上でないと使えない(self) -> None:
        """行の丈を覆えない細ボードは、その行では詰め物にならない。"""
        only_short = [T.TileSku(1000, 2000), T.TileSku(50, 1600, True)]
        comps = T.build_row_comps(only_short, 2000, 900, 1200, 1100)
        for comp in comps:
            self.assertEqual(sum(comp.thin_qty), 0)


class 選定リスト(unittest.TestCase):
    def test_同じ寸法でもタグが違えば別の行(self) -> None:
        """切る板と切らない板の区別を消さない(VBA `PutSku` の変更)。"""
        comp = T.TileRowComp(height=2500, dims=[100], qty=[2],
                             thin_qty=(0, 0, 1), thin_th=(30, 50, 100),
                             thin_count=1, width=300, boards=3)
        cand = T.TileCand(h1=2500, n1=1, c1=comp)
        rows = T.cand_to_selected(cand)
        self.assertEqual([(r.width, r.length, r.count, r.tag) for r in rows],
                         [(100, 2500, 2, alg.TAG_MAIN),
                          (100, 2500, 1, alg.TAG_WIDTH_FILL)])

    def test_同じ寸法同じタグは1行(self) -> None:
        comp = T.TileRowComp(height=1000, dims=[500, 400], qty=[1, 1],
                             width=900, boards=2)
        c2 = T.TileRowComp(height=1000, dims=[500], qty=[1], width=500, boards=1)
        rows = T.cand_to_selected(T.TileCand(h1=1000, n1=1, c1=comp,
                                             h2=1000, n2=1, c2=c2))
        self.assertEqual([(r.width, r.count) for r in rows if r.width == 500], [(500, 2)])

    def test_幅を切る行は最後の1枚だけカット前提(self) -> None:
        """同じ寸法でも、切るのは各行の最後の1枚だけ。"""
        comp = T.TileRowComp(height=2000, dims=[1000], qty=[2], width=1670,
                             boards=2, w_cut=1)
        rows = T.cand_to_selected(T.TileCand(h1=2000, n1=3, c1=comp, cuts=3))
        self.assertEqual([(r.width, r.length, r.count, r.tag) for r in rows],
                         [(1000, 2000, 3, alg.TAG_MAIN),
                          (1000, 2000, 3, alg.TAG_CUT_PREMISE)])

    def test_1枚だけの行を幅で切るなら主は書かない(self) -> None:
        """枚数0の行は書かない(「最後の1枚だけ切る」で残りが0枚)。"""
        comp = T.TileRowComp(height=2000, dims=[1250], qty=[1], width=1150,
                             boards=1, w_cut=1)
        rows = T.cand_to_selected(T.TileCand(h1=2000, n1=1, c1=comp, cuts=1))
        self.assertEqual([(r.count, r.tag) for r in rows], [(1, alg.TAG_CUT_PREMISE)])

    def test_丈を切る案は最後の行をすべてカット前提(self) -> None:
        """以前はタグが付かず、切断依頼に載らなかった。"""
        comp = T.TileRowComp(height=1000, dims=[1250], qty=[1], width=1250, boards=1)
        cand = T.TileCand(h1=1000, n1=3, c1=comp, cuts=1)   # 幅カット0 + 最後の行1枚
        self.assertTrue(T.is_length_cut(cand))
        rows = T.cand_to_selected(cand)
        self.assertEqual([(r.count, r.tag) for r in rows],
                         [(2, alg.TAG_MAIN), (1, alg.TAG_CUT_PREMISE)])

    def test_行数ぶん掛ける(self) -> None:
        comp = T.TileRowComp(height=1000, dims=[500], qty=[2], width=1000,
                             boards=2)
        rows = T.cand_to_selected(T.TileCand(h1=1000, n1=3, c1=comp))
        self.assertEqual(rows[0].count, 6)

    def test_タグが付く(self) -> None:
        _, (a, _, c), _ = solve(1540, 2550, 1505, 2502)
        tags = {r.tag for r in T.cand_to_selected(a)}
        self.assertIn(alg.TAG_MAIN, tags)
        self.assertIn(alg.TAG_WIDTH_FILL, tags)
        self.assertIn(alg.TAG_CUT_PREMISE,
                      {r.tag for r in T.cand_to_selected(c)})

    def test_100mm厚は覆う辺で長辺を選び分ける(self) -> None:
        self.assertEqual(T.thin_pair(100, 1800), (100, 2000))
        self.assertEqual(T.thin_pair(100, 2400), (100, 2500))
        self.assertEqual(T.thin_pair(30, 9999), (30, 2500))
        self.assertEqual(T.thin_pair(50, 100), (50, 1600))

    def test_種類数は細ボードの長辺違いをまとめる(self) -> None:
        """100×2000 と 100×2500 は取りに行く先が同じで、現場には1種類。"""
        comp = T.TileRowComp(height=2000, dims=[500], qty=[1],
                             thin_qty=(0, 0, 1), thin_th=(30, 50, 100),
                             width=600, boards=2)
        cand = T.TileCand(h1=2000, n1=1, c1=comp, thin_rows=[100])
        self.assertEqual(T._count_skus(comp, None, [100]), 2)  # noqa: SLF001
        self.assertEqual(T._count_skus(comp, None, [30]), 3)   # noqa: SLF001
        del cand


# ==================================================================
class 配置(unittest.TestCase):
    """行モデルをそのまま座標に落とす。X=丈方向 / Y=幅方向。"""

    def place(self, pal_w, pal_l, prod_w, prod_l, axis=T.AXIS_A):
        _, axes, _ = solve(pal_w, pal_l, prod_w, prod_l)
        cand = axes[axis]
        self.assertIsNotNone(cand)
        ctx = place.PlacementContext(
            palette=Palette(width=pal_w, length=pal_l),
            product=ProductSize(width=prod_w, length=prod_l))
        T.place_tiling_boards(ctx, cand, place.CATEGORY_LOWER)
        return cand, ctx

    def test_重ならない(self) -> None:
        _, ctx = self.place(1540, 2550, 1505, 2502)
        self.assertTrue(ctx.placed)
        for i, a in enumerate(ctx.placed):
            for b in ctx.placed[i + 1:]:
                with self.subTest(a=a.instance_id, b=b.instance_id):
                    self.assertFalse(
                        a.x < b.x + b.length and a.x + a.length > b.x
                        and a.y < b.y + b.width and a.y + a.width > b.y)

    def test_行を丈方向に積み_行の中は幅方向に並べる(self) -> None:
        """**現行の配置と転置している。** ここを取り違えると誤配置になる。"""
        comp = T.TileRowComp(height=500, dims=[400], qty=[2], width=800,
                             boards=2)
        cand = T.TileCand(h1=500, n1=2, c1=comp)
        ctx = place.PlacementContext(palette=Palette(width=900, length=1200),
                                     product=ProductSize(width=800, length=1000))
        T.place_tiling_boards(ctx, cand, place.CATEGORY_LOWER)
        # 1行目は X=0、2行目は X=500(行高ぶん進む)。
        # Y は**中央へ寄る** ── 行の幅800に対しパレット幅900なので
        # 上下50ずつ(現行の配置と同じ規則。行の中の並びは変わらない)
        self.assertEqual([(p.x, p.y) for p in ctx.placed],
                         [(0, 50), (0, 450), (500, 50), (500, 450)])
        for p in ctx.placed:
            self.assertEqual((p.width, p.length), (400, 500))

    def test_丈補填の行の細ボードは行の幅で長辺を選ぶ(self) -> None:
        """**選定リストと実際の配置がずれてはいけない。**

        丈補填の行は寝かせて幅を覆うので、100mm厚の 2000/2500 を
        選び分ける基準は行高ではなく**行の幅**。行高で選ぶと、
        一覧には 100×2000 と出るのに図には 100×2500 が置かれる。
        """
        comp = T.TileRowComp(height=1200, dims=[1000], qty=[1], width=2400,
                             boards=1)
        cand = T.TileCand(h1=1200, n1=1, c1=comp, thin_rows=[100])
        # 行高は1200だが、覆うのは行の幅2400。2000では足りない
        listed = T.cand_to_selected(cand)[-1]
        self.assertEqual((listed.width, listed.length), (100, 2500))

        ctx = place.PlacementContext(
            palette=Palette(width=2500, length=1400),
            product=ProductSize(width=2400, length=1300))
        T.place_tiling_boards(ctx, cand, place.CATEGORY_LOWER)
        drawn = ctx.placed[-1]
        self.assertEqual((drawn.original_width, drawn.original_length),
                         (2500, 100), "図に置かれるのも 100×2500(寝かせた向き)")

    def test_丈補填の行は幅いっぱいに寝る(self) -> None:
        """VBAはここで縦横が逆で、丈方向に行幅ぶん伸びていた。"""
        comp = T.TileRowComp(height=2500, dims=[1250], qty=[1], width=1250,
                             boards=1)
        cand = T.TileCand(h1=2500, n1=1, c1=comp, thin_rows=[50])
        ctx = place.PlacementContext(
            palette=Palette(width=1300, length=2600),
            product=ProductSize(width=1250, length=2550))
        T.place_tiling_boards(ctx, cand, place.CATEGORY_LOWER)
        fill = ctx.placed[-1]
        self.assertTrue(fill.is_fill_board)
        self.assertEqual(fill.x, 2500, "主ボードの丈の続きに置く")
        self.assertEqual(fill.length, 50, "丈方向に占めるのは厚みだけ")
        self.assertEqual(fill.width, 1250, "幅方向は行いっぱい")

    def test_幅補填は行の中で上下に振り分ける(self) -> None:
        """1本→上0/下1、2本→上1/下1、3本→上1/下2、4本→上2/下2。"""
        for total, upper in ((1, 0), (2, 1), (3, 1), (4, 2)):
            with self.subTest(total=total):
                comp = T.TileRowComp(
                    height=2500, dims=[1000], qty=[1],
                    thin_qty=(total, 0, 0), thin_th=(30, 50, 100),
                    thin_count=total, width=1000 + 30 * total,
                    boards=1 + total)
                ctx = place.PlacementContext(
                    palette=Palette(width=1200, length=2600),
                    product=ProductSize(width=1100, length=2500))
                T.place_tiling_boards(
                    ctx, T.TileCand(h1=2500, n1=1, c1=comp),
                    place.CATEGORY_LOWER)
                main = next(p for p in ctx.placed if not p.is_fill_board)
                above = [p for p in ctx.placed
                         if p.is_fill_board and p.y < main.y]
                self.assertEqual(len(above), upper)
                self.assertEqual(len(ctx.placed) - 1 - len(above),
                                 total - upper)

    def test_行幅を超える分は切って置く(self) -> None:
        """行の最後の1枚は、行幅に着地するところまでしか使わない。"""
        comp = T.TileRowComp(height=1000, dims=[600], qty=[2], width=1000,
                             boards=2, w_cut=1)
        ctx = place.PlacementContext(palette=Palette(width=1000, length=1100),
                                     product=ProductSize(width=950, length=1000))
        T.place_tiling_boards(ctx, T.TileCand(h1=1000, n1=1, c1=comp),
                              place.CATEGORY_LOWER)
        self.assertEqual([p.width for p in ctx.placed], [600, 400])
        # 切る前の実寸は残す(切断依頼が見る)
        self.assertEqual([p.original_width for p in ctx.placed], [600, 600])

    def test_カテゴリはそのまま付く(self) -> None:
        _, ctx = self.place(1540, 2550, 1505, 2502)
        for p in ctx.placed:
            self.assertEqual(p.board_category, place.CATEGORY_LOWER)


class 速度(unittest.TestCase):
    def test_1件あたりの探索が現実的な時間で終わる(self) -> None:
        """VBAで0.05秒。押してから待たされる画面にはしない。"""
        import time
        start = time.perf_counter()
        for _ in range(5):
            solve(2000, 3150, 1950, 3100)
        self.assertLess((time.perf_counter() - start) / 5, 0.5)


if __name__ == "__main__":
    unittest.main()


class 幅方向のセンタリング(unittest.TestCase):
    """**敷き詰め方式の配置も、幅方向は中央へ寄せる。**

    行はどれも y=0 から積むので、幅が足りない行は上詰めで出ていた。
    現行の配置(`place_boards_from_list`)は `center_boards_in_width` で
    寄せているのに、候補変更(敷き詰め方式)だけが通っていなかった ──
    同じ図が、どちらの経路で作ったかで違う位置に出ていた。

    現場の指摘:「ボードを幅方向のセンター配置をしていない」。
    """

    def upper(self, rows, pal=(850, 2350), prod=(812, 2302)):
        """行の並びを置いて、(幅, x, y) を返す。"""
        comps = [T.TileRowComp(height=h, dims=[w], qty=[1], width=w, boards=1)
                 for w, h in rows]
        cand = T.TileCand(h1=rows[0][1], n1=1, c1=comps[0])
        if len(rows) > 1:
            cand.h2, cand.n2, cand.c2 = rows[1][1], 1, comps[1]
        ctx = place.PlacementContext(
            palette=Palette(width=pal[0], length=pal[1]),
            product=ProductSize(width=prod[0], length=prod[1]))
        T.place_tiling_boards(ctx, cand, place.CATEGORY_UPPER)
        return ctx, [(p.width, p.x, p.y) for p in ctx.placed]

    def test_現場が出した図のとおりに寄る(self) -> None:
        """パレット850x2350 / 製品812x2302 の候補C(カット1枚許容)。

            上用 1000x1200 [カット前提] → 幅811へカット(189カット)
            上用 750x1130  [主]

        811 はちょうど一杯(基準812)なので動かない。750 は62mm余るので
        上下31mmずつ。**直す前は 750 が上詰めのままだった。**
        """
        _ctx, placed = self.upper([(811, 1200), (750, 1130)])
        self.assertEqual(placed, [(811, 0, 0), (750, 1200, 31)])

    def test_幅がちょうどなら動かさない(self) -> None:
        _ctx, placed = self.upper([(812, 1200)])
        self.assertEqual(placed, [(812, 0, 0)])

    def test_行ごとに寄せる(self) -> None:
        """行は丈方向に別の場所なので、まとめてではなく行ごとに寄せる。"""
        _ctx, placed = self.upper([(700, 1100), (600, 1100)])
        self.assertEqual(placed, [(700, 0, 56), (600, 1100, 106)])

    def test_幅補填の上下振り分けは崩さない(self) -> None:
        """行の中で上下に振り分けた幅補填は、**既に置き場を決めている**。

        そのうえで中央へ寄せ直すと、振り分けた意味が消える。
        """
        comp = T.TileRowComp(height=1200, dims=[700], qty=[1], width=800,
                             boards=1, thin_th=[50, 0, 0], thin_qty=[2, 0, 0])
        cand = T.TileCand(h1=1200, n1=1, c1=comp)
        ctx = place.PlacementContext(
            palette=Palette(width=850, length=2350),
            product=ProductSize(width=812, length=2302))
        T.place_tiling_boards(ctx, cand, place.CATEGORY_UPPER)
        ys = [p.y for p in ctx.placed]
        self.assertEqual(ys, sorted(ys))
        self.assertEqual(ys[0], 0, "振り分けた幅補填が動いています")


class 丈補填行も同じだけ寄せる(unittest.TestCase):
    """**丈補填の行は、メインの行(c1)と同じ寄せ量で置く**(VBA `RowCenterOffset`)。

    以前は丈補填の行が y=0 固定で、メインの行だけ中央へ寄って段差が
    できていた(VBA の実例):

        上用 1250x1250 at(0,36)   ← 製品幅1322 に対し (1322-1250)/2 = 36
        上用 1250x100  at(1250,0) ← 丈補填行は 0 のまま

    寄せ量は `row_center_offset` の1か所で決め、行と丈補填行の両方が
    同じ値を使う(計算を分けると、どちらかだけ直してズレる)。
    """

    def place(self, category, comp, height, thin_rows, pal, prod):
        cand = T.TileCand(h1=height, n1=1, c1=comp, thin_rows=list(thin_rows))
        ctx = place.PlacementContext(
            palette=Palette(width=pal[0], length=pal[1]),
            product=ProductSize(width=prod[0], length=prod[1]))
        T.place_tiling_boards(ctx, cand, category)
        return [(p.x, p.y, p.width, p.is_fill_board) for p in ctx.placed]

    @staticmethod
    def row(width, height):
        return T.TileRowComp(height=height, dims=[width], qty=[1],
                             width=width, boards=1)

    def test_現場の実例_上用(self) -> None:
        placed = self.place(place.CATEGORY_UPPER, self.row(1250, 1250), 1250,
                            [100], pal=(1400, 2800), prod=(1322, 2700))
        self.assertEqual(placed, [(0, 36, 1250, False), (1250, 36, 1250, True)])

    def test_下用はパレット幅が基準(self) -> None:
        """下用 1310x660 はパレット幅1350 に対し (1350-1310)/2 = 20。"""
        placed = self.place(place.CATEGORY_LOWER, self.row(1310, 660), 660,
                            [30], pal=(1350, 2800), prod=(1322, 2700))
        self.assertEqual([(x, y) for x, y, _w, _f in placed], [(0, 20), (660, 20)])

    def test_端数は上側を少なく(self) -> None:
        """幅補填の振り分け(`本数 // 2` が上側)にそろえる。"""
        self.assertEqual(T.row_center_offset(self.row(1250, 1000), 1323), 36)

    def test_幅補填がある行は寄せない_丈補填行もそろって0(self) -> None:
        """幅補填で幅を埋めている行は、振り分けで置き場を決めている。"""
        comp = T.TileRowComp(height=1000, dims=[1200], qty=[1], width=1300,
                             boards=2, thin_th=(50, 0, 0), thin_qty=(1, 0, 0))
        self.assertEqual(T.row_center_offset(comp, 1322), 0)
        placed = self.place(place.CATEGORY_UPPER, comp, 1000, [100],
                            pal=(1400, 2800), prod=(1322, 2700))
        thin_row = [p for p in placed if p[0] == 1000]
        self.assertEqual([y for _x, y, _w, _f in thin_row], [0])

    def test_基準幅以上なら寄せない(self) -> None:
        self.assertEqual(T.row_center_offset(self.row(1322, 1000), 1322), 0)
        self.assertEqual(T.row_center_offset(self.row(1400, 1000), 1322), 0)

    def test_寄せても最後の1枚をカットしない(self) -> None:
        """`limit_w` にも寄せ量を足す。足さないと寄せた分だけ最後の1枚が
        境界を越えた扱いになり、幅が削られる。"""
        comp = T.TileRowComp(height=1000, dims=[600, 650], qty=[1, 1],
                             width=1250, boards=2)
        placed = self.place(place.CATEGORY_UPPER, comp, 1000, [],
                            pal=(1400, 2800), prod=(1322, 2700))
        self.assertEqual([(y, w) for _x, y, w, _f in placed], [(36, 600), (636, 650)])
