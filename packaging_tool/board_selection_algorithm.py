"""ボード自動選定アルゴリズム (VBA `MaterialMasterForm` の下用選定の移植)

VBAソースを1行ずつ確認して移植した。対応関係:
    AutoSelectBoards                    -> auto_select_boards
    SelectLowerBoardsSinglePiece         -> _select_lower_single_piece
    SelectLowerBoardsPass1Normal          -> _select_lower_pass1_normal
    SelectLowerBoardsPass1Competitive      -> _select_lower_pass1_competitive
    SelectLowerBoards                       -> select_lower_boards
    CanCoverWithFill                         -> can_cover_with_fill
    AdjustOrientationForCoverage              -> adjust_orientation_for_coverage
    RunWidthFillPhase                          -> run_width_fill_phase
    SimulateLowerWidthFill                      -> simulate_lower_width_fill
    RunLowerFillPhase                            -> run_lower_fill_phase
    ApplyLengthFillWidthCorrection                -> apply_length_fill_width_correction
    SelectUpperBoards                              -> select_upper_boards

【VBA版の「ListBox」の扱い】
    VBA版は選定結果を `lstSelectedBoardsLower` というListBoxコントロールに
    直接読み書きし、4列(幅/丈/枚数/タグ)を状態として使っていた。
    Python版はUI非依存の `list[SelectedBoard]` に置き換える。
    タグ文字列("主"/"幅補填"/"丈補填")は後続の配置処理が参照するため
    そのまま踏襲する。

【移植で意図的に踏襲したVBAの既知の不具合】
    - `mPass15Used` フラグはVBA上で宣言・False代入・参照はされているが、
      **一度もTrueに設定されない**。コード上のコメント
      ("PASS2経由はPASS1.5ではない")からはPASS1.5採用時にTrueになる
      想定だったと読めるが、実装が抜けている。結果として代替ボード
      リトライループのガード条件 `And Not mPass15Used` は常に成立する。
      本移植では現行ツールと同じ結果になることを優先してこの挙動を
      そのまま再現している(`PassState.pass15_used` は常にFalse)。
      仕様として直す場合は `_select_lower_pass1_competitive` 内の
      `use_pass15` 採用時に True を立てること。
    - PASS3で選定したボードにはタグが設定されない(VBAはListBoxの
      4列目を未設定のまま残すため空文字列になる)。PASS1/PASS2は"主"を
      設定するため、PASS3経由だけタグが空になる。後続処理は
      `GetEffectiveTag` で空タグを推測する作りになっているため、
      Python版でも空文字列のまま踏襲する。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from .board_scoring import (
    FatigueEntry,
    LOWER_OVERHANG_Y,
    MAX_WIDTH_STRIPS,
    PASS1_TOLERANCE,
    UPPER_WIDTH_TOLERANCE,
    WIDTH_OVERSHOOT_LIMIT,
    get_best_orientation,
    sort_boards_by_target_width,
    total_fatigue,
)
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .logging_utils import get_logger
from .models import BoardModel

log = get_logger("board_selection_algorithm")

FILL_SIZE_100 = 100
FILL_SIZE_50 = 50
FILL_SIZE_30 = 30

# CanCoverWithFill 内の「超過許容幅」(VBA `FILL_OVER_MARGIN`)
FILL_OVER_MARGIN = 30

# 同一サイズのボードを丈補填で積み増せる上限枚数(VBA: existCnt < 4)
LENGTH_FILL_COUNT_CAP = 4

# 丈補填(上用・下用共通): 丈残がこの値を超える場合は主ボードを1枚増やし、
# 以下なら LENGTH_FILL_BOARD_W(100mm)固定サイズで埋める(現場の声:
# 「主サイズ決定⇒残り丈400越えの場合、主と同じサイズのボードを使用/
# 残り丈400以下⇒100補填で丈を埋める(4回まで)、上下とも同じにしてほしい」)。
LENGTH_FILL_THRESHOLD = 400

# 丈補填の専用サイズ(mm)。`apply_length_fill_width_correction` の
# smallSizes(30/50/100)とは切り離す ── 丈補填は「マイナス(未カバー)は
# 禁止・超過は許容」なので、丈残400mm以下という前提(閾値=上記)であれば
# 100mm板をCeiling(丈残/100)枚(最大4枚)追加するだけで必ずカバーできる。
LENGTH_FILL_BOARD_W = 100

# 「カバー済み」とみなす許容差(mm)。VBA各所の `> 3` 判定
COVERED_TOLERANCE = 3
# 残ギャップを小型ボードで埋める反復の上限(VBA `wfFillLoop < 5`)
FILL_RETRY_LIMIT = 5
# 残ギャップ埋めで許す超過(mm)。VBA `wffShort <= wfGapX + 50`
GAP_FILLER_OVER_MARGIN = 50

# 主ボード選定でリストに追加できる種類数の上限(VBA: selectedCount >= 3)
MAX_MAIN_BOARD_KINDS = 3

TAG_MAIN = "主"
TAG_WIDTH_FILL = "幅補填"
TAG_LENGTH_FILL = "丈補填"


@dataclass
class PassState:
    """選定パス間で受け渡す可変状態(VBA のByRef引数群 + モジュール変数)。"""

    remaining_len: int
    selected_count: int = 0
    pass1_done: bool = False
    pass15_used: bool = False  # 上記のとおりVBAでは常にFalse
    narrow_pallet: bool = False
    cut_info: dict[str, int] = field(default_factory=dict)


@dataclass
class LowerSelectionResult:
    boards: list[SelectedBoard]
    state: PassState
    post_fill_max_w: int = 0
    needs_wide_cut: bool = False   # カット前提選定(SelectBoardsForWideLower)が必要
    needs_narrow: bool = False     # 狭幅パレット選定が必要
    # カット前提選定(`select_boards_for_wide_lower`)が記録した丈カット情報。
    # `recalc_length_cut_info` は「カット前提」タグのボードを判定対象外に
    # するので(通常モードの再判定と二重に扱わないため)、その代わりに
    # ここへ記録された情報をそのまま結果へマージする
    length_cut_info: dict[str, int] = field(default_factory=dict)
    # プロテック確定値(唯一の正解)。プロテックでない、またはプロテック
    # 選定が使える在庫を見つけられなかったときは valid=False のまま
    protec_result: "ProtecCutResult" = field(default_factory=lambda: ProtecCutResult())


# ------------------------------------------------------------------
# 補助関数
# ------------------------------------------------------------------
def _orient(w: int, l: int, target_width: int, category: str = "下用") -> tuple[bool, int, int]:
    """`get_best_orientation` を (幅, 丈) の組で呼べるようにする薄いラッパー。"""
    board = BoardModel(width=w, length=l, board_category=category)
    return get_best_orientation(board, target_width, force_category=category)


def adjust_orientation_for_coverage(
    board_w: int, board_l: int, target_w: int, target_l: int,
    rot: bool, eff_w: int, eff_l: int, category: str = "下用",
) -> tuple[bool, int, int]:
    """VBA `AdjustOrientationForCoverage` の移植。

    現在の向きで幅カットが発生する(eff_w > target_w)場合に限り、
    逆向きが「Y方向制約を超えない」かつ「製品丈をカバーできる」なら
    逆向きに差し替える(幅カット回避・丈カバー優先)。
    """
    if eff_w <= target_w:
        return rot, eff_w, eff_l

    alt_rot = not rot
    if alt_rot:
        alt_w, alt_l = board_l, board_w
    else:
        alt_w, alt_l = board_w, board_l

    fit_limit = target_w if category == "上用" else int(target_w * LOWER_OVERHANG_Y)
    if alt_w > fit_limit:
        return rot, eff_w, eff_l
    if alt_l < target_l:
        return rot, eff_w, eff_l

    log.debug("adjust_orientation: 逆向き採用 %sx%s (幅カット回避・丈カバー優先)", alt_w, alt_l)
    return alt_rot, alt_w, alt_l


def _force_short_side_if_overshoot(
    board_w: int, board_l: int, pallet_w: int, rot: bool, eff_w: int, eff_l: int,
) -> tuple[bool, int, int]:
    """幅方向がパレット幅を超える場合、短辺を幅方向に強制する(VBA各PASS共通処理)。

    【VBAからの修正】旧実装は閾値を `pallet_w + WIDTH_OVERSHOOT_LIMIT`(3mm)と
    いう生の値にしていたが、これは下用のY方向はみ出し許容
    `LOWER_OVERHANG_Y`(20%)を無視していたバグだった。`GetBestOrientation`/
    `adjust_orientation_for_coverage` が「はみ出し許容内の長辺側」を正しく
    選んでいても、このチェックがすぐ短辺側へ書き換えてしまい、本来カット
    不要な組み合わせを不当に却下していた。閾値を `pallet_w * LOWER_OVERHANG_Y`
    に直す(=20%許容の境界を超えたときだけ短辺へ強制する)。
    """
    fit_limit = int(pallet_w * LOWER_OVERHANG_Y)
    if eff_w <= fit_limit:
        return rot, eff_w, eff_l
    b_long, b_short = max(board_w, board_l), min(board_w, board_l)
    if b_short <= pallet_w:
        return (b_short == board_l), b_short, b_long
    return rot, eff_w, eff_l


def _orient_lower(board_w: int, board_l: int, palette: Palette, product: ProductSize) -> tuple[bool, int, int]:
    """下用の向き決定〜幅超過補正までの共通前処理。

    VBAが各PASSで同じ順序で呼んでいた
    `GetBestOrientation` → `AdjustOrientationForCoverage` →
    「短辺強制」×2回 をまとめたもの(2回目は1回目と同一条件のため
    実質冗長だが、順序と結果を変えないようそのまま2回分に相当する
    処理を行う: 1回目で補正されれば2回目は条件不成立で何もしない)。
    """
    rot, eff_w, eff_l = _orient(board_w, board_l, palette.width)
    rot, eff_w, eff_l = adjust_orientation_for_coverage(
        board_w, board_l, palette.width, product.length, rot, eff_w, eff_l, "下用",
    )
    rot, eff_w, eff_l = _force_short_side_if_overshoot(board_w, board_l, palette.width, rot, eff_w, eff_l)
    return rot, eff_w, eff_l


def _current_covered_length(boards: list[SelectedBoard], palette: Palette) -> int:
    """既選定ボードが丈方向にカバー済みの合計長さ(VBA `curTotalL` 相当)。"""
    total = 0
    for b in boards:
        _, _, eff_l = _orient(b.width, b.length, palette.width)
        total += eff_l * b.count
    return total


def _fill_consistency_ok(boards: list[SelectedBoard], eff_w: int, palette: Palette, product: ProductSize) -> bool:
    """VBA PASS2/PASS3 の「補填整合性チェック」の移植。

    既選択ボード(補填タグを除く)の最小effWより新ボードが5mm以上狭く、
    かつ製品幅を満たすボードが既に無い場合は却下する
    (幅補填が重複してしまうのを防ぐ)。
    """
    if not boards:
        return True
    min_eff_w = 999999
    has_full = False
    for b in boards:
        if b.tag in (TAG_WIDTH_FILL, TAG_LENGTH_FILL):
            continue
        _, calc_eff_w, _ = _orient(b.width, b.length, palette.width)
        min_eff_w = min(min_eff_w, calc_eff_w)
        if calc_eff_w >= product.width:
            has_full = True
    if not has_full and eff_w < min_eff_w - 5:
        return False
    return True


# ------------------------------------------------------------------
# CanCoverWithFill
# ------------------------------------------------------------------
def can_cover_with_fill(width_gap: int, max_slots: int, available: list[BoardModel]) -> bool:
    """VBA `CanCoverWithFill` の移植。

    30/50/100mmの補填ボード(在庫にあるサイズのみ)の組み合わせで
    指定の幅ギャップを埋められるか総当たりで判定する。
    1サイズあたりの上限は floor(width_gap / size) かつ max_slots 以下。
    合計が [gap - PASS1_TOLERANCE, gap + FILL_OVER_MARGIN] に入れば成立。
    """
    if width_gap <= PASS1_TOLERANCE:
        return True

    fill_sizes = (FILL_SIZE_30, FILL_SIZE_50, FILL_SIZE_100)
    has_size = [False, False, False]
    for b in available:
        short = min(b.width, b.length)
        for i, sz in enumerate(fill_sizes):
            if short == sz:
                has_size[i] = True

    def cap(idx: int) -> int:
        if not has_size[idx] or fill_sizes[idx] <= 0:
            return 0
        return min(max_slots, width_gap // fill_sizes[idx])

    max0, max1, max2 = cap(0), cap(1), cap(2)

    for n2 in range(max2 + 1):
        for n1 in range(min(max1, max_slots - n2) + 1):
            for n0 in range(min(max0, max_slots - n2 - n1) + 1):
                total = n2 * fill_sizes[2] + n1 * fill_sizes[1] + n0 * fill_sizes[0]
                if width_gap - PASS1_TOLERANCE <= total <= width_gap + FILL_OVER_MARGIN:
                    return True
    return False


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
    fatigue_map: Optional[dict[str, FatigueEntry]],
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
    _force_add_for_length_shortage(boards, palette, product, available, fatigue_map)

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


def _pick_gap_filler(
    boards: list[SelectedBoard], sorted_fill: list[BoardModel], gap_x: int, sel_max_w: int,
) -> Optional[BoardModel]:
    """残ギャップを埋めるのに最も適した小型ボードを選ぶ(VBAの内側ループ)。

    ギャップ以下で最も近いものを優先し、無ければギャップ+50mmまでの
    超過を許す。既に4枚選ばれている種類は対象外。
    """
    best: Optional[BoardModel] = None
    best_diff = 999999
    for cand in sorted_fill:
        short, long_side = min(cand.width, cand.length), max(cand.width, cand.length)
        if short < NARROW_MIN_SHORT_SIDE or long_side < sel_max_w:
            continue
        # 【VBAからの修正】タグを見ずに幅・丈だけでマッチしていたため、同サイズの
        # 「主」や「幅補填」ボードが存在すると、そちらの上限判定を誤って見てしまって
        # いた(wfSkipCap)。丈補填タグの行だけを対象にする。
        if any(b.width == cand.width and b.length == cand.length
               and b.tag == TAG_LENGTH_FILL and b.count >= LENGTH_FILL_COUNT_CAP for b in boards):
            continue

        if short <= gap_x:
            diff = gap_x - short
            best_short = min(best.width, best.length) if best is not None else 0
            if diff < best_diff or (best is not None and best_short > gap_x and diff >= 0):
                best, best_diff = cand, diff
        elif short <= gap_x + GAP_FILLER_OVER_MARGIN:
            best_short = min(best.width, best.length) if best is not None else 0
            if best is None or best_short > gap_x:
                diff = short - gap_x
                if best is None or diff < best_diff:
                    best, best_diff = cand, diff
    return best


def _add_or_increment(
    boards: list[SelectedBoard], width: int, length: int, tag: str, count: int = 1,
) -> None:
    """同じW×Lがあれば枚数を足し(上限4枚)、無ければ新規行として追加する。"""
    for b in boards:
        if b.width == width and b.length == length:
            if b.count < LENGTH_FILL_COUNT_CAP:
                b.count = min(b.count + count, LENGTH_FILL_COUNT_CAP)
            return
    boards.append(SelectedBoard(width=width, length=length, count=count, tag=tag))


def _measure_selection(
    boards: list[SelectedBoard], palette: Palette, product: ProductSize,
) -> tuple[int, int, int]:
    """VBA `SelectLowerBoards` 内の selMaxW/selMinW/selTotalL 算出の移植。

    【VBAからの修正 1】旧実装は「パレット幅に近い辺を幅方向にする」という
    簡易ヒューリスティック(dN/dr比較)で向きを決めていたが、これは
    選定時に実際に使う `GetBestOrientation` とは別ロジックだったため、
    選定した向きと補填計算(このあと呼ばれる `run_lower_fill_phase` の
    入力)の向きが食い違うことがあった。

    【VBAからの修正 2】さらに、選定本体(PASS2/PASS3)は
    `GetBestOrientation` の直後に `AdjustOrientationForCoverage`
    (幅カットが出るとき、逆向きの方が丈もカバーできるなら逆向きを
    採用する補正)も呼んでいるが、この算出はそれを欠いていた。
    向き補正が発動するボードでは、実際に選定された向きとまだ
    食い違う余地が残っていたため、選定本体と同じ2段階
    (`get_best_orientation` → `adjust_orientation_for_coverage`)に揃える。
    """
    sel_max_w, sel_min_w, sel_total_l = 0, 999999, 0
    for b in boards:
        rot, eff_w, eff_l = _orient(b.width, b.length, palette.width)
        rot, eff_w, eff_l = adjust_orientation_for_coverage(
            b.width, b.length, palette.width, product.length, rot, eff_w, eff_l, "下用",
        )
        sel_max_w = max(sel_max_w, eff_w)
        sel_min_w = min(sel_min_w, eff_w)
        sel_total_l += eff_l * b.count
    if sel_min_w == 999999:
        sel_min_w = 0
    return sel_max_w, sel_min_w, sel_total_l


# 丈方向のカット判定の閾値(VBA `CUT_OVERL_THRESHOLD` / lenRemainPL)。
# フルサイズの枚数で製品丈をカバーした後、あと何mm残っているか
# (lenRemainPL)を見て、これを超える残りがあればカットしてもう1枚足す。
# 以下なら誤差として切り捨てる(追加しない)。`reports.py` の
# カット依頼書もこの値をそのまま使う(定義元はここの1か所だけ)
LENGTH_CUT_REMAIN_THRESHOLD = 100


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


# ------------------------------------------------------------------
# プロテック専用の下用選定 (VBA `SelectProtecLowerBoards`)
# ------------------------------------------------------------------
def select_protec_lower_boards(
    available: list[BoardModel], product: ProductSize, palette_length: int,
    *, is_1p1216: bool,
) -> tuple[list[SelectedBoard], ProtecCutResult]:
    """VBA `SelectProtecLowerBoards` の移植。

    プロテックモード専用の下用選定。**パレット幅ではなく製品幅を基準**に、
    `decide_protec_orientation` で在庫全件を評価し、最も条件に近い1件を
    採用する。丈方向は基本的に同じボードの枚数を増やすだけでカバーする
    が、フルサイズで覆いきれない残りが100mmを超えるときは、最後の1枚を
    カットして追加する(`decide_length_count_with_cut`。判定基準は
    「超過量」ではなく「あと何mm製品丈が残っているか」)。

    確定した内容は `ProtecCutResult` に記録して返す。これが以降の
    処理(丈カット判定・配置・カット依頼書)が参照する「唯一の正解」。
    採用できる在庫が1件も無ければ、空リストと `valid=False` の
    `ProtecCutResult` を返す(呼び出し側は通常のPASS1-3にフォールバック
    する)。
    """
    best: Optional[ProtecCutResult] = None
    for board in available:
        candidate = decide_protec_orientation(
            board.width, board.length, product.width, is_1p1216=is_1p1216)
        if not candidate.valid:
            continue
        # 製品幅に最も近い(超過・不足とも小さいほど良い)ものを採用。
        # **カット前の実効幅で比べる** ── `cut_eff_width`(カット後の
        # 仕上がりサイズ)は、カットが必要な候補同士だと常に同じ値
        # (製品幅-許容)に揃ってしまい、「どちらが無駄が少ないか」を
        # 比較する基準として使えない。同点なら丈が長い方
        # (枚数が減り、継ぎ目が少ない方)を優先する
        if best is None:
            best = candidate
            continue
        cur_gap = abs(candidate.eff_width_before_cut - product.width)
        best_gap = abs(best.eff_width_before_cut - product.width)
        if cur_gap < best_gap or (cur_gap == best_gap and candidate.eff_length > best.eff_length):
            best = candidate

    if best is None:
        log.debug("SelectProtecLowerBoards: 条件を満たす在庫がありません")
        return [], ProtecCutResult(valid=False)

    count, need_length_cut, length_cut_eff = decide_length_count_with_cut(
        best.eff_length, palette_length)
    best.count = count
    best.need_length_cut = need_length_cut
    best.length_cut_eff = length_cut_eff

    # 幅カットが要るときは「カット前提」タグにする。プロテックは在庫の
    # 種類が少なく組み合わせの余地がほとんど無いため、通常品のように
    # 「主ボード選定→補填→カバー不足を確認してからカット前提へ」という
    # 段階を踏まず、この時点で確定させる(現場の声:「プロテックボードは
    # 通常ボードより種類が少ないので、通常ボードよりもカット前提の
    # フラグをはやく立てるべき」)。タグを「カット前提」にすることで、
    # 配置(`_place_cut_premise`)が `ProtecCutResult` の確定値(カット後
    # サイズ)をそのまま使って描くため、配置図とカット依頼書の内容が
    # 一致する。カット不要なら従来どおり通常の主ボードとして配置する。
    # **丈カットが要るときも同様に「カット前提」にする** ── 幅カット・
    # 丈カットのどちらであっても、選定が済んだ段階でその情報を確実に
    # 後段へ伝える必要があるため
    tag = TAG_CUT_PREMISE if (best.need_cut or need_length_cut) else TAG_MAIN
    board_out = SelectedBoard(
        width=best.orig_width, length=best.orig_length, count=count, tag=tag)
    log.debug("SelectProtecLowerBoards: %sx%s %s枚 (rotated=%s cutEffW=%s needCut=%s "
             "needLengthCut=%s lengthCutEff=%s tag=%s)",
             best.orig_width, best.orig_length, count,
             best.is_rotated, best.cut_eff_width, best.need_cut,
             need_length_cut, length_cut_eff, tag)
    return [board_out], best


def apply_protec_rules_to_lower_list(
    lower: list[SelectedBoard], product: ProductSize, palette_length: int,
    *, is_1p1216: bool,
) -> ProtecCutResult:
    """VBA `ApplyProtecRulesToLowerList` の移植。

    手動追加された下用ボード(タグ空欄)に対し、配置直前に
    `decide_protec_orientation` で向き・カット要否を後付けで適用する。
    自動選定済みの行(タグ"主"/"カット前提")は対象外(スキップする) ──
    そちらは既に `select_protec_lower_boards` が正しい `ProtecCutResult`
    を確定させているので、ここで上書きすると選定結果と食い違う。

    【なぜ必要か】
    手動でボードを増減すると `select_result`(自動選定の結果、
    `ProtecCutResult` を含む)は丸ごと捨てられる(`add_board` 参照)。
    そのまま配置・カット依頼へ進むと、プロテックのはずなのに
    「唯一の正解」がどこにも無い状態になり、配置段階が独自に
    製品幅厳守の判定をし直して静かに配置漏れを起こす。ここで
    手動追加の行から `ProtecCutResult` 相当の値を作り直すことで、
    後続処理は自動選定のときと同じ経路(確定値をそのまま使う)を通れる。

    【丈カット判定も一緒に更新する】以前はここで手動追加の枚数
    (`target.count`)をそのまま `ProtecCutResult.count` に代入するだけで、
    丈カット(100mm閾値超えで最後の1枚をカットして追加する判定)を
    一切行っていなかった。丈カット判定の計算式が
    `select_protec_lower_boards` と重複しないよう、共通関数
    (`decide_length_count_with_cut`)を両方から呼ぶ形にする。

    先頭のタグ空欄の行を対象にする(VBA版が `lstSelectedBoardsLower`
    の最初の該当行を見ていたのに合わせる)。対象が無ければ
    `valid=False` を返す。
    """
    target = next((b for b in lower if b.tag not in (TAG_MAIN, TAG_CUT_PREMISE)), None)
    if target is None:
        return ProtecCutResult(valid=False)

    result = decide_protec_orientation(
        target.width, target.length, product.width, is_1p1216=is_1p1216)
    if result.valid:
        count, need_length_cut, length_cut_eff = decide_length_count_with_cut(
            result.eff_length, palette_length)
        result.count = count
        result.need_length_cut = need_length_cut
        result.length_cut_eff = length_cut_eff
        # 後続処理(配置・カット依頼書)は `ProtecCutResult.count` を
        # 見るので、行自体の枚数もここで合わせておく(手動で入れた
        # 枚数のままだと、確定値と表示上の枚数が食い違う)
        target.count = count
        log.debug("ApplyProtecRulesToLowerList: 手動追加行 %sx%s に後付け適用 "
                 "cutEffW=%s needCut=%s count=%s needLengthCut=%s",
                 target.width, target.length,
                 result.cut_eff_width, result.need_cut, count, need_length_cut)
    return result


# ------------------------------------------------------------------
# 下用選定 本体
# ------------------------------------------------------------------
# 業界標準サイズのショートカット判定(VBA ①モジュール定数)。
#
# 【背景】製品1002×2002で、業界標準サイズ"1×2"(1000×2000、カット不要)
# ではなく"1030×1520"(差0mmだが継ぎ足しカットが必要)が選ばれていた。
# 原因はPASS1が「幅の一致度が最も高い候補を、1件見つけた時点で即採用」
# という設計で、"1030×1520"が候補リストの先頭に来るとそこで打ち切られ、
# 後方にある"1000×2000"は評価すらされなかったこと。
#
# 【対応方針】PASS1の一般ロジック自体(全ケースに影響)を変えるのは
# リスクが大きいため、"1×2""4×8"という業界標準の2ケースだけ、製品
# サイズ範囲判定によるピンポイントショートカットで対応する。範囲に
# 入っていれば、在庫にそのボードサイズがあるかだけ確認し、あれば
# 即採用してPASS1-3をまるごとスキップする。通常モードのPASS1本体
# (スコア順1件即採用ロジック)には一切手を入れない。
SC_1X2_W_MIN, SC_1X2_W_MAX = 997, 1005
SC_1X2_L_MIN, SC_1X2_L_MAX = 1997, 2005
SC_1X2_BOARD_W, SC_1X2_BOARD_L = 1000, 2000

SC_4X8_W_MIN, SC_4X8_W_MAX = 1248, 1255
SC_4X8_L_MIN, SC_4X8_L_MAX = 2499, 2505
SC_4X8_BOARD_W, SC_4X8_BOARD_L = 1250, 2500

# "5×8"業界: 幅1498〜1535mm、丈2499〜2505mm。"1×2"/"4×8"と異なり1枚では
# 収まらず、上下で別々の複数枚構成になる。実運用では製品幅によって
# 1498〜1505→1000×1500 3枚、1506〜1535→1030×1520 2枚+450×1520 1枚の
# 2ルートに分かれるが、「5×8なのに構成が2種類ある」ことを作業者が
# 取り違えるリスクのほうが大きいため、範囲全域を単一構成に決め打ちする
# (現場の運用判断)。
SC_5X8_W_MIN, SC_5X8_W_MAX = 1498, 1535
SC_5X8_L_MIN, SC_5X8_L_MAX = 2499, 2505

# 上用構成: 1250×2500 1枚(主) + 100×2500 2枚(幅補填) + 30×2500 1枚(幅補填) = 幅1480
SC_5X8_U_MAIN_W, SC_5X8_U_MAIN_L = 1250, 2500
SC_5X8_U_FILL1_W, SC_5X8_U_FILL1_L, SC_5X8_U_FILL1_C = 100, 2500, 2
SC_5X8_U_FILL2_W, SC_5X8_U_FILL2_L, SC_5X8_U_FILL2_C = 30, 2500, 1

# 下用構成: 1030×1520 2枚(主) + 450×1520 1枚(丈補填) = 丈2510
SC_5X8_L_MAIN_W, SC_5X8_L_MAIN_L, SC_5X8_L_MAIN_C = 1030, 1520, 2
SC_5X8_L_FILL_W, SC_5X8_L_FILL_L, SC_5X8_L_FILL_C = 450, 1520, 1


def _has_available_board(available: list[BoardModel], w: int, l: int) -> bool:
    """VBA `HasAvailableBoard` の移植。

    `availableBoards` に指定サイズ(向き不問)が存在するか。"5×8"のように
    複数サイズを揃えて使う構成で、1つでも欠品していればショートカット
    自体を諦めるための判定に使う。
    """
    return any((b.width, b.length) in ((w, l), (l, w)) for b in available)


def _is_5x8_range(product: ProductSize) -> bool:
    return (SC_5X8_W_MIN <= product.width <= SC_5X8_W_MAX
            and SC_5X8_L_MIN <= product.length <= SC_5X8_L_MAX)


def _industry_standard_board_size(product: ProductSize) -> Optional[tuple[int, int]]:
    """製品サイズが業界標準("1×2"/"4×8")の範囲に入っていれば

    対応するボードサイズ(幅, 丈)を返す。入っていなければ None。
    """
    if (SC_1X2_W_MIN <= product.width <= SC_1X2_W_MAX
            and SC_1X2_L_MIN <= product.length <= SC_1X2_L_MAX):
        return SC_1X2_BOARD_W, SC_1X2_BOARD_L
    if (SC_4X8_W_MIN <= product.width <= SC_4X8_W_MAX
            and SC_4X8_L_MIN <= product.length <= SC_4X8_L_MAX):
        return SC_4X8_BOARD_W, SC_4X8_BOARD_L
    return None


def _find_industry_standard_stock(
    available: list[BoardModel], board_w: int, board_l: int,
) -> Optional[BoardModel]:
    """在庫の中に、対応ボードサイズ(向きは問わない)がそのままあるか探す。"""
    for b in available:
        if (b.width, b.length) in ((board_w, board_l), (board_l, board_w)):
            return b
    return None


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
            available, product, palette.length, is_1p1216=is_protec_1p1216)
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
# にも同じ値を使う ── 在庫を候補にするかどうかの判定と、カット後の
# 仕上がりサイズは、同じ「製品幅からどれだけマイナスまで許すか」
# という1つの数直線上の話なので、別の値を持たせない
PROTEC_1P1216_TOLERANCE = 10
PROTEC_OTHER_TOLERANCE = 80


@dataclass
class ProtecCutResult:
    """プロテック専用の選定確定値(VBA `ProtecCutResult` / `mProtecCutResult`)。

    選定(`select_protec_lower_boards`)が決めた**唯一の正解**を保持する。
    後続の処理(丈カット判定・配置・カット依頼書)はここに書かれた値を
    そのまま使い、再計算しない ── 以前は配置やカット依頼書がそれぞれ
    独自に「製品幅を超えてよいか」を判定し直しており、選定結果と
    食い違うことがあった(プロテックの上用ボードが製品幅を超過すると
    配置段階が静かに弾いてしまい、`placedBoards` に一切登録されない
    という不具合の原因)。この型が「唯一の正解」の置き場になることで、
    再計算そのものを起こさせない。

    `valid` が False のときは他フィールドを見ない(まだプロテック確定
    値が無い、または通常選定にフォールバックした状態)。
    """

    valid: bool = False
    orig_width: int = 0     # 元の在庫サイズ(幅)
    orig_length: int = 0    # 元の在庫サイズ(丈)
    is_rotated: bool = False
    eff_width_before_cut: int = 0  # 向きを決めた後、カットする前の実効幅
    cut_eff_width: int = 0  # 幅カット後の実効幅(カット不要ならeff_width_before_cutと同じ)
    eff_length: int = 0     # 丈方向の実効サイズ(フルサイズ側の1枚あたりの丈)
    need_cut: bool = False  # 幅カットが必要か
    need_length_cut: bool = False   # 丈カットが必要か(最後の1枚だけ)
    length_cut_eff: int = 0         # 丈カット後の、最後の1枚の丈
    count: int = 1          # 枚数(丈カットする最後の1枚も含む)


def decide_protec_orientation(
    board_width: int, board_length: int, product_width: int, *, is_1p1216: bool,
) -> ProtecCutResult:
    """VBA `DecideProtecOrientation` の移植。

    プロテックルール(**製品幅基準・マイナス方向の許容のみ・超過禁止**)で、
    1つの在庫サイズについてどちらの向きを使うか、カットが必要か、
    カット後の幅はいくつかを判定する。`select_protec_lower_boards`
    (自動選定)と `select_upper_boards` の下用コピー判定の両方から
    呼ばれる共通ロジック ── 呼び出し元によって判定基準がずれると、
    上用が下用と無関係な結果になりうるため、ここに1つだけ置く。

    向きは「有効幅が製品幅-許容 以上」を満たす向きの中から、**カット不要
    (有効幅が製品幅以下)を最優先**で選ぶ。カット不要な向きが無いときだけ、
    カットが要る向き(製品幅超過)から製品幅に最も近いものを選ぶ。
    長辺・短辺どちらを幅方向に使っても条件を満たせない場合は
    `valid=False` を返す(この在庫サイズはプロテックとして採用できない)。

    【設計判断の見直し】以前は「絶対距離が最小」だけで選んでいたため、
    「製品幅よりわずかに超過(カットが要る)」向きが「製品幅より余裕を
    持って不足(カット不要)」向きより僅差で近いというだけで、カットが
    要る側を選んでしまうことがあった。プロテックは在庫の種類が少なく
    カットを避けたいという業務上の前提(この関数のあらゆる呼び出し元が
    「唯一の正解」として扱う)と整合しないため、カット不要を常に優先する
    ように変更した。
    """
    tol = PROTEC_1P1216_TOLERANCE if is_1p1216 else PROTEC_OTHER_TOLERANCE
    min_w = product_width - tol

    candidates = []
    for rotated, eff_w, eff_l in (
        (False, board_width, board_length), (True, board_length, board_width),
    ):
        if eff_w >= min_w:
            candidates.append((rotated, eff_w, eff_l))

    if not candidates:
        return ProtecCutResult(valid=False)

    # カット不要(製品幅以下)な向きがあればその中から選ぶ。無ければ
    # カットが要る向きの中から選ぶ。どちらも「製品幅に最も近いもの」
    # (同点なら回転しない向きを優先、VBAの走査順を踏襲)
    no_cut = [c for c in candidates if c[1] <= product_width]
    pool = no_cut if no_cut else candidates
    rotated, eff_w, eff_l = min(pool, key=lambda c: (abs(c[1] - product_width), c[0]))

    need_cut = eff_w > product_width
    # カットするなら、仕上がり幅は「製品幅そのもの」ではなく
    # 「製品幅-許容」まで削る(超過禁止ルールと同じ許容を仕上がり側にも
    # 適用する。1P1216なら製品幅-10mm、それ以外は製品幅-80mm)
    cut_eff_w = min_w if need_cut else eff_w

    result = ProtecCutResult(
        valid=True, orig_width=board_width, orig_length=board_length,
        is_rotated=rotated, eff_width_before_cut=eff_w,
        cut_eff_width=cut_eff_w, eff_length=eff_l,
        need_cut=need_cut, count=1,
    )
    log.debug("DecideProtecOrientation: %sx%s productW=%s tol=%s -> "
             "rotated=%s cutEffW=%s effL=%s needCut=%s",
             board_width, board_length, product_width, tol,
             rotated, cut_eff_w, eff_l, need_cut)
    return result


@dataclass
class UpperSelectionResult:
    boards: list[SelectedBoard]
    mode: str = "normal"           # "normal"/"共用"/"プロテック"
    narrow_pallet: bool = False
    needs_wide_cut: bool = False   # SelectUpperBoardsWideCut が必要
    cut_info: dict[str, int] = field(default_factory=dict)
    length_cut_info: dict[str, int] = field(default_factory=dict)
    # プロテック確定値(下用選定と共有する「唯一の正解」)。mode="プロテック"
    # のときだけ valid=True になる
    protec_result: ProtecCutResult = field(default_factory=lambda: ProtecCutResult())


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


def _find_small_board(
    available: list[BoardModel], small_sizes: tuple[int, ...], width_gap: int,
) -> Optional[BoardModel]:
    """幅ギャップを埋められる最小の小型ボードを在庫から探す。

    `small_sizes` の並び順ではなく「ギャップ以上で最小のサイズ」を選ぶ
    (VBAも `smallSizes(usbi) >= uBestSmallW` でより小さい方に更新する)。
    """
    best: Optional[BoardModel] = None
    best_size = 0
    for size in small_sizes:
        if size < width_gap:
            continue
        if best_size and size >= best_size:
            continue
        for cand in available:
            if min(cand.width, cand.length) == size:
                best, best_size = cand, size
                break
    return best


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

TAG_CUT_PREMISE = "カット前提"

# 疲労度計算のカット係数(modAngleSelect の CUT_FAT_MULT と同値)
CUT_FAT_MULT = 233.0


@dataclass
class WideCutResult:
    boards: list[SelectedBoard]
    cut_info: dict[str, int] = field(default_factory=dict)
    # 丈カット(VBA `mLengthCutInfo`)。キーは "U_幅x丈"
    length_cut_info: dict[str, int] = field(default_factory=dict)
    used_fallback: bool = False


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
    available: list[BoardModel],
) -> None:
    """カット前提選定後の丈補填(上用・下用共通)。

    丈残が400mm超なら主ボードを1枚増やし、それ以下なら小型ボードを
    丈補填として追加する(最大5回)。
    """
    len_gap = product.length - short_side * cnt
    if len_gap <= 3:
        return
    log.debug("[幅広丈補填] 丈残=%smm", len_gap)

    for _ in range(5):
        if len_gap <= 3:
            return
        if len_gap > 400:
            boards[0].count += 1
            len_gap -= short_side
            log.debug("[幅広丈補填] 丈残400超 → 主+1枚 計%s枚", boards[0].count)
            continue

        found = next(
            (a for a in available
             if min(a.width, a.length) <= len_gap + 50 and min(a.width, a.length) >= NARROW_MIN_SHORT_SIDE),
            None,
        )
        if found is None:
            boards[0].count += 1
            len_gap -= short_side
            log.debug("[幅広丈補填] 小ボードなし → 主+1枚 計%s枚", boards[0].count)
            continue

        f_short = min(found.width, found.length)
        existing = next((s for s in boards if s.width == found.width and s.length == found.length), None)
        if existing is not None:
            existing.count += 1
        else:
            boards.append(SelectedBoard(width=found.width, length=found.length, count=1, tag=TAG_LENGTH_FILL))
        len_gap -= f_short
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
        _wide_cut_length_fill(result.boards, short_side, cnt, product, available)
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
        for b, prefix, pr in (
            (lower[0] if lower else None, "L_", lower_protec_result),
            (upper[0] if upper else None, "U_", upper_protec_result),
        ):
            if b is None or pr is None or not pr.valid:
                continue
            if pr.need_length_cut:
                key = f"{prefix}{b.width}x{b.length}"
                length_cut_info[key] = pr.cut_eff_width
                length_cut_count[key] = 1
                log.debug("丈カット記録[プロテック確定値 %s]: %s lengthCutEff=%s needLengthCut=True",
                         prefix, key, pr.length_cut_eff)
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
        # 幅補填は丈カット対象外。カット前提の丈カットは選定側
        # (`select_upper_boards_wide_cut`)が別途記録し、
        # `UpperSelectionResult.length_cut_info` として渡る
        # (下用と対称。両方とも呼び出し元でマージされる)
        if b.tag in (TAG_WIDTH_FILL, TAG_CUT_PREMISE):
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
@dataclass
class AutoSelectResult:
    lower: list[SelectedBoard]
    upper: list[SelectedBoard]
    lower_result: LowerSelectionResult
    upper_result: UpperSelectionResult
    cut_info: dict[str, int] = field(default_factory=dict)
    length_cut_info: dict[str, int] = field(default_factory=dict)
    length_cut_count: dict[str, int] = field(default_factory=dict)


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
