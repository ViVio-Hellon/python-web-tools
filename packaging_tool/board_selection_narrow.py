"""狭幅パレット選定とカット前提選定 ── 通常のパスで選べなかったときの道

**どちらも最後の手段**で、通常のPASS群と補填が製品幅を覆えなかった
ときだけ通る。

  狭幅パレット … ボードを寝かせて(短辺=幅方向)幅方向に積み重ねる。
                 細いパレットで、幅の合うボードがそもそも無いとき
  カット前提   … 「最大辺が対象幅以上」のボードを選び、幅方向に
                 切って使う前提で短辺を丈方向に並べる

この段は他の段を呼ばない ── 呼ばれる側にだけ置くことで、下用・上用の
どちらからでも同じものが使える。
"""
from __future__ import annotations

from typing import Optional

from .board_scoring import (FatigueEntry, PASS1_TOLERANCE,
                            get_best_orientation,
                            sort_boards_by_target_width)
from .board_selection_common import (FILL_SIZE_30, FILL_SIZE_50,
                                     FILL_SIZE_100, LENGTH_FILL_BOARD_W,
                                     TAG_CUT_PREMISE, TAG_LENGTH_FILL,
                                     TAG_MAIN, TAG_WIDTH_FILL, _orient,
                                     adjust_orientation_for_coverage)
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .board_selection_types import (LowerSelectionResult, PassState,
                                    UpperSelectionResult, WideCutResult)
from .logging_utils import get_logger
from .models import BoardModel

log = get_logger("board_selection.narrow")


# ==================================================================
# 狭幅パレット選定 (VBA `SelectBoardsForNarrowPalette` /
#                  `SelectBoardsForNarrowPaletteUpper` /
#                  `RunNarrowPaletteLowerFill` の移植)
#
# 通常のPASS群でボードを選べなかった場合の代替経路。
# 各ボードを「短辺=幅方向、長辺=丈方向」に寝かせて幅方向に積み重ね、
# 製品幅をカバーする。
# ==================================================================

# 狭幅選定で候補にできる最小の短辺(mm)
NARROW_MIN_SHORT_SIDE = 10

# 狭幅選定のループ上限
NARROW_MAX_LOOPS = 10


