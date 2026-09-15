"""下用ボードの選定 ── パレットの上に敷く側

パレット幅を基準に、PASS1〜3 の順で主ボードを決める。

  1枚物優先 … 1枚で覆えるものがあればそれを採る
  PASS1     … 幅の一致度が高いものから採る(通常/競合の2通り)
  PASS2/3   … PASS1で決まらなかったときの、条件をゆるめた探索

決まらなければ狭幅パレット選定・カット前提選定へ落ちる
(`board_selection_narrow`)。プロテックのときは最初から
`board_selection_protec` が決める。
"""
from __future__ import annotations

from typing import Optional

from .board_scoring import (FatigueEntry, LOWER_OVERHANG_Y, MAX_WIDTH_STRIPS,
                            PASS1_TOLERANCE, UPPER_WIDTH_TOLERANCE,
                            WIDTH_OVERSHOOT_LIMIT, get_best_orientation,
                            sort_boards_by_target_width, total_fatigue)
from .board_selection_common import (
    COVERED_TOLERANCE, FILL_OVER_MARGIN, FILL_RETRY_LIMIT, FILL_SIZE_30,
    FILL_SIZE_50, FILL_SIZE_100, GAP_FILLER_OVER_MARGIN,
    LENGTH_CUT_REMAIN_THRESHOLD, LENGTH_FILL_BOARD_W, LENGTH_FILL_COUNT_CAP,
    LENGTH_FILL_THRESHOLD, MAX_MAIN_BOARD_KINDS, SC_1X2_BOARD_L, SC_1X2_BOARD_W,
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
from .board_selection_fill import (decide_length_count_with_cut,
                                   run_lower_fill_phase,
                                   simulate_lower_width_fill)
from .board_selection_narrow import (run_narrow_palette_lower_fill,
                                     select_boards_for_narrow_palette,
                                     select_boards_for_wide_lower)
from .board_selection_protec import select_protec_lower_boards

log = get_logger("board_selection.lower")


# ------------------------------------------------------------------


# ------------------------------------------------------------------
# 1枚物優先選定
# ------------------------------------------------------------------
def _select_lower_single_piece(
    sorted_boards: list[BoardModel], boards: list[SelectedBoard], state: PassState,
    palette: Palette, product: ProductSize,
    fatigue_map: Optional[dict[str, FatigueEntry]], fatigue_mode: bool,
) -> None:
    """VBA `SelectLowerBoardsSinglePiece` の移植。

    製品を1枚でカバーできるボードを、パレットサイズに関係なく最優先で採用する。
        幅: product.width - PASS1_TOLERANCE <= effW <= palette.width * LOWER_OVERHANG_Y
        丈: product.length <= effL <= palette.length
    通常モードは最初の候補で即決定、疲労度モードは最小疲労度を選ぶ。
    """
    w_low = product.width - PASS1_TOLERANCE
    w_high = int(palette.width * LOWER_OVERHANG_Y)

    best: Optional[tuple[int, int, int, int, float]] = None  # (bw, bl, effW, effL, fat)
    for b in sorted_boards:
        if b.width <= 100 or b.length <= 100:
            continue
        # 向き決定は product.width 基準(パレット幅ではない)
        _, eff_w, eff_l = _orient(b.width, b.length, product.width)
        if eff_w < w_low or eff_w > w_high:
            continue
        if eff_l < product.length or eff_l > palette.length:
            continue

        fat = 0.0
        if fatigue_mode and fatigue_map is not None:
            fat = total_fatigue(fatigue_map, f"{b.width}x{b.length}", 1)

        if not fatigue_mode:
            best = (b.width, b.length, eff_w, eff_l, fat)
            break
        if best is None or fat < best[4]:
            best = (b.width, b.length, eff_w, eff_l, fat)

    if best is None:
        return

    bw, bl, eff_w, eff_l, _ = best
    boards.append(SelectedBoard(width=bw, length=bl, count=1, tag=TAG_MAIN))
    state.remaining_len -= eff_l
    state.selected_count += 1
    state.pass1_done = True
    state.pass15_used = False
    log.debug("下用(1枚物優先): %sx%s 1枚 effW=%s 残=%smm", bw, bl, eff_w, state.remaining_len)


# ------------------------------------------------------------------
# PASS1 (通常モード)
# ------------------------------------------------------------------
def _select_lower_pass1_normal(
    sorted_boards: list[BoardModel], boards: list[SelectedBoard], state: PassState,
    palette: Palette, product: ProductSize,
) -> None:
    """VBA `SelectLowerBoardsPass1Normal` の移植(疲労度比較なし、最初の一致で即決定)。"""
    for b in sorted_boards:
        if state.remaining_len <= 0 or state.selected_count >= MAX_MAIN_BOARD_KINDS:
            break
        if b.width <= 100 or b.length <= 100:
            continue

        if _current_covered_length(boards, palette) >= product.length:
            log.debug("製品丈カバー済み → 追加不要")
            break

        rot, eff_w, eff_l = _orient_lower(b.width, b.length, palette, product)
        if eff_w < palette.width * 0.5:
            continue
        # PASS1_TOLERANCE 内の微小超過は許容してからチェックへ進める
        if eff_w > palette.width + WIDTH_OVERSHOOT_LIMIT:
            if abs(eff_w - palette.width) > PASS1_TOLERANCE:
                continue
        if abs(eff_w - palette.width) > PASS1_TOLERANCE:
            continue

        cnt = state.remaining_len // eff_l
        if cnt <= 0:
            continue

        boards.append(SelectedBoard(width=b.width, length=b.length, count=cnt, tag=TAG_MAIN))
        state.remaining_len -= eff_l * cnt
        state.selected_count += 1
        state.pass1_done = True
        log.debug("下用(PASS1): %sx%s %s枚 effW=%s 残=%smm", b.width, b.length, cnt, eff_w, state.remaining_len)

        # 【仕様更新】旧「PASS1強制追加」(早期)を撤去。remainingLen>=300を
        # 条件に主ボードだけで枚数を確定させていたため、後段の丈補填
        # ループ(同幅完全一致のニアレストフィット)より先に発火し、
        # 同幅の短いボードを使う余地を消していた。丈残は補填フェーズへ
        # 引き渡し、埋まらなかった分のみ丈不足強制追加が受け持つ。
        log.debug("丈残%s → 補填フェーズへ引き渡し", state.remaining_len)
        break


# ------------------------------------------------------------------
# PASS1 / PASS1.5 競合選定 (疲労度モード)
# ------------------------------------------------------------------
def _select_lower_pass1_competitive(
    sorted_boards: list[BoardModel], boards: list[SelectedBoard], state: PassState,
    palette: Palette, product: ProductSize,
    fatigue_map: Optional[dict[str, FatigueEntry]], fatigue_mode: bool,
    available: list[BoardModel],
) -> None:
    """VBA `SelectLowerBoardsPass1Competitive` の移植。

    PASS1(パレット幅ぴったり)とPASS1.5(幅ギャップを補填で埋める前提)の
    両方の候補を集め、疲労度が小さい方を採用する。
    PASS1.5候補は疲労度モードのときだけ収集される。
    """
    max_fill_slots = MAX_WIDTH_STRIPS - 1

    p1: Optional[tuple[int, int, int, int, int, float]] = None   # bw,bl,effW,effL,cnt,fat
    p15: Optional[tuple[int, int, int, int, int, float, int]] = None  # + wGap

    for b in sorted_boards:
        if not fatigue_mode:
            if state.remaining_len <= 0 or state.selected_count >= MAX_MAIN_BOARD_KINDS:
                break
        if b.width <= 100 or b.length <= 100:
            continue

        rot, eff_w, eff_l = _orient_lower(b.width, b.length, palette, product)
        if eff_w < palette.width * 0.5:
            continue
        if eff_w > palette.width + WIDTH_OVERSHOOT_LIMIT:
            if abs(eff_w - palette.width) > PASS1_TOLERANCE:
                continue

        # 丈枚数(パレット丈に収まらない場合は除外)
        cnt = palette.length // eff_l if eff_l > 0 else 0
        if cnt <= 0:
            continue

        fat = 0.0
        if fatigue_mode and fatigue_map is not None:
            fat = total_fatigue(fatigue_map, f"{b.width}x{b.length}", cnt)

        if abs(eff_w - palette.width) <= PASS1_TOLERANCE:
            if not fatigue_mode:
                p1 = (b.width, b.length, eff_w, eff_l, cnt, fat)
                break
            if p1 is None or fat < p1[5]:
                p1 = (b.width, b.length, eff_w, eff_l, cnt, fat)
        elif fatigue_mode:
            w_gap = max(0, product.width - eff_w)
            if can_cover_with_fill(w_gap, max_fill_slots, available):
                if p15 is None or fat < p15[5]:
                    p15 = (b.width, b.length, eff_w, eff_l, cnt, fat, w_gap)
            else:
                log.debug("PASS1.5却下: %sx%s effW=%s gap=%smm (補填不可)", b.width, b.length, eff_w, w_gap)

    # 勝者決定
    use_pass15 = False
    if p15 is not None and (p1 is None or (fatigue_mode and p15[5] < p1[5])):
        use_pass15 = True
    elif p1 is None:
        return  # どちらも見つからず → PASS2 へ

    if use_pass15:
        assert p15 is not None
        sel_bw, sel_bl, sel_eff_w, sel_eff_l = p15[0], p15[1], p15[2], p15[3]
    else:
        assert p1 is not None
        sel_bw, sel_bl, sel_eff_w, sel_eff_l = p1[0], p1[1], p1[2], p1[3]

    cnt = state.remaining_len // sel_eff_l
    if cnt <= 0:
        cnt = 1

    boards.append(SelectedBoard(width=sel_bw, length=sel_bl, count=cnt, tag=TAG_MAIN))
    state.remaining_len -= sel_eff_l * cnt
    state.selected_count += 1
    state.pass1_done = True
    # 【VBAの不具合を踏襲】ここで pass15_used = True にすべきだが、
    # VBAは設定していないため代替ボードリトライループのガードが機能しない。
    log.debug("下用(%s): %sx%s %s枚 effW=%s 残=%smm",
              "PASS1.5採用" if use_pass15 else "PASS1", sel_bw, sel_bl, cnt, sel_eff_w, state.remaining_len)

    # 強制追加チェック(Competitiveはリスト件数を条件に含めない)
    if state.remaining_len >= 300:
        if sel_eff_l * (cnt + 1) <= int(palette.length * palette.overhang_ratio):
            boards[0].count = cnt + 1
            state.remaining_len = 0


# ------------------------------------------------------------------
# PASS2 / PASS3
# ------------------------------------------------------------------
def _select_lower_pass2(
    sorted_boards_strict: list[BoardModel], boards: list[SelectedBoard], state: PassState,
    palette: Palette, product: ProductSize,
) -> None:
    """VBA PASS2(ベストフィット)の移植。パレット幅超過を許さない。"""
    for b in sorted_boards_strict:
        if state.remaining_len <= 0 or state.selected_count >= MAX_MAIN_BOARD_KINDS:
            break
        if b.width <= 100 or b.length <= 100:
            continue

        rot, eff_w, eff_l = _orient_lower(b.width, b.length, palette, product)
        if eff_w < palette.width * 0.5:
            continue
        if not _fill_consistency_ok(boards, eff_w, palette, product):
            log.debug("却下(幅不整合): %sx%s effW=%s", b.width, b.length, eff_w)
            continue
        if eff_w > palette.width + WIDTH_OVERSHOOT_LIMIT:
            log.debug("却下(幅超過): %sx%s effW=%s", b.width, b.length, eff_w)
            continue

        cnt = state.remaining_len // eff_l
        if cnt <= 0:
            log.debug("却下: %sx%s effL=%s (残不足)", b.width, b.length, eff_l)
            continue

        boards.append(SelectedBoard(width=b.width, length=b.length, count=cnt, tag=TAG_MAIN))
        state.remaining_len -= eff_l * cnt
        state.selected_count += 1
        state.pass1_done = True
        state.pass15_used = False  # PASS2経由はPASS1.5ではない
        log.debug("下用(PASS2): %sx%s effW=%s %s枚 残=%s", b.width, b.length, eff_w, cnt, state.remaining_len)

        # 【仕様更新】旧「PASS2早期強制追加」を撤去。理由はPASS1と同一。
        log.debug("丈残%s → 補填フェーズへ引き渡し", state.remaining_len)
        break


def _select_lower_pass3(
    sorted_boards: list[BoardModel], boards: list[SelectedBoard], state: PassState,
    palette: Palette, product: ProductSize,
) -> None:
    """VBA PASS3(はみ出し許容)の移植。

    パレット幅×LOWER_OVERHANG_Y(20%はみ出し)まで許容する。
    選定したボードは `cut_info` に有効丈を記録する。
    【VBA踏襲】PASS3はタグを設定しない(空文字列のまま)。また
    pass1_done も立てないため、この後の狭幅パレット判定が実行される。
    """
    for b in sorted_boards:
        if state.remaining_len <= 0 or state.selected_count >= MAX_MAIN_BOARD_KINDS:
            break

        if _current_covered_length(boards, palette) >= product.length:
            log.debug("製品丈カバー済み → 終了")
            break

        if b.width <= 100 or b.length <= 100:
            continue

        rot, eff_w, eff_l = _orient_lower(b.width, b.length, palette, product)
        if eff_w < palette.width * 0.5:
            continue
        if not _fill_consistency_ok(boards, eff_w, palette, product):
            continue
        if eff_w > int(palette.width * LOWER_OVERHANG_Y):
            continue

        cnt = state.remaining_len // eff_l
        if cnt <= 0:
            continue

        boards.append(SelectedBoard(width=b.width, length=b.length, count=cnt, tag=""))
        state.remaining_len -= eff_l * cnt
        state.selected_count += 1

        cut_key = f"{b.width}x{b.length}"
        state.cut_info.setdefault(cut_key, eff_l)
        log.debug("下用(PASS3): %sx%s effW=%s %s枚 残=%s", b.width, b.length, eff_w, cnt, state.remaining_len)

        # 【仕様更新】旧「PASS3早期強制追加」を撤去。理由はPASS1と同一。
        log.debug("丈残%s → 補填フェーズへ引き渡し", state.remaining_len)
        break

def select_lower_boards(
    available: list[BoardModel], palette: Palette, product: ProductSize,
    *,
    fatigue_map: Optional[dict[str, FatigueEntry]] = None,
    fatigue_mode: bool = False,
    stock_aware: bool = False,
    is_protec_mode: bool = False,
    is_protec_1p1216: bool = False,
) -> LowerSelectionResult:
    """VBA `SelectLowerBoards` の移植。

    処理順:
        0. プロテック判定: `is_protec_mode` のときは `SelectProtecLowerBoards`
           を最優先で試し、成功すれば以下の通常PASS1-3をスキップする
           (プロテックは在庫が少なく、通常フローを一通り試すと遠回りに
           なるため)。在庫が見つからなければ通常フローへフォールバックする
        0.5. 業界標準サイズショートカット(通常モードのみ): 製品サイズが
           "1×2"/"4×8"の範囲に入っていれば、在庫にそのボードサイズが
           あるかだけ確認し、あれば即採用してPASS1-3をまるごとスキップ
           する。PASS1が「最初に見つけた候補で即採用」する設計のため、
           継ぎ足しカットが要らない標準サイズより先に、差はあるが
           カットが要る別サイズが選ばれてしまう不具合への対応
        1. 1枚物優先選定(製品サイズ基準・パレット不問)
        2. PASS1 / PASS1.5(疲労度モードは競合選定、通常モードは即決定)
        3. PASS2(ベストフィット、パレット幅超過不可)
        4. PASS3(はみ出し許容20%まで)
        5. (旧・全体の強制追加は撤去済み。丈残は補填フェーズへ引き渡す)
        6. 狭幅パレット判定
        7. 補填フェーズ(丈補填→幅補填)
        8. 代替ボードリトライループ(疲労度モードのみ、最大5回)
        9. それでも幅が足りなければカット前提選定が必要と報告
    """
    if is_protec_mode:
        protec_boards, protec_result = select_protec_lower_boards(
            available, product, palette, is_1p1216=is_protec_1p1216)
        if protec_result.valid:
            state = PassState(remaining_len=0, pass1_done=True)
            return LowerSelectionResult(
                boards=protec_boards, state=state, protec_result=protec_result)
        log.debug("SelectLowerBoards [プロテック]: 適合する在庫なし → 通常選定へ")

    # 0.5. 業界標準サイズショートカット(通常モードのみ)。
    # PASS1本体(スコア順1件即採用ロジック)には一切手を入れず、その
    # 手前で完全一致の在庫があるかだけを確認する。
    # 【バグ修正】is_protec_mode のガードが抜けていたため、プロテック
    # 選定(SelectProtecLowerBoards)が適合する在庫を見つけられずに
    # 通常選定へフォールバックしてきた場合、ここでプロテックの業務
    # ルール(タグ・ProtecCutResult)を一切通さずに標準サイズを採用
    # してしまっていた。その後 select_upper_boards は protec_result が
    # 無効なので独自に通常選定へ進み、下用=標準サイズ・上用=無関係な
    # 別ボードという「プロテックなのに上下が一致しない」不具合になる。
    # 上用側の同名ショートカットは元から is_protec_mode を見ており、
    # ここは下用だけ揃っていなかった非対称を直す
    if not is_protec_mode:
        std_size = _industry_standard_board_size(product)
        if std_size is not None:
            std_stock = _find_industry_standard_stock(available, *std_size)
            if std_stock is not None:
                eff_l = min(std_stock.width, std_stock.length)
                count, need_length_cut, length_cut_eff = decide_length_count_with_cut(
                    eff_l, palette.length)
                board = SelectedBoard(
                    width=std_stock.width, length=std_stock.length,
                    count=count, tag=TAG_MAIN)
                state = PassState(remaining_len=0, pass1_done=True)
                length_cut_info: dict[str, int] = {}
                if need_length_cut:
                    length_cut_info[f"L_{board.width}x{board.length}"] = length_cut_eff
                log.debug("SelectLowerBoards [業界標準ショートカット]: %sx%s %s枚 "
                         "(製品%sx%s、needLengthCut=%s)",
                         board.width, board.length, count,
                         product.width, product.length, need_length_cut)
                return LowerSelectionResult(
                    boards=[board], state=state, length_cut_info=length_cut_info)

    # 0.6. "5×8"専用サイズショートカット(下用、通常モードのみ)。
    # 上用と対になる構成(主2枚+丈補填1枚で丈2510を確保する)。使用する
    # 2サイズのいずれかが在庫に無い場合はショートカットを諦めて一般
    # ロジックへ落とす(中途半端な構成で確定させる事故を避けるため)。
    if not is_protec_mode and _is_5x8_range(product):
        if (_has_available_board(available, SC_5X8_L_MAIN_W, SC_5X8_L_MAIN_L)
                and _has_available_board(available, SC_5X8_L_FILL_W, SC_5X8_L_FILL_L)):
            board_main = SelectedBoard(
                width=SC_5X8_L_MAIN_W, length=SC_5X8_L_MAIN_L,
                count=SC_5X8_L_MAIN_C, tag=TAG_MAIN)
            board_fill = SelectedBoard(
                width=SC_5X8_L_FILL_W, length=SC_5X8_L_FILL_L,
                count=SC_5X8_L_FILL_C, tag=TAG_LENGTH_FILL)
            state = PassState(remaining_len=0, pass1_done=True)
            log.debug("SelectLowerBoards [5×8ショートカット]: %sx%s %s枚 + %sx%s %s枚 (製品%sx%s)",
                     SC_5X8_L_MAIN_W, SC_5X8_L_MAIN_L, SC_5X8_L_MAIN_C,
                     SC_5X8_L_FILL_W, SC_5X8_L_FILL_L, SC_5X8_L_FILL_C,
                     product.width, product.length)
            return LowerSelectionResult(boards=[board_main, board_fill], state=state)
        log.debug("SelectLowerBoards: 5×8該当だが必要サイズが在庫にない → 一般ロジックへ")

    boards: list[SelectedBoard] = []
    state = PassState(remaining_len=palette.length)

    sorted_lower = sort_boards_by_target_width(
        available, palette.width, strict=False, fatigue_map=fatigue_map,
        base_length=palette.length, stock_aware=stock_aware,
        log_label="下用（PASS1）",
    )

    # 1. 1枚物優先
    _select_lower_single_piece(sorted_lower, boards, state, palette, product, fatigue_map, fatigue_mode)

    # 2. PASS1 / PASS1.5
    # 【VBAからの修正】旧実装はここに `pass1_done` のガードが無く、1枚物優先で
    # 既に選定済み(pass1_done=True)でもPASS1/1.5が続けて実行されてしまい、
    # 意図しない2種類目のボードが追加されることがあった。PASS2/PASS3と
    # 同じ「既に決まっていたら実行しない」というガードに揃える。
    if not state.pass1_done:
        if fatigue_mode:
            _select_lower_pass1_competitive(
                sorted_lower, boards, state, palette, product, fatigue_map, fatigue_mode, available,
            )
        else:
            _select_lower_pass1_normal(sorted_lower, boards, state, palette, product)

    # 3. PASS2
    if not state.pass1_done:
        sorted_strict = sort_boards_by_target_width(
            available, palette.width, strict=True, fatigue_map=fatigue_map,
            base_length=palette.length, stock_aware=stock_aware,
            log_label="下用（PASS2）",
        )
        _select_lower_pass2(sorted_strict, boards, state, palette, product)

    # 4. PASS3
    if not state.pass1_done:
        _select_lower_pass3(sorted_lower, boards, state, palette, product)

    # 5. 【仕様更新】旧「PASS1〜3完了後の共通ブロック」を撤去。
    # 先頭ボードを機械的に+1し、2行目以降(PASS2/PASS3で選ばれた行)を
    # 無条件に破棄していたため、丈補填ループが同幅の短いボードを使う
    # 余地を消していた。丈残は補填フェーズへそのまま引き渡す。
    if state.remaining_len >= 300 and boards:
        log.debug("丈残%s → 補填フェーズへ引き渡し", state.remaining_len)

    # 6. 狭幅パレット判定
    if not state.pass1_done:
        narrow_needed = not boards
        if not narrow_needed and boards:
            if max(boards[0].width, boards[0].length) > palette.length:
                log.debug("狭幅判定: 主ボード丈超過 %sx%s > パレット丈%s",
                          boards[0].width, boards[0].length, palette.length)
                narrow_needed = True
                boards.clear()
        if narrow_needed:
            log.debug("▼狭幅パレット検出 → 小型ボードで幅方向カバー")
            state.narrow_pallet = True
            boards.extend(select_boards_for_narrow_palette(available, palette, product))
            run_narrow_palette_lower_fill(boards, palette, product, available)
            return LowerSelectionResult(boards=boards, state=state, needs_narrow=True)

    # 7. 補填フェーズ
    sel_max_w, sel_min_w, sel_total_l = _measure_selection(boards, palette, product)
    post_fill_max_w = run_lower_fill_phase(
        boards, sel_max_w, sel_min_w, sel_total_l, palette, product, available, fatigue_map, fatigue_mode,
    )

    # 8. 代替ボードリトライループ(疲労度モードのみ)
    retry_count = 0
    last_alt_eff_w = sel_min_w
    while (post_fill_max_w < product.width - 3 and fatigue_mode
           and retry_count < 5 and not state.pass15_used):
        alt_sorted = sort_boards_by_target_width(
            available, palette.width, strict=True, fatigue_map=fatigue_map,
            base_length=palette.length, stock_aware=stock_aware,
        )
        alt_found = False
        for alt in alt_sorted:
            if alt.width <= 100 or alt.length <= 100:
                continue
            _, alt_eff_w, alt_eff_l = _orient(alt.width, alt.length, palette.width)
            if alt_eff_w <= last_alt_eff_w:
                continue
            if alt_eff_w > palette.width + 3:
                continue
            alt_gap = max(0, product.width - alt_eff_w)
            if alt_gap > 100:
                continue
            if simulate_lower_width_fill(alt_eff_w, product.width, alt_sorted) < product.width - 3:
                continue

            boards.clear()
            alt_cnt = max(1, -(-palette.length // alt_eff_l)) if alt_eff_l > 0 else 1
            boards.append(SelectedBoard(width=alt.width, length=alt.length, count=alt_cnt, tag=TAG_MAIN))
            log.debug("[下用] 代替ボード採用: %sx%s effW=%s wGap=%smm fill再実行(%s回目)",
                      alt.width, alt.length, alt_eff_w, alt_gap, retry_count + 1)
            last_alt_eff_w = alt_eff_w
            retry_count += 1
            alt_found = True
            break

        if not alt_found:
            break

        sel_max_w, sel_min_w, sel_total_l = _measure_selection(boards, palette, product)
        post_fill_max_w = run_lower_fill_phase(
            boards, sel_max_w, sel_min_w, sel_total_l, palette, product, available, fatigue_map, fatigue_mode,
        )

    # 9. 幅補填後もカバー不足ならカット前提選定へ
    needs_wide_cut = post_fill_max_w < product.width - PASS1_TOLERANCE
    length_cut_info: dict[str, int] = {}
    if needs_wide_cut:
        log.debug("[下用] 幅補填後もカバー不足 -> カット前提選定へ")
        boards.clear()
        wide = select_boards_for_wide_lower(
            available, palette, product, fatigue_map=fatigue_map, fatigue_mode=fatigue_mode,
        )
        boards.extend(wide.boards)
        state.cut_info.update(wide.cut_info)
        length_cut_info.update(wide.length_cut_info)

    return LowerSelectionResult(
        boards=boards, state=state, post_fill_max_w=post_fill_max_w, needs_wide_cut=needs_wide_cut,
        length_cut_info=length_cut_info,
    )
