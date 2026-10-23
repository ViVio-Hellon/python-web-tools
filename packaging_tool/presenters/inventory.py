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

from .. import config, pallet_service, stock_reserve

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
# 一覧に出す列。VBA の並びの**うしろに**「持出待ち」を足す(前の7列の並びは変えない)。
# 倉庫連携で在庫を充てた発注が、その行(幅×丈×置き場)からまだ持ち出していない台数
WAITING_LABEL = "持出待ち"
LIST_COLUMNS: tuple[tuple[str, str, bool], ...] = STOCK_COLUMNS + ((WAITING_LABEL, "", True),)

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


def _master_values(conn: sqlite3.Connection, table: str, column: str) -> list[str]:
    """パレット記号・業界マスタの有効な行の値。表が無い・読めないなら空(候補は固定の並びだけ)。"""
    from .. import pallet_threshold
    try:
        return [str(row[column]).strip() for row in pallet_threshold.rows(conn, table)]
    except Exception:                               # noqa: BLE001 - 候補が減るだけ。画面は止めない
        return []


def _merge(presets: tuple[str, ...], extra: list[str]) -> tuple[str, ...]:
    out = list(presets)
    out += [v for v in dict.fromkeys(extra) if v and v not in presets]
    return tuple(out)


def symbol_choices(conn: sqlite3.Connection) -> tuple[str, ...]:
    """記号の候補。固定の並びに、パレット記号マスタで足した記号を後ろに足す
    (マスタ反映もれ #11。VER4.8.104。以前は固定の並びだけで、マスタを直しても変わらなかった)。"""
    return _merge(SYMBOL_PRESETS, _master_values(conn, "PalletSymbolMaster", "記号"))


def industry_choices(conn: sqlite3.Connection) -> tuple[str, ...]:
    """業界の候補。固定の並びに、パレット業界マスタで足した業界を後ろに足す。"""
    return _merge(INDUSTRY_PRESETS, _master_values(conn, "PalletIndustryMaster", "業界"))


def choices_dict(conn: sqlite3.Connection) -> dict[str, Any]:
    """マスタが変わったときに画面が引き直すもの: 入力の候補と、何も選んでいない図。"""
    return {"symbols": list(symbol_choices(conn)),
            "industries": list(industry_choices(conn)),
            "map": map_dict(build_map(conn))}

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
    # 倉庫連携で在庫を充てた発注が、この棚からまだ持ち出していない台数(図の「持出」の印)
    waiting: int = 0
    # 持ち出し待ちが在庫数より多い(古い版で払い出した・同時に払い出した)。ずれに気づかせる
    short: bool = False


@dataclass
class MapView:
    """保管位置マップまるごと。SVG にそのまま載る形。"""

    width: float = 840.0
    height: float = 540.0
    positions: list[MapPosition] = field(default_factory=list)
    background: str = ""         # 背景画像のURL(無ければ空)
    # 背景の置き方。**枠いっぱいに引き伸ばさない** ── 写真の画角は図の枠と
    # 一致しないので、ずらし量と倍率を持って合わせこめるようにする
    background_x: float = 0.0
    background_y: float = 0.0
    background_scale: float = 1.0
    # 在庫にはあるのに図に無い位置。図から押せないので気づかせる
    missing: list[str] = field(default_factory=list)
    # 持ち出し待ちのある棚を1行で(図の印は小さいので、字でも出す)
    waiting_note: str = ""
    # 配置編集。押す前に「いま動かせるのか」が読めるように画面へ渡す
    editing: bool = False
    dirty: bool = False

    @property
    def view_box(self) -> str:
        return f"0 0 {self.width:g} {self.height:g}"


@dataclass
class InventoryViewModel:
    rows: list[dict[str, Any]] = field(default_factory=list)
    columns: tuple[tuple[str, str, bool], ...] = LIST_COLUMNS
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
    # 1つの棚を開いたとき、その棚の持ち出し待ち(どのロットの分か)
    waiting_detail: list[str] = field(default_factory=list)


# ------------------------------------------------------------------
# 図
# ------------------------------------------------------------------
def build_map(conn: sqlite3.Connection, *,
              hit: Optional[set[str]] = None,
              active: str = "", session=None) -> MapView:
    """保管位置マップを組み立てる。

    在庫のある棚と空の棚を見た目で分ける。空の棚まで同じ濃さで描くと、
    どこに物があるのかが図から読めない。

    【図はセッションから取る】
    `pallet_map.load()` は毎回ファイルを読みます。編集中に読み直すと
    **まだ保存していない移動が消える**ので、セッションが抱えている
    図をそのまま使います(`layout` の presenter と同じ)。
    """
    if session is None:
        from .. import pallet_map_session
        session = pallet_map_session.get_session()
    plan = session.plan
    hit = hit or set()
    counts = position_counts(conn)
    held = stock_reserve.waiting_by_position(conn)
    short = short_positions(conn)

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
            state=state, count=count, waiting=held.get(_key(item.name), 0),
            short=_key(item.name) in short))

    return MapView(
        width=plan.width, height=plan.height, positions=positions,
        background=_background_url(plan),
        background_x=plan.background.x,
        background_y=plan.background.y,
        background_scale=plan.background.scale,
        missing=plan.unknown(sorted(counts_to_names(conn))),
        editing=session.editing, dirty=session.dirty,
        waiting_note=_waiting_note(conn),
    )


