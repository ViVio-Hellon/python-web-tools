"""置けるかどうかの判定と、置く操作

**ここは「この位置に置けるか」だけを答える。** どこへ置くかを探すのは
`placement_try`、何を先に置くかを決めるのは `placement_algorithm`。

判定が3段になっているのは、下用と上用で見るものが違うため。

    can_place_board_at              … パレット枠と重なりだけ見る(下用)
    can_place_at_with_y_limit       … 幅方向の上限も見る(上用)
    can_place_at_with_y_limit_and_x_bound … X方向のはみ出しも見る(上用)
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
from .placement_types import (CATEGORY_LOWER, CATEGORY_UPPER,
                              GRID_STEP, PlacementContext,
                              X_OVERHANG_LIMIT)

log = get_logger("placement.fit")

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
        # 上用は製品幅+許容(両端で最大80mm)まで。プロテックも同じ境界
        # (上下とも製品幅よりマイナスというルールで、選定
        # (`decide_protec_orientation`)が既に製品幅を超えないカット後
        # サイズを確定させているため、ここで別枠の緩和は不要)
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
