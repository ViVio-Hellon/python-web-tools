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

from dataclasses import dataclass, field
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


def _try_forced_add(boards: list[SelectedBoard], state: PassState, eff_l: int, palette: Palette) -> bool:
    """VBA各PASSの「強制追加(早期)」の移植。

    残丈が300mm以上あり、リストが1種類だけで、1枚増やしても
    パレット丈×はみ出し許容比率に収まるなら枚数を+1する。
    """
    if state.remaining_len < 300 or len(boards) != 1:
        return False
    limit = int(palette.length * palette.overhang_ratio)
    if eff_l * (boards[0].count + 1) > limit:
        return False
    boards[0].count += 1
    state.remaining_len = 0
    log.debug("下用強制追加(早期): %sx%s 計%s枚", boards[0].width, boards[0].length, boards[0].count)
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

        # PASS1の強制追加は「残丈>=300 かつ 1種類のみ」で判定(PASS2/3と同条件)
        _try_forced_add(boards, state, eff_l, palette)
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

        if _try_forced_add(boards, state, eff_l, palette):
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

        if _try_forced_add(boards, state, eff_l, palette):
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


def _run_length_fill_loop(
    boards: list[SelectedBoard], sorted_boards: list[BoardModel], l_gap: int, sel_max_w: int,
    fatigue_map: Optional[dict[str, FatigueEntry]], fatigue_mode: bool,
) -> None:
    """VBA `RunLowerFillPhase` 内の「丈補填反復ループ」の移植(最大5回)。

    残り丈ギャップに最も近い短辺(かつ長辺が主ボード最大幅以上)の
    ボードを選び、同一サイズが既にあれば枚数+1、無ければ新規追加する。
    既に幅補填として使われているサイズ、および同一サイズが上限枚数に
    達しているものは対象外。
    """
    cur_gap = l_gap
    for loop_idx in range(5):
        if cur_gap <= 3:
            return
        log.debug("丈補填ループ%s: 残gap=%smm", loop_idx + 1, cur_gap)

        best: Optional[tuple[int, int, int, int]] = None  # short, long, bw, bl
        best_diff = 999999
        best_score = float("inf")

        for b in sorted_boards:
            f_short, f_long = min(b.width, b.length), max(b.width, b.length)
            if f_short < 10 or f_long < sel_max_w:
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
            return

        b_short, _, bw, bl = best
        # 【VBAからの修正】同サイズの「主」「幅補填」行を誤ってマージ対象にしないよう、
        # 丈補填タグの行に限定する。上限(4枚)到達時は既存行を+1→4クランプするだけで
        # 済ませず、新規行の作成に回す(下用側と上用側で挙動を統一)。
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
            return


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
    final_total_l = sum(
        _orient(b.width, b.length, palette.width)[2] * b.count for b in boards
    )
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
        _run_length_fill_loop(boards, sorted_fill, l_gap, sel_max_w, fatigue_map, fatigue_mode)

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


# ------------------------------------------------------------------
# 下用選定 本体
# ------------------------------------------------------------------
def select_lower_boards(
    available: list[BoardModel], palette: Palette, product: ProductSize,
    *,
    fatigue_map: Optional[dict[str, FatigueEntry]] = None,
    fatigue_mode: bool = False,
    stock_aware: bool = False,
) -> LowerSelectionResult:
    """VBA `SelectLowerBoards` の移植。

    処理順:
        1. 1枚物優先選定(製品サイズ基準・パレット不問)
        2. PASS1 / PASS1.5(疲労度モードは競合選定、通常モードは即決定)
        3. PASS2(ベストフィット、パレット幅超過不可)
        4. PASS3(はみ出し許容20%まで)
        5. 全体の強制追加(残丈300mm以上なら先頭を+1して他を削除)
        6. 狭幅パレット判定
        7. 補填フェーズ(丈補填→幅補填)
        8. 代替ボードリトライループ(疲労度モードのみ、最大5回)
        9. それでも幅が足りなければカット前提選定が必要と報告
    """
    boards: list[SelectedBoard] = []
    state = PassState(remaining_len=palette.length)

    sorted_lower = sort_boards_by_target_width(
        available, palette.width, strict=False, fatigue_map=fatigue_map,
        base_length=palette.length, stock_aware=stock_aware,
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
        )
        _select_lower_pass2(sorted_strict, boards, state, palette, product)

    # 4. PASS3
    if not state.pass1_done:
        _select_lower_pass3(sorted_lower, boards, state, palette, product)

    # 5. 全体の強制追加(先頭ボードを1枚増やして他の種類を削除する)
    if state.remaining_len >= 300 and boards:
        first = boards[0]
        if first.length * (first.count + 1) <= int(palette.length * palette.overhang_ratio):
            first.count += 1
            state.remaining_len = 0
            del boards[1:]
            log.debug("下用強制追加: %sx%s 計%s枚", first.width, first.length, first.count)

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
    if needs_wide_cut:
        log.debug("[下用] 幅補填後もカバー不足 -> カット前提選定へ")
        boards.clear()
        wide = select_boards_for_wide_lower(
            available, palette, product, fatigue_map=fatigue_map, fatigue_mode=fatigue_mode,
        )
        boards.extend(wide.boards)
        state.cut_info.update(wide.cut_info)

    return LowerSelectionResult(
        boards=boards, state=state, post_fill_max_w=post_fill_max_w, needs_wide_cut=needs_wide_cut,
    )


