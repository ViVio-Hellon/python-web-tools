"""ボード自動選定 ── 外向けの1枚窓

**中身は段ごとのモジュールが持つ。** 呼ぶ側はここだけを見ればよい。

    board_selection_types    型(選定が何を返すか)
    board_selection_common   決まりの数値と、向き・寸法合わせ
    board_selection_protec   プロテック(上下同じボード・製品幅超過禁止)
    board_selection_narrow   狭幅パレットとカット前提(最後の手段)
    board_selection_fill     幅補填と丈補填(上用・下用で共通)
    board_selection_lower    下用(パレットに敷く側)
    board_selection_upper    上用(製品に載せる側)
    ここ                     ハブ。上下を呼んで、丈カットを再計算する

上から下へ一方向に依存する。**下線で始まる名前(段の内側)はここから
公開しない** ── 使うときは持ち主のモジュールから引く。

VBAソースを1行ずつ確認して移植した。対応関係:
    AutoSelectBoards                 -> auto_select_boards
    RecalcLengthCutInfo              -> recalc_length_cut_info
    SelectLowerBoards                -> board_selection_lower
    SelectUpperBoards                -> board_selection_upper
    RunWidthFillPhase / RunLowerFillPhase
                                     -> board_selection_fill
    SelectBoardsForNarrowPalette / SelectBoardsForWideLower
                                     -> board_selection_narrow
    DecideProtecOrientation / ComputeProtecLengthCut
                                     -> board_selection_protec

【VBA版の「ListBox」の扱い】
    VBA版は選定結果を `lstSelectedBoardsLower` というListBoxコントロールに
    直接読み書きし、4列(幅/丈/枚数/タグ)を状態として使っていた。
    Python版はUI非依存の `list[SelectedBoard]` に置き換える。
    タグ文字列("主"/"幅補填"/"丈補填")は後続の配置処理が参照するため
    そのまま踏襲する。

【移植で意図的に踏襲したVBAの既知の不具合】
    - `mPass15Used` フラグはVBA上で宣言・False代入・参照はされているが、
      **一度もTrueに設定されない**。結果として代替ボードリトライループの
      ガード条件 `And Not mPass15Used` は常に成立する。現行ツールと同じ
      結果になることを優先してそのまま再現している
      (`PassState.pass15_used` は常にFalse)。仕様として直す場合は
      `board_selection_lower._select_lower_pass1_competitive` 内の
      `use_pass15` 採用時に True を立てること。
    - PASS3で選定したボードにはタグが設定されない(VBAはListBoxの
      4列目を未設定のまま残すため空文字列になる)。後続処理は
      `GetEffectiveTag` で空タグを推測する作りなので、Python版でも
      空文字列のまま踏襲する。
"""
from __future__ import annotations

from typing import Optional

from .board_scoring import FatigueEntry
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .logging_utils import get_logger
from .models import BoardModel

# ------------------------------------------------------------------
# **ここは外向けの1枚窓。** 選定の中身は段ごとのモジュールが持ち、
# 呼ぶ側はこのモジュールだけを見ればよいようにする。
#
#     型 → 共通(寸法合わせ) → プロテック / 狭幅 → 補填 → 下用 / 上用 → ここ
#
# 下線で始まる名前(段の内側)はここには出さない。試験や他の段から
# 使うときは持ち主のモジュールから引く。
# ------------------------------------------------------------------
from .board_selection_types import (  # noqa: F401
    AutoSelectResult, LowerSelectionResult, PassState, ProtecCutResult,
    ProtecLengthCut, UpperSelectionResult, WideCutResult)
from .board_selection_common import (  # noqa: F401
    COVERED_TOLERANCE, FILL_OVER_MARGIN, FILL_RETRY_LIMIT, FILL_SIZE_30,
    FILL_SIZE_50, FILL_SIZE_100, GAP_FILLER_OVER_MARGIN,
    LENGTH_CUT_REMAIN_THRESHOLD, LENGTH_FILL_BOARD_W, LENGTH_FILL_COUNT_CAP,
    LENGTH_FILL_THRESHOLD, MAX_MAIN_BOARD_KINDS, NARROW_MIN_SHORT_SIDE,
    TAG_CUT_PREMISE, TAG_LENGTH_FILL, TAG_MAIN, TAG_WIDTH_FILL,
    _orient, adjust_orientation_for_coverage, can_cover_with_fill)
from .board_selection_protec import (  # noqa: F401
    PROTEC_1P1216_TOLERANCE, PROTEC_OTHER_TOLERANCE,
    apply_protec_rules_to_lower_list, compute_protec_length_cut,
    decide_protec_orientation, select_protec_lower_boards)
