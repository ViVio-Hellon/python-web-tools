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

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "x": self.x, "y": self.y, "w": self.w, "h": self.h}


@dataclass(frozen=True)
class Background:
    """図の下に敷く画像(あれば)。位置と倍率も持たせて合わせられるようにする。"""

    image: str = ""
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0

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
