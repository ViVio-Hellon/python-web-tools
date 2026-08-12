"""棚配置図のデータ (VBA `frmLayout` のコントロール配置の移植)

【VBAの仕組みと、それを保つための設計】
VBA版は配置図の画像を背景にしたフォームの上に、置き場ごとの小さな
Labelコントロール(`lblItem1`〜`lblItem35`)を置いていた。距離や疲労度は
その `.Left` / `.Top` から計算する。この作りの良いところは

    - 置き場が変わったら**ラベルをずらすだけ**で計算も変わる
    - 何が置いてあるかは `BoardMaster.データラベル` を直せば変わる

という点で、コードを触らずに現場の変化に追従できることだった。

Python版もこの性質をそのまま保つ。**座標はソースに書かず、JSONに持つ**。

    packaging_tool/floor_plan_default.json   出荷時の既定値(リポジトリ管理)
    data/floor_plan.json                     現場で編集した内容(あれば優先)

編集は「資材配置」タブの**配置編集**でラベルをドラッグして行う。
Excelのフォームデザイナーに当たるものがPythonには無いので、その代わりを
アプリ自身が持つ形にした(結果として、開発者でなくても動かせる)。

座標系は元のフォームと同じ**ポイント単位・左上原点**。距離は
VBA `CalcDistance` と同じく **Left/Top の差**で測る(中心ではない)ので、
既存の疲労度スコアと数値が一致する。

箱と背景の入れ物そのものは `map_data` にある(UFMAPの保管位置マップと
同じ作りなので共通にした)。ここはそれに「拠点」と「距離」を足したもの。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config, map_data
from .logging_utils import get_logger
from .map_data import Background, MapItem   # 既存の呼び出し元のために再公開

log = get_logger("floor_plan")

DEFAULT_PATH = Path(__file__).resolve().parent / "floor_plan_default.json"
USER_PATH = config.DB_PATH.parent / "floor_plan.json"


@dataclass
class FloorPlan:
    """配置図まるごと。読み書きの単位。"""

    width: float = 1000.0
    height: float = 570.0
    base_points: list[MapItem] = None          # type: ignore[assignment]
    items: list[MapItem] = None                # type: ignore[assignment]
    areas: list[MapItem] = None                # type: ignore[assignment]
    background: Background = None              # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.base_points = self.base_points or []
        self.items = self.items or []
        self.areas = self.areas or []
        self.background = self.background or Background()

    # -- 検索 -------------------------------------------------------
    def item(self, name: str) -> Optional[MapItem]:
        for candidate in (*self.items, *self.base_points):
            if candidate.name == name:
                return candidate
        return None

    def base_point(self, name: str) -> Optional[MapItem]:
        for candidate in self.base_points:
            if candidate.name == name:
                return candidate
        return None

    @property
    def base_point_names(self) -> tuple[str, ...]:
        return tuple(b.name for b in self.base_points)

    @property
    def item_names(self) -> list[str]:
        return [i.name for i in self.items]

    # -- 距離 -------------------------------------------------------
    def distance(self, item_name: str, base_name: str) -> Optional[float]:
        """VBA `CalcDistance` の移植。Left/Topの差のユークリッド距離。

        どちらかが見つからないときは None(VBAは0を返すが、
        「0mm=目の前」と「不明」を区別できたほうが原因を追いやすい)。
        """
        target, base = self.item(item_name), self.base_point(base_name)
        if target is None or base is None:
            return None
        dx, dy = target.x - base.x, target.y - base.y
        return (dx * dx + dy * dy) ** 0.5

    def nearest(self, item_names: Iterable[str], base_name: str
                ) -> tuple[Optional[str], Optional[float], int]:
        """候補のうち拠点から一番近いものを返す。

        戻り値は (ラベル名, 距離, 候補数)。VBA `FindNearestSameSize` 相当。
        """
        best_name: Optional[str] = None
        best_dist: Optional[float] = None
        count = 0
        for name in item_names:
            dist = self.distance(name, base_name)
            if dist is None:
                continue
            count += 1
            if best_dist is None or dist < best_dist:
                best_name, best_dist = name, dist
        return best_name, best_dist, count

    # -- 編集 -------------------------------------------------------
    def move_item(self, name: str, x: float, y: float) -> bool:
        return map_data.move_in((self.items, self.base_points, self.areas),
                                name, x, y)

    def add_item(self, name: str, x: float, y: float,
                 w: float = 18.0, h: float = 18.0) -> MapItem:
        item = MapItem(name=name, x=round(x, 1), y=round(y, 1), w=w, h=h)
        self.items.append(item)
        return item

    def remove_item(self, name: str) -> bool:
        for index, candidate in enumerate(self.items):
            if candidate.name == name:
                del self.items[index]
                return True
        return False

    # -- 入出力 -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "canvas": {"width": self.width, "height": self.height},
            "background": self.background.to_dict(),
            "base_points": [b.to_dict() for b in self.base_points],
            "items": [i.to_dict() for i in self.items],
            "areas": [a.to_dict() for a in self.areas],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FloorPlan":
        canvas = data.get("canvas") or {}
        return cls(
            width=float(canvas.get("width", 1000)),
            height=float(canvas.get("height", 570)),
            base_points=[map_data.item_from_dict(d)
                         for d in data.get("base_points", [])],
            items=[map_data.item_from_dict(d) for d in data.get("items", [])],
            areas=[map_data.item_from_dict(d) for d in data.get("areas", [])],
            background=map_data.background_from_dict(data.get("background")),
        )


def load(path: Optional[Path] = None) -> FloorPlan:
    """配置図を読む。現場で編集したものがあればそれを、無ければ既定値を。"""
    for candidate in ([path] if path is not None else [USER_PATH, DEFAULT_PATH]):
        if candidate is None:
            continue
        data = map_data.read_json(candidate)
        if data is not None:
            return FloorPlan.from_dict(data)
    log.warning("配置図のデータが見つかりません。空の配置図で続行します")
    return FloorPlan()


def save(plan: FloorPlan, path: Optional[Path] = None) -> bool:
    """編集した配置図を保存する。既定値のファイルは上書きしない。"""
    return map_data.write_json(path or USER_PATH, plan.to_dict())


def reset_to_default() -> FloorPlan:
    """現場での編集を捨てて出荷時の配置に戻す。"""
    map_data.drop_user_file(USER_PATH)
    return load()


# ------------------------------------------------------------------
# 既存コードとの互換(以前は座標をこのモジュールに直書きしていた)
# ------------------------------------------------------------------
def base_points() -> tuple[str, ...]:
    """拠点名(VBA `frmLayout` の `POSITIONS = "L1,HVC,LVC"` に相当)。"""
    return load().base_point_names or ("L1", "HVC", "LVC")


# 画面の初期化時に一度だけ読む用途のために、モジュール読込時の値も出しておく
BASE_POINTS: tuple[str, ...] = base_points()