from .board_selection_narrow import (  # noqa: F401
    CUT_FAT_MULT, NARROW_MAX_LOOPS, WIDE_CUT_MIN_SHORT_SIDE,
    run_narrow_palette_lower_fill, select_boards_for_narrow_palette,
    select_boards_for_narrow_palette_upper, select_boards_for_wide_lower,
    select_boards_for_wide_product, select_upper_boards_wide_cut)
from .board_selection_fill import (  # noqa: F401
    apply_length_fill_width_correction, decide_length_count_with_cut,
    run_lower_fill_phase, run_width_fill_phase, simulate_lower_width_fill)
from .board_selection_lower import select_lower_boards  # noqa: F401
from .board_selection_upper import select_upper_boards  # noqa: F401

log = get_logger("board_selection_algorithm")







# ==================================================================
# 丈カット再計算 (VBA `RecalcLengthCutInfo` の移植)
# ==================================================================
def recalc_length_cut_info(
    lower: list[SelectedBoard], upper: list[SelectedBoard],
    palette: Palette, product: ProductSize,
    *, lower_protec_result: Optional[ProtecCutResult] = None,
    upper_protec_result: Optional[ProtecCutResult] = None,
) -> tuple[dict[str, int], dict[str, int]]:
    """VBA `RecalcLengthCutInfo` の移植。

    元ソースが「カット枚数の業務ルールを決める唯一の箇所(SSOT)」と
    明記している処理。丈カットは丈オーバー時に最後の1枚だけ発生する
    (常に枚数1)という業務ルールをここで確定させる。

    `lower_protec_result`/`upper_protec_result` が有効なとき
    (プロテック確定値がある)は、`GetBestOrientation` による再判定を
    行わず、選定(`select_protec_lower_boards`/`select_upper_boards`)が
    `decide_length_count_with_cut` で既に決めた `need_length_cut`を
    そのまま使う。下用(パレット丈基準)と上用(製品丈基準)は判定対象の
    丈が異なるため、それぞれ別々の `ProtecCutResult` を渡す。

    **ここで独自に「lenRemainPL > 100mm」を再計算しない。** 計算し直すと
    選定と食い違う結果になりうる(MAP画面にはカット線が出るのに切断
    依頼書には丈カットの行が出ない、という不一致の原因になっていた)。

    戻り値は (length_cut_info, length_cut_count)。
    キーは下用が "L_幅x丈"、上用が "U_幅x丈"、値は切断線の長さ(有効幅)。
    """
    length_cut_info: dict[str, int] = {}
    length_cut_count: dict[str, int] = {}

    has_protec = ((lower_protec_result is not None and lower_protec_result.valid)
                  or (upper_protec_result is not None and upper_protec_result.valid))
    if has_protec:
        # **下用の確定値だけを使う。上用は判定しない。**
        # プロテックは上下共用・同サイズが確定ルールなので、上用を製品丈
        # 基準で独自に判定すると、下用(パレット丈基準の確定値)と食い違う。
        # 画面でも上用は下用と同じ図の重ね描きになり、上用のラベルだけが
        # 下用の図に残る(現場の声:「上用ラベルが重なる」)
        b = lower[0] if lower else None
        pr = lower_protec_result
        if b is not None and pr is not None and pr.valid and pr.need_length_cut:
            key = f"L_{b.width}x{b.length}"
            length_cut_info[key] = pr.cut_eff_width
            length_cut_count[key] = max(1, pr.len_cut_cnt)
            log.debug("丈カット記録[下用/プロテック確定値]: %s 通常%s枚+丈カット%s枚(→%smm)",
                      key, pr.len_normal_cnt, pr.len_cut_cnt, pr.length_cut_eff)
        else:
            log.debug("RecalcLengthCutInfo[プロテック]: 丈カットなし")
        return length_cut_info, length_cut_count

    for b in lower:
        # 幅補填・丈補填・カット前提は対象外。カット前提の丈カットは
        # 選定側(`select_boards_for_wide_lower`)が別途記録し、
        # `LowerSelectionResult.length_cut_info` として呼び出し元
        # (`auto_select_boards`)に渡る ── ここで再判定すると二重に
        # 扱ってしまう。以前は選定側がその記録を一切していなかったため、
        # ここで除外された「カット前提」の丈カット情報が結果として
        # どこにも残らず、切断依頼書に反映されない不具合になっていた
        # (現場の声:「3枚出るはずが分割されずに出力される」)
        if b.tag in (TAG_WIDTH_FILL, TAG_LENGTH_FILL, TAG_CUT_PREMISE):
            continue
        rot, eff_w, eff_l = _orient(b.width, b.length, palette.width)
        rot, eff_w, eff_l = adjust_orientation_for_coverage(
            b.width, b.length, palette.width, product.length, rot, eff_w, eff_l, "下用",
        )
        if eff_l <= 0:
            continue
        if eff_l * b.count > palette.length:
            key = f"L_{b.width}x{b.length}"
            if key not in length_cut_info:
                length_cut_info[key] = eff_w
                length_cut_count[key] = 1  # 丈カットは最後の1枚のみ(業務ルール)
                log.debug("丈カット記録[下用]: %s effLx%s=%s > パレット丈%s",
                          key, b.count, eff_l * b.count, palette.length)

    for b in upper:
        # 幅補填・丈補填は丈カット対象外(下用と同じ)。丈補填を外していなかった
        # ので、狭幅上用の丈補填(30×2500 を帯の幅に切ったもの)が普通の板として
        # 丈カットに記録され、ボードMAPの疲労度に乗っていた(VBA の仕様更新)。
        # カット前提の丈カットは選定側(`select_upper_boards_wide_cut`)が
        # 別途記録し、`UpperSelectionResult.length_cut_info` として渡る
        # (下用と対称。両方とも呼び出し元でマージされる)
        if b.tag in (TAG_WIDTH_FILL, TAG_LENGTH_FILL, TAG_CUT_PREMISE):
            continue
        rot, eff_w, eff_l = _orient(b.width, b.length, product.width, category="上用")
        rot, eff_w, eff_l = adjust_orientation_for_coverage(
            b.width, b.length, product.width, product.length, rot, eff_w, eff_l, "上用",
        )
        if eff_l <= 0:
            continue
        if eff_l * b.count > product.length:
            key = f"U_{b.width}x{b.length}"
            if key not in length_cut_info:
                length_cut_info[key] = eff_w
                length_cut_count[key] = 1
                log.debug("丈カット記録[上用]: %s effLx%s=%s > 製品丈%s",
                          key, b.count, eff_l * b.count, product.length)

    return length_cut_info, length_cut_count