def _waiting_note(conn: sqlite3.Connection) -> str:
    """「持ち出し待ち: A1 2台・S1 1台」。無ければ空。

    図の印は棚の上の小さな数字なので、**どの棚から何台か**を字でも出す。
    """
    names: dict[str, str] = {}
    totals: dict[str, int] = {}
    for w in stock_reserve.waiting(conn):
        key = _key(w.position)
        names.setdefault(key, w.position)
        totals[key] = totals.get(key, 0) + w.count
    if not totals:
        return ""
    return "持ち出し待ち: " + "・".join(f"{names[k]} {totals[k]}台" for k in sorted(totals))


def short_positions(conn: sqlite3.Connection) -> set[str]:
    """持ち出し待ちが在庫数より多い棚(`_key` の形)。

    古い版の端末で払い出した(持出が残らない)・2台が同時に払い出したときに起きる。
    黙って印を出し続けると「まだ持ち出していない」に見えるので、ずれていると示す。
    """
    from .. import db
    rows = db.fetch_all(
        conn,
        "SELECT 幅, 丈, 位置, 在庫数 FROM PalletMaster WHERE TRIM(位置) <> ''",
        caller_name="presenters.inventory.short_positions") or []
    stock: dict[tuple, int] = {}
    for r in rows:
        place = (stock_reserve._size(r["幅"]), stock_reserve._size(r["丈"]), _key(r["位置"]))
        stock[place] = stock.get(place, 0) + int(r["在庫数"] or 0)
    return {place[2] for place, count in stock_reserve.waiting_by_place(conn).items()
            if count > stock.get(place, 0)}


def position_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """棚ごとのパレットの種類数(**在庫のあるものだけ**)。図の濃淡に使う。

    在庫0の行まで数えると、空になった棚が「在庫あり」の色のまま残る。"""
    from .. import db
    rows = db.fetch_all(
        conn,
        "SELECT TRIM(位置) AS 位置, COUNT(*) AS c FROM PalletMaster "
        f"WHERE TRIM(位置) <> '' AND {pallet_service.IN_STOCK} GROUP BY TRIM(位置)",
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

    中身(`data:` URL)は `pallet_map.json` が持っていますが、画面へは
    **URLで渡します**。図を描き直すたびに数MBの文字列を載せると、
    棚を1つ押すだけで応答がその分だけ重くなるためです。

    以前はここが `background.path` を見ていました。`Background` に
    `path` はありません(あるのは `image`)ので、**背景を設定しても
    必ず空**になり、簡易在庫の図に写真が出たことは一度もありません。
    棚検索(`presenters/layout._background_url`)は `image` を見ています。
    """
    background = getattr(plan, "background", None)
    image = getattr(background, "image", "") if background else ""
    return "/api/inventory/map/background" if image else ""


# ------------------------------------------------------------------
# 一覧
# ------------------------------------------------------------------
def row_dict(row: sqlite3.Row, held: Optional[dict[tuple, int]] = None) -> dict[str, Any]:
    """一覧の1行。表に出す列だけを、表示できる形にして返す。

    `held` は (幅, 丈, 位置) ごとの持ち出し待ち(`stock_reserve.waiting_by_place`)。
    """
    data = {label: _cell(row, column) for label, column, _ in STOCK_COLUMNS}
    place = (stock_reserve._size(row["幅"]), stock_reserve._size(row["丈"]), _key(row["位置"]))
    waiting = (held or {}).get(place, 0)
    data[WAITING_LABEL] = waiting or ""
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
    held = stock_reserve.waiting_by_place(conn)
    rows = [row_dict(r, held) for r in result.rows]
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
    held = stock_reserve.waiting_by_place(conn)
    rows = [row_dict(r, held) for r in pallet_service.pallets_at_position(conn, position)]
    return InventoryViewModel(
        rows=rows,
        map=build_map(conn, active=position),
        caption=f"位置 {position} の在庫",
        message="" if rows else f"位置 {position} には在庫がありません。",
        found=len(rows),
        waiting_detail=waiting_detail(conn, position),
    )


def waiting_detail(conn: sqlite3.Connection, position: str) -> list[str]:
    """その棚の持ち出し待ちを、どのロットの分か分かる形で。古い順。"""
    key = _key(position)
    return [f"{w.width}×{w.length} {w.count}台 ── Lot {w.lot_no or '---'}"
            f"({_short_time(w.since)} に引当)"
            for w in stock_reserve.waiting(conn) if _key(w.position) == key]


def _short_time(text: str) -> str:
    """「2026-10-11 14:02:33」→「10/11 14:02」。読めなければそのまま。"""
    value = (text or "").strip().replace("/", "-")
    try:
        day, clock = value.split(" ")[:2]
        _, month, date = day.split("-")
        return f"{int(month)}/{int(date)} {clock[:5]}"
    except ValueError:
        return text or ""


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
        "waiting_detail": view.waiting_detail,
    }


def map_dict(view: MapView) -> dict[str, Any]:
    return {
        "view_box": view.view_box,
        "width": view.width,
        "height": view.height,
        "background": view.background,
        "background_x": view.background_x,
        "background_y": view.background_y,
        "background_scale": view.background_scale,
        "editing": view.editing,
        "dirty": view.dirty,
        "missing": view.missing,
        "waiting_note": view.waiting_note,
        "positions": [{"name": p.name, "x": p.x, "y": p.y, "w": p.w, "h": p.h,
                       "state": p.state, "count": p.count,
                       "waiting": p.waiting, "short": p.short}
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
