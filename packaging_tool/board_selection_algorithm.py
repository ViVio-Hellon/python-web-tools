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

from dataclasses import dataclass
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

    _record_length_fill_overshoot(lower, palette.length, "L_", palette.width, "下用",
                                  length_cut_info, length_cut_count)
    _record_length_fill_overshoot(upper, product.length, "U_", product.width, "上用",
                                  length_cut_info, length_cut_count)
    return length_cut_info, length_cut_count


def _record_length_fill_overshoot(
    boards: list[SelectedBoard], base_length: int, prefix: str, target_width: int, category: str,
    length_cut_info: dict[str, int], length_cut_count: dict[str, int],
) -> None:
    """丈補填の板(主と同じサイズの板など)が基準の丈を超えたら、その分を丈カットとして記録する(VER4.8.70)。

    現場の声:「350mmはみ出して使うロジックなんかねぇだろ」。丈残が400mmを超えると主と同じサイズの板を
    丈補填で足す(`LENGTH_FILL_THRESHOLD`)。例: パレット丈1650 に 1000x1000 を2枚 → 350mm はみ出す。
    ボードMAPの図は「はみ出し丈カット 約350mm」と描くのに、ここでは丈補填を対象外にしていたので
    記録されず、疲労度の比べで「切らない」と数えていた。同じ並びでも主2枚なら記録される(経路で違った)。

    丈の合計は選定と同じ数え方(幅補填は数えない、丈補填は短辺×枚数、ほかは有効丈×枚数)。
    **30/50/100mm の細板の丈補填は対象外**(「超過は許容」の決まり。`LENGTH_FILL_BOARD_W`)。
    カット前提の行がある区分は、選定側が丈カットを記録するので触らない。切るのは最後の1枚(枚数1)。
    切断線(値)は丈補填の板の幅方向の長さ = 長辺(短辺を丈に向けて置くため)。
    """
    if any(b.tag == TAG_CUT_PREMISE for b in boards):
        return
    if any(key.startswith(prefix) for key in length_cut_info):
        return                                     # 主ボードの丈カットが記録済み
    fills = [b for b in boards if b.tag == TAG_LENGTH_FILL and b.count > 0
             and min(b.width, b.length) not in (FILL_SIZE_30, FILL_SIZE_50, FILL_SIZE_100)]
    if not fills:
        return
    total = 0
    for b in boards:
        if b.tag == TAG_WIDTH_FILL:
            continue
        if b.tag == TAG_LENGTH_FILL:
            total += min(b.width, b.length) * b.count
        else:
            total += _orient(b.width, b.length, target_width,
                             **({"category": "上用"} if category == "上用" else {}))[2] * b.count
    if total <= base_length:
        return
    last = fills[-1]
    key = f"{prefix}{last.width}x{last.length}"
    length_cut_info[key] = max(last.width, last.length)
    length_cut_count[key] = 1
    log.debug("丈カット記録[%s/丈補填]: %s 丈合計%s > 基準丈%s(%smm はみ出し)",
              category, key, total, base_length, total - base_length)


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


