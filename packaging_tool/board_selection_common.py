"""ボード選定の共通部品 ── 決まりの数値と、向き・寸法合わせ

**どの段でも使うものだけを置く。** 下用・上用・プロテック・狭幅の
どれか1つでしか使わないものはここに置かない(置くと、直すときに
どこへ効くのかが読めなくなる)。

置いてあるのは2種類。

  * 決まりの数値 ── 補填の板サイズ、丈補填の閾値、許容差、タグの名前
  * 向きと寸法合わせ ── ボードを縦横どちらで使うか、いまどこまで
    覆えているか、在庫にその寸法があるか

判断(どのボードを選ぶか)はここには無い。それは段ごとのモジュールが
持つ。
"""
from __future__ import annotations

from typing import Optional

from .board_scoring import (LOWER_OVERHANG_Y, PASS1_TOLERANCE,
                            WIDTH_OVERSHOOT_LIMIT, get_best_orientation)
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .logging_utils import get_logger
from .models import BoardModel

log = get_logger("board_selection.common")


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
# カット前提で選んだ印。狭幅・カット前提とプロテックの両方が使う
TAG_CUT_PREMISE = "カット前提"

# 丈カットの判定で「残りはもう切らなくてよい」とみなす長さ(mm)
LENGTH_CUT_REMAIN_THRESHOLD = 100


# 狭幅選定で候補にできる最小の短辺(mm)。**在庫の引き当てでも見る**
NARROW_MIN_SHORT_SIDE = 10




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


# ------------------------------------------------------------------
# 向きと寸法合わせ
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