# ==================================================================
# 上用選定 (VBA `SelectUpperBoards` の移植)
#
# 上用は製品の上に載せるボードで、下用(パレット上に敷く)とは基準が異なる:
#   - 幅の基準がパレット幅ではなく「製品幅」
#   - 製品幅の超過は厳禁(はみ出し許容係数を適用しない)
#   - 幅不足は UPPER_WIDTH_TOLERANCE(80mm、両端-40mm)まで許容
# ==================================================================

# 丈残がこの値を超える場合は主ボードを1枚増やし、以下なら小型補填を優先する
UPPER_LENGTH_FILL_THRESHOLD = 400

# プロテックモードのうち 1P1216 の幅許容(本来-20mmだが切断精度を考慮し-10mm)。
# `reports.protec_cut_size_info` の `PROTEC_CUT_TOL_1P1216` と必ず同じ値にする
# ―― ここが小さいと、切断依頼書では許容内として同サイズ強制するはずの
# ボードが、選定側だけ通常選定に外れてしまい(別のボードが選ばれる)、
# 依頼書の内容と選定結果が食い違う原因になっていた。
PROTEC_1P1216_TOLERANCE = 10


@dataclass
class UpperSelectionResult:
    boards: list[SelectedBoard]
    mode: str = "normal"           # "normal"/"共用"/"プロテック"
    narrow_pallet: bool = False
    needs_wide_cut: bool = False   # SelectUpperBoardsWideCut が必要
    cut_info: dict[str, int] = field(default_factory=dict)
    length_cut_info: dict[str, int] = field(default_factory=dict)


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


