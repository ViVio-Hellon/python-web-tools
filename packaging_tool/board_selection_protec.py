"""プロテック専用の選定 ── 上下で同じボードを使い、幅は製品幅を超えない

プロテックは包装仕様NO(1P1216 / 1P1125 / 1P1211)で決まるモードで、
通常の選定とは基準が違う。

  * 幅は**製品幅**が基準(パレット幅ではない)
  * 製品幅の**超過は禁止**。マイナス側だけ許す
  * 許容は 1P1216 だけ -10mm、ほかは -80mm
  * 上用は下用と**同じボード**を使う

決めた値は `ProtecCutResult` に入れて持ち回る ── **唯一の正解**を1つ
置いて、配置もカット依頼書もそれを読むだけにする。以前はそれぞれが
「製品幅を超えてよいか」を判定し直していて、選定結果と食い違うことが
あった(上用が静かに配置から落ちる不具合の原因)。
"""
from __future__ import annotations

from typing import Optional

from .board_scoring import get_best_orientation
from .board_selection_common import (FILL_SIZE_100,
                                     LENGTH_CUT_REMAIN_THRESHOLD,
                                     TAG_CUT_PREMISE, TAG_MAIN)
from .board_selection_service import Palette, ProductSize, SelectedBoard
from .board_selection_types import ProtecCutResult, ProtecLengthCut
from .logging_utils import get_logger
from .models import BoardModel

log = get_logger("board_selection.protec")

# にも同じ値を使う ── 在庫を候補にするかどうかの判定と、カット後の
# 仕上がりサイズは、同じ「製品幅からどれだけマイナスまで許すか」
# という1つの数直線上の話なので、別の値を持たせない
PROTEC_1P1216_TOLERANCE = 10
PROTEC_OTHER_TOLERANCE = 80




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




