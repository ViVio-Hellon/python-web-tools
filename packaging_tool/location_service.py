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
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from . import angle_service, db, floor_plan, user_settings
from .logging_utils import get_logger

log = get_logger("location_service")


@dataclass
class BasePoint:
    """いま距離を測っている拠点と、**それが誰の決めた拠点か**。

    距離も疲労度もここを原点に測るので、拠点が違えば答えが全部変わる。
    VBAは拠点が選ばれていないとき「拠点未選択 → クリック位置で計算」と
    その場に書いていた(`FindNearestSameSize` / `CalcDistance`)。
    こちらは押した場所という概念が無いかわりに既定の拠点で測るので、
    **測っていること自体は同じでも、誰が決めた拠点なのかを言う**。
    """

    name: str
    chosen: bool   # 人が設定画面で選んだか(Falseなら既定を当てている)
    known: bool    # その名前が配置図にあるか

    @property
    def note(self) -> str:
        """画面に添える一言。**言うことが無ければ空。**"""
        if not self.known:
            return (f"拠点「{self.name}」が配置図にありません。"
                    "棚検索の「配置編集」で図に置くか、設定画面で選び直してください"
                    "(距離を測る原点です)。")
        if not self.chosen:
            return (f"拠点が未登録なので {self.name} から測っています。"
                    "設定画面で選ぶと、自分の持ち場からの距離になります。")
        return ""


def current_base_point() -> BasePoint:
    """設定の拠点と配置図を突き合わせる。"""
    name = user_settings.get_position()
    return BasePoint(name=name,
                     chosen=user_settings.is_position_set(),
                     known=name in floor_plan.load().base_point_names)


# ------------------------------------------------------------------
# 拠点をマスタで足す(VER4.8.62〜)
# ------------------------------------------------------------------
# 現場の声:「拠点ラベルの追加を BoardMaster に設定し(データラベル列: 拠点L-1, 拠点LVC,
# 拠点HVC。例: 追加するとして 拠点大板 と入力)、最寄り置き場の検索の拠点のコンボボックスに
# 追加、配置編集にも拠点の赤色ラベルで出現し配置できる」。
#
#   BoardMaster のデータラベルに「拠点」で始まる字を書くと、その後ろが拠点の名前になる。
#   拠点だけを書く行は ボード幅・丈 を 0、ボードタイプを「拠点」にする(ボードの候補には出ない)。
#   座標は配置図(配置編集)が持つ。マスタにあって図に無い拠点は、配置編集に入ると図に出る。
#
# 名前は**全角半角・ハイフン・空白・大小文字を見ずに**突き合わせる。「拠点L-1」は図の「L1」と
# 同じ拠点(図の名前を使う。設定に保存した拠点名・距離が変わらないように)
BASE_PREFIX = "拠点"


def base_key(name: str) -> str:
    """拠点名を突き合わせるための形(`L-1` と `L1`、`ＨＶＣ` と `HVC` を同じに)。"""
    text = unicodedata.normalize("NFKC", name or "")
    for ch in ("-", "ー", "‐", "−", "_", " ", "\u3000"):
        text = text.replace(ch, "")
    return text.upper()


def _tokens(data_label: Optional[str]) -> list[str]:
    if not data_label:
        return []
    return [t.strip() for t in str(data_label).split(",") if t.strip()]


def _base_name_of(token: str) -> Optional[str]:
    """「拠点大板」→「大板」。拠点の印でなければ None。"""
    text = unicodedata.normalize("NFKC", token).strip()
    if text.startswith(BASE_PREFIX):
        name = text[len(BASE_PREFIX):].strip()
        return name or None
    return None


def _split_labels(data_label: Optional[str]) -> list[str]:
    """置き場のラベルだけ(拠点の印は除く ── 拠点は置き場ではない)。"""
    return [t for t in _tokens(data_label) if _base_name_of(t) is None]


def master_base_points(conn: Optional[sqlite3.Connection]) -> list[str]:
    """BoardMaster のデータラベルに書かれた拠点(書かれた順、重複なし)。"""
    if conn is None:
        return []
    try:
        rows = conn.execute("SELECT データラベル FROM BoardMaster ORDER BY 管理番号").fetchall()
    except sqlite3.Error as exc:
        log.warning("BoardMaster から拠点を読めません: %s", exc)
        return []
    names: list[str] = []
    keys: set[str] = set()
    for row in rows:
        for token in _tokens(row[0]):
            name = _base_name_of(token)
            if name and base_key(name) not in keys:
                keys.add(base_key(name))
                names.append(name)
    return names


