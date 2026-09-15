"""置き場所を探す ── どこへ置けるかを、向きと位置を変えながら試す

`placement_fit` が「この位置に置けるか」を答えるのに対して、ここは
**置ける位置を探す**。探し方はボードの役割で変わる。

    try_place_inside_palette      … 下用。パレット枠の中をグリッドで探し、
                                    既存ボードの右端へスナップも試す
    try_place_with_fixed_rotation … 向きを決め打って探す
    try_place_upper_with_y_offset … 上用。幅方向の位置をずらしながら探す
    try_place_upper_length_fill   … 上用の丈補填
    try_place_y_companion         … Y積み(丈は1枚で足り、幅方向に積む)の
                                    相方を探す
"""
from __future__ import annotations

from typing import Optional

from .board_scoring import (LOWER_OVERHANG_Y, UPPER_WIDTH_TOLERANCE,
                            get_best_orientation)
from .board_selection_algorithm import (PROTEC_1P1216_TOLERANCE,
                                        PROTEC_OTHER_TOLERANCE,
                                        TAG_CUT_PREMISE, TAG_LENGTH_FILL,
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

log = get_logger("placement.try")

def try_place_single_orientation(
    ctx: PlacementContext, board: BoardModel, rotate: bool, min_waste: float,
    y_start: int = 0,
) -> Optional[tuple[int, int, float]]:
    """VBA `TryPlaceSingleOrientation` の移植(単一向きでの位置探索)。

    3段階で探索する:
        PASS1: パレット内をグリッド(20mm刻み)で粗探索
        PASS2: (下用のみ)既存ボードの右端にスナップして5mm刻みで探索
        PASS3: 最良位置の周辺±20mmを1mm刻みで細探索
    戻り値は (x, y, 更新後のminWaste)。見つからなければ None。

    `y_start` は探索開始Y(既定0)。幅補填を主ボードの上下に振り分ける際、
    上側補填ぶんだけ下げた位置から主ボードを探索させるために指定する
    (`place_boards_from_list` 参照)。既定値0では従来と完全に同一挙動。
    """
    w, l = _dims(board, rotate)

    if board.board_category == CATEGORY_UPPER:
        # 上用は常に製品幅が境界(`ctx.limit_width` 参照)。プロテックも
        # 例外ではない(上下とも製品幅よりマイナスというルールで、
        # 製品幅を超えて配置してよいわけではない)
        end_y = ctx.limit_width(CATEGORY_UPPER) - w
        end_x = ctx.palette.length * 5
    else:
        end_y = int(ctx.palette.width * LOWER_OVERHANG_Y) - w
        end_x = ctx.palette.length - l

    # end_y < 0 は本当に配置不可。end_x < 0 でもスナップ探索は試す
    if end_y < 0:
        return None
    if y_start > end_y:
        return None

    best_x = best_y = 0
    found = False

    # PASS1: グリッド粗探索
    if end_x >= 0:
        for y in range(y_start, end_y + 1, GRID_STEP):
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
            for y in range(y_start, end_y + 1, SNAP_STEP):
                if can_place_board_at(ctx, snap_x, y, board, rotate):
                    waste = evaluate_placement(snap_x, y)
                    if waste < min_waste:
                        min_waste, best_x, best_y, found = waste, snap_x, y, True

    # PASS3: 最良位置周辺の細探索
    if found:
        f_start_x = max(0, best_x - FINE_RANGE)
        f_end_x = best_x + FINE_RANGE
        f_start_y = max(y_start, best_y - FINE_RANGE)
        f_end_y = min(best_y + FINE_RANGE, end_y)
        for fy in range(f_start_y, f_end_y + 1):
            for fx in range(f_start_x, f_end_x + 1):
                if can_place_board_at(ctx, fx, fy, board, rotate):
                    fine_waste = evaluate_placement(fx, fy)
                    if fine_waste < min_waste:
                        min_waste, best_x, best_y = fine_waste, fx, fy

    return (best_x, best_y, min_waste) if found else None


def try_place_inside_palette(ctx: PlacementContext, board: BoardModel, y_start: int = 0) -> bool:
    """VBA `TryPlaceInsidePalette` の移植。

    対象幅に近い向きを先に試し、駄目ならもう一方の向きを試す。
    `y_start`(既定0)は `try_place_single_orientation` にそのまま渡す
    (幅補填を主ボードの上下に振り分ける際のオフセット、
    `place_boards_from_list` 参照)。

    【プロテックモード下用の向き決定】プロテックは上下とも「製品幅基準・
    マイナス方向の許容のみ・超過禁止」というルールで向きを決める
    (`decide_protec_orientation` 参照)。自動選定を経由したボードは
    既にこのルールに沿った向きで登録されているが、手動追加ボードは
    これを経由しないため、ここでも同じルールを適用しないと「製品幅を
    大きく下回る向き」がそのまま配置されてしまう(VBA
    `TryPlaceInsidePalette` のプロテック分岐と同じ意図)。
    """
    target_width = ctx.limit_width(board.board_category)
    dist_normal = abs(board.width - target_width)
    dist_rotated = abs(board.length - target_width)

    if ctx.is_protec_mode and board.board_category == CATEGORY_LOWER:
        tol = PROTEC_1P1216_TOLERANCE if ctx.is_1p1216 else PROTEC_OTHER_TOLERANCE
        min_allowed = ctx.product.width - tol
        normal_ok = ctx.product.width >= board.width >= min_allowed
        rotated_ok = ctx.product.width >= board.length >= min_allowed

        if normal_ok and rotated_ok:
            try_normal_first = dist_normal <= dist_rotated
        elif normal_ok:
            try_normal_first = True
        elif rotated_ok:
            try_normal_first = False
        else:
            log.debug(
                "TryPlaceInsidePalette[プロテック][警告] %sx%s はどちらの向きも"
                "製品幅許容(-%smm〜0mm)を満たしません。"
                "SelectProtecLowerBoardsを経由しない追加の可能性があります。productWidth=%s",
                board.width, board.length, tol, ctx.product.width,
            )
            try_normal_first = dist_normal <= dist_rotated

        log.debug(
            "TryPlaceInsidePalette[プロテック]: %sx%s target=%s protecTol=%s "
            "normalOK=%s rotatedOK=%s normalFirst=%s",
            board.width, board.length, target_width, tol, normal_ok, rotated_ok, try_normal_first,
        )
    else:
        try_normal_first = dist_normal <= dist_rotated

    log.debug("TryPlaceInsidePalette: %sx%s target=%s distN=%s distR=%s normalFirst=%s",
              board.width, board.length, target_width, dist_normal, dist_rotated, try_normal_first)

    order = (False, True) if try_normal_first else (True, False)
    # VBA版は minWaste を ByRef で2回の試行に引き継ぐが、1回目が失敗した
    # 場合 minWaste は更新されないため、素直に初期値のまま渡してよい。
    min_waste = 1e15
    for rotate in order:
        result = try_place_single_orientation(ctx, board, rotate, min_waste, y_start)
        if result is not None:
            x, y, _ = result
            return place_board_at(ctx, x, y, board, rotate)
    return False


def try_place_with_fixed_rotation(
    ctx: PlacementContext, board: BoardModel, use_rotation: bool, y_start: int = 0,
) -> bool:
    """VBA `TryPlaceWithFixedRotation` の移植(向きを固定した配置)。

    `y_start`(既定0)は `try_place_single_orientation` にそのまま渡す。
    """
    min_waste = float(ctx.palette.length) * float(ctx.palette.width) * 10
    result = try_place_single_orientation(ctx, board, use_rotation, min_waste, y_start)
    if result is None:
        return False
    x, y, _ = result
    return place_board_at(ctx, x, y, board, use_rotation)

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


def try_place_y_companion(
    ctx: PlacementContext, board: BoardModel, place_upper: bool = False,
) -> bool:
    """VBA `TryPlaceYCompanion` の移植(幅補填ボードの配置)。

    主ボードのうち「幅方向のカバレッジが最も足りない1枚」(`gap_board`)を
    基準にして、そのY端・X位置から幅補填を敷いていく。
    既に補填行があり右端に余裕があれば同じ行に継ぎ足し、
    行が埋まっていれば次の行へ送る。

    `place_upper`(既定False): 幅補填を主ボードの上下に振り分けるための
    向き指定。False(従来)は主ボードの「下側」(Y大方向)へ、Trueは
    「上側」(Y小方向)へ段を積む。X方向のロジック(同一段への丈方向
    継ぎ足し・availLクランプ)は上下共通のため分岐しない。
    """
    category = board.board_category

    # Step1: 幅方向カバレッジが最も短い主ボードを特定。
    # main_min_y(上側モード用)は主ボードの上端の最小値
    gap_board: Optional[PlacedBoardModel] = None
    min_end_y = 2147483647
    main_min_y = 2147483647
    for pb in ctx.placed:
        if pb.board_category == category and not pb.is_fill_board:
            if pb.y + pb.width < min_end_y:
                min_end_y = pb.y + pb.width
                gap_board = pb
            main_min_y = min(main_min_y, pb.y)
    if gap_board is None:
        return False

    # Step2: 全主ボードのX右端(上用は製品丈でクランプ)
    main_x_end = ctx.max_x(category, fill=False)
    if category == CATEGORY_UPPER:
        main_x_end = min(main_x_end, ctx.product.length)

    # Step3: 既存の幅補填ボードの状態。
    # 下側(従来): 主ボード下端(min_end_y)より下にある補填のうち最大Y。
    # 上側(新規): 主ボード上端(main_min_y)より上にある補填のうち最小Y。
    # 丈補填ボードを誤って拾わないよう、X範囲(gap_board.x以上
    # main_x_end未満)でフィルタする(丈補填はX>=main_x_endに置かれるため
    # 本来ここには入らないが、上側モードではY=0付近が初期値と重なり
    # 誤って「既存の上側段」と誤認されていた)。
    row_exists = False
    if not place_upper:
        fill_max_y = min_end_y - 1  # 初期値: 補填行なし
        for pb in ctx.placed:
            if pb.board_category == category and pb.is_fill_board:
                fill_max_y = max(fill_max_y, pb.y)
        row_exists = fill_max_y >= min_end_y
    else:
        fill_max_y = main_min_y + 1  # 初期値: 上側に段は無い
        for pb in ctx.placed:
            if (pb.board_category == category and pb.is_fill_board
                    and pb.y < main_min_y and pb.x >= gap_board.x and pb.x < main_x_end):
                if not row_exists or pb.y < fill_max_y:
                    fill_max_y = pb.y
                    row_exists = True

    # 最新行(fill_max_y)の右端を取得。上側モードは同じX範囲フィルタが要る
    # (丈補填がy=0付近・main_x_end以降に置かれ、fill_max_y=0の初期値と
    # 一致してcur_row_end_xに混入するのを防ぐ)。
    cur_row_end_x = 0
    if row_exists:
        for pb in ctx.placed:
            if pb.board_category == category and pb.is_fill_board and pb.y == fill_max_y:
                if not place_upper or (pb.x >= gap_board.x and pb.x < main_x_end):
                    cur_row_end_x = max(cur_row_end_x, pb.x + pb.length)

    # Step4: placeY と fillEndX を決定。
    # 「同じ行に空きがあれば続けて置く」「無ければ新しい段」は上下共通で、
    # 違うのは新しい段のY座標の計算だけ。
    if row_exists and cur_row_end_x < main_x_end:
        place_y = fill_max_y
        fill_end_x = cur_row_end_x
    elif row_exists:
        # 新行へ: 前の行の実幅を取得してY座標を決める
        prev_row_bw = 0
        for pb in ctx.placed:
            if pb.board_category == category and pb.is_fill_board and pb.y == fill_max_y:
                prev_row_bw = max(prev_row_bw, pb.width)
        if prev_row_bw <= 0:
            prev_row_bw = min(board.width, board.length)
        if not place_upper:
            place_y = fill_max_y + prev_row_bw  # 下側: 前の段のさらに下へ
        else:
            place_y = fill_max_y - min(board.width, board.length)  # 上側: 前の段のさらに上へ
        fill_end_x = 0
    else:
        # 補填ボードが1枚もない場合
        if not place_upper:
            place_y = min_end_y  # 下側: 主ボード下端が基準
        else:
            place_y = main_min_y - min(board.width, board.length)  # 上側: 主ボード上端から短辺ぶん上
        fill_end_x = 0

    # 上側モードでY=0を割り込む場合は配置不可
    # (主ボードのオフセット量が不足している=集計と実配置の食い違い)
    if place_upper and place_y < 0:
        log.debug("TryPlaceYCompanion[上側] placeY=%s が負 → オフセット不足のため配置不可", place_y)
        return False

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

    log.debug("TryPlaceYCompanion%s: %sx%s → at(%s,%s) bW=%s bL=%s",
              "[上側]" if place_upper else "", board.width, board.length, place_x, place_y, b_width, b_length)

    return place_board_at(
        ctx, place_x, place_y, board, rot_flag,
        bypass_check=True, custom_width=b_width, custom_length=b_length, is_fill=True,
    )
