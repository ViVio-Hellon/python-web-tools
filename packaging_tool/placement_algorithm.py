"""資材配置 ── 外向けの1枚窓 (VBA `MaterialMasterForm` の配置系の移植)

**中身は段ごとのモジュールが持つ。** 呼ぶ側はここだけを見ればよい。

    placement_types  型と決まりの数値、置けなかった理由の言葉
    placement_fit    この位置に置けるか(判定)と、置く操作
    placement_try    置ける位置を探す(向きと位置を変えながら)
    placement_fill   補填ボードの配置と、タグの推測
    ここ             並べる順を決める。Y積みへの寄せ、幅方向の
                     中央寄せ、狭幅パレット、上下の取りまとめ

上から下へ一方向に依存する。**下線で始まる名前(段の内側)はここから
公開しない** ── 使うときは持ち主のモジュールから引く。

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

from typing import Optional

from .board_scoring import get_best_orientation
from .board_selection_algorithm import (PROTEC_1P1216_TOLERANCE,
                                        PROTEC_OTHER_TOLERANCE,
                                        TAG_CUT_PREMISE, TAG_LENGTH_FILL,
                                        TAG_WIDTH_FILL, ProtecCutResult)
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .logging_utils import get_logger
from .models import BoardModel, PlacedBoardModel

# ------------------------------------------------------------------
# **ここは外向けの1枚窓。** 配置の中身は段ごとのモジュールが持ち、
# 呼ぶ側はこのモジュールだけを見ればよいようにする。
#
#     型・決まりの数値 → 置けるか(fit) → 置き場所を探す(try)
#                      → 補填の配置(fill) → ここ(並べる順を決める)
# ------------------------------------------------------------------
from .placement_types import (  # noqa: F401
    CATEGORY_LOWER, CATEGORY_UPPER, COVERED_TOLERANCE, FILL_SHORT_SIDE_LIMIT,
    FILL_SORT_TOLERANCE, FINE_RANGE, GRID_STEP, SNAP_STEP, TAG_Y_STACK,
    UPPER_SCAN_MARGIN, X_OVERHANG_LIMIT, PlacementContext, RotationState,
    UnplacedBoard, _make_model, evaluate_placement, explain_unplaced)
from .placement_fit import (  # noqa: F401
    _dims, _overlaps, can_place_at_with_y_limit,
    can_place_at_with_y_limit_and_x_bound, can_place_board_at, place_board_at)
from .placement_try import (  # noqa: F401
    try_place_inside_palette, try_place_single_orientation,
    try_place_upper_length_fill, try_place_upper_with_y_offset,
    try_place_with_fixed_rotation, try_place_y_companion)
from .placement_fill import (  # noqa: F401
    count_width_fill_strips, get_effective_tag, place_length_fill_boards,
    sort_fill_boards)

log = get_logger("placement_algorithm")



# ------------------------------------------------------------------
# メインの配置ループ
# ------------------------------------------------------------------
def _place_cut_premise(
    ctx: PlacementContext, b: SelectedBoard, idx: int, category: str,
) -> None:
    """カット前提ボードの配置(VBA `PlaceBoardsFromList` 内のインライン処理)。

    長辺をY方向(幅)にして対象幅でカットし、短辺ぶんずつX方向に並べる。

    【プロテックのときは確定値をそのまま使う】
    以前は下用のカット前提ボードを常にパレット幅基準で独自に再カット
    しており、プロテックの選定結果(`ProtecCutResult`。製品幅基準で
    決めたカット後サイズ)と配置結果が食い違っていた。`ctx.protec_result`
    が有効なときは、幅・向きとも選定の確定値をそのまま使い、ここでの
    再カットを行わない。
    """
    model = _make_model(b, idx, category)

    pr = ctx.protec_result
    if pr.valid:
        wc_short = min(pr.orig_width, pr.orig_length)
        b_rot_cut = pr.is_rotated
        wc_target_w = pr.cut_eff_width
    else:
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

    # VBA `CountWidthFillStrips`: 幅補填を上下に振り分け、主ボードのYオフセットを
    # 事前に確保する(先に上側の分だけ主ボードを下にずらしておく)。
    wf_total, wf_upper, wf_offset = count_width_fill_strips(boards, category, ctx)
    wf_upper_used = 0
    if wf_upper > 0:
        log.debug(
            "%s 幅補填振り分け: 計%s本 → 上%s本/下%s本 主ボードYオフセット=%s",
            category, wf_total, wf_upper, wf_total - wf_upper, wf_offset,
        )

    # VBA側は Sub スコープの Dim なので、ループを跨いで値が残る。
    # 特に pNum=2 では GetBestOrientation を呼ばないため、rot/eff_w/eff_l は
    # pNum=1 で最後に計算された値がそのまま使われる(VBAの挙動を踏襲)。
    rot = False
    eff_w = 0
    eff_l = 0
    # last_b も同様に Sub スコープ。後述の既知の不具合の原因になる。
    last_b: Optional[PlacedBoardModel] = None

    for pass_num in (1, 2):
        y_off = wf_offset if pass_num == 1 else 0
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
                # カット前提は主ボード扱い(幅補填ではない)なので pNum=1
                # でだけ置く。この分岐は直後の pNum フィルタを飛び越すため、
                # 限定しないと pNum=1 と pNum=2 の**まったく同じ座標**へ
                # 二重に置かれる ── bypassCheck=True で重なり判定も効かない
                # ので、図では重なって見えないまま枚数と使用率だけが倍に
                # なる(現場で見つかった「使用率185.8%」がこれ)。
                # **プロテック限定の話ではない。** 通常モードでカット前提
                # ボードを含むロットも同じように二重配置されていた。
                # (VBA側もこの限定を入れた。以前はこちらだけの修正だった)
                if pass_num == 1:
                    _place_cut_premise(ctx, b, i, category)
                continue

            if pass_num == 1 and effective_tag in (TAG_WIDTH_FILL, TAG_LENGTH_FILL):
                continue
            if pass_num == 2 and effective_tag != TAG_WIDTH_FILL:
                continue

            wf_use_upper = False
            if pass_num == 2 and effective_tag == TAG_WIDTH_FILL:
                if wf_upper_used < wf_upper:
                    wf_use_upper = True
                    wf_upper_used += 1

            model = _make_model(b, i, category)

            # pNum=2(幅補填)は try_place_y_companion が独自に向きを決めるため
            # GetBestOrientation は不要(limitW - yOff が意味を持たない)
            if pass_num == 1:
                rot, eff_w, eff_l = get_best_orientation(model, limit_w - y_off)

            # 手動追加ボード(タグ空)への自動Y積み救済。
            # **生のタグではなく判定後のタグ(effective_tag)で見る** ──
            # 補填と判定された板を横取りしてY積みに書き換えないため。
            # 判定に使う eff_l は pNum=1 でしか計算しないので pNum=1 に限る
            # (VBA も同じ修正: `effectiveTag = "" And pNum = 1`)
            if (category == CATEGORY_UPPER and not effective_tag
                    and pass_num == 1):
                if _try_convert_to_y_stack(ctx, model, rot, eff_l):
                    tag = TAG_Y_STACK

            log.debug("  %s%s: %sx%s Type=[%s]", category, model.id, b.width, b.length, tag)

            state = RotationState(first_placed=False, first_rotation=rot)
            is_l_fill = False
            put = 0                       # この行で実際に置けた枚数

            for _ in range(b.count):
                if tag == TAG_Y_STACK:
                    # 短辺を幅(Y)にする向きを強制
                    y_rot = b.width > b.length
                    placed_ok = try_place_with_fixed_rotation(ctx, model, y_rot)
                    if placed_ok:
                        last_b = ctx.placed[-1]
                        y_off += last_b.width
                elif pass_num == 2:
                    if wf_use_upper:
                        placed_ok = try_place_y_companion(ctx, model, True)
                        if not placed_ok:
                            placed_ok = try_place_y_companion(ctx, model, False)
                    else:
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
                    placed_ok = try_place_inside_palette(ctx, model, wf_offset)
                    if placed_ok:
                        state.first_placed = True
                        last_b = ctx.placed[-1]
                        # 実際に採用された向きを以降の枚数に引き継ぐ
                        if category == CATEGORY_UPPER:
                            state.first_rotation = last_b.width == b.length
                        else:
                            state.first_rotation = last_b.width != b.width
                else:
                    placed_ok = try_place_with_fixed_rotation(ctx, model, state.first_rotation, wf_offset)

                if not placed_ok:
                    break
                put += 1

            # **1枚も置けなかったら、そう言う。** 黙って飛ばすと、手で
            # 追加した人には「押しても何も起きない」としか見えない
            if put == 0:
                ctx.note_unplaced(category, b.width, b.length, b.count)
                log.debug("  %s%s: 置けませんでした ── %s", category, model.id,
                          ctx.unplaced[-1].reason)

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
# 幅方向のセンタリング
# ------------------------------------------------------------------
def _x_groups(boards: list[PlacedBoardModel]) -> list[list[PlacedBoardModel]]:
    """丈方向(X)で重なるボードをひとまとまりにする。

    **一緒に動かさなければならない単位**を作るためのもの。同じ丈の
    位置にあるボード(Y方向に積んだ2枚など)を別々にずらすと、寄せた
    結果として重なってしまう。
    """
    groups: list[list[PlacedBoardModel]] = []
    current: list[PlacedBoardModel] = []
    reach = 0
    for pb in sorted(boards, key=lambda p: (p.x, p.length)):
        if current and pb.x < reach:
            current.append(pb)
            reach = max(reach, pb.x + pb.length)
            continue
        if current:
            groups.append(current)
        current = [pb]
        reach = pb.x + pb.length
    if current:
        groups.append(current)
    return groups


def center_boards_in_width(ctx: PlacementContext, category: str) -> None:
    """幅が足りないボードを、基準の幅の**中央**へ寄せる。

    現場の声:「幅不足のボードも上詰めで配置される。これでは
    マイナス幅を等分にすることが直感的に分からない」。

    上詰めのままだと、足りない分がすべて片側に寄って出ます。図を見た
    人は「この板は下にずらして置くのか」と読んでしまいますが、実際は
    **上下に等分**して置くものです。図がそのまま作業の指示になるので、
    座標のほうを直します(描画側で寄せると、印刷とデータで食い違う)。

    寄せる単位は**丈方向に重なるボードのまとまり**です(`_x_groups`)。
    1枚ずつ寄せると、Y方向に積んだ2枚が重なります。

    【幅補填があるときは触らない】
    幅補填の上下振り分け(1本なら下、2本なら上下1本ずつ…)は、
    **どこに置くかを既に決めている**配置です。そのうえで中央へ
    寄せ直すと、せっかく振り分けた意味が消えます(現場の指示:
    「上下に振り分けているケースはそのままで良い」)。丈補填も
    主ボードとの位置関係で置いているので、同じく触りません。
    """
    boards = [pb for pb in ctx.placed if pb.board_category == category]
    if not boards:
        return

    limit_w = ctx.limit_width(category)
    for group in _x_groups(boards):
        # **補填が居るまとまりだけを避ける。**
        #
        # 以前は補填が1枚でもあると、その面のボードを**全部**そのままに
        # していた。守りたいのは幅補填の上下振り分け(どこに置くかを既に
        # 決めている配置)だけなのに、離れたところに居る主ボードまで
        # 上詰めのまま残っていた ── 丈補填が1枚あるだけで、面ぜんぶの
        # センタリングが効かなくなる(現場の指摘:「ボードを幅方向の
        # センター配置をしていない」)。
        # 幅補填は主ボードの下端に敷くので同じまとまりに入り、これまで
        # どおり触らない。
        #
        # **避けるのは「振り分けが起きているまとまり」だけ。** 補填と主
        # ボードが同じまとまりに居るときが、その振り分けの跡である。
        # 補填だけでできたまとまり(丈補填の帯)は振り分けと関係が無く、
        # 避けると**隣の主ボードと段違いになる** ── 帯は主ボードの丈を
        # 継ぎ足すものなので、幅方向の位置は主ボードと揃っていないと
        # おかしい(実画面で通して見つけた)
        fills = [pb for pb in group if pb.is_fill_board]
        if fills and len(fills) != len(group):
            log.debug("センタリング(%s): x=%s は幅補填の振り分けがあるので"
                      "触りません", category, group[0].x)
            continue
        min_y = min(pb.y for pb in group)
        max_y = max(pb.y + pb.width for pb in group)
        gap = limit_w - (max_y - min_y)
        # はみ出している(gap<0)なら寄せる先が無い。ちょうど(gap=0)も
        # 動かす必要が無い
        if gap <= 0:
            continue
        shift = gap // 2 - min_y
        if shift == 0:
            continue
        for pb in group:
            pb.y += shift
        log.debug("センタリング(%s): x=%s の %s枚を %s だけ寄せました "
                  "(幅%s / 基準%s)",
                  category, group[0].x, len(group), shift, max_y - min_y, limit_w)


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
    protec_result: Optional[ProtecCutResult] = None,
    is_protec_mode: bool = False, is_1p1216: bool = False,
) -> PlacementContext:
    """VBA `AutoPlaceBoards` の移植(下用→上用の順に配置する)。

    `narrow_lower`/`narrow_upper` は選定側の `mNarrowPaletteLower` /
    `mNarrowPaletteUpper`(狭幅パレット選定が使われたか)に対応する。

    `is_protec_mode`/`is_1p1216` は `MaterialMasterForm.mIsProtecMode` /
    `mIsProtec1P1216` に対応し、`try_place_inside_palette` の
    プロテック向き決定分岐(手動追加ボード向けのガード)に使われる。

    プロテックであっても、配置の境界(`limit_width`)は通常モードと
    完全に同じ(上用は製品幅、下用はパレット幅) ── プロテックは
    「上下とも製品幅よりマイナス」というルールで、製品幅を超えて
    配置してよいわけではない。選定(`decide_protec_orientation`)が
    既に製品幅を超えないカット後サイズを確定させているので、配置側で
    境界を緩める必要はない(以前そう緩めていたのは誤りだったため
    元に戻した)。

    `protec_result` は選定(または手動追加後の後付け適用)が決めた
    「唯一の正解」。カット前提ボードの配置(`_place_cut_premise`)が
    これを参照し、独自の再計算をしない。
    """
    ctx = PlacementContext(
        palette=palette, product=product,
        protec_result=protec_result or ProtecCutResult(),
        is_protec_mode=is_protec_mode, is_1p1216=is_1p1216)

    # 狭幅パレットは `place_narrow_palette_boards` が積み上げる時点で
    # 中央から始めているので、あとから寄せ直さない(二重に寄る)
    if narrow_lower:
        place_narrow_palette_boards(ctx, lower, CATEGORY_LOWER)
        ctx.lower_order = list(lower)
    else:
        ctx.lower_order = place_boards_from_list(ctx, lower, CATEGORY_LOWER)
        center_boards_in_width(ctx, CATEGORY_LOWER)

    if narrow_upper:
        place_narrow_palette_boards(ctx, upper, CATEGORY_UPPER)
        ctx.upper_order = list(upper)
    else:
        ctx.upper_order = place_boards_from_list(ctx, upper, CATEGORY_UPPER)
        center_boards_in_width(ctx, CATEGORY_UPPER)

    log.debug("=== AutoPlaceBoards 完了: %s個配置 ===", len(ctx.placed))
    return ctx
