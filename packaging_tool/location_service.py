"""棚(拠点)配置図・疲労度スコアリング (VBA `frmLayout` の移植)

`frmLayout` は配置図の画像を背景にしたフォームで、置き場ごとに置かれた
小さなLabelコントロール(`lblItem1`〜`lblItem35`)の座標から、拠点
(L1/HVC/LVC)までの距離を測り、取り出しやすさを疲労度として計算していた。
何がどこにあるかは `BoardMaster`/`CornerboardMaster` の「データラベル」列
(ラベル名をカンマ区切りで持つ)が決める。

ラベルの座標は `floor_plan` がJSONで保持する。コードに埋めていないので、
置き場が変わっても「資材配置」タブの配置編集でラベルを動かすだけで
距離計算まで追従する(VBAでフォームデザイナー上のラベルをずらしていたのと
同じ運用を、アプリ内でできるようにしたもの)。

VBA版はハイライト状態をラベルの背景色に直接書き込む副作用的なSubの
集まりだったが、Python版は「最寄り棚+距離+スコア」を返す純粋関数として
実装し、描画はUI層(tkinter Canvas)が行う。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional, Sequence

from . import angle_service, db, floor_plan
from .logging_utils import get_logger

log = get_logger("location_service")


def _split_labels(data_label: Optional[str]) -> list[str]:
    if not data_label:
        return []
    return [t.strip() for t in data_label.split(",") if t.strip()]


def list_all_data_labels(conn: sqlite3.Connection) -> set[str]:
    """BoardMaster/CornerboardMasterに登場する全データラベルトークン。"""
    labels: set[str] = set()
    for table, col in (("BoardMaster", "データラベル"), ("CornerboardMaster", "データラベル")):
        rows = db.fetch_all(conn, f"SELECT [{col}] AS v FROM [{table}]", caller_name="list_all_data_labels") or []
        for row in rows:
            labels.update(_split_labels(row["v"]))
    return labels


# 距離計算(VBA `CalcDistance`)は `floor_plan.FloorPlan.distance` が本体。
# 中心ではなく左上どうしの差で測るのもVBAのまま(疲労度の数値を揃えるため)。


@dataclass
class NearestShelfResult:
    shelf_name: Optional[str]
    distance: float
    candidate_count: int


def _nearest_among(label_names: list[str], base_point: str) -> NearestShelfResult:
    """候補ラベルのうち拠点から一番近いものを返す(VBA `FindNearestSameSize`)。"""
    plan = floor_plan.load()
    name, dist, count = plan.nearest(sorted(set(label_names)), base_point)
    return NearestShelfResult(name, dist or 0.0, count)


def find_nearest_shelf_for_board(
    conn: sqlite3.Connection, width: int, length: int, base_point: str, board_type: str = "",
) -> NearestShelfResult:
    """VBA `FindNearestSameSize`(ボード用)の移植。

    `BoardMaster` から同一サイズ(向き入れ替え含む)の行を探し、その
    `データラベル`(`lblItem*`)が配置図のどこにあるかを見て、拠点から
    最も近いものを返す。ラベルの座標は `floor_plan` が持つ。
    """
    where = "((ボード幅 = ? AND ボード丈 = ?) OR (ボード幅 = ? AND ボード丈 = ?))"
    params: list = [width, length, length, width]
    if board_type:
        where += " AND ボードタイプ = ?"
        params.append(board_type)
    rows = db.fetch_all(
        conn, f"SELECT データラベル FROM BoardMaster WHERE {where}", params,
        caller_name="find_nearest_shelf_for_board",
    ) or []

    labels: list[str] = []
    for row in rows:
        labels.extend(_split_labels(row["データラベル"]))
    return _nearest_among(labels, base_point)


def find_nearest_shelf_for_angle(
    conn: sqlite3.Connection, angle_length: int, base_point: str,
) -> NearestShelfResult:
    """VBA `FindNearestSameSize`(アングル用)の移植。"""
    rows = db.fetch_all(
        conn, "SELECT データラベル FROM CornerboardMaster WHERE アングル丈 = ?",
        (angle_length,), caller_name="find_nearest_shelf_for_angle",
    ) or []
    labels: list[str] = []
    for row in rows:
        labels.extend(_split_labels(row["データラベル"]))
    return _nearest_among(labels, base_point)


def unplaced_data_labels(conn: sqlite3.Connection) -> list[str]:
    """マスタが参照しているのに配置図に無いラベル(配置編集で足す目安)。"""
    plan = floor_plan.load()
    known = set(plan.item_names) | set(plan.base_point_names)
    return sorted(list_all_data_labels(conn) - known)


# BoardMaster.ボードタイプ → Board MAPで表示するカテゴリ名。
# 「プロテック」「IK」はBoardMaster内の種別区分であって別マスタでは
# 無いので、ここで束ねる(現場の「アングル/ボード/プロテック/IK」の
# 4分類に合わせる)
BOARD_TYPE_TO_CATEGORY = {
    "ハードボード": "ボード",
    "IKボード": "IK",
    "プロテックボード": "プロテック",
}


def label_categories(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """置き場ラベルがどの資材カテゴリ(アングル/ボード/プロテック/IK)に
    使われているかを返す(Board MAPの色分け用)。

    VBA版には無い機能。1つのラベルが複数カテゴリを兼ねていることも
    あるため(同じ置き場にサイズ違いのボードとプロテックを置く運用など)、
    値は集合で返す。
    """
    mapping: dict[str, set[str]] = {}
    rows = db.fetch_all(
        conn, "SELECT データラベル, ボードタイプ FROM BoardMaster",
        caller_name="label_categories") or []
    for row in rows:
        category = BOARD_TYPE_TO_CATEGORY.get(row["ボードタイプ"])
        if not category:
            continue
        for label in _split_labels(row["データラベル"]):
            mapping.setdefault(label, set()).add(category)

    rows = db.fetch_all(
        conn, "SELECT データラベル FROM CornerboardMaster",
        caller_name="label_categories") or []
    for row in rows:
        for label in _split_labels(row["データラベル"]):
            mapping.setdefault(label, set()).add("アングル")
    return mapping


@dataclass
class ShelfMaterial:
    category: str            # "アングル" / "ボード" / "プロテック" / "IK"
    width: Optional[int]     # アングルは幅を持たないのでNone
    length: int


def materials_at_label(conn: sqlite3.Connection, label: str) -> list[ShelfMaterial]:
    """置き場ラベルに割り当てられている資材の一覧。

    VBA版には無い機能(ユーザー要望: Board MAP上のラベルをクリックしたら
    そこにある資材が分かるようにしてほしい)。BoardMaster/CornerboardMaster
    の「データラベル」列に自分の名前が含まれる行を集めるだけで、
    `find_nearest_shelf_for_*` と同じデータソースを使う。
    """
    result: list[ShelfMaterial] = []
    rows = db.fetch_all(
        conn, "SELECT ボード幅, ボード丈, ボードタイプ, データラベル FROM BoardMaster",
        caller_name="materials_at_label") or []
    for row in rows:
        if label not in _split_labels(row["データラベル"]):
            continue
        category = BOARD_TYPE_TO_CATEGORY.get(row["ボードタイプ"], row["ボードタイプ"])
        result.append(ShelfMaterial(category=category, width=row["ボード幅"], length=row["ボード丈"]))

    rows = db.fetch_all(
        conn, "SELECT アングル丈, データラベル FROM CornerboardMaster",
        caller_name="materials_at_label") or []
    for row in rows:
        if label not in _split_labels(row["データラベル"]):
            continue
        result.append(ShelfMaterial(category="アングル", width=None, length=row["アングル丈"]))
    return result


# ------------------------------------------------------------------
# 疲労度スコア (VBA `AddFatigue`/`AddAngleFatigue`/`AddCutFatigue`/`AddLengthCutFatigue`)
# ------------------------------------------------------------------
def score_board_pick(dist: float, width: int, length: int, count: int = 1) -> float:
    """VBA `AddFatigue` の移植(距離x1 + 面積x枚数)。"""
    return angle_service.fat_dist_score(dist) + angle_service.fat_area_score1(width, length) * count


def score_angle_pick(dist: float, angle_length: int, count: int = 1) -> float:
    """VBA `AddAngleFatigue` の移植(距離1回・長さ×本数)。"""
    length_score = min((angle_length / angle_service.FAT_MAX_LEN) * angle_service.FAT_SCORE_CAP, angle_service.FAT_SCORE_CAP)
    return angle_service.fat_dist_score(dist) + length_score * count


def build_angle_fatigue_map(
    conn: sqlite3.Connection, angles: Sequence[int], base_point: str,
) -> dict[str, float]:
    """VBA `btnAngleAuto_Click` の `angleFatMap` 構築の移植。

    候補アングル丈ごとに「拠点からの距離 + 長さ」の疲労度を出し、
    `angle_service.select_angles` に渡せる形(キーは丈の文字列)にする。
    棚が引けない丈は入れない(疲労度不明として通常の優先順位に任せる)。
    """
    fat_map: dict[str, float] = {}
    for length in angles:
        key = str(length)
        if length <= 0 or key in fat_map:
            continue
        nearest = find_nearest_shelf_for_angle(conn, length, base_point)
        if nearest.shelf_name is None:
            continue
        fat_map[key] = score_angle_pick(nearest.distance, length)
    log.debug("build_angle_fatigue_map: %s件", len(fat_map))
    return fat_map