# ==================================================================
# 疲労度優先: 通常の選定と疲労度の選定を**本当の合計点**で比べる(VER4.8.65)
# ==================================================================
# 現場の声:「カットするぐらいなら運搬するという現場の思想は生きていますか？」。
# 実データ(共有マスタの写し・ハードボード544通り)で比べると、疲労度ONのほうが切る選定が
# 多かった(拠点L1: 切る選定 55 → 116、板 4761 → 5387枚)。選定の**前の見込み**で比べて
# いるため、次の3つが点に入らないまま選ばれていた:
#   1. 見込みの丈カットは「切る」と「補填板で埋める」を見分けられない(どちらも1回と数える)
#   2. 下用PASS1(疲労度モードだけ)は、点を数えたあとで主ボードを1枚足す(丈残300以上)
#   3. 上用の並べ替えは、幅が足りずカット前提(全数カット)になることを見込めない
# 見込みの式を細かく直すより、**できあがった選定を数える**ほうが確か。両方作って、実際に切る
# 箇所・補填板・枚数をすべて数えた合計点で低いほうを採る。ONがOFFより疲れる結果にならない。
#
# **カットは最終手段**(VER4.8.66。現場の声:「カットは絶対しないというわけではないですよね？
# 最終手段の位置づけとしてください」)。比べるときは**切る回数が少ないほうを先に**採り、
# 同じ回数なら合計点で比べる。切らずに済む選定があればそちらを採り、どちらも切るときだけ切る。
# **プロテックは別の考え方**(「プロテックのカット前提とは別の考え方とも理解してください」)。
# プロテックは製品幅に合わせて切るのが決まりで、疲労度でも比べない(下用・上用とも疲労度を
# 使わない選定なので、ここでは疲労度の選定をそのまま採る)。
@dataclass
class FatigueTally:
    """できあがった選定の疲労度(`docs/疲労度の計算.md` と同じ式)。"""

    distance: float = 0.0     # 棚まで歩く(サイズごとに1往復)
    carry: float = 0.0        # 運ぶ(面積 + 手間)x枚数。補填板も入れる
    cut: float = 0.0          # 実際に切るぶん
    sheets: int = 0
    cuts: int = 0             # 切る回数
    low: int = 0              # 在庫薄の板の枚数(在庫考慮ONのときだけ数える。VER4.8.67)

    @property
    def total(self) -> float:
        return self.distance + self.carry + self.cut

    def text(self) -> str:
        low = f" / 在庫薄 {self.low}枚" if self.low else ""
        return (f"{self.total:.1f}点(歩く {self.distance:.1f} / 運ぶ {self.carry:.1f} / "
                f"切る {self.cut:.1f}・{self.cuts}回 / 板 {self.sheets}枚{low})")


def actual_fatigue(
    result: AutoSelectResult,
    fatigue_map_lower: Optional[dict[str, FatigueEntry]],
    fatigue_map_upper: Optional[dict[str, FatigueEntry]],
    low_sizes: frozenset = frozenset(),
) -> FatigueTally:
    """できあがった選定(上用・下用・補填板)を、実際に切る箇所まで数える。

        歩く … サイズごとに1回(棚まで1往復。疲労度マップの距離。置き場不明は既定の25点)
        運ぶ … (面積 + 手間) x 枚数。**補填板も1枚ずつ**
        切る … 丈カット: 記録された丈カット(`length_cut_info` の L_/U_)x 切る枚数。切断線 = 値
               幅カット: カット前提の板は全数(切断線 = 短辺)
    """
    from . import angle_service

    tally = FatigueTally()
    seen: set[tuple[int, int]] = set()
    for b in list(result.lower) + list(result.upper):
        size = (min(b.width, b.length), max(b.width, b.length))
        n = max(int(b.count or 0), 0)
        tally.sheets += n
        if size in low_sizes:
            tally.low += n
        tally.carry += (angle_service.fat_area_score1(b.width, b.length)
                        + angle_service.FAT_SHEET_SCORE) * n
        if size not in seen:
            seen.add(size)
            key = f"{b.width}x{b.length}"
            entry = ((fatigue_map_lower or {}).get(key) or (fatigue_map_upper or {}).get(key))
            tally.distance += entry.dist if entry else angle_service.FAT_DIST_DEFAULT
        if (b.tag or "").strip() == TAG_CUT_PREMISE and n:
            tally.cut += angle_service.fat_cut_score1(size[0]) * n
            tally.cuts += n
    for key, line in result.length_cut_info.items():
        if not key.startswith(("L_", "U_")):
            continue
        n = max(1, int(result.length_cut_count.get(key, 1) or 1))
        tally.cut += angle_service.fat_cut_score1(line) * n
        tally.cuts += n
    return tally