# ------------------------------------------------------------------
def compute_protec_length_cut(
    eff_length: int, product_length: int, palette_max_length: int,
) -> ProtecLengthCut:
    """VBA `ComputeProtecLengthCut` の移植。**基準は製品丈。**

    フルサイズで覆える枚数を製品丈から出し、残りが100mmを超えるなら
    最後の1枚をカットして足す ── ここまでは従来どおり。

    【追加: カットが「必須」か「任意」か】
    もう1枚をフルサイズのまま足しても**パレット丈に収まる**なら、
    カットは必須ではありません。製品丈は超えますが、はみ出しては
    いないからです。この場合は

        `need_cut=False` … 配置図にはカット線を出さない(切らない姿を描く)
        `optional=True`  … 切断依頼書だけが「もし切るなら」の内訳を出す

    と分けます。**はみ出すなら訊かずに切る、はみ出さないなら訊く** ──
    現場が選べるのは後者だけで、前者は選択肢がありません。

    `palette_max_length` は `Palette.max_length`(はみ出し許容後)。
    未設定(0以下)なら判定できないので、従来どおり必須カットにします。
    """
    if eff_length <= 0:
        return ProtecLengthCut(count=1)

    full_count = max(0, product_length // eff_length)
    remain = product_length - eff_length * full_count

    if remain > LENGTH_CUT_REMAIN_THRESHOLD:
        if palette_max_length > 0 and eff_length * (full_count + 1) <= palette_max_length:
            log.debug("ComputeProtecLengthCut: 残%smm、%sx%s=%s ≦ パレット%s "
                      "のため丈カットは任意",
                      remain, eff_length, full_count + 1,
                      eff_length * (full_count + 1), palette_max_length)
            return ProtecLengthCut(
                count=max(1, full_count + 1), need_cut=False,
                normal_cnt=full_count + 1, cut_cnt=0, cut_eff=0,
                optional=True, opt_normal_cnt=full_count, opt_cut_cnt=1,
                opt_cut_eff=remain)
        return ProtecLengthCut(
            count=max(1, full_count + 1), need_cut=True,
            normal_cnt=full_count, cut_cnt=1, cut_eff=remain)

    return ProtecLengthCut(
        count=max(1, full_count), need_cut=False,
        normal_cnt=full_count, cut_cnt=0, cut_eff=0)


def _apply_length_cut(result: ProtecCutResult, cut: ProtecLengthCut) -> None:
    """`ProtecLengthCut` を確定値へ写す。**写す場所を1か所にする。**"""
    result.count = cut.count
    result.need_length_cut = cut.need_cut
    result.length_cut_eff = cut.cut_eff if cut.need_cut else result.eff_length
    result.len_normal_cnt = cut.normal_cnt
    result.len_cut_cnt = cut.cut_cnt
    result.len_cut_optional = cut.optional
    result.len_opt_normal_cnt = cut.opt_normal_cnt
    result.len_opt_cut_cnt = cut.opt_cut_cnt
    result.len_opt_cut_eff = cut.opt_cut_eff


def _palette_max_length(palette: Palette) -> int:
    """VBA `PaletteMaxLength`。未設定ならパレット丈そのもの。"""
    return int(palette.max_length) if palette.max_length > 0 else int(palette.length)


# ------------------------------------------------------------------
# プロテック専用の下用選定 (VBA `SelectProtecLowerBoards`)
# ------------------------------------------------------------------
def select_protec_lower_boards(
    available: list[BoardModel], product: ProductSize, palette: Palette,
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
    pal_max = _palette_max_length(palette)
    best: Optional[ProtecCutResult] = None
    best_fits_pallet = False
    for board in available:
        # **細い板は主ボードにしない**(VBA `SelectProtecLowerBoards` 冒頭の
        # `ab.width <= 100 Or ab.length <= 100` )。補填に使う30/50/100の帯を
        # 主ボードとして寝かせて使うと、丈方向が帯の厚みぶんずつになり、
        # 何十枚も並べる解になる。他のPASSでは元から外していたが、
        # ここだけ移植が抜けていた
        if board.width <= FILL_SIZE_100 or board.length <= FILL_SIZE_100:
            continue
        candidate = decide_protec_orientation(
            board.width, board.length, product.width, is_1p1216=is_1p1216)
        if not candidate.valid:
            continue
        # 丈が0の候補は枚数が決まらない。**先に落とす** ── そのまま
        # 進めると、丈方向に何も覆えない板が「最有力」になりうる
        if candidate.eff_length <= 0:
            log.debug("    候補 %sx%s effL=0のため除外", board.width, board.length)
            continue

        # **パレット丈に収まるか(=丈カットが要らないか)が第1の評価軸。**
        # カットの向きは1つに減らしたい ── 幅カットだけで済む候補が
        # あるのに、幅も丈も切る候補を選ぶ理由は無い(現場の指示)
        cut = compute_protec_length_cut(
            candidate.eff_length, product.length, pal_max)
        fits_pallet = pal_max <= 0 or candidate.eff_length * cut.count <= pal_max
        log.debug("    候補 %sx%s effL=%s x%s枚=%s パレット内=%s",
                  board.width, board.length, candidate.eff_length, cut.count,
                  candidate.eff_length * cut.count, fits_pallet)

        if best is None:
            best, best_fits_pallet = candidate, fits_pallet
            continue
        if fits_pallet != best_fits_pallet:
            if fits_pallet:
                best, best_fits_pallet = candidate, fits_pallet
            continue
        # 以降は同点(どちらもパレット内、またはどちらも超える)のときだけ。
        # 製品幅に最も近い(超過・不足とも小さいほど良い)ものを採用。
        # **カット前の実効幅で比べる** ── `cut_eff_width`(カット後の
        # 仕上がりサイズ)は、カットが必要な候補同士だと常に同じ値
        # (製品幅-許容)に揃ってしまい、「どちらが無駄が少ないか」を
        # 比較する基準として使えない。同点なら丈が長い方
        # (枚数が減り、継ぎ目が少ない方)を優先する
        cur_gap = abs(candidate.eff_width_before_cut - product.width)
        best_gap = abs(best.eff_width_before_cut - product.width)
        if cur_gap < best_gap or (cur_gap == best_gap and candidate.eff_length > best.eff_length):
            best, best_fits_pallet = candidate, fits_pallet

    if best is None:
        log.debug("SelectProtecLowerBoards: 条件を満たす在庫がありません")
        return [], ProtecCutResult(valid=False)

    cut = compute_protec_length_cut(best.eff_length, product.length, pal_max)
    _apply_length_cut(best, cut)
    count, need_length_cut = cut.count, cut.need_cut
    length_cut_eff = best.length_cut_eff
    if cut.optional:
        log.debug("SelectProtecLowerBoards: 丈カットは任意 (通常%s枚+カット%s枚→%smm)",
                  cut.opt_normal_cnt, cut.opt_cut_cnt, cut.opt_cut_eff)

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
    lower: list[SelectedBoard], product: ProductSize, palette: Palette,
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
        cut = compute_protec_length_cut(
            result.eff_length, product.length, _palette_max_length(palette))
        _apply_length_cut(result, cut)
        count, need_length_cut = cut.count, cut.need_cut
        # 後続処理(配置・カット依頼書)は `ProtecCutResult.count` を
        # 見るので、行自体の枚数もここで合わせておく(手動で入れた
        # 枚数のままだと、確定値と表示上の枚数が食い違う)
        target.count = count
        log.debug("ApplyProtecRulesToLowerList: 手動追加行 %sx%s に後付け適用 "
                 "cutEffW=%s needCut=%s count=%s needLengthCut=%s",
                 target.width, target.length,
                 result.cut_eff_width, result.need_cut, count, need_length_cut)
    return result
