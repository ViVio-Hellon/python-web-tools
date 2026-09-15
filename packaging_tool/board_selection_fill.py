"""幅補填と丈補填 ── 主ボードで覆いきれなかった残りを埋める

主ボードを決めたあとに残る隙間を埋める段。**上用と下用の両方が同じ
ここを通る**ので、片方だけ直すと上下で埋め方が食い違う。

  幅補填 … 幅方向の残りを小さいボード(30/50/100mm)で埋める
  丈補填 … 丈方向の残りを埋める。残り400mm超なら主と同じサイズを
           1枚増やし、400mm以下なら100mm板で埋める(最大4枚)

丈カットの枚数決め(`decide_length_count_with_cut`)もここに置く ──
「あと何枚か」と「最後の1枚を切るか」は同じ丈の話で、離すと片方だけ
直る。
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

log = get_logger("board_selection.fill")



# ------------------------------------------------------------------
# 幅補填
# ------------------------------------------------------------------
def run_width_fill_phase(
    boards: list[SelectedBoard], main_min_w: int, product_w: int, pallet_w: int,
    target_len: int, available: list[BoardModel],
) -> int:
    """VBA `RunWidthFillPhase` の移植。

    残り幅ギャップに 100→50→30mm の順で補填ストリップを追加し、
    新しい postFillMaxW (主ボード最小幅 + 幅補填の合計短辺) を返す。
    1ストリップ=1エントリとして追加する(枚数は丈方向の必要数)。
    """
    small_sizes = (FILL_SIZE_100, FILL_SIZE_50, FILL_SIZE_30)
    pal_over = max(0, pallet_w - product_w)

    # 既存ストリップ数(丈補填は幅方向に並ばないので除外)
    exist_strips = sum(1 for b in boards if b.tag != TAG_LENGTH_FILL)
    remain_slots = MAX_WIDTH_STRIPS - exist_strips

    exist_fill = sum(min(b.width, b.length) for b in boards if b.tag == TAG_WIDTH_FILL)
    gap_w = product_w - main_min_w - exist_fill

    if gap_w <= PASS1_TOLERANCE:
        log.debug("[幅補填スキップ] gap=%smm <= PASS1_TOLERANCE(%smm)", gap_w, PASS1_TOLERANCE)
        return main_min_w + exist_fill

    for sz in small_sizes:
        if gap_w <= 0:
            break
        if remain_slots <= 0:
            log.debug("[幅補填制限] 幅方向%sストリップ上限に達しました", MAX_WIDTH_STRIPS)
            break

        # パレット許容と残スロットの両方で上限を決める
        max_pcs = min((gap_w + pal_over) // sz, remain_slots)
        if max_pcs < 1:
            continue

        # 在庫検索(短辺が一致する最初のボード)
        found = next((b for b in available if min(b.width, b.length) == sz), None)
        if found is None:
            continue
        b_w, b_l = found.width, found.length

        need_pcs = min(-(-gap_w // sz), max_pcs)  # 切り上げ後 max_pcs で頭打ち

        # 安全ブレーキ: パレット幅を超える場合は枚数を削る
        while need_pcs > 0 and main_min_w + exist_fill + sz * need_pcs > pallet_w:
            need_pcs -= 1
        if need_pcs < 1:
            continue

        long_side = max(b_w, b_l)
        len_cnt = max(1, -(-target_len // long_side)) if long_side > 0 else 1

        for _ in range(need_pcs):
            boards.append(SelectedBoard(width=b_w, length=b_l, count=len_cnt, tag=TAG_WIDTH_FILL))
        log.debug("[幅補填] %sx%s %sストリップ×%s枚 (gap=%smm)", b_w, b_l, need_pcs, len_cnt, gap_w)

        gap_w = max(0, gap_w - sz * need_pcs)
        exist_strips += need_pcs
        remain_slots -= need_pcs

    fill_sum = sum(min(b.width, b.length) for b in boards if b.tag == TAG_WIDTH_FILL)
    return main_min_w + fill_sum


def simulate_lower_width_fill(
    main_eff_w: int, product_w: int, sorted_boards: list[BoardModel],
) -> int:
    """VBA `SimulateLowerWidthFill` の移植(状態を変更しない試算)。

    主ボードの有効幅から製品幅までのギャップを、在庫の短辺で
    最大5回まで貪欲に埋めた場合の到達幅を返す。
    """
    sim_gap = product_w - main_eff_w
    if sim_gap <= 0:
        return main_eff_w

    fill_sum = 0
    for _ in range(5):
        remain = sim_gap - fill_sum
        if remain <= 3:
            break
        added = False
        for b in sorted_boards:
            short = min(b.width, b.length)
            if short < 10 or short > remain + 30:
                continue
            fill_sum += short
            added = True
            break
        if not added:
            break
    return main_eff_w + fill_sum


def _apply_fixed_length_fill_100mm(
    boards: list[SelectedBoard], length_remaining: int, available: list[BoardModel],
    *, category: str = "下用",
) -> None:
    """丈補填の100mm固定ロジック(上用・下用共通)。

    丈残が `LENGTH_FILL_THRESHOLD`(400mm)以下のときだけ、
    `LENGTH_FILL_BOARD_W`(100mm)固定サイズをCeiling(丈残/100)枚
    (最大 `LENGTH_FILL_COUNT_CAP`=4枚)追加する。400mm以下という前提が
    あるからこそ最大4枚で必ずカバーできる ── 400超のまま入ると
    4枚にクランプされて埋めきれず、直後の丈不足強制追加で主ボードも
    追加される二重取りになるため、上限を超える丈残はここでは扱わず
    そのまま丈不足強制追加ブロックへ委ねる。
    """
    if length_remaining <= 3 or length_remaining > LENGTH_FILL_THRESHOLD or not boards:
        return

    needed = -(-length_remaining // LENGTH_FILL_BOARD_W)  # 切り上げ
    if needed > LENGTH_FILL_COUNT_CAP:
        needed = LENGTH_FILL_COUNT_CAP  # 400以下の前提を超える異常値の保険

    board100 = next(
        (b for b in available
         if b.width == LENGTH_FILL_BOARD_W or b.length == LENGTH_FILL_BOARD_W),
        None,
    )
    if board100 is None:
        log.debug("丈補填[%s]: 100mm在庫なし → 丈不足強制追加ブロックへ委譲", category)
        return

    existing = next(
        (s for s in boards if s.width == board100.width and s.length == board100.length
         and s.tag == TAG_LENGTH_FILL),
        None,
    )
    if existing is not None:
        existing.count += needed
        log.debug("丈補填[%s](100mm固定・既存+%s): %sx%s 計%s枚",
                 category, needed, board100.width, board100.length, existing.count)
    else:
        boards.append(SelectedBoard(width=board100.width, length=board100.length,
                                     count=needed, tag=TAG_LENGTH_FILL))
        log.debug("丈補填[%s](100mm固定・新規): %sx%s %s枚 丈残=%s",
                 category, board100.width, board100.length, needed, length_remaining)


def _run_length_fill_loop(
    boards: list[SelectedBoard], sorted_boards: list[BoardModel], l_gap: int, sel_max_w: int,
    fatigue_map: Optional[dict[str, FatigueEntry]], fatigue_mode: bool,
) -> int:
    """VBA `RunLowerFillPhase` 内の「丈補填反復ループ」の移植(最大5回)。

    在庫全体から「主ボードと同幅(長辺がsel_max_wに完全一致)」の候補のうち、
    短辺が残り丈に最も近い(無ければ+50mmまでの超過を許容)ものを選び、
    同一サイズが既にあれば枚数+1、無ければ新規追加する。既に幅補填として
    使われているサイズ、および同一サイズが上限枚数に達しているものは対象外。

    【仕様更新】主ボードと同幅への完全一致に限定した(以前は「同幅以上」
    だったため、主ボードより幅広の候補まで拾ってしまい、選ばれた候補の
    向きによっては丈補填ボード自体の幅が主ボードと食い違う状態が生じ、
    後追いの幅補填が必要になる不整合の原因だった)。現場の例:
    製品丈3600、主2500使用→残1100、この100超の間は在庫全体から主ボードと
    同幅の候補(500×1250など)を探して埋め、400以下に落ちたら
    `_apply_fixed_length_fill_100mm` に引き継ぐ。

    戻り値は埋めきれずに残った丈残(呼び出し側が100mm固定・丈不足強制追加へ
    引き継ぐ)。
    """
    cur_gap = l_gap
    for loop_idx in range(5):
        if cur_gap <= 3:
            return cur_gap
        log.debug("丈補填ループ%s: 残gap=%smm", loop_idx + 1, cur_gap)

        best: Optional[tuple[int, int, int, int]] = None  # short, long, bw, bl
        best_diff = 999999
        best_score = float("inf")

        for b in sorted_boards:
            f_short, f_long = min(b.width, b.length), max(b.width, b.length)
            if f_short < 10:
                continue
            # 【仕様更新】主ボードと同幅(sel_max_w)への完全一致に限定
            if f_long != sel_max_w:
                continue
            # 幅補填として既に使われているサイズは除外
            if any(s.width == b.width and s.length == b.length and s.tag == TAG_WIDTH_FILL for s in boards):
                continue
            # 同一サイズが上限枚数に達していれば除外
            # 【VBAからの修正】タグを見ずに幅・丈だけでマッチしていたため、同サイズの
            # 「主」や「幅補填」ボードが存在すると、そちらの上限判定を誤って見てしまって
            # いた(skipCapped)。丈補填タグの行だけを対象にする。
            existing = next(
                (s for s in boards if s.width == b.width and s.length == b.length
                 and s.tag == TAG_LENGTH_FILL),
                None,
            )
            if existing is not None and existing.count >= LENGTH_FILL_COUNT_CAP:
                continue

            fat_score = 0.0
            if fatigue_mode and fatigue_map is not None:
                entry = fatigue_map.get(f"{b.width}x{b.length}")
                if entry is not None:
                    fat_score = entry.dist + entry.area

            if f_short <= cur_gap:
                if best is not None and best[0] > cur_gap:
                    continue
                this_diff = cur_gap - f_short
                full_score = this_diff * 10 + fat_score
                if this_diff < best_diff or (this_diff <= best_diff + 5 and full_score < best_score):
                    best_diff, best_score = this_diff, full_score
                    best = (f_short, f_long, b.width, b.length)
            elif f_short <= cur_gap + 50:
                if best is None or best[0] > cur_gap:
                    this_diff = f_short - cur_gap
                    full_score = this_diff * 10 + fat_score
                    if best is None or full_score < best_score:
                        best_diff, best_score = this_diff, full_score
                        best = (f_short, f_long, b.width, b.length)

        if best is None:
            return cur_gap

        b_short, _, bw, bl = best
        # 【VBAからの修正】同サイズの「主」「幅補填」行を誤ってマージ対象にしないよう、
        # 丈補填タグの行に限定する。上限(4枚)到達時は既存行を+1→4クランプするだけで
        # 済ませず、新規行の作成に回す(下用側と上用側で挙動を統一)
        existing = next(
            (s for s in boards if s.width == bw and s.length == bl
             and s.tag == TAG_LENGTH_FILL),
            None,
        )
        if existing is not None and existing.count < LENGTH_FILL_COUNT_CAP:
            existing.count += 1
            added_eff_l = min(bw, bl)
            log.debug("丈補填(既存+1): %sx%s 計%s枚", bw, bl, existing.count)
        else:
            boards.append(SelectedBoard(width=bw, length=bl, count=1, tag=TAG_LENGTH_FILL))
            added_eff_l = b_short
            log.debug("丈補填(新規): %sx%s 1枚", bw, bl)

        cur_gap -= added_eff_l
        if cur_gap <= 3:
            return cur_gap

    return cur_gap


def apply_length_fill_width_correction(
    boards: list[SelectedBoard], sel_max_w: int, palette: Palette, product: ProductSize,
    available: list[BoardModel],
) -> None:
    """VBA `ApplyLengthFillWidthCorrection` の移植。

    補填ボードの幅が製品幅に届かない場合に30/50/100mmの小型ボードで補う。
    元ソースに「複数回の修正・破損が繰り返されたため独立関数化した。
    選定ロジックを変更する際にここを触らないこと」という警告があるため、
    条件の順序も含めて逐語で移植している。

    対象外(スキップ)となる行:
        - 30/50/100mm のいずれかの辺を持つ行(補填ボード自身)
        - タグが "主" または "丈補填" の行
          (丈補填はパレット幅で配置されるため原理的に幅ギャップが出ない)
        - 幅ギャップが3mm以下、または既に主ボード最大幅以上の行
    """
    small_sizes = (50, 30, 100)  # VBA の smallSizes 配列順(結果には影響しない)

    idx = len(boards) - 1
    while idx >= 1:
        b = boards[idx]
        if b.width in small_sizes or b.length in small_sizes:
            idx -= 1
            continue
        if b.tag in (TAG_MAIN, TAG_LENGTH_FILL):
            idx -= 1
            continue

        _, calc_eff_w, calc_eff_l = _orient(b.width, b.length, palette.width)
        width_gap = product.width - calc_eff_w
        if width_gap <= 3 or calc_eff_w >= sel_max_w:
            idx -= 1
            continue

        log.debug("丈補填幅不足: %sx%s effW=%s widthGap=%s", b.width, b.length, calc_eff_w, width_gap)

        # ギャップ以上で最小の在庫サイズを選ぶ
        best_size = 0
        best_board: Optional[BoardModel] = None
        for size in small_sizes:
            if size < width_gap:
                continue
            if best_size > 0 and size >= best_size:
                continue
            found = next((a for a in available if min(a.width, a.length) == size), None)
            if found is not None:
                best_size = size
                best_board = found

        if best_board is None:
            log.debug("小ボードなし → 主ボード+1枚追加")
            boards[0].count += 1
            del boards[idx]
            idx -= 1
            continue

        sm_long = max(best_board.width, best_board.length)
        small_needed = max(1, -(-(calc_eff_l * b.count) // sm_long)) if sm_long > 0 else 1

        if small_needed >= 4:
            log.debug("小ボード%s枚超 → 主ボード+1枚追加", small_needed)
            boards[0].count += 1
            del boards[idx]
        else:
            existing = next(
                (s for s in boards if s.width == best_board.width and s.length == best_board.length), None,
            )
            if existing is not None:
                existing.count += small_needed
                log.debug("小ボード補填(既存+): %sx%s +%s枚", best_board.width, best_board.length, small_needed)
            else:
                boards.append(SelectedBoard(
                    width=best_board.width, length=best_board.length,
                    count=small_needed, tag=TAG_WIDTH_FILL,
                ))
                log.debug("小ボード補填(新規): %sx%s %s枚", best_board.width, best_board.length, small_needed)
        idx -= 1


def _drop_length_fill_when_covered(boards: list[SelectedBoard], palette: Palette, product: ProductSize) -> None:
    """VBA「主ボードで丈カバー済みなら丈補填を削除」の移植。

    先頭(主)ボードだけで製品丈をカバーできている場合、index1以降の
    行を削除する。ただし幅補填は幅方向の役割があるため残す。
    """
    if len(boards) <= 1:
        return
    main = boards[0]
    _, _, main_eff_l = _orient(main.width, main.length, palette.width)
    if main_eff_l * main.count < product.length:
        return
    for i in range(len(boards) - 1, 0, -1):
        if boards[i].tag == TAG_WIDTH_FILL:
            log.debug("補填不要削除スキップ(幅補填): %sx%s", boards[i].width, boards[i].length)
            continue
        log.debug("補填不要につき削除: %sx%s", boards[i].width, boards[i].length)
        del boards[i]


def _force_add_for_length_shortage(
    boards: list[SelectedBoard], palette: Palette, product: ProductSize, available: list[BoardModel],
    fatigue_map: Optional[dict[str, FatigueEntry]], sel_max_w: int,
) -> None:
    """VBA「丈不足強制追加」の移植。

    全行の有効丈合計が製品丈に3mm超足りない場合、
    ギャップ以上かつギャップ3倍以内の短辺を持つボードを1件だけ追加
    (同一サイズがあれば枚数+1、上限4枚)。
    見つからなければ主ボードを必要枚数だけ増やす。
    """
    if not boards:
        return
    # 【仕様更新】タグ別に丈計上の仕方を分ける(上用の`_upper_total_length`に
    # 合わせる)。幅補填は丈方向に寄与しないため除外、丈補填は短辺×枚数
    # (パレット幅で寝かせて使うため)、それ以外(主)は実効丈×枚数で合算する。
    # 以前は全行を実効丈で計上していたため、丈補填タグの100mm板が長辺
    # (例: 100x2000の2000)で計上されfinalTotalLが過大になり、実際には
    # 丈が不足しているケースで必要な追加が行われないまま完了することがあった
    final_total_l = 0
    for b in boards:
        if b.tag == TAG_WIDTH_FILL:
            continue
        if b.tag == TAG_LENGTH_FILL:
            final_total_l += min(b.width, b.length) * b.count
        else:
            final_total_l += _orient(b.width, b.length, palette.width)[2] * b.count
    final_gap = product.length - final_total_l
    log.debug("下用丈再確認: finalTotalL=%s finalLGap=%s", final_total_l, final_gap)
    if final_gap <= 3:
        return

    lf_sorted = sort_boards_by_target_width(
        available, palette.width, strict=True, fatigue_map=fatigue_map, base_length=palette.length,
    )
    for cand in lf_sorted:
        lf_short, lf_long = min(cand.width, cand.length), max(cand.width, cand.length)
        if lf_short < final_gap or lf_short > final_gap * 3 or lf_long < 10:
            continue
        # 【仕様更新】主ボードと同幅(完全一致)のみ候補とする。理由は丈補填
        # 反復ループ(`_run_length_fill_loop`)と同一。これが欠けていたため、
        # 丈不足強制追加ブロックだけは主ボードと異なる幅の板(実例:
        # 295x1080)を選んでしまう不具合が、丈補填反復ループを直した後も
        # 残っていた。
        if lf_long != sel_max_w:
            continue
        # 【仕様更新】30/50/100mmは幅補填専用サイズのため、丈不足強制追加の
        # 候補にもしない。理由は上用の同幅探索ループの除外と同一
        # (補填専用サイズが主ボード代替として選ばれ続ける穴)。
        if lf_short in (FILL_SIZE_30, FILL_SIZE_50, FILL_SIZE_100):
            continue
        # 【VBAからの修正】タグを見ずに幅・丈だけでマッチしていたため、同サイズの
        # 「主」や「幅補填」ボードが存在すると、そちらの上限/カウントを誤って見て
        # しまっていた(lfSkipCap/lfExists)。丈補填タグの行だけを対象にする。
        existing = next(
            (s for s in boards if s.width == cand.width and s.length == cand.length
             and s.tag == TAG_LENGTH_FILL),
            None,
        )
        if existing is not None:
            if existing.count >= LENGTH_FILL_COUNT_CAP:
                continue
            existing.count = min(existing.count + 1, LENGTH_FILL_COUNT_CAP)
            log.debug("下用丈補填(既存+1): %sx%s", cand.width, cand.length)
        else:
            boards.append(SelectedBoard(width=cand.width, length=cand.length, count=1, tag=TAG_LENGTH_FILL))
            log.debug("下用丈補填(新規): %sx%s", cand.width, cand.length)
        return

    # 候補なし → 主ボードを必要枚数増やす
    main = boards[0]
    if abs(main.width - palette.width) <= abs(main.length - palette.width):
        m0_eff_l = main.length
    else:
        m0_eff_l = main.width
    if m0_eff_l <= 0:
        return
    add_more = -(-final_gap // m0_eff_l)
    main.count += add_more
    log.debug("下用丈不足強制追加: %sx%s +%s枚 計%s枚", main.width, main.length, add_more, main.count)


def run_lower_fill_phase(
    boards: list[SelectedBoard], sel_max_w: int, sel_min_w: int, sel_total_l: int,
    palette: Palette, product: ProductSize, available: list[BoardModel],
    fatigue_map: Optional[dict[str, FatigueEntry]] = None, fatigue_mode: bool = False,
) -> int:
    """VBA `RunLowerFillPhase` の移植(下用の補填フェーズ)。

    VBAと同じ順序で処理する:
        1. 幅/丈ギャップ算出
        2. 丈補填反復ループ(最大5回)
        3. ApplyLengthFillWidthCorrection(補填ボードの幅不足を小型で補正)
        4. 主ボードで丈カバー済みなら丈補填を削除
        5. 丈不足強制追加
        6. postMinMainW(補填以外の最小有効幅)と
           fillTargetLen(その幅を担う行の丈合計)を算出
        7. RunWidthFillPhase で幅補填を追加し postFillMaxW を返す

        8. 幅補填行の丈カバレッジ再確認(`_recheck_width_fill_length`)
    """
    w_gap = max(0, product.width - sel_min_w)
    l_gap = max(0, product.length - sel_total_l)
    log.debug("下用補填開始: wGap=%s lGap=%s selMaxW=%s selTotalL=%s", w_gap, l_gap, sel_max_w, sel_total_l)

    sorted_fill = sort_boards_by_target_width(
        available, palette.width, strict=False, fatigue_map=fatigue_map, base_length=palette.length,
    )

    if l_gap > 3:
        remaining_gap = _run_length_fill_loop(
            boards, sorted_fill, l_gap, sel_max_w, fatigue_map, fatigue_mode)
        _apply_fixed_length_fill_100mm(boards, remaining_gap, available, category="下用")

    apply_length_fill_width_correction(boards, sel_max_w, palette, product, available)
    _drop_length_fill_when_covered(boards, palette, product)
    _force_add_for_length_shortage(boards, palette, product, available, fatigue_map, sel_max_w)

    # 補填以外の行のうち最小の有効幅と、その幅を担う行の丈合計
    post_min_main_w = 999999
    for b in boards:
        if b.tag == TAG_WIDTH_FILL or b.tag == TAG_LENGTH_FILL:
            continue
        _, eff_w, _ = _orient(b.width, b.length, palette.width)
        post_min_main_w = min(post_min_main_w, eff_w)
    if post_min_main_w == 999999:
        post_min_main_w = 0

    fill_target_len = 0
    for b in boards:
        if b.tag in (TAG_WIDTH_FILL, TAG_LENGTH_FILL):
            continue
        _, eff_w, eff_l = _orient(b.width, b.length, palette.width)
        if eff_w == post_min_main_w:
            fill_target_len += eff_l * b.count
    if fill_target_len == 0:
        fill_target_len = product.length

    w_fill_start_idx = len(boards)
    post_fill_max_w = run_width_fill_phase(
        boards, post_min_main_w, product.width, palette.width, fill_target_len, available,
    )

    # 8. 幅補填行の丈カバレッジ再確認
    if post_fill_max_w > post_min_main_w and len(boards) >= 2 and sorted_fill:
        _recheck_width_fill_length(
            boards, w_fill_start_idx, sel_max_w, palette, product, sorted_fill)
    return post_fill_max_w


def _recheck_width_fill_length(
    boards: list[SelectedBoard], start_idx: int, sel_max_w: int,
    palette: Palette, product: ProductSize, sorted_fill: list[BoardModel],
) -> None:
    """VBA `RunLowerFillPhase` 最終段「幅補填行の丈カバレッジ再確認」の移植。

    幅補填として足したストリップは、主ボード列と同じ長さまで伸びていないと
    途中で途切れてしまう。主ボードのX方向カバー長(製品丈が上限)に対して
    足りない分を、まず同じボードの追加で、次に別の小型ボードで埋める。

    `start_idx` は幅補填が追加され始めたインデックス。
    主ボードより幅が広い行(`wfShort >= 主ボード有効幅`)は対象外。

    【VBAからの修正】"丈補填" タグの行も対象外にする。丈補填ボードは
    「丈方向の穴埋め専用」で、幅方向ストリップとして丈カバー量を判定する
    対象ではない。このタグ除外が無いと、丈補填ボード(例: 30×2500)を
    誤ってこのチェックにかけてしまい、無関係な幅補填ストリップの枚数を
    芋づる式に水増しする(過剰選定の直接原因になる)。
    """
    if not boards:
        return
    main = boards[0]
    if abs(main.width - palette.width) <= abs(main.length - palette.width):
        main_eff_w, main_eff_l = main.width, main.length
    else:
        main_eff_w, main_eff_l = main.length, main.width
    main_cover_x = main_eff_l * main.count

    for idx in range(start_idx, len(boards)):
        row = boards[idx]
        if row.tag == TAG_LENGTH_FILL:
            continue
        short, long_side = min(row.width, row.length), max(row.width, row.length)
        if short >= main_eff_w:
            continue

        target_x = min(main_cover_x, product.length)
        gap_x = target_x - long_side * row.count
        log.debug("幅補填丈チェック[%s]: %sx%s %s枚 coverX=%s targetX=%s gapX=%s",
                  idx, row.width, row.length, row.count,
                  long_side * row.count, target_x, gap_x)
        if gap_x <= COVERED_TOLERANCE:
            continue

        # まず同じボードを足す(上限4枚)
        add = min(gap_x // long_side, LENGTH_FILL_COUNT_CAP)
        if add > 0:
            row.count += add
            gap_x -= long_side * add

        # 残りは別の小型ボードで埋める(最大5回)
        for _ in range(FILL_RETRY_LIMIT):
            if gap_x <= COVERED_TOLERANCE:
                break
            best = _pick_gap_filler(boards, sorted_fill, gap_x, sel_max_w)
            if best is None:
                break
            best_short = min(best.width, best.length)
            # 【VBAからの修正】タグを問わず(width,length)一致だけでマージすると、
            # 「主」や「幅補填」ボードにこの残gap埋め分が誤って合算されていた
            # (wfAlready)。丈補填タグの行だけを対象にし、上限到達時は新規行に回す。
            existing = next(
                (s for s in boards if s.width == best.width and s.length == best.length
                 and s.tag == TAG_LENGTH_FILL),
                None,
            )
            if existing is not None and existing.count < LENGTH_FILL_COUNT_CAP:
                existing.count += 1
                log.debug("幅補填残gap(既存+1): %sx%s 計%s枚", best.width, best.length, existing.count)
            else:
                boards.append(SelectedBoard(width=best.width, length=best.length, count=1, tag=TAG_LENGTH_FILL))
                log.debug("幅補填残gap(新規): %sx%s", best.width, best.length)
            if best_short >= gap_x:
                break
            gap_x -= best_short




# 丈方向のカット判定の閾値(VBA `CUT_OVERL_THRESHOLD` / lenRemainPL)。
# フルサイズの枚数で製品丈をカバーした後、あと何mm残っているか
# (lenRemainPL)を見て、これを超える残りがあればカットしてもう1枚足す。


def decide_length_count_with_cut(
    board_length: int, target_length: int,
) -> tuple[int, bool, int]:
    """丈方向に何枚必要か、最後の1枚をカットする必要があるかを判定する。

    VBA の `lenRemainPL` 判定と同じ考え方(判定しているのは「超過量」
    ではなく「最後の1枚に必要な残りの長さ」):

        lenRemainPL = 対象丈 - (1枚の丈 × フルサイズで収まる枚数)
        lenRemainPL が 100mm を超える → カットして1枚追加する
        lenRemainPL が 100mm 以下     → 切り捨てて無視する

    例(1枚1000mm、対象丈2910mm): フルサイズ2枚で2000mm、残り910mm。
    910mm > 100mm なのでカットして3枚目を910mmで追加する(合計3枚)。

    例(1枚1000mm、対象丈2050mm): フルサイズ2枚で2000mm、残り50mm。
    50mm ≤ 100mm なので追加しない(2枚のみ、50mm分は切り捨てる)。

    戻り値は `(枚数, 丈カットが必要か, 丈カット後の最後の1枚の丈)`。
    丈カットが不要なら3番目の値は `board_length` と同じ。

    **MAP画面のカット線描画(`recalc_length_cut_info`)と切断依頼書
    (`reports.protec_cut_size_info`)の両方が、この関数が返した
    `count`/`need_length_cut` を経由した `ProtecCutResult` を見る**。
    枚数の決め方をここ以外で計算し直すと、画面にはカット線が出るのに
    依頼書には丈カットの行が出ない(またはその逆)という食い違いが
    起きる(現場の声で報告された不具合の一因)。
    """
    if board_length <= 0:
        return 1, False, 0

    full_count = target_length // board_length
    remain = target_length - full_count * board_length

    if full_count == 0:
        # 在庫1枚だけで対象丈を超える(またはちょうど覆う)。1枚をそのまま
        # 使うか、対象丈に合わせてカットするかだけの話で、フルサイズの
        # 上に「もう1枚」を足す状況ではない
        need_cut = board_length > target_length
        cut_eff = target_length if need_cut else board_length
        return 1, need_cut, cut_eff

    if remain > LENGTH_CUT_REMAIN_THRESHOLD:
        # あと1枚、remain の長さにカットして足す
        return full_count + 1, True, remain

    # 端数は切り捨てる
    return full_count, False, board_length