def base_point_names(conn: Optional[sqlite3.Connection] = None) -> list[str]:
    """選べる拠点。配置図の拠点 + マスタで足した拠点(図と同じものは図の名前で1つ)。"""
    plan_names = list(floor_plan.base_points())
    keys = {base_key(n) for n in plan_names}
    return plan_names + [n for n in master_base_points(conn) if base_key(n) not in keys]


def unplaced_base_points(conn: Optional[sqlite3.Connection]) -> list[str]:
    """マスタにあって、まだ配置図に置いていない拠点(距離を測れない)。"""
    keys = {base_key(n) for n in floor_plan.load().base_point_names}
    return [n for n in master_base_points(conn) if base_key(n) not in keys]


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
    # 看板が出ている(切れている)ので外した置き場(在庫考慮。VER4.8.68)
    skipped: tuple[str, ...] = ()


def _nearest_among(label_names: list[str], base_point: str) -> NearestShelfResult:
    """候補ラベルのうち拠点から一番近いものを返す(VBA `FindNearestSameSize`)。"""
    plan = floor_plan.load()
    name, dist, count = plan.nearest(sorted(set(label_names)), base_point)
    return NearestShelfResult(name, dist or 0.0, count)


def find_nearest_shelf_for_board(
    conn: sqlite3.Connection, width: int, length: int, base_point: str, board_type: str = "",
    *, skip: Iterable[str] = (),
) -> NearestShelfResult:
    """VBA `FindNearestSameSize`(ボード用)の移植。

    `BoardMaster` から同一サイズ(向き入れ替え含む)の行を探し、その
    `データラベル`(`lblItem*`)が配置図のどこにあるかを見て、拠点から
    最も近いものを返す。ラベルの座標は `floor_plan` が持つ。

    `skip` は看板が出ている(切れている)置き場(在庫考慮。VER4.8.68)。そこを外して、残りでいちばん
    近い置き場を返す ── 切れている棚には取りに行かない。**全部切れているときは外さない**
    (どこかで取るしかない。その板は在庫薄として選定が避ける)。
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
    out = set(skip or ())
    left = [name for name in labels if name not in out]
    if out and left and len(left) < len(labels):
        nearest = _nearest_among(left, base_point)
        nearest.skipped = tuple(sorted(set(labels) & out))
        return nearest
    return _nearest_among(labels, base_point)


@dataclass
class SizeSearchHit:
    """片側だけ・あいまいでも探せる検索の、1種別ぶんの答え。"""

    kind: str                      # "board" / "angle"
    nearest: NearestShelfResult
    matched_sizes: list[str]       # 当たった寸法(画面に出して確かめてもらう)

    @property
    def found(self) -> bool:
        return self.nearest.shelf_name is not None


def find_shelves_by_size(
    conn: sqlite3.Connection, width: Optional[int], length: Optional[int],
    base_point: str, board_type: str = "",
) -> list[SizeSearchHit]:
    """**幅か丈のどちらかだけでも探す。ボードとアングルを一度に見る。**

    以前は「ボードなら幅と丈の両方が要る」「アングルなら丈だけ」で、
    しかも種別を先に選ばせていた。手元に片方の寸法しか分からないときに
    打ちようがなく、種別を選び直すのも手間になっていた
    (現場の指摘:「両方の入力が必須になっていて使いにくい」
    「両方チェックとか両方でいいのでは」)。

    渡された寸法だけで絞る。幅だけならその幅の板を全部、丈だけなら
    その丈の板とアングルを全部。**向きは入れ替えても当てる**
    (1000x2000 を探すのに 2000x1000 が出ないと、探せないのと同じ)。
    """
    hits: list[SizeSearchHit] = []
    if width is None and length is None:
        return hits

    # --- ボード -----------------------------------------------------
    where, params = _board_where(width, length)
    if where:
        if board_type:
            where += " AND ボードタイプ = ?"
            params.append(board_type)
        rows = db.fetch_all(
            conn,
            f"SELECT ボード幅, ボード丈, データラベル FROM BoardMaster WHERE {where}",
            params, caller_name="find_shelves_by_size") or []
        labels: list[str] = []
        sizes: set[str] = set()
        for row in rows:
            labels.extend(_split_labels(row["データラベル"]))
            sizes.add(f"{row['ボード幅']}×{row['ボード丈']}")
        if rows:
            hits.append(SizeSearchHit("board", _nearest_among(labels, base_point),
                                      sorted(sizes)))

    # --- アングル ---------------------------------------------------
    # アングルは丈しか持たない。**打たれた数のどれかに当たれば出す。**
    # ボード側は向きを入れ替えて当てるので、アングルだけ「幅の欄に
    # 打ったから当たらない」では不揃いになる(1130 と打ったのに
    # 1130のアングルが出てこない)
    values = sorted({v for v in (width, length) if v is not None})
    if values:
        marks = ", ".join("?" for _ in values)
        rows = db.fetch_all(
            conn,
            "SELECT アングル丈, データラベル FROM CornerboardMaster"
            f" WHERE アングル丈 IN ({marks})", list(values),
            caller_name="find_shelves_by_size") or []
        labels = []
        sizes = set()
        for row in rows:
            labels.extend(_split_labels(row["データラベル"]))
            sizes.add(f"{row['アングル丈']}")
        if rows:
            hits.append(SizeSearchHit("angle", _nearest_among(labels, base_point),
                                      sorted(sizes)))
    return hits


def _board_where(width: Optional[int],
                 length: Optional[int]) -> tuple[str, list]:
    """渡された寸法だけで絞る条件。**向きの入れ替えも当てる。**"""
    if width is not None and length is not None:
        return ("((ボード幅 = ? AND ボード丈 = ?) OR (ボード幅 = ? AND ボード丈 = ?))",
                [width, length, length, width])
    only = width if width is not None else length
    if only is None:
        return "", []
    return "(ボード幅 = ? OR ボード丈 = ?)", [only, only]


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

    VBA `OpenSizePopup` → `frmSizePopup.SetListData` に当たる。
    BoardMaster/CornerboardMaster の「データラベル」列に自分の名前が
    含まれる行を集めるだけで、`find_nearest_shelf_for_*` と同じデータ
    ソースを使う。

    **原文と違って、ボードとアングルを両方返す。** 原文はアングルの
    ある置き場でボードの一覧を隠していた(`ShowAngleSection` の
    `mAngleOnly`)ので、両方置いてある場所ではボードが見えなかった。
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
def carry_board_score(width: int, length: int, count: int = 1) -> float:
    """ボードを運ぶぶん(距離を除く): (面積 + 手間)x枚数。"""
    n = max(count, 1)
    return (angle_service.fat_area_score1(width, length) + angle_service.FAT_SHEET_SCORE) * n


def carry_angle_score(angle_length: int, count: int = 1) -> float:
    """アングルを運ぶぶん(距離を除く): (長さ + 手間)x本数。"""
    n = max(count, 1)
    return (angle_service.fat_angle_length_score1(angle_length) + angle_service.FAT_SHEET_SCORE) * n


def score_board_pick(dist: float, width: int, length: int, count: int = 1) -> float:
    """VBA `AddFatigue` の移植(距離x1 + (面積 + 手間)x枚数)。

    棚から取ってくるぶん(運搬)だけ。カットは入れない ── 棚配置図は
    「全部でどれだけ歩いて運ぶか」を見る画面。
    """
    return angle_service.fat_dist_score(dist) + carry_board_score(width, length, count)


def score_angle_pick(dist: float, angle_length: int, count: int = 1) -> float:
    """VBA `AddAngleFatigue` の移植(距離1回 + (長さ + 手間)x本数)。"""
    return angle_service.fat_dist_score(dist) + carry_angle_score(angle_length, count)


def build_angle_fatigue_map(
    conn: sqlite3.Connection, angles: Sequence[int], base_point: str,
) -> dict[str, float]:
    """VBA `btnAngleAuto_Click` の `angleFatMap` 構築の移植。

    候補アングル丈ごとに**拠点からの距離スコアだけ**を出し、
    `angle_service.select_angles` に渡せる形(キーは丈の文字列)にする。
    棚が引けない丈は入れない(疲労度不明として通常の優先順位に任せる)。

    【修正 VER4.8.61】以前は「距離 + 長さ1本分」を入れていた。受け取る側
    (`angle_service._agl_fat_score`)が長さx本数を自分で足すので、長さが
    本数+1回入っていた(2500x1本 と 1300x2本 の順位が逆転していた)。
    """
    fat_map: dict[str, float] = {}
    for length in angles:
        key = str(length)
        if length <= 0 or key in fat_map:
            continue
        nearest = find_nearest_shelf_for_angle(conn, length, base_point)
        if nearest.shelf_name is None:
            continue
        fat_map[key] = angle_service.fat_dist_score(nearest.distance)
    log.debug("build_angle_fatigue_map: %s件", len(fat_map))
    return fat_map