@dataclass
class FatigueChoice:
    result: AutoSelectResult
    normal: FatigueTally
    fatigue: FatigueTally
    used_fatigue: bool        # 疲労度の選定を採ったか(False なら通常の選定のほうが楽だった)
    compared: bool = True     # 比べたか(プロテックは比べない)

    @property
    def why(self) -> str:
        """採った理由(選定ログに出す)。"""
        if not self.compared:
            return "プロテックは製品幅に合わせて切るのが決まりなので、比べずに疲労度の選定を採りました"
        chosen, other = ((self.fatigue, self.normal) if self.used_fatigue
                         else (self.normal, self.fatigue))
        name = "疲労度の選定" if self.used_fatigue else "通常の選定"
        if chosen.low < other.low:
            return f"在庫薄の板が少ないので{name}を採りました(在庫考慮)"
        if chosen.cuts < other.cuts:
            return f"切る回数が少ないので{name}を採りました(カットは最終手段)"
        if chosen.total < other.total:
            return f"切る回数が同じで、合計点が低いので{name}を採りました"
        return f"同じなので{name}を採りました"


def fatigue_rank(tally: FatigueTally) -> tuple[int, int, float]:
    """比べる順: **在庫薄の枚数**(在庫考慮ONのとき)→ **切る回数**(カットは最終手段)→ 合計点。

    在庫が先(VER4.8.67。現場の声:「在庫と疲労度は重複で切る必要はあります(両方on)。
    両方onの場合在庫がなければ切るしかないので仕方がない」)。
    """
    return tally.low, tally.cuts, round(tally.total, 6)


def auto_select_lowest_fatigue(
    available: list[BoardModel], palette: Palette, product: ProductSize,
    *,
    fatigue_map_lower: dict[str, FatigueEntry],
    fatigue_map_upper: dict[str, FatigueEntry],
    stock_aware: bool = False,
    low_sizes: frozenset = frozenset(),
    **flags,
) -> FatigueChoice:
    """疲労度優先: 通常の選定と疲労度の選定を作り、**切る回数が少ないほう**、同じなら
    本当の合計点が低いほうを採る(`fatigue_rank`)。同じなら疲労度の選定(押した人が選んだ方式)。

    プロテック(`is_protec_mode`)は比べない(カットは決まりで、選定も疲労度を使わない)。
    """
    fatigue = auto_select_boards(available, palette, product,
                                 fatigue_map_lower=fatigue_map_lower,
                                 fatigue_map_upper=fatigue_map_upper,
                                 fatigue_mode=True, stock_aware=stock_aware, **flags)
    f_tally = actual_fatigue(fatigue, fatigue_map_lower, fatigue_map_upper, low_sizes)
    if flags.get("is_protec_mode"):
        return FatigueChoice(result=fatigue, normal=f_tally, fatigue=f_tally,
                             used_fatigue=True, compared=False)
    normal = auto_select_boards(available, palette, product, fatigue_mode=False,
                                stock_aware=stock_aware, **flags)
    n_tally = actual_fatigue(normal, fatigue_map_lower, fatigue_map_upper, low_sizes)
    use_fatigue = fatigue_rank(f_tally) <= fatigue_rank(n_tally)
    log.debug("疲労度の比べ: 通常 %s / 疲労度 %s → %s",
              n_tally.text(), f_tally.text(), "疲労度" if use_fatigue else "通常")
    return FatigueChoice(result=fatigue if use_fatigue else normal,
                         normal=n_tally, fatigue=f_tally, used_fatigue=use_fatigue)


# ==================================================================
# 在庫考慮: 先に在庫薄の板を外して選ぶ(VER4.8.67)
# ==================================================================
# 現場の声:「在庫加味が正しく動くか(在庫が少ないものをよけているか)」。点検すると、在庫考慮は
# 「在庫のある板を先に並べる」だけで、544通り中289通りで在庫薄の板を使っていた(外せば全部
# 在庫のある板だけで選べた)。選定は「1枚で覆える板」から順に探すので、その段の候補が在庫薄だけ
# だとそれで決まり、在庫のある板を使う次の段まで進まない。
# →「在庫考慮ONのときは、先に在庫薄の板を外して選ぶ」(現場で了承)。外して選べなかったときだけ
#   在庫薄も入れて選ぶ(在庫のある板が先に並ぶ、前のやり方)。疲労度もONなら、外した板の中で
#   疲労度の比べ(在庫薄の枚数 → 切る回数 → 合計点)をする。外すと切ることになっても外すほうを
#   採る(「在庫がなければ切るしかないので仕方がない」)。
@dataclass
class BoardSelection:
    result: AutoSelectResult
    choice: Optional[FatigueChoice] = None    # 疲労度で比べたとき
    low_sizes: frozenset = frozenset()        # 候補の中の在庫薄(在庫考慮ONのとき)
    avoided_low: bool = False                 # 在庫薄を外して選べた
    low_used: tuple[str, ...] = ()            # 使ってしまった在庫薄の板("1000×1500")


