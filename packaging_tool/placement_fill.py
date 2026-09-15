"""補填ボードの配置と、タグの扱い

補填(幅補填・丈補填)は主ボードの隙間を埋めるものなので、置く順も
置き方も主ボードとは別。

**タグが空のことがある。** VBAはListBoxの4列目を未設定のまま残す経路
(PASS3)があり、そこを通ったボードはタグを持たない。`get_effective_tag`
が寸法から推し量って埋める ── 移植でタグを必須にすると、VBAで動いて
いた選定が配置で落ちる。
"""
from __future__ import annotations

from typing import Optional

from .board_scoring import (LOWER_OVERHANG_Y, UPPER_WIDTH_TOLERANCE,
                            get_best_orientation)
from .board_selection_algorithm import (TAG_CUT_PREMISE, TAG_LENGTH_FILL,
                                        TAG_WIDTH_FILL)
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .logging_utils import get_logger
from .models import BoardModel, PlacedBoardModel
from .placement_fit import (_dims, _overlaps, can_place_at_with_y_limit_and_x_bound,
                            can_place_board_at, place_board_at)
from .placement_types import (CATEGORY_LOWER, CATEGORY_UPPER, COVERED_TOLERANCE,
                              FILL_SHORT_SIDE_LIMIT, FILL_SORT_TOLERANCE,
                              FINE_RANGE, GRID_STEP, SNAP_STEP, TAG_Y_STACK,
                              UPPER_SCAN_MARGIN, X_OVERHANG_LIMIT,
                              PlacementContext, RotationState,
                              evaluate_placement)

from .placement_types import _make_model

log = get_logger("placement.fill")

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


def count_width_fill_strips(
    boards: list[SelectedBoard], category: str, ctx: PlacementContext,
) -> tuple[int, int, int]:
    """VBA `CountWidthFillStrips` の移植。

    `boards` 内の「幅補填」タグの行数(=ストリップ本数)を数え、上側に
    配置する本数(本数 // 2)と、そのオフセット量(上側に置く分の短辺の
    合計、主ボードを下げる量になる)を算出する。2本未満なら上側0本・
    オフセット0で従来どおり全て下側に置く。

    振り分け仕様: 1本→上0/下1、2本→上1/下1、3本→上1/下2、4本→上2/下2。

    行のcount(丈方向の枚数)は幅方向のストリップ本数ではないため数えない
    (`RunWidthFillPhase` は1ストリップ=1行としてAddItemしている)。
    """
    short_sides: list[int] = []
    for i, b in enumerate(boards):
        if get_effective_tag(boards, i, category, ctx) == TAG_WIDTH_FILL:
            short_sides.append(min(b.width, b.length))

    total = len(short_sides)
    if total < 2:
        return total, 0, 0

    upper = total // 2
    offset = sum(short_sides[:upper])
    return total, upper, offset


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

    【修正】幅方向を無条件で `limit_w`(製品幅/パレット幅)に引き伸ばして
    いたため、在庫の実サイズ(長辺)より大きい架空の寸法で配置されて
    いた(例: 在庫295×1080なのに1122×295として配置=面積が水増しされる)。
    幅方向は「在庫の長辺」と `limit_w` の小さい方を使う。在庫の長辺が
    `limit_w` を超える場合だけ、実際にカットが発生する(`ctx.cut_info` に記録)。
    """
    limit_w = ctx.limit_width(category)

    for i, b in enumerate(boards):
        if get_effective_tag(boards, i, category, ctx) != TAG_LENGTH_FILL:
            continue

        model = _make_model(b, i, category, prefix="LF_")
        # 長辺をY方向(幅方向)にするため、lengthが長辺なら回転させる
        lf_rot = b.length > b.width
        b_short = min(b.width, b.length)
        b_long = max(b.width, b.length)

        needs_fill_cut = b_long > limit_w
        place_w = limit_w if needs_fill_cut else b_long
        if needs_fill_cut:
            key = f"{b.width}x{b.length}"
            if key not in ctx.cut_info:
                ctx.cut_info[key] = b_short
            log.debug("[丈補填][幅カット] %sx%s 幅%s→%smmへカット", b.width, b.length, b_long, limit_w)

        for _ in range(b.count):
            place_x = ctx.max_x(category)

            if not _check_length_fill_overlap(ctx, category, place_x, b_short, place_w):
                log.debug("[丈補填配置] 重なり検出 → スキップ %sx%s at(%s,0)",
                          b.width, b.length, place_x)
                break

            log.debug("[丈補填配置] %sx%s at(%s,0) placeW=%s bL=%s needsFillCut=%s",
                      b.width, b.length, place_x, place_w, b_short, needs_fill_cut)
            place_board_at(
                ctx, place_x, 0, model, lf_rot,
                bypass_check=True, custom_width=place_w, custom_length=b_short, is_fill=True,
            )
