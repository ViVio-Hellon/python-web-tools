"""資材配置アルゴリズム (VBA `MaterialMasterForm` の配置系の移植)

選定済みのボード(`SelectedBoard`)を、パレット/製品の座標平面上の
どこに何枚置くかを決める処理。VBAソースを1行ずつ確認して移植した。

対応関係:
    AutoPlaceBoards               -> auto_place_boards
    PlaceBoardsFromList            -> place_boards_from_list
    PlaceNarrowPaletteBoards(Upper) -> place_narrow_palette_boards
    PlaceLengthFillBoards            -> place_length_fill_boards
    CheckLengthFillOverlap            -> _check_length_fill_overlap
    SortFillBoards                     -> sort_fill_boards
    GetEffectiveTag                     -> get_effective_tag
    PlaceBoardAt                         -> place_board_at
    CanPlaceBoardAt                       -> can_place_board_at
    CanPlaceAtWithYLimit                   -> can_place_at_with_y_limit
    CanPlaceAtWithYLimitAndXBound           -> can_place_at_with_y_limit_and_x_bound
    EvaluatePlacement                        -> evaluate_placement
    TryPlaceSingleOrientation                 -> try_place_single_orientation
    TryPlaceInsidePalette                      -> try_place_inside_palette
    TryPlaceWithFixedRotation                   -> try_place_with_fixed_rotation
    TryPlaceUpperWithYOffset                     -> try_place_upper_with_y_offset
    TryPlaceUpperLengthFill                       -> try_place_upper_length_fill
    TryPlaceYCompanion                             -> try_place_y_companion

【座標系】
    VBA版と同じく X = 丈方向、Y = 幅方向 で扱う。
    配置結果 `PlacedBoardModel` の `width` はY方向の寸法、
    `length` はX方向の寸法を表す(名前と軸が直感に反するがVBA踏襲)。

【上用と下用の違い】
    - 下用: パレット幅を基準にし、Y方向は `LOWER_OVERHANG_Y`(20%)まで
      はみ出しを許容。X方向はパレット丈まで。
    - 上用: 製品幅を基準にし、Y方向は `UPPER_WIDTH_TOLERANCE`(80mm)まで。
      X方向は製品丈未満(`x >= productSize.length` は禁止)。

【VBA側の未使用コード(移植対象外)】
    以下7つは、VBAソース全3ファイルを検索しても呼び出し元が存在しない
    (定義と自身の本体・デバッグログのみ)デッドコードだったため移植していない。
    いずれも `Private` 宣言なので、呼び出せるのは同一モジュール
    (MaterialMasterForm)内だけであり、他モジュールからの参照はあり得ない。

        TryPlaceLowerLengthFill   下用丈補填 → PlaceLengthFillBoards に統合済み
        TryPlaceLowerWidthFill    下用幅補填 → TryPlaceYCompanion に統合済み
        TryPlaceLowerFill         下用補填(旧世代)
        GetLowerRemainingWidth    上記の補助
        GetLowerRemainingLength   上記の補助
        SortBoardsByArea          面積順ソート(配置順の決定に使われていない)
        TryPlaceSpecificRotation  回転指定配置 → TryPlaceWithFixedRotation に統合済み

    下用の補填配置は実際には `place_length_fill_boards`(丈補填)と
    `try_place_y_companion`(幅補填)が担っており、どちらも上用・下用の
    両方に対応している。上記は「カテゴリ別に分かれていた旧実装」が
    共通化されたあとも残されたもの、と読める。

    なおVBAソースL3380付近には作者による設計コメントがあり、そこでは
    `PlaceBoardsFromList` から
    「`TryPlaceUpperWithYOffset` / `TryPlaceLowerFill`」を呼ぶ想定と
    書かれている。しかし実コードには上用の分岐(`category = "上用" And i > 0`)
    しか存在せず、対になる下用の分岐は書かれていない。
    設計意図としては下用にも専用経路があるはずだったが未実装のまま、
    という状態(下用は汎用探索で処理されるため機能上の欠落はない)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .board_scoring import (
    LOWER_OVERHANG_Y,
    UPPER_WIDTH_TOLERANCE,
    get_best_orientation,
)
from .board_selection_algorithm import (
    TAG_CUT_PREMISE,
    TAG_LENGTH_FILL,
    TAG_WIDTH_FILL,
)
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .logging_utils import get_logger
from .models import BoardModel, PlacedBoardModel

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

    def limit_width(self, category: str) -> int:
        """カテゴリごとの幅方向の基準値(VBA `limitW`)。"""
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


# ------------------------------------------------------------------
# 配置可否判定・評価
# ------------------------------------------------------------------
def evaluate_placement(x: int, y: int) -> float:
    """VBA `EvaluatePlacement` の移植。左上に近いほど良い(小さいほど良い)。

    VBA版は使われない `length`/`width` も引数に取っていたが、本体は
    `CDbl(x) + CDbl(y)` のみだったため引数から落としている。
    """
    return float(x) + float(y)


def _dims(board: BoardModel, rotate: bool) -> tuple[int, int]:
    """回転フラグを寸法に反映する (Y方向=幅, X方向=丈) を返す。"""
    return (board.length, board.width) if rotate else (board.width, board.length)


def _overlaps(x: int, y: int, w: int, l: int, pb: PlacedBoardModel) -> bool:
    """矩形 (x, y, 丈=l, 幅=w) と配置済みボード `pb` が重なるか。"""
    return (x < pb.x + pb.length and x + l > pb.x
            and y < pb.y + pb.width and y + w > pb.y)


def can_place_board_at(
    ctx: PlacementContext, x: int, y: int, board: BoardModel, rotate: bool = False,
) -> bool:
    """VBA `CanPlaceBoardAt` の移植。

    境界条件(カテゴリ別)と、同カテゴリの既配置ボードとの重なりを判定する。
    """
    board_width, board_length = _dims(board, rotate)

    if board.board_category == CATEGORY_UPPER:
        # X<0 に加えて X >= 製品丈 も禁止
        if x < 0 or x >= ctx.product.length:
            return False
        if y < 0 or y + board_width > ctx.product.width + UPPER_WIDTH_TOLERANCE:
            return False
    else:
        if x < 0:
            return False
        # 下用はY方向の負側にも overhangRatio 分だけはみ出せる
        if y < -board_width * (ctx.palette.overhang_ratio - 1):
            return False
        if y + board_width > int(ctx.palette.width * LOWER_OVERHANG_Y):
            return False

    for pb in ctx.placed:
        if pb.board_category != board.board_category:
            continue
        if _overlaps(x, y, board_width, board_length, pb):
            return False
    return True


def can_place_at_with_y_limit(
    ctx: PlacementContext, x: int, y: int, board: BoardModel, rotate: bool, limit_w: int,
) -> bool:
    """VBA `CanPlaceAtWithYLimit` の移植(上用・下用共通のY固定版)。

    `can_place_board_at` と違い、Y方向の上限は `limit_w` ちょうど
    (はみ出し許容なし)で、Y方向下限のチェックも行わない。
    """
    board_width, board_length = _dims(board, rotate)

    if x < 0:
        return False
    if y + board_width > limit_w:
        return False

    for pb in ctx.placed:
        if pb.board_category != board.board_category:
            continue
        if _overlaps(x, y, board_width, board_length, pb):
            return False
    return True


def can_place_at_with_y_limit_and_x_bound(
    ctx: PlacementContext, x: int, y: int, board: BoardModel, rotate: bool,
    limit_w: int, x_bound: int,
) -> bool:
    """VBA `CanPlaceAtWithYLimitAndXBound` の移植(上用専用)。

    `can_place_at_with_y_limit` にX方向の上限チェックを足したもの。
    """
    _, board_length = _dims(board, rotate)
    if x + board_length > x_bound + X_OVERHANG_LIMIT:
        return False
    return can_place_at_with_y_limit(ctx, x, y, board, rotate, limit_w)


def place_board_at(
    ctx: PlacementContext, x: int, y: int, board: BoardModel, rotate: bool = False,
    *, bypass_check: bool = False, custom_width: int = 0, custom_length: int = 0,
    is_fill: bool = False,
) -> bool:
    """VBA `PlaceBoardAt` の移植。配置を確定して `placed` に追加する。

    `custom_width`/`custom_length` を両方指定すると、その寸法で配置する
    (カット前提・丈補填でパレット幅いっぱいに敷く場合に使う)。
    `original_*` には回転を反映した「カット前の実寸」を保持する。
    """
    if not bypass_check and not can_place_board_at(ctx, x, y, board, rotate):
        log.debug("配置失敗(最終チェック): %s,%s", x, y)
        return False

    original_width, original_length = _dims(board, rotate)

    if custom_width > 0 and custom_length > 0:
        placed_width, placed_length = custom_width, custom_length
    else:
        placed_width, placed_length = original_width, original_length

    ctx.placed.append(PlacedBoardModel(
        id=board.id,
        width=placed_width,
        length=placed_length,
        instance_id=f"{board.instance_id}_{len(ctx.placed) + 1}",
        x=x,
        y=y,
        board_category=board.board_category,
        original_width=original_width,
        original_length=original_length,
        is_fill_board=is_fill,
    ))
    log.debug("配置完了: %s %sx%s at(%s,%s) isFill=%s",
              board.board_category, placed_width, placed_length, x, y, is_fill)
    return True


# ------------------------------------------------------------------
# 位置探索
# ------------------------------------------------------------------
def try_place_single_orientation(
    ctx: PlacementContext, board: BoardModel, rotate: bool, min_waste: float,
) -> Optional[tuple[int, int, float]]:
    """VBA `TryPlaceSingleOrientation` の移植(単一向きでの位置探索)。

    3段階で探索する:
        PASS1: パレット内をグリッド(20mm刻み)で粗探索
        PASS2: (下用のみ)既存ボードの右端にスナップして5mm刻みで探索
        PASS3: 最良位置の周辺±20mmを1mm刻みで細探索
    戻り値は (x, y, 更新後のminWaste)。見つからなければ None。
    """
    w, l = _dims(board, rotate)

    if board.board_category == CATEGORY_UPPER:
        end_y = ctx.product.width - w        # 上用: 製品幅超過厳禁
        end_x = ctx.palette.length * 5
    else:
        end_y = int(ctx.palette.width * LOWER_OVERHANG_Y) - w
        end_x = ctx.palette.length - l

    # end_y < 0 は本当に配置不可。end_x < 0 でもスナップ探索は試す
    if end_y < 0:
        return None

    best_x = best_y = 0
    found = False

    # PASS1: グリッド粗探索
    if end_x >= 0:
        for y in range(0, end_y + 1, GRID_STEP):
            for x in range(0, end_x + 1, GRID_STEP):
                if can_place_board_at(ctx, x, y, board, rotate):
                    waste = evaluate_placement(x, y)
                    if waste < min_waste:
                        min_waste, best_x, best_y, found = waste, x, y, True

    # PASS2: 既存ボードの右端にスナップ(下用のみ)
    if board.board_category == CATEGORY_LOWER:
        for pb in list(ctx.placed):
            if pb.board_category != board.board_category:
                continue
            snap_x = pb.x + pb.length
            for y in range(0, end_y + 1, SNAP_STEP):
                if can_place_board_at(ctx, snap_x, y, board, rotate):
                    waste = evaluate_placement(snap_x, y)
                    if waste < min_waste:
                        min_waste, best_x, best_y, found = waste, snap_x, y, True

    # PASS3: 最良位置周辺の細探索
    if found:
        f_start_x = max(0, best_x - FINE_RANGE)
        f_end_x = best_x + FINE_RANGE
        f_start_y = max(0, best_y - FINE_RANGE)
        f_end_y = min(best_y + FINE_RANGE, end_y)
        for fy in range(f_start_y, f_end_y + 1):
            for fx in range(f_start_x, f_end_x + 1):
                if can_place_board_at(ctx, fx, fy, board, rotate):
                    fine_waste = evaluate_placement(fx, fy)
                    if fine_waste < min_waste:
                        min_waste, best_x, best_y = fine_waste, fx, fy

    return (best_x, best_y, min_waste) if found else None


def try_place_inside_palette(ctx: PlacementContext, board: BoardModel) -> bool:
    """VBA `TryPlaceInsidePalette` の移植。

    対象幅に近い向きを先に試し、駄目ならもう一方の向きを試す。
    """
    target_width = ctx.limit_width(board.board_category)
    dist_normal = abs(board.width - target_width)
    dist_rotated = abs(board.length - target_width)
    try_normal_first = dist_normal <= dist_rotated

    log.debug("TryPlaceInsidePalette: %sx%s target=%s distN=%s distR=%s normalFirst=%s",
              board.width, board.length, target_width, dist_normal, dist_rotated, try_normal_first)

    order = (False, True) if try_normal_first else (True, False)
    # VBA版は minWaste を ByRef で2回の試行に引き継ぐが、1回目が失敗した
    # 場合 minWaste は更新されないため、素直に初期値のまま渡してよい。
    min_waste = 1e15
    for rotate in order:
        result = try_place_single_orientation(ctx, board, rotate, min_waste)
        if result is not None:
            x, y, _ = result
            return place_board_at(ctx, x, y, board, rotate)
    return False


def try_place_with_fixed_rotation(
    ctx: PlacementContext, board: BoardModel, use_rotation: bool,
) -> bool:
    """VBA `TryPlaceWithFixedRotation` の移植(向きを固定した配置)。"""
    min_waste = float(ctx.palette.length) * float(ctx.palette.width) * 10
    result = try_place_single_orientation(ctx, board, use_rotation, min_waste)
    if result is None:
        return False
    x, y, _ = result
    return place_board_at(ctx, x, y, board, use_rotation)


# ------------------------------------------------------------------
# 上用専用の配置(Y固定 / 丈補填)
# ------------------------------------------------------------------
@dataclass
class RotationState:
    """VBA の `ByRef firstPlaced` / `ByRef firstRotation` を束ねたもの。

    「1枚目で決めた向きを2枚目以降にも引き継ぐ」ためのキャリー変数。
    """

    first_placed: bool = False
    first_rotation: bool = False


def try_place_upper_with_y_offset(
    ctx: PlacementContext, board: BoardModel, y_offset: int, state: RotationState,
) -> bool:
    """VBA `TryPlaceUpperWithYOffset` の移植(上用専用・Y固定配置)。

    Y座標を `y_offset` に固定し、X方向のみ20mm刻みで走査して
    最も左に置ける位置を探す。向きは1枚目で決めたものを引き継ぐ。
    """
    limit_w = ctx.limit_width(board.board_category)

    # 残りY空間を基準に向きを決定(limitWはyOffsetで減らさない)
    remaining_y = limit_w - y_offset
    rot_tmp, eff_w, eff_l = get_best_orientation(board, remaining_y)

    log.debug("TryPlaceUpperWithYOffset: %sx%s yOffset=%s limitW=%s remainingY=%s "
              "effW=%s effL=%s rot=%s",
              board.width, board.length, y_offset, limit_w, remaining_y,
              eff_w, eff_l, rot_tmp)

    if not state.first_placed:
        state.first_rotation = rot_tmp
        state.first_placed = True

    b_width, _ = _dims(board, state.first_rotation)

    if y_offset + b_width > limit_w + UPPER_WIDTH_TOLERANCE:
        log.debug("  Y方向オーバー → 配置不可")
        return False

    # X探索上限: 上用ボード最大X端 + effL + バッファ(最低でも製品丈まで)
    search_end_x = max(ctx.max_x(CATEGORY_UPPER) + eff_l + UPPER_SCAN_MARGIN,
                       ctx.product.length)
    x_bound = ctx.product.length

    best_x = -1
    min_waste = 1e15
    for x in range(0, search_end_x + 1, GRID_STEP):
        if can_place_at_with_y_limit_and_x_bound(
                ctx, x, y_offset, board, state.first_rotation, limit_w, x_bound):
            if float(x) < min_waste:
                min_waste = float(x)
                best_x = x

    log.debug("  bestX=%s", best_x)
    if best_x < 0:
        return False
    return place_board_at(ctx, best_x, y_offset, board, state.first_rotation, bypass_check=True)


def try_place_upper_length_fill(ctx: PlacementContext, board: BoardModel) -> bool:
    """VBA `TryPlaceUpperLengthFill` の移植(上用丈補填・X方向末尾に配置)。

    Y=0の主ボード列の右端に、長辺をY方向・短辺をX方向にして継ぎ足す。
    寸法は元のまま置く(はみ出しは描画側で表現する、というVBA側の方針)。
    """
    max_x = ctx.max_x(CATEGORY_UPPER, y=0)
    main_max_y = ctx.max_y(CATEGORY_UPPER, y=0)

    # 長辺→Y方向(b_width), 短辺→X方向(b_length)
    rotate_flag = board.width < board.length
    b_width, b_length = _dims(board, rotate_flag)

    log.debug("TryPlaceUpperLengthFill: %sx%s maxX=%s mainMaxY=%s → bW(Y)=%s bL(X)=%s",
              board.width, board.length, max_x, main_max_y, b_width, b_length)

    if b_width <= 0 or b_length <= 0:
        return False

    # 重なりチェック: Y=0の主ボードのみ対象
    for pb in ctx.placed:
        if pb.board_category != CATEGORY_UPPER or pb.y != 0:
            continue
        if _overlaps(max_x, 0, b_width, b_length, pb):
            log.debug("  重なり検出(主ボード) → 配置不可")
            return False

    return place_board_at(ctx, max_x, 0, board, rotate_flag, bypass_check=True)


def try_place_y_companion(ctx: PlacementContext, board: BoardModel) -> bool:
    """VBA `TryPlaceYCompanion` の移植(幅補填ボードの配置)。

    主ボードのうち「幅方向のカバレッジが最も足りない1枚」(`gap_board`)を
    基準にして、そのY端・X位置から幅補填を敷いていく。
    既に補填行があり右端に余裕があれば同じ行に継ぎ足し、
    行が埋まっていれば次の行(Y方向に1段下)へ送る。
    """
    category = board.board_category

    # Step1: 幅方向カバレッジが最も短い主ボードを特定
    gap_board: Optional[PlacedBoardModel] = None
    min_end_y = 2147483647
    for pb in ctx.placed:
        if pb.board_category == category and not pb.is_fill_board:
            if pb.y + pb.width < min_end_y:
                min_end_y = pb.y + pb.width
                gap_board = pb
    if gap_board is None:
        return False

    # Step2: 全主ボードのX右端(上用は製品丈でクランプ)
    main_x_end = ctx.max_x(category, fill=False)
    if category == CATEGORY_UPPER:
        main_x_end = min(main_x_end, ctx.product.length)

    # Step3: 既存の幅補填ボードの状態(最新行のY座標とその右端)
    fill_max_y = min_end_y - 1  # 初期値: 補填行なし
    for pb in ctx.placed:
        if pb.board_category == category and pb.is_fill_board:
            fill_max_y = max(fill_max_y, pb.y)
    cur_row_end_x = ctx.max_x(category, fill=True, y=fill_max_y)

    # Step4: placeY と fillEndX を決定
    if fill_max_y >= min_end_y and cur_row_end_x < main_x_end:
        # 既存行に空きあり → 同じ行に続けて配置
        place_y = fill_max_y
        fill_end_x = cur_row_end_x
    elif fill_max_y >= min_end_y:
        # 新しい行へ: 前の行の実幅ぶんだけY方向に下げる
        prev_row_bw = 0
        for pb in ctx.placed:
            if pb.board_category == category and pb.is_fill_board and pb.y == fill_max_y:
                prev_row_bw = max(prev_row_bw, pb.width)
        if prev_row_bw <= 0:
            prev_row_bw = min(board.width, board.length)
        place_y = fill_max_y + prev_row_bw
        fill_end_x = 0
    else:
        # 補填ボードが1枚もない場合は gap_board のY端から始める
        place_y = min_end_y
        fill_end_x = 0

    # Step5: placeX を決定(補填行が無ければ gap_board のX位置が起点)
    place_x = fill_end_x if fill_end_x > 0 else gap_board.x

    end_x = main_x_end
    if category == CATEGORY_UPPER:
        end_x = min(end_x, ctx.product.length)
    if place_x >= end_x:
        return False

    # Step6: 短辺をY方向にした寸法にし、残り丈でカットする
    rot_flag = board.width > board.length
    b_width, b_length = _dims(board, rot_flag)

    avail_l = end_x - place_x
    b_length = min(b_length, avail_l)
    if b_length <= 0:
        return False

    log.debug("TryPlaceYCompanion: %sx%s → at(%s,%s) bW=%s bL=%s",
              board.width, board.length, place_x, place_y, b_width, b_length)

    return place_board_at(
        ctx, place_x, place_y, board, rot_flag,
        bypass_check=True, custom_width=b_width, custom_length=b_length, is_fill=True,
    )


# ------------------------------------------------------------------
# タグ推測・補填ボードの並べ替え
# ------------------------------------------------------------------
def get_effective_tag(
    boards: list[SelectedBoard], idx: int, category: str, ctx: PlacementContext,
) -> str:
    """VBA `GetEffectiveTag` の移植。

    タグが設定されていればそのまま返す。未設定の場合は短辺100mm以下かつ
    index>0 のときだけ推測し、主ボード(index=0)が幅をカバー済み(±3mm)なら
    "丈補填"、幅不足なら "幅補填" とみなす。
    (保存済みパターンの読込等でタグが失われた場合の復元用)
    """
    b = boards[idx]
    tag = b.tag.strip()
    if tag:
        return tag
    if min(b.width, b.length) > FILL_SHORT_SIDE_LIMIT or idx == 0:
        return ""

    limit_w = ctx.limit_width(category)
    main = boards[0]
    main_model = BoardModel(width=main.width, length=main.length, board_category=category)
    _, main_eff_w, _ = get_best_orientation(main_model, limit_w)
    return TAG_LENGTH_FILL if abs(main_eff_w - limit_w) <= COVERED_TOLERANCE else TAG_WIDTH_FILL


def sort_fill_boards(boards: list[SelectedBoard], start_idx: int, remaining_y: int) -> None:
    """VBA `SortFillBoards` の移植(リストを破壊的に並べ替える)。

    `start_idx` 以降を「残りY空間に収まるもの(=幅補填候補)」→
    「収まらないもの(=丈補填候補)」の順に並べ替える。
    元の並び順は各グループ内で保たれる(VBA版もCollectionへ順に
    詰め直すだけなので安定)。
    """
    if start_idx >= len(boards):
        return

    width_fills: list[SelectedBoard] = []
    length_fills: list[SelectedBoard] = []
    for b in boards[start_idx:]:
        short_side = min(b.width, b.length)
        if short_side <= remaining_y + FILL_SORT_TOLERANCE:
            width_fills.append(b)
        else:
            length_fills.append(b)

    boards[start_idx:] = width_fills + length_fills


# ------------------------------------------------------------------
# 丈補填の配置
# ------------------------------------------------------------------
def _check_length_fill_overlap(
    ctx: PlacementContext, category: str, place_x: int, b_short: int, limit_w: int,
) -> bool:
    """VBA `CheckLengthFillOverlap` の移植。

    丈補填を (place_x, 0) に幅 limit_w・丈 b_short で置く前に、
    同カテゴリの既配置ボードと重ならないか確認する。
    True=配置可 / False=重なりあり。
    """
    for pb in ctx.placed:
        if pb.board_category != category:
            continue
        if _overlaps(place_x, 0, limit_w, b_short, pb):
            log.debug("CheckLengthFillOverlap: 重なり検出 placeX=%s bShort=%s limitW=%s "
                      "vs pb(%s,%s %sx%s)",
                      place_x, b_short, limit_w, pb.x, pb.y, pb.length, pb.width)
            return False
    return True


def place_length_fill_boards(
    ctx: PlacementContext, boards: list[SelectedBoard], category: str,
) -> None:
    """VBA `PlaceLengthFillBoards` の移植。

    丈補填ボードは「既存ボードの最大X端の続き・Y=0」に、
    長辺をY方向(幅方向)・短辺をX方向(丈方向)に固定して敷き詰める。
    幅は `limit_w`(上用=製品幅 / 下用=パレット幅)で固定するため、
    幅方向のギャップは原理的に発生しない。
    """
    limit_w = ctx.limit_width(category)

    for i, b in enumerate(boards):
        if get_effective_tag(boards, i, category, ctx) != TAG_LENGTH_FILL:
            continue

        model = _make_model(b, i, category, prefix="LF_")
        # 長辺をY方向(幅方向)にするため、lengthが長辺なら回転させる
        lf_rot = b.length > b.width
        b_short = min(b.width, b.length)

        for _ in range(b.count):
            place_x = ctx.max_x(category)

            if not _check_length_fill_overlap(ctx, category, place_x, b_short, limit_w):
                log.debug("[丈補填配置] 重なり検出 → スキップ %sx%s at(%s,0)",
                          b.width, b.length, place_x)
                break

            log.debug("[丈補填配置] %sx%s at(%s,0) bW=%s bL=%s",
                      b.width, b.length, place_x, limit_w, b_short)
            place_board_at(
                ctx, place_x, 0, model, lf_rot,
                bypass_check=True, custom_width=limit_w, custom_length=b_short, is_fill=True,
            )


# ------------------------------------------------------------------
# メインの配置ループ
# ------------------------------------------------------------------
def _place_cut_premise(
    ctx: PlacementContext, b: SelectedBoard, idx: int, category: str,
) -> None:
    """カット前提ボードの配置(VBA `PlaceBoardsFromList` 内のインライン処理)。

    長辺をY方向(幅)にして対象幅でカットし、短辺ぶんずつX方向に並べる。
    """
    model = _make_model(b, idx, category)
    wc_short = min(b.width, b.length)
    b_rot_cut = b.width < b.length          # 長辺をY方向(幅)へ
    wc_target_w = ctx.limit_width(category)

    xp_cut = 0
    for _ in range(b.count):
        if xp_cut >= ctx.product.length:
            break
        place_board_at(
            ctx, xp_cut, 0, model, b_rot_cut,
            bypass_check=True, custom_width=wc_target_w, custom_length=wc_short,
        )
        xp_cut += wc_short


def _try_convert_to_y_stack(
    ctx: PlacementContext, board: BoardModel, rot: bool, eff_l: int,
) -> bool:
    """VBA の「手動追加ボード(タグ空)への自動Y積み救済ロジック」の移植。

    今の向きでは丈が足りないが、反転すれば丈が足り、かつ製品幅を
    ほぼ使い切る枚数(端数50mm以内)で並べられる場合だけY積みに変換する。
    """
    if eff_l >= ctx.product.length - COVERED_TOLERANCE:
        return False

    # 反転した場合の寸法
    alt_w, alt_l = _dims(board, not rot)
    if alt_l < ctx.product.length - COVERED_TOLERANCE:
        return False
    if alt_w <= 0:
        return False

    count = ctx.product.width // alt_w
    if count > 0 and (ctx.product.width - alt_w * count) <= 50:
        log.debug("  手動ボードをY積みに自動変換: %sx%s", board.width, board.length)
        return True
    return False


def place_boards_from_list(
    ctx: PlacementContext, source: list[SelectedBoard], category: str,
) -> list[SelectedBoard]:
    """VBA `PlaceBoardsFromList` の移植(2パス構成の配置ループ)。

        pNum=1: 主ボード・カット前提・Y積みを配置し、その後に丈補填を配置
        pNum=2: 幅補填を `try_place_y_companion` で主ボードの下端に敷く

    戻り値は `sort_fill_boards` による並べ替えを反映したボード順。
    (VBA版は画面のリストボックスを直接並べ替えていたが、Python版は
    引数を書き換えないようコピーを操作する)
    """
    boards = list(source)
    if not boards:
        return boards
    log.debug("%sボード配置開始: %s種類", category, len(boards))

    limit_w = ctx.limit_width(category)

    # VBA側は Sub スコープの Dim なので、ループを跨いで値が残る。
    # 特に pNum=2 では GetBestOrientation を呼ばないため、rot/eff_w/eff_l は
    # pNum=1 で最後に計算された値がそのまま使われる(VBAの挙動を踏襲)。
    rot = False
    eff_w = 0
    eff_l = 0
    # last_b も同様に Sub スコープ。後述の既知の不具合の原因になる。
    last_b: Optional[PlacedBoardModel] = None

    for pass_num in (1, 2):
        y_off = 0
        if pass_num == 2:
            for pb in ctx.placed:
                if pb.board_category == category and not pb.is_fill_board:
                    y_off = max(y_off, pb.y + pb.width)

        # sort_fill_boards が i=0 の処理後に boards を並べ替えるため、
        # enumerate ではなくインデックス参照で「その時点の並び」を読む。
        # (VBA も For の上限だけを開始時に確定し、中身は都度読み直す)
        board_count = len(boards)
        for i in range(board_count):
            b = boards[i]
            tag = b.tag.strip()
            effective_tag = get_effective_tag(boards, i, category, ctx)

            if effective_tag == TAG_CUT_PREMISE:
                # 【VBAからの意図的な変更】VBA版のカット前提ブロックには
                # pNum の判定が無く、しかも bypassCheck=True で重なり判定も
                # 効かないため、同じボードが pNum=1 と pNum=2 で
                # まったく同じ座標に二重配置されていた(描画上は重なって
                # 見えないが枚数が倍になる)。pNum=1 のみに限定して修正する。
                if pass_num == 1:
                    _place_cut_premise(ctx, b, i, category)
                continue

            if pass_num == 1 and effective_tag in (TAG_WIDTH_FILL, TAG_LENGTH_FILL):
                continue
            if pass_num == 2 and effective_tag != TAG_WIDTH_FILL:
                continue

            model = _make_model(b, i, category)

            # pNum=2(幅補填)は try_place_y_companion が独自に向きを決めるため
            # GetBestOrientation は不要(limitW - yOff が意味を持たない)
            if pass_num == 1:
                rot, eff_w, eff_l = get_best_orientation(model, limit_w - y_off)

            # 手動追加ボード(タグ空)への自動Y積み救済
            if category == CATEGORY_UPPER and not tag:
                if _try_convert_to_y_stack(ctx, model, rot, eff_l):
                    tag = TAG_Y_STACK

            log.debug("  %s%s: %sx%s Type=[%s]", category, model.id, b.width, b.length, tag)

            state = RotationState(first_placed=False, first_rotation=rot)
            is_l_fill = False

            for _ in range(b.count):
                if tag == TAG_Y_STACK:
                    # 短辺を幅(Y)にする向きを強制
                    y_rot = b.width > b.length
                    placed_ok = try_place_with_fixed_rotation(ctx, model, y_rot)
                    if placed_ok:
                        last_b = ctx.placed[-1]
                        y_off += last_b.width
                elif pass_num == 2:
                    placed_ok = try_place_y_companion(ctx, model)
                elif category == CATEGORY_UPPER and i > 0:
                    rem_y = ctx.product.width - y_off
                    s_side = min(b.width, b.length)
                    is_l_fill = s_side > rem_y + 5
                    if is_l_fill:
                        placed_ok = try_place_upper_length_fill(ctx, model)
                    else:
                        placed_ok = try_place_upper_with_y_offset(ctx, model, y_off, state)
                elif tag == TAG_LENGTH_FILL:
                    placed_ok = try_place_with_fixed_rotation(ctx, model, state.first_rotation)
                elif not state.first_placed:
                    placed_ok = try_place_inside_palette(ctx, model)
                    if placed_ok:
                        state.first_placed = True
                        last_b = ctx.placed[-1]
                        # 実際に採用された向きを以降の枚数に引き継ぐ
                        if category == CATEGORY_UPPER:
                            state.first_rotation = last_b.width == b.length
                        else:
                            state.first_rotation = last_b.width != b.width
                else:
                    placed_ok = try_place_with_fixed_rotation(ctx, model, state.first_rotation)

                if not placed_ok:
                    break

            if pass_num == 1 and tag != TAG_Y_STACK:
                if not is_l_fill:
                    # 【VBA既知の不具合を踏襲】上用のi>0では配置に
                    # `try_place_upper_with_y_offset` を使うが、VBA版はそこで
                    # `lastB` を更新しないため、ここで参照される `last_b` は
                    # 「前のボードの配置結果」のままになる(=そのボード自身の
                    # 幅ではなく1つ前の幅で y_off が進む)。元ツールの配置座標を
                    # 再現するためあえて同じ挙動にしている。
                    # 直すなら try_place_upper_with_y_offset 成功時にも
                    # last_b = ctx.placed[-1] を代入する。
                    # なお `last_b is not None` の判定はPython版で追加した安全弁。
                    # (i=0が補填タグでスキップされた場合、VBA版は未設定の
                    #  オブジェクト変数を参照して実行時エラーになる)
                    if state.first_placed and last_b is not None:
                        y_off += last_b.width
                    else:
                        y_off += eff_w
                if i == 0 and board_count > 1:
                    sort_fill_boards(boards, 1, limit_w - y_off)

        # pNum=1完了後に丈補填ボードを配置
        if pass_num == 1:
            place_length_fill_boards(ctx, boards, category)

    return boards


# ------------------------------------------------------------------
# 狭幅パレット専用配置
# ------------------------------------------------------------------
def place_narrow_palette_boards(
    ctx: PlacementContext, boards: list[SelectedBoard], category: str,
) -> None:
    """VBA `PlaceNarrowPaletteBoards` / `PlaceNarrowPaletteBoardsUpper` の移植。

    狭幅パレットで選定されたボードは、短辺をY方向(幅)・長辺をX方向(丈)に
    寝かせて幅方向に積み重ねる。通常の位置探索(`try_place_inside_palette`)は
    使わず、Y座標を順に積み上げていく。開始Y座標は全体の合計幅を基準に
    センタリングする。
    """
    # 全ボードの短辺合計を基準にセンタリング
    total_narrow_w = sum(min(b.width, b.length) for b in boards)
    center_base = ctx.product.width if category == CATEGORY_UPPER else ctx.palette.width
    y_off = max((center_base - total_narrow_w) // 2, 0)
    log.debug("PlaceNarrowPaletteBoards(%s): totalNarrowW=%s yStart=%s",
              category, total_narrow_w, y_off)

    # 長辺のクリップ先: 下用はパレット丈、上用は製品丈
    clip_length = ctx.product.length if category == CATEGORY_UPPER else ctx.palette.length

    for i, b in enumerate(boards):
        model = _make_model(b, i, category, prefix="N_")
        b_short = min(b.width, b.length)
        b_long = max(b.width, b.length)
        eff_l = min(b_long, clip_length)
        rot_flag = b.length > b.width

        x_pos = 0
        for _ in range(b.count):
            if x_pos >= ctx.product.length:
                break
            place_board_at(
                ctx, x_pos, y_off, model, rot_flag,
                bypass_check=True, custom_width=b_short, custom_length=eff_l,
            )
            log.debug("  配置: %sx%s at(%s,%s) effL=%s", b.width, b.length, x_pos, y_off, eff_l)
            x_pos += eff_l

        y_off += b_short


# ------------------------------------------------------------------
# ハブ
# ------------------------------------------------------------------
def auto_place_boards(
    lower: list[SelectedBoard], upper: list[SelectedBoard],
    palette: Palette, product: ProductSize,
    *, narrow_lower: bool = False, narrow_upper: bool = False,
) -> PlacementContext:
    """VBA `AutoPlaceBoards` の移植(下用→上用の順に配置する)。

    `narrow_lower`/`narrow_upper` は選定側の `mNarrowPaletteLower` /
    `mNarrowPaletteUpper`(狭幅パレット選定が使われたか)に対応する。
    """
    ctx = PlacementContext(palette=palette, product=product)

    if narrow_lower:
        place_narrow_palette_boards(ctx, lower, CATEGORY_LOWER)
        ctx.lower_order = list(lower)
    else:
        ctx.lower_order = place_boards_from_list(ctx, lower, CATEGORY_LOWER)

    if narrow_upper:
        place_narrow_palette_boards(ctx, upper, CATEGORY_UPPER)
        ctx.upper_order = list(upper)
    else:
        ctx.upper_order = place_boards_from_list(ctx, upper, CATEGORY_UPPER)

    log.debug("=== AutoPlaceBoards 完了: %s個配置 ===", len(ctx.placed))
    return ctx