def _low_sizes(available: list[BoardModel]) -> frozenset:
    return frozenset((min(b.width, b.length), max(b.width, b.length))
                     for b in available if getattr(b, "stock_low", False))


def _low_used(result: AutoSelectResult, low: frozenset) -> tuple[str, ...]:
    seen: list[str] = []
    for b in list(result.lower) + list(result.upper):
        if (min(b.width, b.length), max(b.width, b.length)) in low:
            name = f"{b.width}×{b.length}"
            if name not in seen:
                seen.append(name)
    return tuple(seen)


def _usable_without_low(trial: AutoSelectResult, full: AutoSelectResult) -> bool:
    """在庫薄を外して選んだ結果が使えるか。在庫薄も入れた選定に比べて、

        - 下用・上用が空になっていない
        - 幅が足りないまま採っていない(カット前提でも届かなかった)
        - 狭幅パレットの選定(細い板を並べる最後の手段)に落ちていない
          (在庫薄を外したせいで細板しか残らず、製品幅を覆えない並べ方になる)
        - プロテックの確定値が取れている(プロテックのとき)

    切ることになるのは構わない(在庫が無ければ切るしかない)。
    """
    if full.lower and not trial.lower:
        return False
    if full.upper and not trial.upper:
        return False
    full_short = full.lower_result.wide_fallback or full.upper_result.wide_fallback
    trial_short = trial.lower_result.wide_fallback or trial.upper_result.wide_fallback
    if trial_short and not full_short:
        return False
    full_narrow = full.lower_result.needs_narrow or full.upper_result.narrow_pallet
    trial_narrow = trial.lower_result.needs_narrow or trial.upper_result.narrow_pallet
    if trial_narrow and not full_narrow:
        return False
    if full.lower_result.protec_result.valid and not trial.lower_result.protec_result.valid:
        return False
    return True


def select_boards(
    available: list[BoardModel], palette: Palette, product: ProductSize,
    *,
    fatigue_map_lower: Optional[dict[str, FatigueEntry]] = None,
    fatigue_map_upper: Optional[dict[str, FatigueEntry]] = None,
    stock_aware: bool = False,
    **flags,
) -> BoardSelection:
    """資材選択の「ボード選定」の入口。在庫考慮と疲労度優先の組み合わせをここで決める。

        在庫考慮 … ONなら先に在庫薄の板を外して選ぶ。使えなければ在庫薄も入れて選ぶ
        疲労度   … マップがあれば通常と疲労度の選定を比べる(在庫薄の枚数 → 切る回数 → 合計点)
    """
    low = _low_sizes(available) if stock_aware else frozenset()
    use_fatigue = fatigue_map_lower is not None and fatigue_map_upper is not None

    def once(boards: list[BoardModel]) -> tuple[AutoSelectResult, Optional[FatigueChoice]]:
        if use_fatigue:
            choice = auto_select_lowest_fatigue(
                boards, palette, product, fatigue_map_lower=fatigue_map_lower,
                fatigue_map_upper=fatigue_map_upper, stock_aware=stock_aware,
                low_sizes=low, **flags)
            return choice.result, choice
        return auto_select_boards(boards, palette, product, fatigue_mode=False,
                                  stock_aware=stock_aware, **flags), None

    full, full_choice = once(available)
    if low:
        in_stock = [b for b in available if not getattr(b, "stock_low", False)]
        if in_stock:
            trial, trial_choice = once(in_stock)
            if _usable_without_low(trial, full):
                log.debug("在庫考慮: 在庫薄 %s 種を外して選べた", len(low))
                return BoardSelection(trial, trial_choice, low, avoided_low=True)
        log.debug("在庫考慮: 在庫薄を外すと選べないので、在庫薄も入れて選んだ")
    return BoardSelection(full, full_choice, low, avoided_low=False,
                          low_used=_low_used(full, low))
