"""上用ボードの選定 ── 製品の上に載せる側

下用と基準が違う。

  * 幅の基準がパレット幅ではなく**製品幅**
  * 製品幅の**超過は厳禁**(はみ出し許容係数を掛けない)
  * 幅不足は UPPER_WIDTH_TOLERANCE(80mm、両端-40mm)まで許す

丈補填の閾値と専用サイズは下用と共通(`board_selection_common`)。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .board_scoring import (FatigueEntry, LOWER_OVERHANG_Y, MAX_WIDTH_STRIPS,
                            PASS1_TOLERANCE, UPPER_WIDTH_TOLERANCE,
                            WIDTH_OVERSHOOT_LIMIT, get_best_orientation,
                            sort_boards_by_target_width, total_fatigue)
from .board_selection_common import (
    COVERED_TOLERANCE, FILL_OVER_MARGIN, FILL_RETRY_LIMIT, FILL_SIZE_30,
    FILL_SIZE_50, FILL_SIZE_100, GAP_FILLER_OVER_MARGIN,
    LENGTH_CUT_REMAIN_THRESHOLD, LENGTH_FILL_BOARD_W, LENGTH_FILL_COUNT_CAP,
    LENGTH_FILL_THRESHOLD, MAX_MAIN_BOARD_KINDS, NARROW_MIN_SHORT_SIDE,
    SC_1X2_BOARD_L, SC_1X2_BOARD_W,
    SC_1X2_L_MAX, SC_1X2_L_MIN, SC_1X2_W_MAX, SC_1X2_W_MIN, SC_4X8_BOARD_L,
    SC_4X8_BOARD_W, SC_4X8_L_MAX, SC_4X8_L_MIN, SC_4X8_W_MAX, SC_4X8_W_MIN,
    SC_5X8_L_FILL_C, SC_5X8_L_FILL_L, SC_5X8_L_FILL_W, SC_5X8_L_MAIN_C,
    SC_5X8_L_MAIN_L, SC_5X8_L_MAIN_W, SC_5X8_U_FILL1_C, SC_5X8_U_FILL1_L,
    SC_5X8_U_FILL1_W, SC_5X8_U_FILL2_C, SC_5X8_U_FILL2_L, SC_5X8_U_FILL2_W,
    SC_5X8_U_MAIN_L, SC_5X8_U_MAIN_W, TAG_CUT_PREMISE, TAG_LENGTH_FILL,
    TAG_MAIN, TAG_WIDTH_FILL, _add_or_increment, _current_covered_length,
    _fill_consistency_ok, _find_industry_standard_stock, _find_small_board,
    _force_short_side_if_overshoot, _has_available_board,
    _industry_standard_board_size, _is_5x8_range, _measure_selection, _orient,
    _orient_lower, _pick_gap_filler, adjust_orientation_for_coverage,
    can_cover_with_fill)
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .board_selection_types import (LowerSelectionResult, PassState,
                                    ProtecCutResult, UpperSelectionResult)
from .logging_utils import get_logger
from .models import BoardModel
from .board_selection_fill import (_apply_fixed_length_fill_100mm,
                                   decide_length_count_with_cut,
                                   run_width_fill_phase)
from .board_selection_narrow import (select_boards_for_narrow_palette_upper,
                                     select_upper_boards_wide_cut)
from .board_selection_protec import (PROTEC_1P1216_TOLERANCE,
                                     PROTEC_OTHER_TOLERANCE,
                                     decide_protec_orientation)

log = get_logger("board_selection.upper")



# ==================================================================
# 上用選定 (VBA `SelectUpperBoards` の移植)
#
# 上用は製品の上に載せるボードで、下用(パレット上に敷く)とは基準が異なる:
#   - 幅の基準がパレット幅ではなく「製品幅」
#   - 製品幅の超過は厳禁(はみ出し許容係数を適用しない)
#   - 幅不足は UPPER_WIDTH_TOLERANCE(80mm、両端-40mm)まで許容
# ==================================================================

# 丈補填の閾値・専用サイズは下用と共通(`LENGTH_FILL_THRESHOLD`/
# `LENGTH_FILL_BOARD_W`、モジュール冒頭で定義)。

# プロテックの上用/下用が製品幅より小さくてよい許容量(mm)。
# 1P1216 のみ精度の都合で厳しめの10mm(本来は-20mmだが切断精度を
# 考慮して厳しめにする)、それ以外は80mm(VBA `DecideProtecOrientation`
# の判定基準)。カットが必要なときの仕上がり幅(「製品幅-この許容」)



def _copy_lower_main_to_upper(lower: list[SelectedBoard]) -> list[SelectedBoard]:
    """下用の主ボード(先頭)をそのまま上用にコピーする(タグは空)。"""
    main = lower[0]
    return [SelectedBoard(width=main.width, length=main.length, count=main.count, tag="")]


def _upper_total_length(boards: list[SelectedBoard], product: ProductSize) -> int:
    """VBA「上用:丈合計計算」の移植。

    幅補填は丈方向に寄与しないため除外、丈補填は短辺×枚数、
    それ以外は有効丈×枚数で合算する。
    """
    total = 0
    for b in boards:
        if b.tag == TAG_WIDTH_FILL:
            continue
        if b.tag == TAG_LENGTH_FILL:
            total += min(b.width, b.length) * b.count
        else:
            _, _, eff_l = _orient(b.width, b.length, product.width, category="上用")
            total += eff_l * b.count
    return total


def _run_upper_length_fill_loop(
    boards: list[SelectedBoard], product: ProductSize, available: list[BoardModel],
    fatigue_map: Optional[dict[str, FatigueEntry]], length_remaining: int,
) -> int:
    """VBA `SelectUpperBoards`「上用丈補填(同幅探索ループ)」の移植(最大5回)。

    丈残が `LENGTH_FILL_THRESHOLD`(400mm)を超える間、在庫全体から
    「主ボードのeffWと長辺が完全一致」する候補のうち、残り丈との差が
    最小(無ければ+50mmまでの超過を許容し、差が同じなら超過側を優先)の
    ものを1枚ずつ追加する。

    【仕様更新】以前は丈残400mm超のとき主ボードを1枚増やすだけの単純な
    規則だったが、主+1後もまだ400mm超が残るケース(例: 製品丈3600、
    主2500使用→残1100)で、在庫にある同幅の板(500×1250等)を一度も
    検討しないまま主ボードだけを積み増していた(現場の声:「丈残400超の
    間は在庫全体から丈残に近い短辺のボードを探す、を繰り返してほしい」)。
    下用の丈補填反復ループ(`_run_length_fill_loop`)と同じ「同幅の
    手頃な板を探す」規則に揃える。

    【VBA踏襲】このループ自体は既存の同一サイズ行への合算(マージ)を
    行わない ── 見つかるたびに新規の"丈補填"行を1枚ずつ追加する
    (下用の丈補填反復ループとは異なる)。また疲労度は候補の並び順
    (ソート)にのみ影響し、この関数自身の採否スコアには使わない。

    見つからなくなった時点で打ち切り、残った丈残を返す(呼び出し側が
    100mm固定・丈不足強制追加へ引き継ぐ)。
    """
    if length_remaining <= LENGTH_FILL_THRESHOLD or not boards:
        return length_remaining

    main = boards[0]
    _, main_eff_w, _ = _orient(main.width, main.length, product.width, category="上用")

    for loop_idx in range(5):
        if length_remaining <= LENGTH_FILL_THRESHOLD:
            break
        sorted_upper = sort_boards_by_target_width(
            available, product.width, strict=False, fatigue_map=fatigue_map)

        best: Optional[tuple[int, int, int, int]] = None  # short, long, w, l
        best_diff = 999999
        for b in sorted_upper:
            short, long_side = min(b.width, b.length), max(b.width, b.length)
            if long_side != main_eff_w or short < 10:
                continue
            # 【仕様更新】30/50/100mmは丈残400以下用の補填専用サイズであり、
            # このループ(丈残400超が対象)の候補にしてはならない。除外しないと、
            # 同幅の在庫がこれらしか無い場合に延々と選ばれ続け、5回の上限まで
            # 使い切ってしまう(現場の声:「ループが同じ50mm板を何度でも選べて
            # しまう構造になっている」)。
            if short in (FILL_SIZE_30, FILL_SIZE_50, FILL_SIZE_100):
                continue
            if short <= length_remaining:
                diff = length_remaining - short
            elif short <= length_remaining + 50:
                diff = short - length_remaining
            else:
                continue

            take = best is None or diff < best_diff
            if not take and diff == best_diff:
                take = short > length_remaining and best[0] <= length_remaining
            if take:
                best_diff = diff
                best = (short, long_side, b.width, b.length)

        if best is None:
            log.debug("上用丈補填(同幅探索)ループ%s: 適合なし → 終了", loop_idx + 1)
            break

        short, _, bw, bl = best
        boards.append(SelectedBoard(width=bw, length=bl, count=1, tag=TAG_LENGTH_FILL))
        length_remaining -= short
        log.debug("上用丈補填(同幅採用): %sx%s 丈残=%s", bw, bl, length_remaining)

    return length_remaining


def _run_upper_length_fill(
    boards: list[SelectedBoard], product: ProductSize, available: list[BoardModel],
    fatigue_map: Optional[dict[str, FatigueEntry]] = None,
) -> None:
    """VBA「上用:丈補填フェーズ」の移植。"""
    total_upper_l = _upper_total_length(boards, product)
    length_remaining = product.length - total_upper_l
    if length_remaining <= 3:
        return
    log.debug("上用丈補填フェーズ: 丈残=%s", length_remaining)
    length_remaining = _run_upper_length_fill_loop(
        boards, product, available, fatigue_map, length_remaining)
    _apply_fixed_length_fill_100mm(boards, length_remaining, available, category="上用")


def select_upper_boards(
    lower_boards: list[SelectedBoard], available: list[BoardModel],
    palette: Palette, product: ProductSize,
    *,
    fatigue_map: Optional[dict[str, FatigueEntry]] = None,
    fatigue_mode: bool = False,
    stock_aware: bool = False,
    is_protec_mode: bool = False,
    is_protec_1p1216: bool = False,
    last_hosozai: str = "",
    protec_result: Optional[ProtecCutResult] = None,
) -> UpperSelectionResult:
    """VBA `SelectUpperBoards` の移植。

    処理順:
        1. 上下共用モード(ザラ板等): 保護材が確定していてアングルでも
           「一致なし」でもなければ、上用は下用の主ボードと同サイズを強制
        2. プロテックモード: 下用選定(`select_protec_lower_boards`)が
           確定させた `protec_result` をそのまま踏襲する。**上用が
           独自に判定し直さない** ── 以前は上用がここで
           `GetBestOrientation` を素で呼び直しており、下用と同じ条件で
           判定したはずなのに、丸め誤差やロジックの違いで「条件に
           合わない」と判断し、下用と無関係な別サイズを選んでしまう
           ことがあった。`protec_result` が無い(下用がプロテック選定を
           使わなかった)場合のみ、フォールバックとして
           `decide_protec_orientation` で下用主ボードを判定し直す
        2.5. 業界標準サイズショートカット(通常モードでのみ発動)。
           下用の同名ショートカットと同じ判定範囲・対応サイズを使う
        3. 主ボード選択(製品幅基準・strict・最初に条件を満たしたもの)
        4. 1件も選べなければ狭幅パレット扱い
        5. 幅補填(不足がUPPER_WIDTH_TOLERANCEを超えるときのみ)
           → 補填後も足りなければカット前提選定が必要と報告
        6. 丈合計計算 → 丈補填フェーズ
        7. 丈不足強制追加(`_force_add_for_upper_length_shortage`)
        8. 補填ボードの幅不足を30/50/100mmで補填
           (`_apply_upper_fill_width_correction`)
    """
    boards: list[SelectedBoard] = []
    result = UpperSelectionResult(boards=boards)

    # 1. 上下共用モード
    if (not is_protec_mode and last_hosozai
            and last_hosozai not in ("アングル", "一致なし") and lower_boards):
        result.boards = _copy_lower_main_to_upper(lower_boards)
        result.mode = "共用"
        log.debug("SelectUpperBoards [上下共用]: 下用と同サイズ強制 (%s)", last_hosozai)
        return result

    # 2. プロテックモード
    if is_protec_mode and lower_boards:
        pr = protec_result
        if pr is None or not pr.valid:
            # フォールバック: 下用選定がプロテック確定値を持っていない
            # 場合だけ、ここで判定し直す(通常は起こらない経路)
            main = lower_boards[0]
            pr = decide_protec_orientation(
                main.width, main.length, product.width, is_1p1216=is_protec_1p1216)
        if pr.valid:
            # 【上用も下用と同じ「製品幅よりマイナス」ルールに従う】
            # プロテックは上下とも製品幅を超えてはいけない
            # (以前は上用だけ製品幅を超えてよいと誤って実装していた)。
            # `_copy_lower_main_to_upper` は元の在庫サイズをそのまま
            # コピーするだけなので、幅カットが要るときはそれでは
            # ならず、下用と同じ確定値(`ProtecCutResult`)から作り直す。
            # 幅カットが不要なら、元の在庫サイズがそのまま製品幅以内に
            # 収まっているのでコピーで問題ない。
            #
            # 【丈方向は下用と別に決め直す】幅の確定値(cut_eff_width等)は
            # 下用と共通(同じボードを使うため)だが、丈方向の枚数・
            # カット要否は下用(パレット丈基準)と上用(製品丈基準)で
            # 別の答えになりうる。ここで上用専用の丈判定を行い、
            # `ProtecCutResult` のコピーに上書きする(下用の `protec_result`
            # は変えない ── `recalc_length_cut_info`/カット依頼書が
            # それぞれ正しい方を参照できるようにするため)
            u_count, u_need_len_cut, u_len_cut_eff = decide_length_count_with_cut(
                pr.eff_length, product.length)
            upper_pr = replace(
                pr, count=u_count, need_length_cut=u_need_len_cut,
                length_cut_eff=u_len_cut_eff)

            tag = TAG_CUT_PREMISE if (pr.need_cut or u_need_len_cut) else TAG_MAIN
            main = lower_boards[0]
            result.boards = [SelectedBoard(
                width=main.width, length=main.length, count=u_count, tag=tag)]
            result.mode = "プロテック"
            result.protec_result = upper_pr
            log.debug("SelectUpperBoards [プロテック]: 下用の確定値を踏襲 "
                     "cutEffW=%s needCut=%s upperCount=%s needLengthCut=%s tag=%s",
                     pr.cut_eff_width, pr.need_cut, u_count, u_need_len_cut, tag)
            return result
        log.debug("SelectUpperBoards [プロテック]: 下用サイズが許容外 → 通常選定へ")

    # 2.5. 業界標準サイズショートカット(通常モードでのみ発動)。
    # 下用の同名ショートカット(`select_lower_boards`)と同じ判定範囲・
    # 対応サイズを使う(`_industry_standard_board_size`)。上下共用・
    # プロテックの強制コピー判定より後に置くのは、それらが優先される
    # べきモードだから ── 通常モードでのみ、PASS1が最初に見つけた
    # 候補で打ち切ってしまう問題への対応として発動する
    if not is_protec_mode and not (last_hosozai and last_hosozai not in ("アングル", "一致なし")):
        std_size = _industry_standard_board_size(product)
        if std_size is not None:
            std_stock = _find_industry_standard_stock(available, *std_size)
            if std_stock is not None:
                eff_l = min(std_stock.width, std_stock.length)
                count, need_length_cut, length_cut_eff = decide_length_count_with_cut(
                    eff_l, product.length)
                board = SelectedBoard(
                    width=std_stock.width, length=std_stock.length,
                    count=count, tag=TAG_MAIN)
                result.boards = [board]
                if need_length_cut:
                    result.length_cut_info[f"U_{board.width}x{board.length}"] = length_cut_eff
                log.debug("SelectUpperBoards [業界標準ショートカット]: %sx%s %s枚 "
                         "(製品%sx%s、needLengthCut=%s)",
                         board.width, board.length, count,
                         product.width, product.length, need_length_cut)
                return result

    # 2.6. "5×8"専用サイズショートカット(上用、通常モードでのみ発動)。
    # "1×2"/"4×8"と違い1枚では収まらないため、主1枚+幅補填2種を直接
    # 組み立てる。使用する3サイズのいずれかが在庫に無い場合は、中途半端な
    # 構成で確定させず一般ロジックへ落とす。
    if not is_protec_mode and not (last_hosozai and last_hosozai not in ("アングル", "一致なし")):
        if _is_5x8_range(product):
            if (_has_available_board(available, SC_5X8_U_MAIN_W, SC_5X8_U_MAIN_L)
                    and _has_available_board(available, SC_5X8_U_FILL1_W, SC_5X8_U_FILL1_L)
                    and _has_available_board(available, SC_5X8_U_FILL2_W, SC_5X8_U_FILL2_L)):
                result.boards = [
                    SelectedBoard(width=SC_5X8_U_MAIN_W, length=SC_5X8_U_MAIN_L,
                                  count=1, tag=""),
                    SelectedBoard(width=SC_5X8_U_FILL1_W, length=SC_5X8_U_FILL1_L,
                                  count=SC_5X8_U_FILL1_C, tag=TAG_WIDTH_FILL),
                    SelectedBoard(width=SC_5X8_U_FILL2_W, length=SC_5X8_U_FILL2_L,
                                  count=SC_5X8_U_FILL2_C, tag=TAG_WIDTH_FILL),
                ]
                log.debug("SelectUpperBoards [5×8ショートカット]: %sx%s 1枚 + %sx%s %s枚 + %sx%s %s枚 (製品%sx%s)",
                         SC_5X8_U_MAIN_W, SC_5X8_U_MAIN_L,
                         SC_5X8_U_FILL1_W, SC_5X8_U_FILL1_L, SC_5X8_U_FILL1_C,
                         SC_5X8_U_FILL2_W, SC_5X8_U_FILL2_L, SC_5X8_U_FILL2_C,
                         product.width, product.length)
                return result
            log.debug("SelectUpperBoards: 5×8該当だが必要サイズが在庫にない → 一般ロジックへ")

    # 3. 主ボード選択(最初に条件を満たしたものを採用)
    sorted_upper = sort_boards_by_target_width(
        available, product.width, strict=True, fatigue_map=fatigue_map,
        base_length=product.length, stock_aware=stock_aware,
        log_label="上用",
    )
    main_eff_w = 0
    for b in sorted_upper:
        _, eff_w, eff_l = _orient(b.width, b.length, product.width, category="上用")
        if eff_w > product.width:
            log.debug("却下(幅超過): %sx%s effW=%s", b.width, b.length, eff_w)
            continue
        if eff_w < product.width * 0.5:
            log.debug("却下(幅不足): %sx%s effW=%s", b.width, b.length, eff_w)
            continue
        if b.width <= 100 or b.length <= 100:
            log.debug("上用除外(補填専用): %sx%s", b.width, b.length)
            continue

        if eff_l > 0:
            adjusted_len = max(0, product.length - 5)
            cnt = max(1, adjusted_len // eff_l)
        else:
            cnt = 1

        boards.append(SelectedBoard(width=b.width, length=b.length, count=cnt, tag=""))
        key = f"{b.width}x{b.length}"
        if eff_l * cnt > product.length:
            result.length_cut_info.setdefault(key, eff_w)
        if eff_w > product.width:
            result.cut_info.setdefault(key, eff_l)
        main_eff_w = eff_w
        log.debug("上用選択: %sx%s %s枚 effW=%s", b.width, b.length, cnt, eff_w)
        break

    # 4. 狭幅パレット判定
    if not boards:
        log.debug("▼上用 狭幅パレット検出 → 小型ボードで幅方向カバー")
        result.narrow_pallet = True
        boards.extend(select_boards_for_narrow_palette_upper(available, palette, product))
        return result

    # 5. 幅補填(許容を超える不足のときだけ実行)
    upper_width_gap = product.width - main_eff_w
    if upper_width_gap > UPPER_WIDTH_TOLERANCE:
        post_fill_w = run_width_fill_phase(
            boards, main_eff_w, product.width, product.width, product.length, available,
        )
    else:
        post_fill_w = main_eff_w

    if post_fill_w < product.width - UPPER_WIDTH_TOLERANCE:
        log.debug("[上用] 幅補填後もカバー不足(%s < %s) → カット前提選定へ", post_fill_w, product.width)
        boards.clear()
        result.needs_wide_cut = True
        wide = select_upper_boards_wide_cut(
            available, product, fatigue_map=fatigue_map, fatigue_mode=fatigue_mode,
        )
        boards.extend(wide.boards)
        result.cut_info.update(wide.cut_info)
        result.length_cut_info.update(wide.length_cut_info)
        # 丈計算/補填フェーズは select_upper_boards_wide_cut 内で完結する
        return result

    # 6. 丈合計計算 → 丈補填フェーズ
    _run_upper_length_fill(boards, product, available, fatigue_map)

    # 7. 丈不足強制追加
    _force_add_for_upper_length_shortage(boards, product, available, fatigue_map)

    # 8. 補填ボードの幅不足を小型ボードで補填
    _apply_upper_fill_width_correction(boards, product, available)
    return result


def _force_add_for_upper_length_shortage(
    boards: list[SelectedBoard], product: ProductSize, available: list[BoardModel],
    fatigue_map: Optional[dict[str, FatigueEntry]] = None,
) -> None:
    """VBA「上用: 丈不足強制追加」の移植。

    丈合計が製品丈に3mm超足りない場合、まず「ギャップ以上・ギャップの3倍
    以内の短辺を持つ小型ボード」を1種類だけ足す。見つからなければ
    主ボードを必要枚数だけ増やして強制的に丈を満たす。
    """
    if not boards:
        return
    gap = product.length - _upper_total_length(boards, product)
    log.debug("上用丈再確認: finalULGap=%s", gap)
    if gap <= COVERED_TOLERANCE:
        return

    # 【仕様更新】主ボードの実効幅をこのブロック内で再計算する。丈補填
    # ループで算出した値を持ち回すと、上流のブロックが実行されたかに
    # このブロックの判定が依存してしまうため、単体で完結させる
    main = boards[0]
    _, main_eff_w, _ = _orient(main.width, main.length, product.width, "上用")

    for cand in sort_boards_by_target_width(
            available, product.width, strict=True,
            fatigue_map=fatigue_map, base_length=product.length):
        short, long_side = min(cand.width, cand.length), max(cand.width, cand.length)
        # ギャップを埋められる最小限のサイズだけを許す(大きすぎる板は使わない)
        if short < gap or short > gap * 3 or long_side < NARROW_MIN_SHORT_SIDE:
            continue
        # 【仕様更新】主ボードと同幅(完全一致)のみ候補とする。従来は
        # 短辺が丈残の1〜3倍という条件だけで採用しており、主ボードより
        # 幅の狭い通常サイズ板が選ばれて後追い幅補填が必要になる
        # 構造的不具合の原因だった(下用のRunLowerFillPhase側と同一)
        if long_side != main_eff_w:
            continue
        # 【仕様更新】30/50/100mmは幅補填専用サイズのため、丈不足強制追加の
        # 候補にもしない。理由は同幅探索ループの除外と同一。
        if short in (FILL_SIZE_30, FILL_SIZE_50, FILL_SIZE_100):
            continue
        # 【VBAからの修正】タグを見ずに幅・丈だけでマッチしていたため、同サイズの
        # 「主」や「幅補填」ボードが存在すると、そちらの上限判定を誤って見てしまって
        # いた(sfSkipCap)。丈補填タグの行だけを対象にする。
        if any(b.width == cand.width and b.length == cand.length
               and b.tag == TAG_LENGTH_FILL and b.count >= LENGTH_FILL_COUNT_CAP for b in boards):
            continue
        # 【VBAからの修正】同上の理由(sfExists)。丈補填タグの行だけを対象にし、
        # 上限到達時は既存行を+1→4クランプするだけで済ませず、新規行の作成に回す。
        existing = next(
            (b for b in boards if b.width == cand.width and b.length == cand.length
             and b.tag == TAG_LENGTH_FILL),
            None,
        )
        if existing is not None:
            existing.count += 1
            log.debug("上用丈補填(小ボード既存+1): %sx%s lGap=%s", cand.width, cand.length, gap)
        else:
            boards.append(SelectedBoard(width=cand.width, length=cand.length, count=1, tag=TAG_LENGTH_FILL))
            log.debug("上用丈補填(小ボード新規): %sx%s lGap=%s", cand.width, cand.length, gap)
        return

    # 適当な小型ボードが無ければ主ボードを増やす
    main = boards[0]
    _, _, main_eff_l = _orient(main.width, main.length, product.width, "上用")
    if main_eff_l <= 0:
        return
    add = -(-gap // main_eff_l)          # 切り上げ
    main.count += add
    log.debug("上用丈不足強制追加: %sx%s +%s枚 計%s枚",
              main.width, main.length, add, main.count)


def _apply_upper_fill_width_correction(
    boards: list[SelectedBoard], product: ProductSize, available: list[BoardModel],
) -> None:
    """VBA「上用: 補填ボードの幅不足を30/50/100mmで補填」の移植。

    丈補填などで足した行が製品幅に届いていない場合、その脇に小型ボード
    (50/30/100mmの順で探す)を敷いて幅を埋める。小型ボードが無い、または
    必要枚数が4枚以上になる場合は割に合わないので、その行を捨てて
    主ボードを1枚増やす。

    下用の `apply_length_fill_width_correction` と同じ考え方だが、
    基準が製品幅であることと、判定に「選定中の最大有効幅」を使う点が異なる。
    """
    small_sizes = (FILL_SIZE_50, FILL_SIZE_30, FILL_SIZE_100)

    idx = len(boards) - 1
    while idx >= 1:
        row = boards[idx]
        if row.tag == TAG_WIDTH_FILL or row.width in small_sizes or row.length in small_sizes:
            idx -= 1
            continue

        _, eff_w, eff_l = _orient(row.width, row.length, product.width, "上用")
        width_gap = product.width - eff_w
        if width_gap <= COVERED_TOLERANCE:
            idx -= 1
            continue

        # 主ボード群の最大有効幅より狭い行だけが補填対象
        max_main_w = 0
        for b in boards:
            if b.tag in (TAG_WIDTH_FILL, TAG_LENGTH_FILL):
                continue
            _, w, _ = _orient(b.width, b.length, product.width, "上用")
            max_main_w = max(max_main_w, w)
        if eff_w >= max_main_w:
            idx -= 1
            continue

        log.debug("上用補填幅不足: %sx%s effW=%s widthGap=%s",
                  row.width, row.length, eff_w, width_gap)

        best = _find_small_board(available, small_sizes, width_gap)
        if best is None:
            log.debug("  上用小ボードなし → 主ボード+1")
            boards[0].count += 1
            del boards[idx]
            idx -= 1
            continue

        small_long = max(best.width, best.length)
        needed = -(-(eff_l * row.count) // small_long) if small_long > 0 else 1
        needed = max(needed, 1)
        if needed >= LENGTH_FILL_COUNT_CAP:
            log.debug("  上用小ボード%s枚>=4 → 主ボード+1", needed)
            boards[0].count += 1
            del boards[idx]
        else:
            _add_or_increment(boards, best.width, best.length, TAG_WIDTH_FILL, needed)
            log.debug("  上用小ボード補填: %sx%s %s枚", best.width, best.length, needed)
        idx -= 1