def _run_upper_length_fill(
    boards: list[SelectedBoard], product: ProductSize, available: list[BoardModel],
    fatigue_map: Optional[dict[str, FatigueEntry]],
) -> None:
    """VBA「上用:丈補填フェーズ」の移植。

    丈残が400mm超なら先に主ボードを1枚増やし、その後
    最大5回のループで小型補填ボードを追加する。
    候補は「短辺が丈残+50mm以内」かつ「長辺(製品幅で頭打ち)が10mm以上」で、
    同一サイズが4枚に達しているものは対象外。
    """
    total_upper_l = _upper_total_length(boards, product)
    length_remaining = product.length - total_upper_l
    if length_remaining <= 3:
        return

    if length_remaining > UPPER_LENGTH_FILL_THRESHOLD and boards:
        main = boards[0]
        _, _, t_eff_l = _orient(main.width, main.length, product.width, category="上用")
        main.count += 1
        length_remaining -= t_eff_l
        log.debug("上用丈補填: 丈残400超 → 主+1枚 計%s枚 丈残=%s", main.count, length_remaining)
    else:
        log.debug("上用丈補填: 丈残400以下のため主ボード追加せず小型補填へ remaining=%s", length_remaining)

    # VBA版はこのソートに base_length を渡していない(下用の補填ソートとの相違点)
    ul_sorted = sort_boards_by_target_width(
        available, product.width, strict=False, fatigue_map=fatigue_map,
    )

    remain = length_remaining
    for _ in range(5):
        if remain <= 3:
            return
        best_short = 0
        best: Optional[BoardModel] = None
        best_diff = 999999

        for cand in ul_sorted:
            short_side = min(cand.width, cand.length)
            long_side = max(cand.width, cand.length)
            if short_side > remain + 50:
                continue
            effective_long = min(long_side, product.width)
            if effective_long < 10:
                continue
            # 【VBAからの修正】タグを見ずに幅・丈だけでマッチしていたため、同サイズの
            # 「主」や「幅補填」ボードが存在すると、そちらの上限判定を誤って見てしまって
            # いた(ulSkipCap)。丈補填タグの行だけを対象にする(下用側と同じ考え方)。
            existing = next(
                (s for s in boards if s.width == cand.width and s.length == cand.length
                 and s.tag == TAG_LENGTH_FILL),
                None,
            )
            if existing is not None and existing.count >= LENGTH_FILL_COUNT_CAP:
                continue

            if short_side <= remain:
                this_diff = remain - short_side
                if this_diff < best_diff or best_short == 0:
                    best_diff, best_short, best = this_diff, short_side, cand
            elif short_side <= remain + 50:
                # 丈残を超過するが+50mm以内: 既存ベストより超過が少なければ採用
                if best_short == 0 or short_side < best_short:
                    best_diff, best_short, best = short_side - remain, short_side, cand

        if best is None:
            log.debug("上用丈補填: 適合なし → 終了")
            return

        # 【VBAからの修正】既存行へマージするのは "丈補填" タグの行だけに限定する。
        # タグを問わず(width, length)一致だけでマージすると、"幅補填" タグの
        # 行(丈カバーには寄与しない専用枠)に丈補填の+1が紛れ込むことがあり、
        # 実際の丈カバーが伸びないまま丈残だけが減った扱いになって、直後の
        # 「丈不足強制追加」で別ボードがさらに足される二重補填の原因になる。
        # 【VBAからの修正】既存の丈補填行が上限(4枚)の場合、+1→4クランプするだけで
        # 実際には枚数が増えていないのに「解消済み」と誤認していた。上限到達時は
        # 既存行を触らず、新規の別行として追加する(下用側と同じ方針)。
        existing = next(
            (s for s in boards if s.width == best.width and s.length == best.length
             and s.tag == TAG_LENGTH_FILL),
            None,
        )
        if existing is not None and existing.count < LENGTH_FILL_COUNT_CAP:
            existing.count += 1
            # 【VBAからの修正】丈補填ボードは短辺だけを丈方向に使う配置なので、
            # 新規追加時と同じ best_short(短辺)を減算量にする。ここを
            # _orient() の再計算結果(幅フィット優先で長辺をeffLとする
            # ことがある)から取ると、実際には少ししか埋まっていない丈残を
            # 大きく埋まったと誤認し、後続の丈不足チェックが空振りする。
            added = best_short
            log.debug("上用丈補填(既存+1): %sx%s", best.width, best.length)
        else:
            boards.append(SelectedBoard(width=best.width, length=best.length, count=1, tag=TAG_LENGTH_FILL))
            added = best_short
            log.debug("上用丈補填(新規): %sx%s", best.width, best.length)

        remain -= added


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
) -> UpperSelectionResult:
    """VBA `SelectUpperBoards` の移植。

    処理順:
        1. 上下共用モード(ザラ板等): 保護材が確定していてアングルでも
           「一致なし」でもなければ、上用は下用の主ボードと同サイズを強制
        2. プロテックモード: 下用主ボードの有効幅が許容内なら同サイズを強制
           (1P1216は許容-5mm、それ以外は-80mm)。許容外なら通常選定へ
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
        protec_tol = PROTEC_1P1216_TOLERANCE if is_protec_1p1216 else UPPER_WIDTH_TOLERANCE
        main = lower_boards[0]
        _, p_eff_w, _ = _orient(main.width, main.length, product.width, category="上用")
        if p_eff_w >= product.width - protec_tol:
            result.boards = _copy_lower_main_to_upper(lower_boards)
            result.mode = "プロテック"
            log.debug("SelectUpperBoards [プロテック]: 下用と同サイズ強制 effW=%s tol=%s", p_eff_w, protec_tol)
            return result
        log.debug("SelectUpperBoards [プロテック]: 下用サイズが許容外 → 通常選定へ")

    # 3. 主ボード選択(最初に条件を満たしたものを採用)
    sorted_upper = sort_boards_by_target_width(
        available, product.width, strict=True, fatigue_map=fatigue_map,
        base_length=product.length, stock_aware=stock_aware,
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

    for cand in sort_boards_by_target_width(
            available, product.width, strict=True,
            fatigue_map=fatigue_map, base_length=product.length):
        short, long_side = min(cand.width, cand.length), max(cand.width, cand.length)
        # ギャップを埋められる最小限のサイズだけを許す(大きすぎる板は使わない)
        if short < gap or short > gap * 3 or long_side < NARROW_MIN_SHORT_SIDE:
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
    """
    return _select_wide_cut(available, palette.width, product, TAG_MAIN, fatigue_map, fatigue_mode)


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
) -> tuple[dict[str, int], dict[str, int]]:
    """VBA `RecalcLengthCutInfo` の移植。

    元ソースが「カット枚数の業務ルールを決める唯一の箇所(SSOT)」と
    明記している処理。丈カットは丈オーバー時に最後の1枚だけ発生する
    (常に枚数1)という業務ルールをここで確定させる。

    戻り値は (length_cut_info, length_cut_count)。
    キーは下用が "L_幅x丈"、上用が "U_幅x丈"、値は切断線の長さ(有効幅)。
    """
    length_cut_info: dict[str, int] = {}
    length_cut_count: dict[str, int] = {}

    for b in lower:
        # 上用側と対称にする。カット前提は選定側で確定済みなのでここでの
        # 再計算対象から外す(プロテックの共用ボードは常に無タグなので
        # 現状は無効だが、上下の扱いを揃えておく)
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
        # 幅補填は丈カット対象外。カット前提は SelectUpperBoardsWideCut が記録済み
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
    )
    upper_result = select_upper_boards(
        lower_result.boards, available, palette, product,
        fatigue_map=fatigue_map_upper, fatigue_mode=fatigue_mode, stock_aware=stock_aware,
        is_protec_mode=is_protec_mode, is_protec_1p1216=is_protec_1p1216, last_hosozai=last_hosozai,
    )
    length_cut_info, length_cut_count = recalc_length_cut_info(
        lower_result.boards, upper_result.boards, palette, product,
    )
    # カット前提(幅広)の上用は `recalc_length_cut_info` の対象外なので、
    # 選定時に記録した丈カットをここで合流させる
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