# ==================================================================
# ハブ (VBA `AutoSelectBoards` の移植)
# ==================================================================


def auto_select_boards(
    available: list[BoardModel], palette: Palette, product: ProductSize,
    *,
    fatigue_map_lower: Optional[dict[str, FatigueEntry]] = None,
    fatigue_map_upper: Optional[dict[str, FatigueEntry]] = None,
    fatigue_mode: bool = False,
    stock_aware: bool = False,
    is_protec_mode: bool = False,
    is_protec_1p1216: bool = False,
    last_hosozai: str = "",
) -> AutoSelectResult:
    """VBA `AutoSelectBoards` の移植(下用選定 → 上用選定 → 丈カット再計算)。"""
    lower_result = select_lower_boards(
        available, palette, product,
        fatigue_map=fatigue_map_lower, fatigue_mode=fatigue_mode, stock_aware=stock_aware,
        is_protec_mode=is_protec_mode, is_protec_1p1216=is_protec_1p1216,
    )
    upper_result = select_upper_boards(
        lower_result.boards, available, palette, product,
        fatigue_map=fatigue_map_upper, fatigue_mode=fatigue_mode, stock_aware=stock_aware,
        is_protec_mode=is_protec_mode, is_protec_1p1216=is_protec_1p1216, last_hosozai=last_hosozai,
        protec_result=lower_result.protec_result,
    )
    length_cut_info, length_cut_count = recalc_length_cut_info(
        lower_result.boards, upper_result.boards, palette, product,
        lower_protec_result=lower_result.protec_result,
        upper_protec_result=upper_result.protec_result,
    )
    # カット前提(幅広)の下用・上用は `recalc_length_cut_info` の対象外
    # なので、選定時に記録した丈カットをここで合流させる
    # (以前は下用側のこのマージが漏れており、丈カット情報がどこにも
    # 残らなかった)
    for key, value in lower_result.length_cut_info.items():
        if key not in length_cut_info:
            length_cut_info[key] = value
            length_cut_count[key] = 1
    for key, value in upper_result.length_cut_info.items():
        if key not in length_cut_info:
            length_cut_info[key] = value
            length_cut_count[key] = 1

    cut_info = dict(lower_result.state.cut_info)
    cut_info.update(upper_result.cut_info)

    log.debug("選定完了 下用:%s種 上用:%s種", len(lower_result.boards), len(upper_result.boards))
    return AutoSelectResult(
        lower=lower_result.boards, upper=upper_result.boards,
        lower_result=lower_result, upper_result=upper_result,
        cut_info=cut_info, length_cut_info=length_cut_info, length_cut_count=length_cut_count,
    )