def _select_narrow_boards(
    boards: list[SelectedBoard], available: list[BoardModel],
    target_w: int, target_l: int, product_width: int, eff_length_cap: Optional[int] = None,
) -> int:
    """狭幅選定の共通処理。カバーできた幅の合計を返す。

    残り幅以下で最大の短辺を持つ未使用ボードを繰り返し選び、
    幅方向に積み重ねる。`eff_length_cap` を指定すると丈方向の
    有効長をその値で頭打ちにする(上用がパレット丈で頭打ちにするため)。
    """
    covered_w = 0
    for _ in range(NARROW_MAX_LOOPS):
        if covered_w >= product_width - 3:
            break
        remain_w = product_width - covered_w

        best: Optional[BoardModel] = None
        best_short = 0
        for ab in available:
            ab_short = min(ab.width, ab.length)
            if ab_short > target_w or ab_short < NARROW_MIN_SHORT_SIDE or ab_short > remain_w + 3:
                continue
            if any(s.width == ab.width and s.length == ab.length for s in boards):
                continue
            if ab_short > best_short:
                best_short, best = ab_short, ab

        if best is None:
            log.debug("狭幅選定: 適合ボードなし 残=%s", remain_w)
            break

        b_short = min(best.width, best.length)
        b_long = max(best.width, best.length)
        eff_l = min(b_long, eff_length_cap) if eff_length_cap else b_long
        cnt = max(1, -(-target_l // eff_l)) if eff_l > 0 else 1

        boards.append(SelectedBoard(width=best.width, length=best.length, count=cnt, tag=TAG_MAIN))
        covered_w += b_short
        log.debug("狭幅選定: %sx%s %s枚 幅累計=%s", best.width, best.length, cnt, covered_w)

    return covered_w


def select_boards_for_narrow_palette(
    available: list[BoardModel], palette: Palette, product: ProductSize,
) -> list[SelectedBoard]:
    """VBA `SelectBoardsForNarrowPalette` の移植(下用)。"""
    boards: list[SelectedBoard] = []
    covered = _select_narrow_boards(
        boards, available, palette.width, product.length, product.width,
    )
    if covered < product.width - 3:
        log.debug("[狭幅選定] 幅カバー不足 (%s/%smm) → 配置不可", covered, product.width)
    return boards


def select_boards_for_narrow_palette_upper(
    available: list[BoardModel], palette: Palette, product: ProductSize,
) -> list[SelectedBoard]:
    """VBA `SelectBoardsForNarrowPaletteUpper` の移植(上用)。

    下用との違いは丈方向の有効長をパレット丈で頭打ちにする点のみ
    (配置側が `Min(長辺, パレット丈)` で進むため枚数もそれに合わせる)。
    """
    boards: list[SelectedBoard] = []
    _select_narrow_boards(
        boards, available, palette.width, product.length, product.width,
        eff_length_cap=palette.length,
    )
    return boards


def run_narrow_palette_lower_fill(
    boards: list[SelectedBoard], palette: Palette, product: ProductSize, available: list[BoardModel],
) -> None:
    """VBA `RunNarrowPaletteLowerFill` の移植(狭幅パレット下用専用の補填)。

    `RunLowerFillPhase` とは完全に独立。前提として各ボードは
    「短辺=Y方向(幅)、長辺=X方向(丈)」で積み重ね配置される。

        (1) Y方向: 短辺合計がパレット幅に足りなければ 30→50→100mm で補填
        (2) X方向: 各ボードの 有効長×枚数 が製品丈に足りなければ枚数を追加
    """
    target_y = palette.width
    target_x = product.length

    # (1) Y方向カバレッジ
    covered_y = sum(min(b.width, b.length) for b in boards)
    y_gap = target_y - covered_y
    log.debug("[狭幅下用補填] Y合計=%smm gap=%smm", covered_y, y_gap)

    if y_gap > PASS1_TOLERANCE:
        for sz in (FILL_SIZE_30, FILL_SIZE_50, FILL_SIZE_100):
            if y_gap <= PASS1_TOLERANCE:
                break
            if sz > y_gap + 10:
                continue
            found = next((a for a in available if min(a.width, a.length) == sz), None)
            if found is None:
                continue
            fb_long = max(found.width, found.length)
            x_cnt = max(1, -(-target_x // fb_long)) if fb_long > 0 else 1
            boards.append(SelectedBoard(width=found.width, length=found.length, count=x_cnt, tag=TAG_WIDTH_FILL))
            y_gap -= sz
            log.debug("[狭幅Y補填] %sx%s %s枚 残gap=%smm", found.width, found.length, x_cnt, y_gap)
        if y_gap > PASS1_TOLERANCE:
            log.debug("[狭幅Y補填] gap残存=%smm (在庫不足)", y_gap)

    # (2) X方向カバレッジ(幅補填は対象外)
    for b in boards:
        if b.tag == TAG_WIDTH_FILL:
            continue
        b_long = max(b.width, b.length)
        eff_l = min(b_long, palette.length)
        if eff_l <= 0:
            continue
        x_gap = target_x - eff_l * b.count
        if x_gap > 3:
            add_cnt = max(1, -(-x_gap // eff_l))
            b.count += add_cnt
            log.debug("[狭幅X補填] %sx%s +%s枚 (計%s枚)", b.width, b.length, add_cnt, b.count)


# ==================================================================
# カット前提選定 (VBA `SelectBoardsForWideLower` / `SelectBoardsForWideProduct` /
#                `SelectUpperBoardsWideCut` の移植)
#
# 通常選定・補填でも製品幅をカバーできない場合の最終手段。
# 「最大辺が対象幅以上」のボードを選び、幅方向にカットして使う前提で
# 短辺を丈方向に並べる。
# ==================================================================

# カット前提選定で候補にできる最小の短辺(補填専用サイズを除外するため)
WIDE_CUT_MIN_SHORT_SIDE = 200


# 疲労度計算のカット係数(modAngleSelect の CUT_FAT_MULT と同値)
CUT_FAT_MULT = 233.0



def _pick_wide_cut_board(
    available: list[BoardModel], target_width: int, product_length: int,
    fatigue_map: Optional[dict[str, FatigueEntry]], fatigue_mode: bool,
) -> Optional[tuple[BoardModel, int, int, int]]:
    """カット前提の主ボードを1件選ぶ。戻り値は (ボード, 最大辺, 短辺, 枚数)。

    疲労度モードは「距離x1 + 面積x枚数 + カット前提の幅カット疲労」が
    最小のものを、通常モードは「最大辺と対象幅の差」が最小
    (同値なら短辺が大きい方)を選ぶ。
    """
    best: Optional[tuple[BoardModel, int, int, int]] = None

    if fatigue_mode and fatigue_map is not None:
        best_total = float("inf")
        for ab in available:
            ab_max, ab_min = max(ab.width, ab.length), min(ab.width, ab.length)
            if ab_min < WIDE_CUT_MIN_SHORT_SIDE or ab_max < target_width:
                continue
            cnt = max(1, -(-product_length // ab_min))
            entry = fatigue_map.get(f"{ab.width}x{ab.length}")
            dist = entry.dist if entry else 0.0
            area = entry.area if entry else 0.0
            # カット前提: 幅カットが全数発生する(マップのカット値は使わず前提値で上書き)
            cut_fat = (ab_min / 3000) * CUT_FAT_MULT * cnt
            total = dist + area * cnt + cut_fat
            if total < best_total:
                best_total = total
                best = (ab, ab_max, ab_min, cnt)
        return best

    best_diff = 2 ** 31
    best_short = 0
    for ab in available:
        ab_max, ab_min = max(ab.width, ab.length), min(ab.width, ab.length)
        if ab_min < WIDE_CUT_MIN_SHORT_SIDE or ab_max < target_width:
            continue
        diff = ab_max - target_width
        if diff < best_diff or (diff == best_diff and ab_min > best_short):
            best_diff, best_short = diff, ab_min
            cnt = max(1, -(-product_length // ab_min))
            best = (ab, ab_max, ab_min, cnt)
    return best


def _wide_cut_length_fill(
    boards: list[SelectedBoard], short_side: int, cnt: int, product: ProductSize,
    available: list[BoardModel], target_width: int,
) -> None:
    """カット前提選定後の丈補填(上用・下用共通)。

    丈残に収まる中で**最も短辺が大きい**ボードを丈補填として追加する
    (最大5回)。候補が1枚も無いときだけ主ボードを1枚増やす。

    【早期強制追加の撤去】以前は冒頭に「丈残400超 → 主ボード+1枚」という
    分岐があった。下用PASS2/PASS3・PASS共通ブロック・上用丈補填フェーズで
    撤去済みの「早期強制追加」と同じ性質の処理で、この関数がフォールバック
    経路だったために撤去対象から漏れていた。丈残が400以下になるまで主ボード
    だけで消化してしまい、直後の同幅補填探索に一度も到達できないため、
    同幅の短いボードを使う余地を消していた。丈残はそのまま補填探索へ
    引き渡し、候補が無い場合のみ既存の「主ボード+1枚」分岐が受け持つ。

    【この関数は現状到達しない】`_pick_wide_cut_board` の枚数が
    `ceil(製品丈 / 短辺)` なので `短辺 × 枚数 >= 製品丈` が常に成り立ち、
    `len_gap` は 0 以下にしかならない(VBA `SelectBoardsForWideLower` の
    `cntL` も同じ天井計算なので、VBA側も同様に到達しない)。枚数の決め方が
    変わったときに正しく動くよう、仕様どおりに保守してある。
    """
    len_gap = product.length - short_side * cnt
    if len_gap <= 3:
        return
    log.debug("[幅広丈補填] 丈残=%smm", len_gap)

    # VBA `SortBoardsByTargetWidth(Palette.width)`。採用は「丈残に収まる中で
    # 短辺が最大」なので順序は同点時の決まり方にだけ効く(VBAは先に現れた方)
    sorted_boards = sort_boards_by_target_width(available, target_width)

    for _ in range(5):
        if len_gap <= 3:
            return

        found: Optional[BoardModel] = None
        best_short = 0
        for cand in sorted_boards:
            c_short = min(cand.width, cand.length)
            if c_short < NARROW_MIN_SHORT_SIDE:
                continue
            if c_short <= len_gap + 3 and c_short > best_short:
                best_short, found = c_short, cand

        if found is None:
            boards[0].count += 1
            len_gap -= short_side
            log.debug("[幅広丈補填] 小ボードなし → 主+1枚 計%s枚", boards[0].count)
            continue

        existing = next((s for s in boards if s.width == found.width and s.length == found.length), None)
        if existing is not None:
            existing.count += 1
        else:
            boards.append(SelectedBoard(width=found.width, length=found.length, count=1, tag=TAG_LENGTH_FILL))
        len_gap -= best_short
        log.debug("[幅広丈補填] 小ボード補填 %sx%s 残=%smm", found.width, found.length, len_gap)


def _select_wide_cut(
    available: list[BoardModel], target_width: int, product: ProductSize,
    fallback_tag: str,
    fatigue_map: Optional[dict[str, FatigueEntry]], fatigue_mode: bool,
) -> WideCutResult:
    """カット前提選定の共通処理(上用・下用で対象幅とフォールバックのタグのみ異なる)。"""
    result = WideCutResult(boards=[])
    picked = _pick_wide_cut_board(available, target_width, product.length, fatigue_map, fatigue_mode)

    if picked is not None:
        board_obj, max_side, short_side, cnt = picked
        result.boards.append(SelectedBoard(
            width=board_obj.width, length=board_obj.length, count=cnt, tag=TAG_CUT_PREMISE,
        ))
        # 切断線は短辺方向
        result.cut_info.setdefault(f"{board_obj.width}x{board_obj.length}", short_side)
        log.debug("幅広選定: %sx%s 最大辺=%s 短辺=%s %s枚 (カット前提)",
                  board_obj.width, board_obj.length, max_side, short_side, cnt)
        _wide_cut_length_fill(result.boards, short_side, cnt, product, available, target_width)
        return result

    # 最大辺が対象幅に届くボードが無い → 最大辺が最大のボードを採用(カットなし)
    fall = max(available, key=lambda a: max(a.width, a.length), default=None)
    if fall is not None:
        f_short = min(fall.width, fall.length)
        f_cnt = max(1, -(-product.length // f_short)) if f_short > 0 else 1
        result.boards.append(SelectedBoard(width=fall.width, length=fall.length, count=f_cnt, tag=fallback_tag))
        result.used_fallback = True
        log.debug("採用(幅不足フォールバック): %sx%s %s枚 effW=%s",
                  fall.width, fall.length, f_cnt, max(fall.width, fall.length))
    return result


def select_boards_for_wide_lower(
    available: list[BoardModel], palette: Palette, product: ProductSize,
    *, fatigue_map: Optional[dict[str, FatigueEntry]] = None, fatigue_mode: bool = False,
) -> WideCutResult:
    """VBA `SelectBoardsForWideLower` の移植(下用カット前提選定)。

    対象幅はパレット幅。フォールバック時のタグは "主"。

    【丈カット情報の記録漏れを修正】以前はここで丈カットが発生しても
    `length_cut_info` に何も記録していなかった(上用の
    `select_upper_boards_wide_cut` には対応する記録処理があったが、
    下用側は無かった)。`recalc_length_cut_info` は「カット前提」
    タグのボードを判定対象外にするので、その代わりにここで記録
    しないと、丈カットの情報がどこにも残らない(現場の声で報告された
    「3枚出るはずが分割されずに出力される」不具合の一因)。丈補填
    (`_wide_cut_length_fill`、100mm/400mm閾値で枚数を調整する)が
    終わった後、それでもわずかに超過が残っていれば記録する
    (`select_upper_boards_wide_cut` と同じ `+3` の誤差吸収)。
    """
    result = _select_wide_cut(
        available, palette.width, product, TAG_MAIN, fatigue_map, fatigue_mode)
    if not result.boards:
        return result

    main = result.boards[0]
    if main.tag != TAG_CUT_PREMISE:
        # フォールバック(在庫が対象幅に届かず、最大辺が最大のものを
        # そのまま採用した)ときはタグが"主"のままで、`recalc_length_cut_info`
        # の通常ループがこのボードを対象にする(除外されない)。ここで
        # 二重に記録すると、`_orient` による向きの判断が異なるせいで
        # 別の値になり、後からマージしても食い違ったまま残ってしまう
        return result

    short_side = min(main.width, main.length)
    if short_side <= 0:
        return result

    final_total_l = short_side * main.count
    if final_total_l > palette.length + 3:
        result.length_cut_info.setdefault(f"L_{main.width}x{main.length}", short_side)
        log.debug("select_boards_for_wide_lower: 丈カット記録 L_%sx%s shortSide=%s",
                 main.width, main.length, short_side)
    return result


def select_boards_for_wide_product(
    available: list[BoardModel], product: ProductSize,
    *, fatigue_map: Optional[dict[str, FatigueEntry]] = None, fatigue_mode: bool = False,
) -> WideCutResult:
    """VBA `SelectBoardsForWideProduct` の移植(上用カット前提選定)。

    対象幅は製品幅。フォールバック時のタグは空("")。
    """
    return _select_wide_cut(available, product.width, product, "", fatigue_map, fatigue_mode)


def select_upper_boards_wide_cut(
    available: list[BoardModel], product: ProductSize,
    *, fatigue_map: Optional[dict[str, FatigueEntry]] = None, fatigue_mode: bool = False,
) -> WideCutResult:
    """VBA `SelectUpperBoardsWideCut` の移植。

    ボード選定は `SelectBoardsForWideProduct` に委譲し、その後
    「短辺×枚数」で丈合計を見て不足分の枚数を追加する
    (丈超過はカット前提のため許容)。丈超過時は丈カット情報を記録する。
    """
    result = select_boards_for_wide_product(
        available, product, fatigue_map=fatigue_map, fatigue_mode=fatigue_mode,
    )
    if not result.boards:
        log.debug("SelectUpperBoardsWideCut: ボード選定失敗")
        return result

    main = result.boards[0]
    short_side = min(main.width, main.length)
    l_gap = product.length - short_side * main.count

    if l_gap > 3:
        add_cnt = max(1, -(-l_gap // short_side))
        main.count += add_cnt
        log.debug("SelectUpperBoardsWideCut: 丈不足 +%s枚 計%s枚", add_cnt, main.count)
    else:
        log.debug("SelectUpperBoardsWideCut: lGap=%smm (カット前提で許容)", l_gap)

    if main.tag != TAG_CUT_PREMISE:
        # フォールバック(在庫が対象幅に届かず、最大辺が最大のものを
        # そのまま採用した)ときはタグが空("")のままで、
        # `recalc_length_cut_info` の通常ループがこのボードを対象に
        # する(除外されない)。ここで二重に記録すると、`_orient` に
        # よる向きの判断が異なるせいで別の値になり、後からマージしても
        # 食い違ったまま残ってしまう
        return result

    final_total_l = short_side * main.count
    if final_total_l > product.length + 3:
        # 【修正】これは丈カットの記録なので `length_cut_info` に入れる。
        # VBAも `mLengthCutInfo.Add "U_" & mainW & "x" & mainL, shortSide` と
        # している。以前は幅カット側(`cut_info`)に入れていたため、
        #   - 切断依頼書が丈カットを検出できない
        #   - 幅カット辞書に "U_" 付きキーが紛れ、幅カット判定にも当たらない
        # という二重の取りこぼしになっていた
        # (`recalc_length_cut_info` はカット前提の上用をスキップするので、
        #  ここで入れないと丈カットの記録がどこにも残らない)
        result.length_cut_info.setdefault(f"U_{main.width}x{main.length}", short_side)
    return result
