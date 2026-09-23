"""図の上に置く「名前つきの箱」と背景画像 (VBAのフォーム配置の移植)

VBA版には座標を持つ図が2つある。

    frmLayout   棚配置図。置き場のLabelを並べ、拠点からの距離で疲労度を出す
    UFMAP       保管位置のマップ。位置ごとのCommandButtonを押して在庫を見る

どちらも「背景に写真を敷き、その上にコントロールを実際の位置へ置く」
という同じ作りで、**現場が変わったらコントロールをずらすだけ**で
追従できるのが利点だった。Python版でもその性質を保つため、座標は
ソースではなくJSONに持ち、画面上のドラッグで編集できるようにする。

このモジュールは2つの図に共通する部分(箱・背景・ファイルの読み書き)だけを
持つ。図ごとの意味づけは `floor_plan` と `pallet_map` の側にある。

座標系は元のフォームと同じ**ポイント単位・左上原点**。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from .logging_utils import get_logger

log = get_logger("map_data")


@dataclass(frozen=True)
class MapItem:
    """図の上に置く1つの箱(置き場・拠点・保管位置)。"""

    name: str
    x: float
    y: float
    w: float = 18.0
    h: float = 18.0

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    def moved_to(self, x: float, y: float) -> "MapItem":
        return replace(self, x=round(x, 1), y=round(y, 1))

    def sized_to(self, w: float, h: float) -> "MapItem":
        """大きさを変える。**左上は動かさない。**

        中心を保って伸ばすと、掴んでいた角と反対側も動きます。
        どちらが動くのか分からない操作は、合わせこみの邪魔になります。
        """
        return replace(self, w=round(w, 1), h=round(h, 1))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(frozen=True)
class Background:
    """図の下に敷く画像(あれば)。位置と倍率も持たせて合わせられるようにする。

    【なぜ位置と倍率を持つのか】
    背景は現場を撮った写真です。撮った向き・画角は図の枠と一致しない
    ので、**枠いっぱいに引き伸ばすと箱と棚がずれます**。写真の側を
    ずらして拡げ縮めできれば、箱を動かさずに下敷きを合わせられます。
    """

    image: str = ""
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0

    def placed_at(self, x: float, y: float) -> "Background":
        return replace(self, x=round(x, 1), y=round(y, 1))

    def scaled_to(self, scale: float) -> "Background":
        return replace(self, scale=round(scale, 3))

    def to_dict(self) -> dict[str, Any]:
        return {"image": self.image, "x": self.x, "y": self.y, "scale": self.scale}


def item_from_dict(data: dict[str, Any]) -> MapItem:
    return MapItem(name=str(data.get("name", "")),
                   x=float(data.get("x", 0)), y=float(data.get("y", 0)),
                   w=float(data.get("w", 18)), h=float(data.get("h", 18)))


def background_from_dict(data: Optional[dict[str, Any]]) -> Background:
    data = data or {}
    return Background(image=str(data.get("image", "")),
                      x=float(data.get("x", 0)), y=float(data.get("y", 0)),
                      scale=float(data.get("scale", 1.0)))


def move_in(groups, name: str, x: float, y: float) -> bool:
    """`groups` の中から `name` の箱を探して動かす。見つかれば True。"""
    for group in groups:
        for index, candidate in enumerate(group):
            if candidate.name == name:
                group[index] = candidate.moved_to(x, y)
                return True
    return False


def resize_in(groups, name: str, w: float, h: float) -> bool:
    """`groups` の中から `name` の箱を探して大きさを変える。"""
    for group in groups:
        for index, candidate in enumerate(group):
            if candidate.name == name:
                group[index] = candidate.sized_to(w, h)
                return True
    return False


# ------------------------------------------------------------------
# 合わせこみの上限・下限
#
# **どちらの図でも同じ**。棚検索と簡易在庫で限度が違うと、片方で
# 作れた大きさがもう片方で直せなくなる。
# ------------------------------------------------------------------
# 箱の最小。これより小さいと掴めなくなり、編集モードから戻せない
MIN_ITEM_SIZE = 4.0
# 背景の倍率。0倍は消えたのと同じで、戻し方が分からなくなる
MIN_BACKGROUND_SCALE = 0.1
MAX_BACKGROUND_SCALE = 10.0


def clamp_size(w: float, h: float, max_w: float, max_h: float
               ) -> tuple[float, float]:
    """箱の大きさを図の中に収める。

    上は図の大きさで止めます ── 図より大きい箱は、動かしても端が
    見えないので位置を合わせられません。
    """
    return (min(max(MIN_ITEM_SIZE, w), max(MIN_ITEM_SIZE, max_w)),
            min(max(MIN_ITEM_SIZE, h), max(MIN_ITEM_SIZE, max_h)))


def clamp_scale(scale: float) -> float:
    return min(max(MIN_BACKGROUND_SCALE, scale), MAX_BACKGROUND_SCALE)


def clamp_point(x: float, y: float, item_w: float, item_h: float,
                width: float, height: float) -> tuple[float, float]:
    """箱の左上を図の中に留める。**図の外へ出すと二度と掴めない。**"""
    return (min(max(0.0, x), max(0.0, width - item_w)),
            min(max(0.0, y), max(0.0, height - item_h)))


# ------------------------------------------------------------------
# そろえる・詰める(複数選んだ箱をまとめて並べ直す)
#
# 現場の声:「配置編集でコントロールをきれいに並べれない」。1つずつ
# ドラッグで合わせると、1ドットずつずれた段々や、重なり・隙間が残る。
# 目で合わせる作業を、**そろえる/詰める**の1押しに置き換える。
#
# ここは**計算だけ**します(どこへ動かすかを返す)。枠に収める・
# 保存しないで残す、は呼ぶ側のセッションが今までの「動かす」
# 「大きさを変える」と同じ道で行います ── 2通りの道を作ると、
# 片方だけ枠からはみ出す、が起きます。
# ------------------------------------------------------------------
ALIGN_LEFT = "left"
ALIGN_RIGHT = "right"
ALIGN_TOP = "top"
ALIGN_BOTTOM = "bottom"
SAME_WIDTH = "same_width"
SAME_HEIGHT = "same_height"
PACK_ROW = "pack_row"        # 横に並べて隙間をなくす
PACK_COLUMN = "pack_column"  # 縦に並べて隙間をなくす

# 画面に出す言葉。**言葉はサーバが持つ**(設計書 §1)
ARRANGE_LABELS = {
    ALIGN_LEFT: "左をそろえました",
    ALIGN_RIGHT: "右をそろえました",
    ALIGN_TOP: "上をそろえました",
    ALIGN_BOTTOM: "下をそろえました",
    SAME_WIDTH: "幅をそろえました",
    SAME_HEIGHT: "高さをそろえました",
    PACK_ROW: "横の隙間をなくしました",
    PACK_COLUMN: "縦の隙間をなくしました",
}
ARRANGE_OPS = frozenset(ARRANGE_LABELS)

Box = tuple[float, float, float, float]      # x, y, w, h


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def arrange(boxes: dict[str, Box], op: str) -> dict[str, Box]:
    """選んだ箱をそろえる/詰める。**動かした後の** (x, y, w, h) を返す。

    【何に合わせるか】
    - 左・上   … いちばん左/上の箱に合わせる(選んだ範囲の端)
    - 右・下   … いちばん右/下の箱の端に合わせる
    - 幅・高さ … **選んだ箱のまん中の値(中央値)**。いちばん大きい箱に
                  合わせると、1つだけ大きく作ってしまった箱に全部が
                  引っ張られる。最初に選んだ箱に合わせる作りは、どれが
                  最初だったか画面から分からないので採らない
    - 横に詰める … 左から順に、**いちばん左の箱を起点に**右の箱を左へ
                    寄せて隙間をなくす(重なっていれば離す)。上下は触らない
    - 縦に詰める … 上から順に同じことをする。左右は触らない

    詰めるときの順番は**いまの位置**で決めます(左にあるものが左)。
    名前順にすると、現場で並べ替えた順番が崩れます。同じ位置の箱は
    名前で並べます(結果が毎回同じになるように)。

    知らない `op` は `ValueError`。箱が2つ未満なら何もしない(そのまま返す)。
    """
    if op not in ARRANGE_OPS:
        raise ValueError(f"知らない並べ方です: {op}")
    if len(boxes) < 2:
        return dict(boxes)

    out: dict[str, Box] = {}
    if op == ALIGN_LEFT:
        left = min(x for x, _y, _w, _h in boxes.values())
        out = {n: (left, y, w, h) for n, (x, y, w, h) in boxes.items()}
    elif op == ALIGN_RIGHT:
        right = max(x + w for x, _y, w, _h in boxes.values())
        out = {n: (right - w, y, w, h) for n, (x, y, w, h) in boxes.items()}
    elif op == ALIGN_TOP:
        top = min(y for _x, y, _w, _h in boxes.values())
        out = {n: (x, top, w, h) for n, (x, y, w, h) in boxes.items()}
    elif op == ALIGN_BOTTOM:
        bottom = max(y + h for _x, y, _w, h in boxes.values())
        out = {n: (x, bottom - h, w, h) for n, (x, y, w, h) in boxes.items()}
    elif op == SAME_WIDTH:
        width = _median([w for _x, _y, w, _h in boxes.values()])
        out = {n: (x, y, width, h) for n, (x, y, w, h) in boxes.items()}
    elif op == SAME_HEIGHT:
        height = _median([h for _x, _y, _w, h in boxes.values()])
        out = {n: (x, y, w, height) for n, (x, y, w, h) in boxes.items()}
    elif op == PACK_ROW:
        order = sorted(boxes, key=lambda n: (boxes[n][0], boxes[n][1], n))
        cursor = boxes[order[0]][0]
        for n in order:
            x, y, w, h = boxes[n]
            out[n] = (cursor, y, w, h)
            cursor += w
    elif op == PACK_COLUMN:
        order = sorted(boxes, key=lambda n: (boxes[n][1], boxes[n][0], n))
        cursor = boxes[order[0]][1]
        for n in order:
            x, y, w, h = boxes[n]
            out[n] = (x, cursor, w, h)
            cursor += h
    return out


# ------------------------------------------------------------------
# ファイルの読み書き(現場で編集したもの → 出荷時の既定値、の順に探す)
# ------------------------------------------------------------------
def read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("図のデータを読めませんでした(%s): %s", path, exc)
        return None


def write_json(path: Path, data: dict[str, Any]) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    except OSError as exc:
        log.warning("図のデータを保存できませんでした(%s): %s", path, exc)
        return False
    log.info("図のデータを保存しました: %s", path)
    return True


def drop_user_file(path: Path) -> None:
    """現場での編集を捨てる(出荷時の配置に戻すとき)。"""
    if not path.exists():
        return
    try:
        path.unlink()
        log.info("編集した図のデータを削除しました: %s", path)
    except OSError as exc:
        log.warning("図のデータを削除できませんでした: %s", exc)
