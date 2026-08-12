"""保管位置マップのデータ (VBA `UFMAP` の位置ボタン配置の移植)

【VBAの仕組み】
UFMAPは配置図の写真を背景にしたフォームの上に、保管位置ごとの
CommandButton(`S1` `S2` `A1`〜`A11` `B1`〜`B5` `C1`〜`C7` `D1`〜`D3`)を
実際の並びどおりに置いていた。押すとその位置の在庫が一覧に出る
(`ShowInventoryByLocation`)。検索で当たった位置は青、いま見ている位置は
赤に変わる(`clsMapBtn`)。

ボタンの並びが現場の並びと同じなので、**どこの棚の話をしているかが
図を見た瞬間に分かる**。これがこの画面の値打ちなので、Python版でも
実際の座標をそのまま使う。

座標はソースに書かず、棚配置図(`floor_plan`)と同じくJSONに持つ。

    packaging_tool/pallet_map_default.json   出荷時の配置(リポジトリ管理)
    data/pallet_map.json                     現場で編集した内容(あれば優先)

位置が増減・移動したら「簡易在庫」タブの**配置編集**でドラッグして直す。

座標系は元のフォームと同じ**ポイント単位・左上原点**で、値は
実際の `UFMAP` から採ったもの(TempFM 840x540 の中)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config, map_data
from .logging_utils import get_logger
from .map_data import Background, MapItem

log = get_logger("pallet_map")

DEFAULT_PATH = Path(__file__).resolve().parent / "pallet_map_default.json"
USER_PATH = config.DB_PATH.parent / "pallet_map.json"

# 新しい位置を足すときの既定の大きさ(VBAのB列ボタンと同じ 24x36)
NEW_POSITION_W = 24.0
NEW_POSITION_H = 36.0


@dataclass
class PalletMap:
    """保管位置マップまるごと。読み書きの単位。"""

    width: float = 840.0
    height: float = 540.0
    positions: list[MapItem] = field(default_factory=list)
    background: Background = field(default_factory=Background)

    # -- 検索 -------------------------------------------------------
    def position(self, name: str) -> Optional[MapItem]:
        for candidate in self.positions:
            if candidate.name == name:
                return candidate
        return None

    @property
    def names(self) -> list[str]:
        return [p.name for p in self.positions]

    def unknown(self, used: list[str]) -> list[str]:
        """在庫にはあるのにマップに無い位置。

        位置を増やしたのにマップを直し忘れると、その棚のパレットは
        図から押せなくなる。気づけるように名前を返す。
        """
        known = set(self.names)
        seen: list[str] = []
        for name in used:
            if name and name not in known and name not in seen:
                seen.append(name)
        return seen

    # -- 編集 -------------------------------------------------------
    def move_position(self, name: str, x: float, y: float) -> bool:
        return map_data.move_in((self.positions,), name, x, y)

    def resize_position(self, name: str, w: float, h: float) -> bool:
        return map_data.resize_in((self.positions,), name, w, h)

    def add_position(self, name: str, x: float, y: float,
                     w: float = NEW_POSITION_W,
                     h: float = NEW_POSITION_H) -> MapItem:
        item = MapItem(name=name, x=round(x, 1), y=round(y, 1), w=w, h=h)
        self.positions.append(item)
        return item

    def remove_position(self, name: str) -> bool:
        for index, candidate in enumerate(self.positions):
            if candidate.name == name:
                del self.positions[index]
                return True
        return False

    # -- 入出力 -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "canvas": {"width": self.width, "height": self.height},
            "background": self.background.to_dict(),
            "positions": [p.to_dict() for p in self.positions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PalletMap":
        canvas = data.get("canvas") or {}
        return cls(
            width=float(canvas.get("width", 840)),
            height=float(canvas.get("height", 540)),
            positions=[map_data.item_from_dict(d)
                       for d in data.get("positions", [])],
            background=map_data.background_from_dict(data.get("background")),
        )


def load(path: Optional[Path] = None) -> PalletMap:
    """保管位置マップを読む。現場で編集したものがあればそれを優先する。"""
    for candidate in ([path] if path is not None else [USER_PATH, DEFAULT_PATH]):
        if candidate is None:
            continue
        data = map_data.read_json(candidate)
        if data is not None:
            return PalletMap.from_dict(data)
    log.warning("保管位置マップが見つかりません。空のマップで続行します")
    return PalletMap()


def save(plan: PalletMap, path: Optional[Path] = None) -> bool:
    """編集した配置を保存する。既定値のファイルは上書きしない。"""
    return map_data.write_json(path or USER_PATH, plan.to_dict())


def reset_to_default() -> PalletMap:
    """現場での編集を捨てて出荷時の配置に戻す。"""
    map_data.drop_user_file(USER_PATH)
    return load()
