"""簡易在庫画面の表示内容

UIツールキットに依存しない。tkinter からも Flask からも同じものを呼ぶ。

【この画面の値打ちは「図」にある】
VBA `UFMAP` は配置図の写真の上に、保管位置ごとのボタンを**実際の並びどおり**に
置いていた。押すとその位置の在庫が出て、検索で当たった位置は青、
いま見ている位置は赤に変わる(`clsMapBtn`)。
図を見た瞬間に「どこの棚の話か」が分かるのがこの画面の要点なので、
座標も色の意味もそのまま引き継ぐ。

tkinter版は Canvas に矩形を描いていた。Web版は同じ座標をそのまま
SVG の `viewBox` に載せる ── 論理座標が1つで済み、拡大しても
文字がにじまない。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import config, pallet_map, pallet_service

# 一覧の列。VBA `hdrStk0`〜`hdrStk6` の並びをそのまま引き継ぐ。
#   (見出し, DBの列名, 数値か)
STOCK_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("幅", "幅", True),
    ("丈", "丈", True),
    ("業界", "業界", False),
    ("記号", "記号", False),
    ("位置", "位置", False),
    ("在庫数", "在庫数", True),
    ("更新日時", "更新日時", False),
)

# 入力の候補。VBA のプリセットボタンに対応する。
# **打つ手間を減らすためのもの**で、ここに無い値も入れられる
SYMBOL_PRESETS = (
    "C1", "C1 脚高", "C2", "C3", "C4", "C5", "C6", "C7", "C8",
    "EX2方向", "EXﾀｲﾄ", "P1", "P2", "P3", "P4", "P5", "P6", "P7",
    "Z1", "Z2", "Z3", "Z4", "Z5", "Z6", "Z7", "Z8", "Z9",
    "脚高", "強度UP", "東芝",
)
INDUSTRY_PRESETS = ("1×2", "3×6", "4×10", "4×8", "5×10", "5×8",
                    "スカシ", "タイト", "全面")
UNIT_PRESETS = ("組", "台", "枚")

# 検索の仕方(VBA `optExact` / `optRange`)
SEARCH_MODES: tuple[tuple[str, str, str], ...] = (
    ("exact", "完全一致", "幅・丈がぴたりと同じものだけ"),
    ("range", f"±{config.SEARCH_RANGE_TOLERANCE}mm",
     f"幅・丈が±{config.SEARCH_RANGE_TOLERANCE}mmに収まるもの"),
)
SEARCH_MODE_KEYS = frozenset(key for key, _, _ in SEARCH_MODES)

# 図の中の位置の状態。**色の意味はVBAのまま**(検索ヒット=青 / 選択中=赤)
STATE_IDLE = "idle"          # 在庫があるが、いまの話には出てきていない
STATE_EMPTY = "empty"        # 在庫が1つも無い棚
STATE_HIT = "hit"            # 検索で当たった
STATE_ACTIVE = "active"      # いま一覧に出している


@dataclass
class MapPosition:
    """図の上の1つの棚。"""

    name: str
    x: float
    y: float
    w: float
    h: float
    state: str = STATE_EMPTY
    count: int = 0               # その棚にあるパレットの種類数


@dataclass
class MapView:
    """保管位置マップまるごと。SVG にそのまま載る形。"""

    width: float = 840.0
    height: float = 540.0
    positions: list[MapPosition] = field(default_factory=list)
    background: str = ""         # 背景画像のURL(無ければ空)
    # 在庫にはあるのに図に無い位置。図から押せないので気づかせる
    missing: list[str] = field(default_factory=list)

    @property
    def view_box(self) -> str:
        return f"0 0 {self.width:g} {self.height:g}"


@dataclass
class InventoryViewModel:
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: tuple[tuple[str, str, bool], ...] = STOCK_COLUMNS
    map: MapView = field(default_factory=MapView)
    # 何を出しているのか。一覧だけ見ても分かるようにする
    caption: str = ""
    message: str = ""
    found: int = 0
    # 払出のために選ばれている1行
    selected: Optional[dict[str, Any]] = None
    # 検索欄に入れておく値。資材選択から寸法を渡されたときだけ入る
    width_text: str = ""
    length_text: str = ""
    # 資材選択から渡ってきたか。**渡された寸法だと画面に書く**ために持つ
    # (打っていないのに欄が埋まっている理由が読めないと不気味に映る)
    from_selection: bool = False


# ------------------------------------------------------------------
# 図
# ------------------------------------------------------------------
def build_map(conn: sqlite3.Connection, *,
              hit: Optional[set[str]] = None,
              active: str = "") -> MapView:
    """保管位置マップを組み立てる。

    在庫のある棚と空の棚を見た目で分ける。空の棚まで同じ濃さで描くと、
    どこに物があるのかが図から読めない。
    """
    plan = pallet_map.load()
    hit = hit or set()
    counts = position_counts(conn)

    positions = []
    for item in plan.positions:
        count = counts.get(_key(item.name), 0)
        if item.name == active:
            state = STATE_ACTIVE
        elif item.name in hit:
            state = STATE_HIT
        elif count:
            state = STATE_IDLE
        else:
            state = STATE_EMPTY
        positions.append(MapPosition(
            name=item.name, x=item.x, y=item.y, w=item.w, h=item.h,
            state=state, count=count))

    return MapView(
        width=plan.width, height=plan.height, positions=positions,
        background=_background_url(plan),
        missing=plan.unknown(sorted(counts_to_names(conn))),
    )


def position_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """棚ごとのパレットの種類数。図の濃淡に使う。"""
    from .. import db
    rows = db.fetch_all(
        conn,
        "SELECT TRIM(位置) AS 位置, COUNT(*) AS c FROM PalletMaster "
        "WHERE TRIM(位置) <> '' GROUP BY TRIM(位置)",
        caller_name="presenters.inventory.position_counts") or []
    return {_key(r["位置"]): r["c"] for r in rows}


def counts_to_names(conn: sqlite3.Connection) -> list[str]:
    """在庫に出てくる位置の名前(表記はDBのまま)。"""
    return pallet_service.list_positions(conn)


def _key(name: str) -> str:
    """位置の突き合わせ用。前後の空白と大小文字を無視する。

    `pallets_at_position` が `TRIM(...) COLLATE NOCASE` で引いているので、
    図の側も同じ扱いにしないと件数と中身が食い違う。
    """
    return (name or "").strip().upper()


def _background_url(plan) -> str:
    """背景画像。無ければ空。

    tkinter版はファイルパスを直接読んでいたが、ブラウザからは読めない。
    サーバ経由で配る(差し替えは Phase 7 の配置編集で作る)。
    """
    background = getattr(plan, "background", None)
    path = getattr(background, "path", "") if background else ""
    return "/api/inventory/map/background" if path else ""


# ------------------------------------------------------------------
# 一覧
# ------------------------------------------------------------------
def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    """一覧の1行。表に出す列だけを、表示できる形にして返す。"""
    data = {label: _cell(row, column) for label, column, _ in STOCK_COLUMNS}
    # 払出で使う識別子。幅・丈・位置の3つで1行が決まる(VBAと同じ)
    data["key"] = {"width": row["幅"], "length": row["丈"], "position": row["位置"]}
    data["list_managed"] = (row["リスト管理"] or "").strip() == "要"
    return data


def _cell(row: sqlite3.Row, column: str) -> Any:
    try:
        value = row[column]
    except (IndexError, KeyError):
        return ""
    return "" if value is None else value


def search(conn: sqlite3.Connection, width: int, length: int,
           mode: str = "exact") -> InventoryViewModel:
    """幅・丈で探す(VBA `SearchAndHighlight`)。"""
    if mode not in SEARCH_MODE_KEYS:
        mode = "exact"
    result = pallet_service.search_pallets(conn, width, length, mode)
    label = next(name for key, name, _ in SEARCH_MODES if key == mode)
    rows = [row_dict(r) for r in result.rows]
    return InventoryViewModel(
        rows=rows,
        map=build_map(conn, hit=result.matched_positions),
        caption=f"{width}×{length} の検索結果({label})",
        message=("" if rows else
                 f"{width}×{length} に当てはまる在庫はありません。"
                 f"「±{config.SEARCH_RANGE_TOLERANCE}mm」でも試してみてください。"),
        found=len(rows),
    )


def at_position(conn: sqlite3.Connection, position: str) -> InventoryViewModel:
    """1つの棚の中身(VBA `ShowInventoryByLocation`)。"""
    rows = [row_dict(r) for r in pallet_service.pallets_at_position(conn, position)]
    return InventoryViewModel(
        rows=rows,
        map=build_map(conn, active=position),
        caption=f"位置 {position} の在庫",
        message="" if rows else f"位置 {position} には在庫がありません。",
        found=len(rows),
    )


def initial(conn: sqlite3.Connection) -> InventoryViewModel:
    """開いた直後。

    資材選択の「在庫を見る」から来たときは、**渡された寸法で引いた
    ところから始める**(旧版 `btnUFMAP`)。渡ってきているのに空の画面を
    出して打ち直させるのでは、渡した意味がない。
    """
    from .. import work_context

    size = work_context.get_context().stock_size
    if size:
        view = search(conn, size["width"], size["length"])
        view.width_text = str(size["width"])
        view.length_text = str(size["length"])
        view.from_selection = True
        return view
    return InventoryViewModel(
        map=build_map(conn),
        caption="",
        message="幅と丈を入れて検索するか、図の位置を押してください。",
    )


# ------------------------------------------------------------------
# JSON
# ------------------------------------------------------------------
def to_dict(view: InventoryViewModel) -> dict[str, Any]:
    return {
        "rows": view.rows,
        "columns": [{"label": label, "numeric": numeric}
                    for label, _, numeric in view.columns],
        "map": map_dict(view.map),
        "caption": view.caption,
        "message": view.message,
        "found": view.found,
        "selected": view.selected,
        "width_text": view.width_text,
        "length_text": view.length_text,
        "from_selection": view.from_selection,
    }


def map_dict(view: MapView) -> dict[str, Any]:
    return {
        "view_box": view.view_box,
        "width": view.width,
        "height": view.height,
        "background": view.background,
        "missing": view.missing,
        "positions": [{"name": p.name, "x": p.x, "y": p.y, "w": p.w, "h": p.h,
                       "state": p.state, "count": p.count}
                      for p in view.positions],
    }


def result_dict(result: pallet_service.TransactionResult) -> dict[str, Any]:
    """受入・払出の結果。文言はサービス層が持つ(設計書 §6.1)。"""
    return {
        "ok": result.ok,
        "message": result.message,
        "new_stock": result.new_stock,
        "conflict": result.conflict,
    }
