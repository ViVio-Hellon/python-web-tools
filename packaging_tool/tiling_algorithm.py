"""ボード選定(敷き詰め方式) ── 現行選定の**補助**

現行の選定(`board_selection_algorithm`)はそのまま運用します。
こちらは「候補変更」を押したときだけ動く別の考え方で、
**現行の選定・配置の経路には一切入りません**。

【現行が出せない解がある】

現行は幅から入ります。`sort_boards_by_target_width` で幅の近さだけを
見て主ボードを決め、丈は `枚数 = 残丈 // 実効丈` で機械的に決まる。
丈方向の適合度が評価に入っていないため、**丈から入らないと出ない解**は
原理的に出ません。

    パレット 1540×2550 / 製品 1505×2502

    幅から(現行) … 1030×1520 を選び、丈方向に 1030×2 + 450 で 2510
    丈から        … 1250×2500 を選べば丈カットゼロ。幅の不足は細板で埋める

後者は今まで「5×8ショートカット」として手で決め打ちするしかありません
でした。

【行モデル ── 現行の配置と転置している】

「行(幅方向にボードを並べた1列)」を作り、それを丈方向に積んで面にします。
行高は在庫の実寸から選び、最大2種まで混在できます。

                  並べる方向   積む方向
    敷き詰め      幅方向       丈方向
    現行の配置    丈方向       幅方向

**ここが最大の注意点です。** 現行の配置(`place_boards_from_list`)の
1行は「count枚を丈方向に継ぎ足す1本の帯」を意味するので、
敷き詰めの結果をそのまま流すと誤配置になります。配置は
`place_tiling_boards` が専用に行います。

【探索の入れ子】

    ① 行高 h を選ぶ(在庫の実寸)
    ② 幅構成を数え上げる(コアの多重集合 + 細ボード3枚まで で許容幅に着地)
    ③ 行数 n(n×h が許容丈に収まる)
    ④ 丈の端数を細ボードの行(4行まで)で埋める

【評価の順序】

    ① 要カット枚数(細ボード4種を除く)
    ② 枚数
    ③ **超過**の段(0〜50 / 51〜200 / 201mm以上)
    ④ 種類数

③が超過だけで不足を数えないのは、はみ出しは邪魔になるのに対し、
不足は保護材の内側に収まるだけで性質が違うためです。段を2段に
簡略化すると、種類を1〜2減らす代わりにパレットからのはみ出しが
中央値で480mm増えます(全件検証)。

【3つの軸】

    A … 要カット0で枚数最小
    B … 要カット0で種類最小
    C … カット1枚まで許して枚数最小

「候補変更」を押すたびに A→B→C と回ります。カットゼロを優先する
代償は平均1.27枚の追加運搬で、3つ出す意味はそこにあります。

==================================================================
VBA からの移植で**変えたところ**(いずれも意図的)
==================================================================

1. **モジュール変数を持ちません。** VBA は行構成表を `m_rc` という
   モジュール変数に置いていたため、下用→上用と2回解くと上用の計算後に
   下用の表が消え、`SnapshotRowComps`/`RestoreRowComps` で退避する必要が
   ありました(忘れると `1030×1500` のような存在しないSKUが出る)。
   Python版は候補が行構成を**そのまま参照**するので、退避も復元も
   要りません。

2. **丈補填の行は専用の置き方をします**(`_place_thin_row`)。
   行の中に並べる1枚(`_place_one_board`)とは縦横が違い、長辺を選ぶ
   基準も違う ── 寝かせて幅を覆うので、幅方向=行幅・丈方向=厚みで、
   長辺は行高ではなく**行の幅**で選びます。まとめていたころは
   幅方向に厚みぶんしか置かれず、丈方向に行幅ぶん伸びていました
   (VBA も同じ誤りで、`PlaceThinRow` を新設して直しています)。

【VBAだけの不具合で、こちらには無いもの】

VBA の `TryThinRows` は固定長ローカル配列 `ths(1 To 4)` を使い回して
おり、`tn` は毎回0に戻るのに中身が残るため、実在しない丈補填行が
候補に混入していました(`Erase ths` で修正)。Python版は毎回
`[t1]*a + [t2]*b + [t3]*c` と作り直すので、この形の間違いは起きません。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from . import placement_algorithm as place
from .placement_types import CUT_UPPER_MINUS
from .board_selection_algorithm import (TAG_CUT_PREMISE, TAG_LENGTH_FILL,
                                        TAG_MAIN, TAG_WIDTH_FILL)
from .board_selection_common import (SC_1X2_BOARD_L, SC_1X2_L_MAX,
                                     SC_4X8_BOARD_L, SC_4X8_L_MAX,
                                     SC_5X8_L_MAX, SC_5X8_U_MAIN_L)
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .logging_utils import get_logger
from .models import BoardModel

log = get_logger("tiling_algorithm")

# ==================================================================
# 制約(変えるのはここだけ。VBA の TILE_* と同じ値)
# ==================================================================
MAX_THIN_ROW = 4    # 丈補填の行数
MAX_THIN_PER = 3    # 1行あたりの細ボード枚数
MAX_PER_ROW = 8     # 1行あたりのコアボード枚数
MAX_ROWS = 8        # 行数
MAX_BOARDS = 10     # 総枚数
TIER1 = 50          # 超過の段(mm)
TIER2 = 200
# B軸(種類最小)がA軸(枚数最小)より何枚まで多くてよいか。
# **10枚の上限は動かさないこと。** 14枚に上げてもA軸は一切変わらず、
# B軸だけが「7枚2種 → 14枚1種」のように悪くなる(全件検証)
MAX_B_EXTRA = 3

# 許容範囲(片側解釈)。上用はプラス絶対禁止 ── 保護材なので製品幅より
# 必ずマイナスに収める。上下共用の上蓋は保護材ではないのでこの禁止が
# 外れ、下用と同じ扱いになる。
#
# 20mm の根拠: 補填板の最小幅が30mmなので、30mm未満の不足は埋めても
# 無駄になる。上限は30未満であるべき、という在庫側からの導出
UPPER_W_MINUS = 80
# 上用の丈は**業界シリーズ(1×2・4×8・5×8・5×10)と同じ許容**にする
# (現場の指示。上用は丈のマイナスがNG)。シリーズは「製品丈がボードより
# 最大5mm長い」ところまで1枚で覆ってよいとしている(1×2: 製品丈〜2005 に
# ボード2000 / 4×8・5×8: 製品丈〜2505 にボード2500)ので、その5mmを使う。
# 以前は下用と同じ20mm で、製品丈より20mm短い別案が出ていた。
# 5×10 はシリーズ判定の定数を持たないので、ほかの3つに合わせる
UPPER_L_MINUS = max(SC_1X2_L_MAX - SC_1X2_BOARD_L,
                    SC_4X8_L_MAX - SC_4X8_BOARD_L,
                    SC_5X8_L_MAX - SC_5X8_U_MAIN_L)
UPPER_L_PLUS = 50
LOWER_W_MINUS = 20
LOWER_W_PLUS = 50
LOWER_L_MINUS = 20
LOWER_L_RATIO = 1.2

# 細ボード4種。**厚みは切りません**(100を30にするのは細すぎて危険)。
# 長辺は切ってよく(100×2500 → 100×1520)、そのカットは要カット枚数に
# 数えません。幅カットを禁止しても成立率は落ちません(97.9%のまま) ──
# 厚み30/50/100の3段階で刻みは最大30mm、許容窓が80mm/70mmあるためです
THIN_TABLE: tuple[tuple[int, int], ...] = ((30, 2500), (50, 1600),
                                           (100, 2000), (100, 2500))

# 軸の名前。画面にも出す
AXIS_A, AXIS_B, AXIS_C = 0, 1, 2
AXIS_NAMES = ("A(枚数最小)", "B(種類最小)", "C(カット1枚許容)")


# ==================================================================
# 型
# ==================================================================
@dataclass(frozen=True)
class TileSku:
    """在庫の1種類。`is_thin` は細ボード4種のいずれかであること。"""

    width: int
    length: int
    is_thin: bool = False


@dataclass
class TileRowComp:
    """1行の幅構成(コアの並び + 細ボードでの詰め物)。"""

    height: int = 0                  # この行の丈方向の寸法(=行高)
    dims: list[int] = field(default_factory=list)   # コアの幅方向寸法
    qty: list[int] = field(default_factory=list)    # 同 枚数
    thin_qty: tuple[int, int, int] = (0, 0, 0)      # 細ボードの枚数
    thin_th: tuple[int, int, int] = (0, 0, 0)       # 同 厚み
    thin_count: int = 0
    width: int = 0                   # 行の幅(この行が覆う幅)
    boards: int = 0                  # 行の枚数(コア+細)
    w_cut: int = 0                   # 1なら最後の1枚を幅カットする前提
    n_sku: int = 0                   # 行の中の種類数
    over_w: int = 0                  # 幅の超過(mm)


@dataclass
class TileCand:
    """候補1件。行高2種まで + 丈補填の行。"""

    h1: int = 0
    n1: int = 0
    c1: Optional[TileRowComp] = None
    h2: int = 0
    n2: int = 0
    c2: Optional[TileRowComp] = None
    thin_rows: list[int] = field(default_factory=list)  # 丈補填の行の厚み
    cuts: int = 0
    board_count: int = 0
    type_count: int = 0
    tier: int = 0
    tot_w: int = 0
    tot_l: int = 0
    over_w: int = 0
    over_l: int = 0


@dataclass(frozen=True)
class TileBounds:
    """許容範囲。`ov_*` は「これを超えたら超過」の基準。"""

    w_lo: int
    w_hi: int
    l_lo: int
    l_hi: int
    ov_w: int
    ov_l: int
    # 幅が許容上限(`w_hi`)を超えた行を**切る幅**(切断依頼の定義。VBA `m_cutW`)。
    # 0 なら従来どおり `w_hi` で切る
    cut_w: int = 0


# ==================================================================
# 在庫と許容範囲
# ==================================================================
def is_thin_sku(width: int, length: int) -> bool:
    """細ボード4種のいずれかか。向きは問わない。"""
    return any((width, length) == pair or (length, width) == pair
               for pair in THIN_TABLE)


def build_stock(rows: Sequence[Any]) -> list[TileSku]:
    """候補ボード一覧(`board_selection_service.BoardRow`)を在庫に直す。

    **寸法の重複だけを除きます。** VBA も同じで、`boardCategory` が
    `availableBoards` の時点では空のため種別で分けられないためです
    (プロテック/IK が混ざる場合は寸法だけでは見分けられません ──
    そもそもプロテック・IKは敷き詰めの対象外なので、呼ぶ側で外します)。
    """
    out: list[TileSku] = []
    seen: set[tuple[int, int]] = set()
    for row in rows:
        key = (int(row.width), int(row.length))
        if key in seen:
            continue
        seen.add(key)
        out.append(TileSku(key[0], key[1], is_thin_sku(*key)))
    return out


def get_bounds(is_upper: bool, share_mode: bool,
               product: ProductSize, palette: Palette) -> TileBounds:
    """モードによる分岐はここだけ(VBA `GetBounds`)。

    上用(通常)だけがプラス禁止です。上下共用の上蓋は保護材では
    ないので、下用と同じ扱いになります。
    """
    if is_upper and not share_mode:
        return TileBounds(
            w_lo=product.width - UPPER_W_MINUS,
            w_hi=product.width - 1,
            l_lo=product.length - UPPER_L_MINUS,
            l_hi=product.length + UPPER_L_PLUS,
            ov_w=product.width, ov_l=product.length,
            # 切る幅は切断依頼の定義(上用 = 製品幅 − 20)
            cut_w=product.width - CUT_UPPER_MINUS)
    return TileBounds(
        w_lo=product.width - LOWER_W_MINUS,
        w_hi=palette.width + LOWER_W_PLUS,
        l_lo=product.length - LOWER_L_MINUS,
        l_hi=int(palette.length * LOWER_L_RATIO),
        ov_w=palette.width, ov_l=palette.length,
        # 下用はパレット幅ぴったりで切る
        cut_w=palette.width)


# ==================================================================
# ① 行構成の数え上げ
# ==================================================================
class _RowBuilder:
    """1つの行高について、許容幅に着地する幅構成をすべて数え上げる。

    VBA の `BuildRowComps` / `RecComp` / `AddThinFills` / `AddComp`。
    モジュール変数の代わりにインスタンスへ持たせている。
    """

    def __init__(self, cores: list[int], thicks: list[int], height: int,
                 w_lo: int, w_hi: int, ov_w: int, limit: int,
                 cut_w: int = 0) -> None:
        self.cores = cores
        self.thicks = thicks
        self.height = height
        self.w_lo, self.w_hi, self.ov_w = w_lo, w_hi, ov_w
        self.cut_w = cut_w
        # コアの合計は許容幅そのものではなく **許容幅+在庫の最大寸法** まで
        # 許す。行の最後の1枚を幅カットして着地させる構成(`w_cut=1`)を
        # 拾うため ── ここで許容幅で切ると、カット前提の解が消える
        self.limit = limit
        self.qty = [0] * len(cores)
        self.out: list[TileRowComp] = []

    def run(self) -> list[TileRowComp]:
        self._rec(0, 0, 0)
        return _pareto_filter(self.out)

    def _rec(self, i: int, cur: int, n: int) -> None:
        if cur > self.limit or n > MAX_PER_ROW or n > MAX_BOARDS:
            return
        if i >= len(self.cores):
            if cur > self.w_hi:
                # 許容幅を超えた ── 最後の1枚を幅カットして着地させる。
                # **切る位置は許容上限ではなく切断依頼の定義の幅**(`cut_w`)。
                # 未設定(0)のときだけ従来どおり許容上限で切る(VBA `RecComp`)
                if n >= 1:
                    self._add(n, 0, 0, 0, self.cut_w if self.cut_w > 0 else self.w_hi, 1)
                return
            self._thin_fills(cur, n)
            return

        d = self.cores[i]
        m = 0
        while cur + d * m <= self.limit and n + m <= MAX_PER_ROW:
            self.qty[i] = m
            self._rec(i + 1, cur + d * m, n + m)
            self.qty[i] = 0
            m += 1

    def _thin_fills(self, cur: int, n: int) -> None:
        t1, t2, t3 = (self._thick(0), self._thick(1), self._thick(2))
        for a in range(_span(t1, MAX_THIN_PER)):
            for b in range(_span(t2, MAX_THIN_PER - a)):
                for c in range(_span(t3, MAX_THIN_PER - a - b)):
                    total = n + a + b + c
                    if not 1 <= total <= MAX_BOARDS:
                        continue
                    width = cur + a * t1 + b * t2 + c * t3
                    if self.w_lo <= width <= self.w_hi:
                        self._add(n, a, b, c, width, 0)

    def _thick(self, i: int) -> int:
        return self.thicks[i] if i < len(self.thicks) else 0

    def _add(self, n: int, a: int, b: int, c: int,
             width: int, w_cut: int) -> None:
        dims: list[int] = []
        qty: list[int] = []
        for i, d in enumerate(self.cores):
            if self.qty[i] > 0:
                dims.append(d)
                qty.append(self.qty[i])
        thin_qty = (a, b, c)
        thin_count = a + b + c
        self.out.append(TileRowComp(
            height=self.height, dims=dims, qty=qty,
            thin_qty=thin_qty,
            thin_th=(self._thick(0), self._thick(1), self._thick(2)),
            thin_count=thin_count,
            width=width,
            boards=n + thin_count,
            w_cut=w_cut,
            n_sku=len(dims) + sum(1 for q in thin_qty if q > 0),
            over_w=max(0, width - self.ov_w)))


def _span(thickness: int, upper: int) -> int:
    """`range()` の引数。その厚みが在庫に無ければ0枚だけを試す。"""
    return max(0, upper) + 1 if thickness else 1


def _pareto_filter(comps: list[TileRowComp]) -> list[TileRowComp]:
    """劣る幅構成を落とす(VBA `ParetoFilter`)。

    **これが無いと実用になりません。** 下用1件に1.7秒かかっていたものが
    0.05秒になります。4つの指標(枚数・種類数・幅カット・幅超過)すべてで
    既に残したものに負けていれば捨てます。

    残した側とだけ比べる貪欲な絞り込みで、厳密なパレート境界では
    ありません(入力の順に依存します)。VBAと同じにしてあります ──
    厳密にすると候補が増え、狙いだった速度が出ません。
    """
    keep: list[TileRowComp] = []
    for comp in comps:
        if any(k.boards <= comp.boards and k.n_sku <= comp.n_sku
               and k.w_cut <= comp.w_cut and k.over_w <= comp.over_w
               for k in keep):
            continue
        keep.append(comp)
    return keep


def build_row_comps(stock: Sequence[TileSku], height: int,
                    w_lo: int, w_hi: int, ov_w: int,
                    cut_w: int = 0) -> list[TileRowComp]:
    """行高 `height` の行構成を数え上げる(VBA `BuildRowComps`)。"""
    cores: list[int] = []
    max_dim = 0
    for sku in stock:
        if sku.is_thin:
            continue
        d = 0
        if sku.length == height:
            d = sku.width
        if sku.width == height:
            d = sku.length
        if d > 0 and d not in cores:
            cores.append(d)
        max_dim = max(max_dim, sku.width, sku.length)
    if not cores:
        return []
    cores.sort(reverse=True)

    # 細ボードは長辺が行高以上でないと、その行の丈を覆えない。
    #
    # **在庫の向きに素直に従います。** VBA も `stock(i).L >= h` と
    # 素の長辺で見ており、マスタが 2500×30 の向きで入っていると
    # 厚みとして 2500 を拾ってしまいます。ここで向きを正規化すると
    # VBAと違う解が出てしまうので、そろえてあります(マスタの向きが
    # 揃っていることが前提)
    thicks: list[int] = []
    for sku in stock:
        if sku.is_thin and sku.length >= height and sku.width not in thicks:
            if len(thicks) >= MAX_THIN_PER:
                # VBA は `m_th(1 To 3)` の固定長で、4種類目で落ちる
                log.debug("細ボードの厚みが3種類を超えました: %s を無視します",
                          sku.width)
                continue
            thicks.append(sku.width)

    return _RowBuilder(cores, thicks, height, w_lo, w_hi, ov_w,
                       w_hi + max_dim, cut_w).run()


def _distinct_dims(stock: Sequence[TileSku]) -> list[int]:
    """在庫に現れる寸法すべて(幅も丈も)を昇順で。行高の候補になる。"""
    dims: list[int] = []
    for sku in stock:
        for value in (sku.width, sku.length):
            if value not in dims:
                dims.append(value)
    dims.sort()
    return dims


def _thin_row_thicks(stock: Sequence[TileSku], need_w: int) -> list[int]:
    """丈補填の行に使える厚み(昇順)。

    丈補填の行は**幅いっぱいに寝かせる**ので、長辺が覆う幅以上に
    ないと使えません。
    """
    thicks: list[int] = []
    for sku in stock:
        if sku.is_thin and sku.length >= need_w and sku.width not in thicks:
            thicks.append(sku.width)
    thicks.sort()
    return thicks


# ==================================================================
# ② 行高の組み合わせ探索
# ==================================================================
def solve_tiling(stock: Sequence[TileSku], b: TileBounds) -> list[TileCand]:
    """候補をすべて作る(VBA `SolveTiling`)。"""
    table: list[tuple[int, list[TileRowComp]]] = []
    for dim in _distinct_dims(stock):
        if dim > b.l_hi:
            continue
        comps = build_row_comps(stock, dim, b.w_lo, b.w_hi, b.ov_w, b.cut_w)
        if comps:
            table.append((dim, comps))
    if not table:
        return []

    thicks = _thin_row_thicks(stock, b.ov_w)
    res: list[TileCand] = []

    for i, (h1, comps) in enumerate(table):
        for c1 in comps:
            p1 = c1.boards
            if p1 > MAX_BOARDS:
                continue
            for n1 in range(1, MAX_ROWS + 1):
                if p1 * n1 > MAX_BOARDS:
                    break
                base = n1 * h1
                # 1行前で既に丈を満たしていたなら、これ以上積む意味は無い
                if base - h1 >= b.l_lo and base > b.l_hi:
                    break
                if base > b.l_hi:
                    if base - h1 < b.l_lo:
                        # 1行少ないと足りず、この行数だと超える。
                        # **最後の行を丈カットして収める**前提で1件だけ残す
                        res.append(_make_cand(
                            h1, n1, c1, 0, 0, None, [],
                            cuts=c1.w_cut * n1 + p1, boards=p1 * n1,
                            tot_w=c1.width, tot_l=b.ov_l, bounds=b))
                    break

                rem = MAX_BOARDS - p1 * n1
                _try_thin_rows(res, h1, n1, c1, 0, 0, None,
                               base, rem, thicks, b)
                for k in range(i):
                    _try_second_height(res, table, h1, n1, c1,
                                       base, rem, k, thicks, b)
    return res


def _try_thin_rows(res: list[TileCand], h1: int, n1: int, c1: TileRowComp,
                   h2: int, n2: int, c2: Optional[TileRowComp],
                   base_len: int, rem_boards: int,
                   thicks: Sequence[int], b: TileBounds) -> None:
    """丈の端数を細ボードの行で埋める(VBA `TryThinRows`)。

    端数が0でも呼びます(a=b=c=0 が「補填なしでちょうど収まる」候補)。
    """
    t1 = thicks[0] if len(thicks) > 0 else 0
    t2 = thicks[1] if len(thicks) > 1 else 0
    t3 = thicks[2] if len(thicks) > 2 else 0
    max_row = min(MAX_THIN_ROW, rem_boards)

    for a in range(_span(t1, max_row)):
        for x in range(_span(t2, max_row - a)):
            for c in range(_span(t3, max_row - a - x)):
                total = base_len + a * t1 + x * t2 + c * t3
                if not b.l_lo <= total <= b.l_hi:
                    continue
                cuts = c1.w_cut * n1 + (c2.w_cut * n2 if c2 else 0)
                boards = (c1.boards * n1 + (c2.boards * n2 if c2 else 0)
                          + a + x + c)
                width = max(c1.width, c2.width) if c2 else c1.width
                res.append(_make_cand(
                    h1, n1, c1, h2, n2, c2, [t1] * a + [t2] * x + [t3] * c,
                    cuts=cuts, boards=boards,
                    tot_w=width, tot_l=total, bounds=b))


def _try_second_height(res: list[TileCand],
                       table: list[tuple[int, list[TileRowComp]]],
                       h1: int, n1: int, c1: TileRowComp,
                       base_len: int, rem_boards: int, i2: int,
                       thicks: Sequence[int], b: TileBounds) -> None:
    """2種類目の行高を積む(VBA `TrySecondHeight`)。行高は2種まで。"""
    h2, comps = table[i2]
    for c2 in comps:
        p2 = c2.boards
        if not 0 < p2 <= rem_boards:
            continue
        for n2 in range(1, MAX_ROWS - n1 + 1):
            if p2 * n2 > rem_boards:
                break
            base2 = base_len + n2 * h2
            if base2 > b.l_hi:
                break
            _try_thin_rows(res, h1, n1, c1, h2, n2, c2,
                           base2, rem_boards - p2 * n2, thicks, b)


def _make_cand(h1: int, n1: int, c1: TileRowComp,
               h2: int, n2: int, c2: Optional[TileRowComp],
               thins: list[int], *, cuts: int, boards: int,
               tot_w: int, tot_l: int, bounds: TileBounds) -> TileCand:
    rows = [t for t in thins if t > 0][:MAX_THIN_ROW]
    over_w = max(0, tot_w - bounds.ov_w)
    over_l = max(0, tot_l - bounds.ov_l)
    return TileCand(
        h1=h1, n1=n1, c1=c1, h2=h2, n2=n2, c2=c2, thin_rows=rows,
        cuts=cuts, board_count=boards,
        type_count=_count_skus(c1, c2, rows),
        tier=tile_tier(over_w + over_l),
        tot_w=tot_w, tot_l=tot_l, over_w=over_w, over_l=over_l)


def _sku_key(a: int, b: int) -> tuple[int, int]:
    """向きを問わない種類のキー。"""
    return (a, b) if a <= b else (b, a)


def _count_skus(c1: TileRowComp, c2: Optional[TileRowComp],
                thin_rows: Sequence[int]) -> int:
    """候補全体で何種類のボードを使うか(VBA `CountSkus`)。

    細ボードは長辺を切ってよいので、丈の値を持たない架空のキー
    (`9999`)でまとめます ── `100×2000` と `100×2500` は取りに行く先が
    同じで、現場にとっては1種類だからです。
    """
    keys: set[tuple[int, int]] = set()
    for comp in (c1, c2):
        if comp is None:
            continue
        for d in comp.dims:
            keys.add(_sku_key(d, comp.height))
        for i in range(3):
            if comp.thin_qty[i] > 0:
                keys.add(_sku_key(comp.thin_th[i], 9999))
    for thickness in thin_rows:
        keys.add(_sku_key(thickness, 9999))
    return len(keys)


def tile_tier(over: int) -> int:
    """超過の段。0: 〜50mm / 1: 〜200mm / 2: それ以上。"""
    if over <= TIER1:
        return 0
    return 1 if over <= TIER2 else 2


# ==================================================================
# ③ A/B/C 軸の判定
# ==================================================================
def _key_a(c: TileCand) -> tuple:
    return (c.board_count, c.tier, c.type_count)


def _key_b(c: TileCand) -> tuple:
    return (c.type_count, c.board_count, c.tier)


def _key_c(c: TileCand) -> tuple:
    return (c.board_count, c.cuts, c.tier, c.type_count)


def pick_axes(cands: Sequence[TileCand]) -> tuple[Optional[TileCand], ...]:
    """A/B/C の3候補を選ぶ(VBA `PickAxes`)。戻りは `(A, B, C)`。

    B(種類最小)がA(枚数最小)より `MAX_B_EXTRA` 枚を超えて多くなるなら
    **Bは出しません。** 疲労度の式には枚数の項が無く面積で代用して
    いるため、小さいボードを多数使う構成が過小評価されます
    (A:3枚2種 と B:6枚1種 で面積差はわずか0.694点)。テープ貼りは
    枚数に比例するので、枚数の判断を疲労度に委ねると逆転します。
    """
    best: list[Optional[TileCand]] = [None, None, None]
    for cand in cands:
        if cand.cuts == 0:
            if best[AXIS_A] is None or _key_a(cand) < _key_a(best[AXIS_A]):
                best[AXIS_A] = cand
            if best[AXIS_B] is None or _key_b(cand) < _key_b(best[AXIS_B]):
                best[AXIS_B] = cand
        if cand.cuts <= 1:
            if best[AXIS_C] is None or _key_c(cand) < _key_c(best[AXIS_C]):
                best[AXIS_C] = cand
    a, b = best[AXIS_A], best[AXIS_B]
    if a is not None and b is not None and b.board_count > a.board_count + MAX_B_EXTRA:
        best[AXIS_B] = None
    return tuple(best)


def cand_signature(cand: Optional[TileCand]) -> Optional[tuple]:
    """**画面に出るものが同じか**を見るための鍵。

    A(枚数最小)とB(種類最小)は、しばしば**同じ候補**が両方の1位に
    なります。「3枚1種」のように枚数でも種類でも最適なものが1つしか
    無ければ当然そうなり、全件検証でも3軸すべて同じになるのが
    上用15.3% / 下用42.4% ── 珍しいことではありません。

    軸が違っても中身が同じなら、押しても画面は何も変わりません。
    使う側からは**押しても反応しない=壊れている**に見えるので、
    呼ぶ側(`selection_session`)はこの鍵で見比べて飛ばします。

    見るのは**選定リストと図に出るものすべて**です。使うSKUだけを
    比べると、同じ寸法を違う積み方で並べた候補を「同じ」と誤判定して
    図だけが変わる候補を飛ばしてしまいます。
    """
    if cand is None:
        return None

    def comp(c: Optional[TileRowComp], height: int, rows: int):
        if c is None:
            return None
        return (height, rows, tuple(c.dims), tuple(c.qty),
                c.thin_qty, c.thin_th, c.width, c.w_cut)

    return (comp(cand.c1, cand.h1, cand.n1),
            comp(cand.c2, cand.h2, cand.n2),
            tuple(cand.thin_rows))


# ==================================================================
# 候補 → 選定リスト
# ==================================================================
def sku_pair(dim: int, height: int) -> tuple[int, int]:
    """コアボードの在庫寸法(幅, 丈)。短いほうが幅。"""
    return (dim, height) if dim <= height else (height, dim)


def thin_pair(thickness: int, cover: int) -> tuple[int, int]:
    """細ボードの在庫寸法(幅, 丈)。

    `cover` は**その細ボードが覆う辺の長さ**。100mm厚だけ 2000 と 2500 の
    2種類があるので、覆えるほうを選びます。長辺は切ってよいので、
    覆う辺より長ければどちらでも構いません。
    """
    if thickness == 30:
        return (30, 2500)
    if thickness == 50:
        return (50, 1600)
    if thickness == 100:
        return (100, 2000 if cover <= 2000 else 2500)
    return (thickness, 2500)


def cuts_of(c1: Optional[TileRowComp], n1: int,
            c2: Optional[TileRowComp], n2: int) -> int:
    """幅カットの枚数(VBA `CutsOf`)。各行の最後の1枚ずつ。"""
    return ((c1.w_cut * n1 if c1 is not None else 0)
            + (c2.w_cut * n2 if c2 is not None else 0))


def is_length_cut(cand: TileCand) -> bool:
    """丈方向で最後の行を切る案か。

    `solve_tiling` はこの種類の案を「幅カット分 + 最後の行の枚数」で
    数えるので、カット数が幅カット分より多ければ丈カットの案です
    (VBA `WriteCandToList` と同じ見分け方)。
    """
    return cand.cuts > cuts_of(cand.c1, cand.n1, cand.c2, cand.n2)


def cand_to_selected(cand: TileCand) -> list[SelectedBoard]:
    """候補を選定リストに直す(VBA `WriteCandToList`)。

    **切られる板にだけ「カット前提」を付けます。** 同じ寸法でも、切る板と
    切らない板は別の行にします(タグが違えば合算しない)── 1行にまとめると
    「どれを切るのか」が消え、切断依頼に全部載る/1枚も載らない、になります。

        幅を切る行    … 切られるのは各行の最後の1枚だけ
                        → 最後の寸法の板を「主」(枚数−1)と「カット前提」(1枚)に分ける
        丈を切る案    … 最後の行の板はすべて「カット前提」、それより前の行は通常どおり
    """
    out: list[SelectedBoard] = []
    if cand.c1 is not None:
        if is_length_cut(cand):
            _put_row_comp(out, cand.c1, cand.h1, cand.n1 - 1)
            _put_row_comp(out, cand.c1, cand.h1, 1, force_cut=True)
            log.debug("[敷詰]cand_to_selected: 丈カットの案 → 最後の行をカット前提に")
        else:
            _put_row_comp(out, cand.c1, cand.h1, cand.n1)
    if cand.c2 is not None:
        _put_row_comp(out, cand.c2, cand.h2, cand.n2)
    # 丈補填行は幅方向へ寝かせるので、必要な長さは行高ではなく行の幅で決まる
    for thickness in cand.thin_rows:
        width, length = thin_pair(thickness, cand.c1.width if cand.c1 else 0)
        _put_sku(out, width, length, 1, TAG_LENGTH_FILL)
    return out


def _put_row_comp(out: list[SelectedBoard], comp: TileRowComp,
                  height: int, rows: int, *, force_cut: bool = False) -> None:
    """1種類の行を `rows` 行ぶん書く(VBA `PutRowComp`)。"""
    if rows <= 0:
        return
    last = len(comp.dims) - 1
    for i, dim in enumerate(comp.dims):
        width, length = sku_pair(dim, height)
        if force_cut:
            # 丈方向で切る行。この行の板はすべてカット前提
            _put_sku(out, width, length, comp.qty[i] * rows, TAG_CUT_PREMISE)
        elif comp.w_cut > 0 and i == last:
            # 幅を切る行。切られるのは**各行の最後の1枚だけ**
            _put_sku(out, width, length, (comp.qty[i] - 1) * rows, TAG_MAIN)
            _put_sku(out, width, length, rows, TAG_CUT_PREMISE)
        else:
            _put_sku(out, width, length, comp.qty[i] * rows, TAG_MAIN)
    for i in range(3):
        if comp.thin_qty[i] > 0:
            width, length = thin_pair(comp.thin_th[i], height)
            _put_sku(out, width, length, comp.thin_qty[i] * rows, TAG_WIDTH_FILL)


def _put_sku(out: list[SelectedBoard], width: int, length: int,
             count: int, tag: str) -> None:
    """1行足す(VBA `PutSku`)。**幅・丈・タグが同じときだけ**合算する。

    枚数0は書かない(「最後の1枚だけ切る」で残りが0枚になる場合)。
    """
    if count <= 0:
        return
    for row in out:
        if row.width == width and row.length == length and row.tag == tag:
            row.count += count
            return
    out.append(SelectedBoard(width=width, length=length, count=count, tag=tag))


# ==================================================================
# 配置 ── 行モデルをそのまま座標に落とす
#   X = 丈方向(行高の累積) / Y = 幅方向(行内の並び)
# ==================================================================
class _Seq:
    """置いた順の通し番号。`instance_id` を一意にするためだけのもの。"""

    def __init__(self) -> None:
        self.n = 0

    def next(self) -> int:
        self.n += 1
        return self.n


def place_tiling_boards(ctx: place.PlacementContext, cand: TileCand,
                        category: str) -> None:
    """候補1件を配置する(VBA `PlaceTilingBoards`)。

    **現行の配置経路(`place_boards_from_list`)は通りません。**
    現行は丈方向に継ぎ足して幅方向へ積むので、行モデルの結果を
    流し込むと誤配置になります。
    """
    seq = _Seq()
    # 中央寄せの基準幅(VBA `ovW`)。上用 = 製品幅 / 下用 = パレット幅。
    # `limit_width` がちょうどこの定義(`GetBounds` の ovW と同じ考え方)
    ov_w = ctx.limit_width(category)
    x_pos = 0
    if cand.c1 is not None:
        for _ in range(cand.n1):
            _place_one_row(ctx, cand.c1, cand.h1, x_pos, category, seq, ov_w)
            x_pos += cand.h1
    if cand.c2 is not None:
        for _ in range(cand.n2):
            _place_one_row(ctx, cand.c2, cand.h2, x_pos, category, seq, ov_w)
            x_pos += cand.h2

    # 丈補填の行は**行の中の1枚ではない**ので、専用の置き方をする。
    #
    # **幅方向の位置はメインの行(c1)とそろえる。** 以前は y=0 固定で、
    # メインの行だけが中央へ寄って段差ができていた(VBA の実例:
    #   上用 1250x1250 at(0,36)  ← 製品幅1322 に対し (1322-1250)/2 = 36
    #   上用 1250x100  at(1250,0) ← 丈補填行は 0 のまま
    # )。寄せ量は `row_center_offset` の**同じ値**を使う ── 計算を
    # 2か所に分けると、今回のようにどちらかだけ直してズレる
    row_w = cand.c1.width if cand.c1 is not None else 0
    thin_off = row_center_offset(cand.c1, ov_w) if cand.c1 is not None else 0
    for thickness in cand.thin_rows:
        _place_thin_row(ctx, thickness, row_w, x_pos, thin_off, category, seq)
        x_pos += thickness


def row_center_offset(comp: TileRowComp, ov_w: int) -> int:
    """行の中央寄せ量(VBA `RowCenterOffset`)。

    **幅補填が無く、基準幅より狭い行だけ**寄せます。幅補填がある行は、
    補填で幅を埋めている前提(上下の振り分けで置き場を決めている)なので
    寄せません(現場の指示:「上下に振り分けているケースはそのままで良い」)。

    端数は幅補填の振り分け(`本数 // 2` が上側)にそろえ、**上側を少なく**
    します(VBA `Int((ovW - .Width) / 2)`。正の数なので `//` と同じ)。

    `_place_one_row` と丈補填の行(`place_tiling_boards`)の**両方が
    この値を使う**こと。計算を分けるとズレます。
    """
    if sum(comp.thin_qty) == 0 and comp.width < ov_w:
        return (ov_w - comp.width) // 2
    return 0


def _place_thin_row(ctx: place.PlacementContext, thickness: int, row_w: int,
                    x_pos: int, y_pos: int, category: str, seq: _Seq) -> None:
    """丈補填の1行を置く(VBA `PlaceThinRow`)。

        幅方向(Y) = 行の幅ぶん。細ボードの長辺が余れば切る
        丈方向(X) = 板厚

    **行の中に並べる1枚とは置き方が違う**ので、`_place_one_row` が使う
    `_place_one_board` とは分けてあります。まとめていたころは
    「幅方向に厚みぶん・丈方向に行幅ぶん」という**逆**の板になって
    いました(VBA も同じ誤りで、専用の `PlaceThinRow` を新設して
    直しています)。

    覆う辺の長さで細ボードの長辺を選ぶのも、この行では**行高ではなく
    行の幅**です ── 寝かせて幅を覆うので、行高では 100×2500 と
    100×2000 が選定リストと実際の配置で食い違います。
    """
    sku_w, sku_l = thin_pair(thickness, row_w)
    number = seq.next()
    board = BoardModel(id=number, width=sku_w, length=sku_l, count=1,
                       board_category=category,
                       instance_id=f"{category}_T{number}")
    # 長辺を幅方向へ寝かせるので rotate=True。境界の判定は通さない
    # (収まることは探索の段階で決まっている)
    # 幅方向の位置(`y_pos`)はメインの行の寄せ量(`row_center_offset`)
    place.place_board_at(ctx, x_pos, y_pos, board, rotate=True,
                         bypass_check=True, custom_width=row_w,
                         custom_length=thickness, is_fill=True)


def _place_one_row(ctx: place.PlacementContext, comp: TileRowComp, height: int,
                   x_pos: int, category: str, seq: _Seq, ov_w: int) -> None:
    """1行を置く。幅補填が2枚以上なら**行内の上下に振り分ける**。

        1本 → 上0 / 下1        3本 → 上1 / 下2
        2本 → 上1 / 下1        4本 → 上2 / 下2

    現行の選定が幅補填でしていることを、行モデルにも持ち込みます
    (`count_width_fill_strips` と同じ `本数 // 2` が上側)。
    丈方向の細ボード行は対象外です ── 丈を継ぎ足すものなので、
    上下に振るという概念が成り立ちません。

    幅補填が無く基準幅(`ov_w`)より狭い行は**中央へ寄せて**置きます
    (`row_center_offset`)。
    """
    limit_w = comp.width
    wf_total = sum(comp.thin_qty)
    wf_upper = wf_total // 2 if wf_total >= 2 else 0

    # 中央寄せ(判定は `row_center_offset` に集約)。
    # **`limit_w` にも同じ量を足す** ── 足さないと、寄せた分だけ最後の
    # 1枚が境界を越えたことになり、カット扱いになる
    off = row_center_offset(comp, ov_w)
    limit_w += off

    y_pos = off
    placed_upper = 0
    for i in range(3):
        for _ in range(comp.thin_qty[i]):
            if placed_upper >= wf_upper:
                break
            _place_one_board(ctx, comp.thin_th[i], height, x_pos, y_pos,
                             limit_w, category, seq,
                             thin=True)
            y_pos += comp.thin_th[i]
            placed_upper += 1
        if placed_upper >= wf_upper:
            break

    for i, dim in enumerate(comp.dims):
        for _ in range(comp.qty[i]):
            if y_pos >= limit_w:
                break
            _place_one_board(ctx, dim, height, x_pos, y_pos, limit_w,
                             category, seq)
            y_pos += min(dim, limit_w - y_pos)

    skipped = 0
    for i in range(3):
        for _ in range(comp.thin_qty[i]):
            if skipped < wf_upper:
                skipped += 1
                continue
            _place_one_board(ctx, comp.thin_th[i], height, x_pos, y_pos,
                             limit_w, category, seq,
                             thin=True)
            y_pos += comp.thin_th[i]


def _place_one_board(ctx: place.PlacementContext, d_y: int, d_x: int,
                     x: int, y: int, limit_w: int, category: str, seq: _Seq,
                     *, thin: bool = False) -> None:
    """**行の中に並べる1枚**を置く。`d_y` が幅方向、`d_x` が丈方向。

    `thin` なら細ボード(幅補填)として扱い、在庫の実寸を表から引きます。
    覆うのは行の丈方向なので、長辺を選ぶ基準は行高(`d_x`)です。

    丈補填の**行**はここを通しません(`_place_thin_row`)── 寝かせて
    幅を覆うので、縦横も長辺の選び方も違います。
    """
    if thin:
        sku_w, sku_l = thin_pair(d_y, d_x)
    else:
        sku_w, sku_l = sku_pair(d_y, d_x)

    use_y = d_y
    if y + use_y > limit_w:
        use_y = limit_w - y
    if use_y <= 0:
        return

    number = seq.next()
    board = BoardModel(id=number, width=sku_w, length=sku_l, count=1,
                       board_category=category,
                       instance_id=f"{category}_T{number}")
    # **境界の判定は通しません。** 収まることは探索の段階で決まって
    # いて、ここは決まった座標へ落とすだけです(VBA も bypassCheck=True)
    place.place_board_at(ctx, x, y, board, rotate=sku_w != d_y,
                         bypass_check=True, custom_width=use_y,
                         custom_length=d_x, is_fill=thin)
