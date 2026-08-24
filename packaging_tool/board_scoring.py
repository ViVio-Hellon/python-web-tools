"""ボード候補のスコアリング・向き判定 (VBA `MaterialMasterForm` の一部)

`SelectLowerBoards`/`SelectUpperBoards`(次フェーズで移植)の両方から
共通で使われる中核ロジック。実ソースを1行ずつ確認して移植した。

対応関係:
    SortBoardsByTargetWidth   -> sort_boards_by_target_width
    GetBestOrientation         -> get_best_orientation
    GetBestOrientationPublic    -> get_best_orientation(force_category="上用")
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import angle_service, location_service
from .logging_utils import get_logger
from .models import BoardModel

log = get_logger("board_scoring")

# MaterialMasterForm 変数宣言(整理版)より
CHECK_STEP_SIZE = 20            # 配置チェックのステップサイズ(mm)
LOWER_OVERHANG_Y = 1.2          # 下用Y方向はみ出し20%許容
UPPER_WIDTH_TOLERANCE = 80      # 上用: 製品幅より最大80mm小さくてもOK(両端-40)
LOWER_WIDTH_TOLERANCE = 50      # 下用: パレット幅より最大50mmはみ出しOK(両端+25)
PASS1_TOLERANCE = 11            # 下用PASS1の幅一致許容量(mm)
WIDTH_OVERSHOOT_LIMIT = 3       # 幅超過フィルタ閾値(mm)
MAX_WIDTH_STRIPS = 3            # 横一列に並べる最大ストリップ数(メイン含む)
FILL_SIZE_100 = 100             # 補填ボードのサイズ(大)
FILL_SIZE_50 = 50               # 補填ボードのサイズ(中)
FILL_SIZE_30 = 30               # 補填ボードのサイズ(小)
STOCK_BONUS = 1_000_000_000.0   # 在庫あり優先用
WIDTH_SCORE_MULTIPLIER = 10_000  # STOCK_BONUS(10億)より十分小さい値に保つこと(modAngleSelectで定義)


@dataclass
class FatigueEntry:
    """VBA `BuildFatigueMap3`/`FatigueMapGet4` が返す4値タプルの移植。"""

    dist: float
    area: float
    width_cut: float
    length_cut: float


def estimate_cut_fatigue(
    width: int, length: int, target_width: int, base_len_for_cut: int,
    board_category: str,
) -> tuple[float, float]:
    """この向きで置いたときに発生するカット疲労(幅1枚分, 丈1回分)。

    VBA `AddFatigueEntry3` の後半の移植。

    カットが起きるかは**選定してみないと分からない**ように見えるが、
    VBAは選定の前に「この寸法をこの向きで使うならカットが要るか」を
    推定してマップに入れ、それを見て選定する。カットが要る組み合わせを
    最初から不利に評価するための作りで、「カットするくらいなら遠くから
    運ぶ」という現場ルールはここで効く。

        幅カット: 有効幅が目標幅を超えるなら発生(全数カットなので1枚分を
                  持ち、合算時に枚数を掛ける)。切断線の長さは有効丈
        丈カット: 基準丈を有効丈で割り切れず、端数が出るなら発生
                  (最後の1枚だけなので常に1回分)。切断線の長さは有効幅

    `base_len_for_cut` は 下用=パレット丈 / 上用=製品丈(VBA踏襲)。
    """
    board = BoardModel(width=width, length=length)
    _rot, eff_w, eff_l = get_best_orientation(
        board, target_width, force_category=board_category)

    width_cut = angle_service.fat_cut_score1(eff_l) if eff_w > target_width else 0.0

    length_cut = 0.0
    if eff_l > 0 and base_len_for_cut > 0:
        est_count = max(1, -(-base_len_for_cut // eff_l))   # 切り上げ
        if est_count * eff_l > base_len_for_cut and base_len_for_cut % eff_l != 0:
            length_cut = angle_service.fat_cut_score1(eff_w)
    return width_cut, length_cut


def build_fatigue_map(
    conn: sqlite3.Connection, boards: list[BoardModel], base_point: str,
    *, board_type: str = "", target_width: int = 0,
    base_len_for_cut: int = 0, board_category: str = "",
) -> dict[str, FatigueEntry]:
    """VBA `BuildFatigueMap3` の移植。

    サイズ("WxL")ごとに、拠点からの最寄り棚距離(距離スコア)・面積スコア・
    幅カット(1枚分)・丈カット(1回分)を計算する。合算は `total_fatigue`。

    VBA同様、**正キーと反転キーの両方**を登録する(同じ板でも向きが違えば
    カットの有無が変わるので、向きごとに別のエントリになる)。

    `target_width` / `base_len_for_cut` を省略するとカットの推定はしない
    (距離と面積だけ)。棚検索の絞り込みには `board_type` を使う。

    `boards` の要素は `width`/`length` を持てばよく、`BoardModel` でも
    `board_selection_service.BoardRow` でも渡せる。
    """
    result: dict[str, FatigueEntry] = {}

    def add(width: int, length: int) -> None:
        key = f"{width}x{length}"
        if key in result:
            return
        nearest = location_service.find_nearest_shelf_for_board(
            conn, width, length, base_point, board_type,
        )
        width_cut = length_cut = 0.0
        if target_width > 0:
            width_cut, length_cut = estimate_cut_fatigue(
                width, length, target_width, base_len_for_cut, board_category)
        result[key] = FatigueEntry(
            dist=angle_service.fat_dist_score(nearest.distance),
            area=angle_service.fat_area_score1(width, length),
            width_cut=width_cut, length_cut=length_cut)

    for board in boards:
        add(board.width, board.length)
        add(board.length, board.width)
    return result


def build_selection_fatigue_maps(
    conn: sqlite3.Connection, boards: list, base_point: str, *,
    board_type: str = "",
    pallet_width: int = 0, pallet_length: int = 0,
    product_width: int = 0, product_length: int = 0,
) -> tuple[Optional[dict[str, FatigueEntry]], Optional[dict[str, FatigueEntry]]]:
    """自動選定に渡す下用・上用の疲労度マップ。

    VBA `btnAutoSelect_Click` は `BuildFatigueMap3` を下用・上用で
    別々に呼ぶ。基準になる幅・丈が違うためで、この幅・丈が
    「カットが要るか」の推定に使われる:

        下用: 目標幅=パレット幅  基準丈=パレット丈
        上用: 目標幅=製品幅      基準丈=製品丈

    **片方だけ成功した状態を返さない。** 棚データが引けないときは
    どちらも `None` にして通常選定へ倒す。半分だけ疲労度が効いた選定は
    「疲労度優先」でも「通常」でもなく、どちらの理屈でも説明できない
    結果になる(片方の失敗でもう片方が残る作りになっていた)。

    呼び出し側(tkinter版の画面 / Web版のセッション)が同じものを見るよう、
    2か所に書かずここに置く。
    """
    try:
        lower = build_fatigue_map(
            conn, boards, base_point, board_type=board_type,
            target_width=pallet_width, base_len_for_cut=pallet_length,
            board_category="下用")
        upper = build_fatigue_map(
            conn, boards, base_point, board_type=board_type,
            target_width=product_width, base_len_for_cut=product_length,
            board_category="上用")
    except Exception as exc:                      # noqa: BLE001 - 疲労度は補助情報
        # VBA版も「マップ取得失敗 → 通常選択で実行」とフォールバックする
        log.warning("疲労度マップ取得失敗: %s", exc)
        return None, None
    return lower, upper


def total_fatigue(
    fatigue_map: Optional[dict[str, FatigueEntry]], key: str, count: int,
) -> float:
    """VBA `CalcTotalFat3` の移植(統一式による合算)。

        総疲労 = 距離x1 + 面積x枚数 + 幅カットx枚数 + 丈カットx1

    マップに該当キーが無ければ0を返す(疲労度不明=罰則なしとして扱う)。
    """
    if fatigue_map is None:
        return 0.0
    entry = fatigue_map.get(key)
    if entry is None:
        return 0.0
    n = max(count, 1)
    return entry.dist + entry.area * n + entry.width_cut * n + entry.length_cut


def sort_boards_by_target_width(
    available_boards: list[BoardModel],
    target_width: int,
    *,
    strict: bool = False,
    fatigue_map: Optional[dict[str, FatigueEntry]] = None,
    base_length: int = 0,
    stock_aware: bool = False,
    log_label: str = "",
) -> list[BoardModel]:
    """VBA `SortBoardsByTargetWidth` の移植。

    候補ボードを対象幅への適合度でスコアリングし、降順に並べ替えて
    返す(同一W×Lは重複除去し初出のみ残す)。`strict=True`(上用向け)は
    `target_width`を超えるボードを候補外にする。`strict=False`(下用向け)
    は`LOWER_OVERHANG_Y`(20%)までのはみ出しを許容する。

    `log_label` を渡すと、上位5件をスコア付きで選定ログへ出す
    (現場の声:「スコアでやるとユーザーが納得しやすい」)。ここに出る
    順位が最終的にそのまま採用されるとは限らない(この後さらに
    PASS内の判定が続く)が、「なぜこの並び順になったか」の手がかりになる。
    """
    if not available_boards:
        return []

    fit_limit = target_width if strict else int(target_width * LOWER_OVERHANG_Y)

    scored: list[tuple[float, BoardModel, int, int]] = []
    for board in available_boards:
        w1, l1 = board.width, board.length
        w2, l2 = board.length, board.width  # 回転した場合

        # 【バグ修正】以前は fit_limit 以下の候補のうち「大きい方」を無条件で
        # 採用していた。strict(上用)では fit_limit==target_width なので
        # 「大きい方」と「target_widthに近い方」は一致するが、非strict
        # (下用、fit_limit=target_width×1.2)では一致しない。target_width
        # を20%超えてはみ出す向きのほうが、target_widthに収まりきらないが
        # 近い向きより「大きい」という理由だけで選ばれ、しかもはみ出し側は
        # 直後の不足ペナルティ(gap_ratio)が掛からないため無罰則の好条件
        # として誤って高スコアになっていた。実際の選定(`get_best_orientation`
        # 経由の `_orient_lower`)は常に「target_widthに最も近い向き」を
        # 採用するため、ランキングの基準と実際の評価の基準が食い違い、
        # 本来先に評価されるべき候補が並び順で後回しにされることがあった。
        # target_widthに最も近い向きを採用するよう揃える(同点はw1優先)
        best_w = best_l = 0
        best_dist: Optional[float] = None
        if w1 <= fit_limit:
            best_w, best_l, best_dist = w1, l1, abs(target_width - w1)
        if w2 <= fit_limit:
            dist2 = abs(target_width - w2)
            if best_dist is None or dist2 < best_dist:
                best_w, best_l, best_dist = w2, l2, dist2

        if best_w > 0:
            score = best_w * WIDTH_SCORE_MULTIPLIER + best_l
            if target_width > 0:
                gap_ratio = (target_width - best_w) / target_width
                if gap_ratio > 0:
                    score *= 1 - gap_ratio * 0.5

            key = f"{board.width}x{board.length}"
            if fatigue_map is not None and key in fatigue_map:
                expected_cnt = 1
                if base_length > 0 and best_l > 0:
                    expected_cnt = max(1, -(-base_length // best_l))  # 切り上げ除算
                fat_penalty = 100.0 / (100.0 + total_fatigue(fatigue_map, key, expected_cnt))
                score *= fat_penalty
        else:
            score = -1.0

        if stock_aware and score > 0 and not board.stock_low:
            score += STOCK_BONUS  # 在庫あり -> 最上位層へ

        scored.append((score, board, best_w, best_l))

    # VBA版はバブルソート(安定ソート)。Pythonのsortも安定ソートなので、
    # 同点時の順序(availableBoardsに現れた順)がそのまま保たれる。
    scored.sort(key=lambda item: item[0], reverse=True)

    if log_label:
        _log_top_candidates(log_label, scored, base_length)

    result: list[BoardModel] = []
    seen: set[tuple[int, int]] = set()
    for _, board, _best_w, _best_l in scored:
        key = (board.width, board.length)
        if key not in seen:
            seen.add(key)
            result.append(board)
    return result


def _log_top_candidates(
    label: str,
    scored: list[tuple[float, BoardModel, int, int]],
    base_length: int,
    limit: int = 5,
) -> None:
    shown = [item for item in scored if item[0] > 0][:limit]
    if not shown:
        return
    log.debug("%sソート上位%s件:", label, len(shown))
    for i, (score, board, best_w, best_l) in enumerate(shown, start=1):
        expected_cnt = 1
        if base_length > 0 and best_l > 0:
            expected_cnt = max(1, -(-base_length // best_l))  # 切り上げ除算
        log.debug("  %s. %sx%s effW=%s score=%s x%s枚",
                 i, board.width, board.length, best_w, round(score), expected_cnt)


def get_best_orientation(
    board: BoardModel, target_width: int, *, force_category: str = "",
) -> tuple[bool, int, int]:
    """VBA `GetBestOrientation`(および`GetBestOrientationPublic`)の移植。

    戻り値は (回転したか, 有効幅, 有効丈)。
    下用(`LOWER_OVERHANG_Y`まではみ出し許容) / 上用(製品幅超過厳禁)で
    フィット判定の基準が異なる。
    """
    w1, l1 = board.width, board.length
    w2, l2 = board.length, board.width

    effective_category = force_category or board.board_category
    if effective_category == "下用":
        fit_limit = int(target_width * LOWER_OVERHANG_Y)
    else:
        fit_limit = target_width  # 上用: 製品幅超過厳禁

    fit1 = w1 <= fit_limit
    fit2 = w2 <= fit_limit

    if fit1 and fit2:
        if abs(target_width - w1) <= abs(target_width - w2):
            return False, w1, l1
        return True, w2, l2
    if fit1:
        return False, w1, l1
    if fit2:
        return True, w2, l2
    if w1 <= w2:
        return False, w1, l1
    return True, w2, l2
