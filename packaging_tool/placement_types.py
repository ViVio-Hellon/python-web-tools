"""配置の型と決まりの数値 ── 置いた結果の入れ物と、置けなかった理由

`PlacementContext` が配置1回ぶんの入れ物。置けたボード、置けなかった
ボードと**その理由**、実測のカット量が入る。

**置けなかった理由を持つのが要点。** 以前は0枚でも「完了: 0枚」とだけ
出ていて、成功と区別が付かなかった(現場の指摘:「追加しても配置すら
しない」)。`explain_unplaced` が、そのボードが何にぶつかったのかを
その場で言葉にする。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .board_scoring import (LOWER_OVERHANG_Y, UPPER_WIDTH_TOLERANCE,
                            get_best_orientation)
from .board_selection_algorithm import (PROTEC_1P1216_TOLERANCE,
                                        PROTEC_OTHER_TOLERANCE,
                                        TAG_CUT_PREMISE, TAG_LENGTH_FILL,
                                        TAG_WIDTH_FILL, ProtecCutResult)
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .logging_utils import get_logger
from .models import BoardModel, PlacedBoardModel

log = get_logger("placement.types")

log = get_logger("placement_algorithm")

CATEGORY_LOWER = "下用"
CATEGORY_UPPER = "上用"

# 「Y積み」= 丈方向は1枚で足りるので幅方向に積み上げる、という配置タグ。
# 選定側ではなく配置側(PlaceBoardsFromList)で動的に付与される。
TAG_Y_STACK = "Y積み"

# グリッド探索のステップ幅(VBA `checkstepsize`)
GRID_STEP = 20

# 既存ボード右端へのスナップ探索のY方向ステップ(下用のみ)
SNAP_STEP = 5

# 最良位置周辺の細探索の範囲(±mm)
FINE_RANGE = 20

# 上用のX方向はみ出し許容量(VBA `CanPlaceAtWithYLimitAndXBound` の
# ローカル定数 `X_OVERHANG_LIMIT`)
X_OVERHANG_LIMIT = 80

# `TryPlaceUpperWithYOffset` のX探索範囲に足す余裕(mm)
UPPER_SCAN_MARGIN = 200

# 主ボードが幅をカバー済みとみなす許容差(mm)
COVERED_TOLERANCE = 3

# タグ推測の対象にする短辺の上限(mm)
FILL_SHORT_SIDE_LIMIT = 100

# `SortFillBoards` で幅補填とみなす短辺の許容超過(mm)
FILL_SORT_TOLERANCE = 5


@dataclass
class UnplacedBoard:
    """置けなかったボード1件。**理由まで持つ。**"""

    category: str
    width: int
    length: int
    count: int
    reason: str

    def label(self) -> str:
        return (f"{self.category} {self.width}x{self.length}"
                f"{f' × {self.count}枚' if self.count > 1 else ''}"
                f": {self.reason}")

def explain_unplaced(ctx: "PlacementContext", width: int, length: int,
                     category: str) -> str:
    """なぜ置けなかったのかを、現場の言葉で1行にする。

    **判定そのものはやり直さない。** 置く側(`try_place_*`)が駄目だと
    決めたあとに、境界と突き合わせて「どこが足りないか」を言うだけ。
    どちらの向きでも幅が入らないのか、幅は入るが丈が足りないのか、
    寸法は足りているが場所が空いていないのか、で次にすることが違う。
    """
    # **現場が知っている数で言う。** 許容ぶんを足した内部の数だけ出すと、
    # 「うちのパレットは550なのに660とは何のことだ」になる
    if category == CATEGORY_UPPER:
        base_w, limit_w = ctx.product.width, ctx.limit_width(CATEGORY_UPPER)
        base_l, limit_l = ctx.product.length, ctx.product.length + X_OVERHANG_LIMIT
        w_name, l_name = "製品幅", "製品丈"
    else:
        base_w, limit_w = ctx.palette.width, int(ctx.palette.width * LOWER_OVERHANG_Y)
        base_l, limit_l = ctx.palette.length, ctx.palette.length
        w_name, l_name = "パレット幅", "パレット丈"

    def bound(base: int, limit: int) -> str:
        """許容ぶんが乗っているなら、そこまで言う。"""
        return (f"{base}mm" if limit == base
                else f"{base}mm(はみ出し許容を入れて{limit}mm)")

    # 幅方向に入る向き(短辺でも入らなければ、どう置いても入らない)
    fits = [(w, l) for w, l in ((width, length), (length, width)) if w <= limit_w]
    if not fits:
        return (f"幅が{w_name}{bound(base_w, limit_w)}を超えています"
                f"(どちらの向きでも {min(width, length)}mm)")

    shortest = min(l for _w, l in fits)
    if shortest > limit_l:
        return (f"丈が{l_name}{bound(base_l, limit_l)}を超えています"
                f"(入る向きで {shortest}mm)")

    return "寸法は入りますが、置ける場所が残っていません(先のボードで埋まっています)"

@dataclass
class PlacementContext:
    """配置中の状態(VBA のモジュール変数 `placedBoards` 相当)。

    `lower_order`/`upper_order` には `sort_fill_boards` による並べ替え後の
    ボード順を保持する。VBA版は`lstSelectedBoards*`(画面上のリストボックス)
    そのものを並べ替えていたが、Python版は引数のリストを書き換えると
    呼び出し側にとって予期しない副作用になるため、コピーを並べ替えて
    ここに残す方式にしている(UIに反映したい場合はこちらを参照する)。
    """

    palette: Palette
    product: ProductSize
    placed: list[PlacedBoardModel] = field(default_factory=list)
    lower_order: list[SelectedBoard] = field(default_factory=list)
    upper_order: list[SelectedBoard] = field(default_factory=list)
    # プロテック確定値(選定が決めた「唯一の正解」)。カット前提ボードの
    # 配置(`_place_cut_premise`)が、ここにある値をそのまま使う。
    # valid=False(既定)なら、従来どおりパレット幅/製品幅で計算する
    protec_result: ProtecCutResult = field(default_factory=lambda: ProtecCutResult())
    # プロテックモード中かどうか(VBA `MaterialMasterForm.mIsProtecMode`)。
    # `try_place_inside_palette` が、選定(`decide_protec_orientation`)を
    # 経由しない手動追加ボードにも向き決定ルールを適用するために使う。
    is_protec_mode: bool = False
    is_1p1216: bool = False
    # 配置段階で判明した幅カット(VBA `mCutInfo`)。丈補填ボードの
    # 在庫の実寸(長辺)が limit_w を超えるときだけ `place_length_fill_boards`
    # が記録する。キーは在庫の"幅x丈"、値は短辺(丈方向のサイズ)
    cut_info: dict[str, int] = field(default_factory=dict)
    # **置けなかったボードと、その理由。**
    #
    # 以前は置けないボードを黙って飛ばしていた。手で「上用へ追加」した
    # のに図に出てこず、選定ログにも何も残らないので、現場からは
    # 「追加しても配置すらしない、このボタンはいらないのでは」に
    # しか見えない(実際にそう言われた)。断るなら理由を言う
    unplaced: list["UnplacedBoard"] = field(default_factory=list)

    def note_unplaced(self, category: str, width: int, length: int,
                      count: int) -> None:
        """置けなかったことを、理由付きで覚える。"""
        self.unplaced.append(UnplacedBoard(
            category=category, width=width, length=length, count=count,
            reason=explain_unplaced(self, width, length, category)))

    def limit_width(self, category: str) -> int:
        """カテゴリごとの幅方向の基準値(VBA `limitW`)。

        上用は製品幅、下用はパレット幅が境界(通常モード)。

        【プロテックも上用は製品幅が境界のまま】
        プロテックは「上下とも製品幅よりマイナス(1P1216は-10mm、
        それ以外は-80mm)」というルールで、上用も下用と同じく製品幅を
        超えてはいけない ── 上用だけパレット幅まで緩めるのは誤り
        (以前そう実装していたが誤りだったため元に戻した)。選定
        (`decide_protec_orientation`)が既に「製品幅を超えない
        カット後サイズ」を確定させているので、配置側で境界を緩める
        必要はない。プロテックの下用がカット前提タグで配置される
        ときも、実際に置くのは `ProtecCutResult.cut_eff_width`
        (製品幅以下)であり、この境界と矛盾しない。
        """
        return self.product.width if category == CATEGORY_UPPER else self.palette.width

    def max_x(self, category: str, *, fill: Optional[bool] = None, y: Optional[int] = None) -> int:
        """同カテゴリ配置済みボードのX右端の最大値。

        `fill` を指定すると `is_fill_board` で、`y` を指定すると Y座標で
        絞り込む(VBA側に何度も現れる `maxX` 算出パターンの共通化)。
        """
        result = 0
        for pb in self.placed:
            if pb.board_category != category:
                continue
            if fill is not None and pb.is_fill_board != fill:
                continue
            if y is not None and pb.y != y:
                continue
            result = max(result, pb.x + pb.length)
        return result

    def max_y(self, category: str, *, fill: Optional[bool] = None, y: Optional[int] = None) -> int:
        """同カテゴリ配置済みボードのY下端の最大値。"""
        result = 0
        for pb in self.placed:
            if pb.board_category != category:
                continue
            if fill is not None and pb.is_fill_board != fill:
                continue
            if y is not None and pb.y != y:
                continue
            result = max(result, pb.y + pb.width)
        return result


def _make_model(b: SelectedBoard, idx: int, category: str, prefix: str = "") -> BoardModel:
    """`SelectedBoard`(選定結果の1行)を配置用の`BoardModel`に変換する。"""
    return BoardModel(
        id=idx + 1,
        width=b.width,
        length=b.length,
        count=b.count,
        instance_id=f"{category}_{prefix}{idx + 1}",
        board_category=category,
    )

def evaluate_placement(x: int, y: int) -> float:
    """VBA `EvaluatePlacement` の移植。左上に近いほど良い(小さいほど良い)。

    VBA版は使われない `length`/`width` も引数に取っていたが、本体は
    `CDbl(x) + CDbl(y)` のみだったため引数から落としている。
    """
    return float(x) + float(y)

@dataclass
class RotationState:
    """VBA の `ByRef firstPlaced` / `ByRef firstRotation` を束ねたもの。

    「1枚目で決めた向きを2枚目以降にも引き継ぐ」ためのキャリー変数。
    """

    first_placed: bool = False
    first_rotation: bool = False
