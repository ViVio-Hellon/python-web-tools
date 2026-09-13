"""棚検索 (旧 `frmLayout`)

置き場の配置図を出し、拠点から一番近い置き場を探す。あわせて、
**置き場が変わったら現場で図を直せる**ようにする(配置編集)。

【この画面の性質】
VBA版は配置図の画像を背景にしたフォームの上に置き場のLabel
(`lblItem1`〜`lblItem35`)を並べ、その座標から拠点までの距離を測って
いた。何がどこにあるかは `BoardMaster`/`CornerboardMaster` の
「データラベル」列が決めるので、**ラベルをずらせば置き場が変わり、
テーブルを直せば中身が変わる**。

その性質はそのまま保つ。座標はコードではなく `floor_plan`(JSON)にあり、
この画面のドラッグで動かせる。Excelのフォームデザイナーの代わりを
アプリ自身が持つ形。

【tkinter版から変えたこと】
- 図は Canvas ではなく **SVG**。拡大縮小はブラウザに任せる
- 背景画像は `<input type="file">` から。`tk.PhotoImage` の PNG/GIF 制約が
  無くなるので **JPEG も使える**(§7.2)
- 置き場をドラッグする当たり判定を自前で書かない。SVG の要素をそのまま掴む
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from .. import location_service as svc, user_settings, work_context
from ..logging_utils import get_logger

log = get_logger("presenters.layout")

# 置き場の状態。色は `tokens.css` が決める(§設計指針 3.8)
STATE_IDLE = "idle"        # 資材が割り当ててある置き場
STATE_EMPTY = "empty"      # データラベルがどの資材にも紐付いていない
STATE_HIT = "hit"          # 検索で当たった
STATE_BASE = "base"        # 拠点

# 資材の種別。VBA `frmLayout` はこれをラジオボタンで先に選ばせていたが、
# **いまは選ばせない**(検索は両方を一度に見る)。結果に「どちらが
# 当たったか」を書くために名前だけ残す
KIND_BOARD = "board"
KIND_ANGLE = "angle"
KINDS = (KIND_BOARD, KIND_ANGLE)
KIND_LABEL = {KIND_BOARD: "ボード", KIND_ANGLE: "アングル"}

# 資材カテゴリ。色分けの種別として画面へ渡す(VBA版には無い区別で、
# 「mapのラベルがどの資材の置き場か一目で分からない」という要望への対応)
CATEGORIES = ("アングル", "ボード", "プロテック", "IK")
CATEGORY_MIXED = "混在"


@dataclass
class Shelf:
    """図の上の1つの置き場。"""

    name: str
    x: float
    y: float
    w: float
    h: float
    state: str = STATE_EMPTY
    # 何の置き場か。複数のカテゴリを兼ねていれば「混在」
    category: str = ""
    # 拠点からの距離。拠点が決まっていなければ None
    distance: Optional[float] = None


@dataclass
class ShelfMaterialRow:
    """置き場を押したときに出す中身の1行。"""

    category: str = ""
    size: str = ""


@dataclass
class LayoutViewModel:
    """棚検索の画面ぜんぶ。"""

    available: bool = True
    unavailable_message: str = ""

    width: float = 1000.0
    height: float = 570.0
    background: str = ""
    # 背景の置き方。**枠いっぱいに引き伸ばさない** ── 写真の画角は図の枠と
    # 一致しないので、ずらし量と倍率を持って合わせこめるようにする
    background_x: float = 0.0
    background_y: float = 0.0
    background_scale: float = 1.0
    shelves: list[Shelf] = field(default_factory=list)
    bases: list[Shelf] = field(default_factory=list)
    areas: list[Shelf] = field(default_factory=list)

    base_point: str = ""
    base_points: list[str] = field(default_factory=list)

    # 検索
    kind: str = KIND_BOARD
    width_text: str = ""
    length_text: str = ""
    result: str = ""
    found: bool = False

    # 資材選択から渡ってきた資材の件数(旧版 `btnMap` の受け側)。
    # **0なら「まとめて」は押せない。** 渡っていないのに押せると、
    # 押してから「渡ってきていません」と言われることになる
    handoff: int = 0

    # 押した置き場の中身
    selected: str = ""
    materials: list[ShelfMaterialRow] = field(default_factory=list)
    materials_note: str = ""

    # 配置編集
    editing: bool = False
    # マスタが参照しているのに図に無いラベル。**足す目安**になる
    unplaced: list[str] = field(default_factory=list)
    dirty: bool = False

    @property
    def view_box(self) -> str:
        return f"0 0 {self.width:g} {self.height:g}"


def has_data(conn: sqlite3.Connection) -> bool:
    """置き場を引く元(ボード・アングルのマスタ)が入っているか。"""
    from .. import db
    rows = db.fetch_all(conn, "SELECT 1 FROM BoardMaster LIMIT 1",
                        caller_name="layout.has_data")
    return bool(rows)


def build(conn: sqlite3.Connection, session: Any) -> LayoutViewModel:
    """画面ぜんぶを組み立てる。`session` は `layout_session.LayoutSession`。"""
    plan = session.plan
    available = has_data(conn)
    view = LayoutViewModel(
        available=available,
        unavailable_message=(
            "" if available else
            "ボードマスタが未取り込みです。設定画面から取り込んでください。"),
        width=plan.width, height=plan.height,
        background=_background_url(plan),
        background_x=plan.background.x,
        background_y=plan.background.y,
        background_scale=plan.background.scale,
        base_point=user_settings.get_position(),
        base_points=list(plan.base_point_names),
        kind=session.kind,
        width_text=session.width_text,
        length_text=session.length_text,
        result=session.result,
        found=bool(session.highlight),
        handoff=len(work_context.get_context().map_items),
        selected=session.selected,
        editing=session.editing,
        dirty=session.dirty,
    )

    categories = svc.label_categories(conn) if available else {}
    base = view.base_point
    view.shelves = [
        _shelf(item, categories.get(item.name, set()),
               hit=item.name in session.highlight,
               distance=plan.distance(item.name, base))
        for item in plan.items]
    view.bases = [
        Shelf(name=b.name, x=b.x, y=b.y, w=b.w, h=b.h, state=STATE_BASE)
        for b in plan.base_points]
    view.areas = [
        Shelf(name=a.name, x=a.x, y=a.y, w=a.w, h=a.h, state="area")
        for a in plan.areas]

    if session.selected:
        view.materials, view.materials_note = _materials(conn, session.selected)
    if available:
        view.unplaced = svc.unplaced_data_labels(conn)
    return view


def _shelf(item: Any, categories: set, *, hit: bool,
           distance: Optional[float]) -> Shelf:
    if hit:
        state = STATE_HIT
    elif categories:
        state = STATE_IDLE
    else:
        # どの資材にも紐付いていない置き場。図に残っているのに使われて
        # いないので、空だと分かるようにしておく
        state = STATE_EMPTY
    return Shelf(
        name=item.name, x=item.x, y=item.y, w=item.w, h=item.h, state=state,
        category=(CATEGORY_MIXED if len(categories) > 1
                  else next(iter(categories)) if categories else ""),
        distance=round(distance) if distance is not None else None)


def _materials(conn: sqlite3.Connection,
               label: str) -> tuple[list[ShelfMaterialRow], str]:
    """置き場の中身(VBA `OpenSizePopup` → `frmSizePopup`)。

    原文は押した置き場のボード(幅/丈/タイプ)とアングル(丈)を別窓に
    出していた。**アングルがある置き場ではボードの一覧を隠していた**
    (`ShowAngleSection` の `mAngleOnly`)ので、両方置いてある場所では
    ボードが見えなかった。こちらは両方出す。
    """
    rows = svc.materials_at_label(conn, label)
    if not rows:
        # **作業者の言葉で言う。** 「マスタの列を確認してください」は
        # 作り手の言葉で、読んだ作業者は次に何をすればよいか分からない
        # (現場の指摘:「ツール制作者よりのコメントすぎる」)
        return [], (f"{label} に置いてある資材が登録されていません。"
                    f"資材課の人に伝えてください(マスタの「データラベル」欄)。")
    items = [ShelfMaterialRow(
        category=m.category,
        size=(f"{m.width}×{m.length}" if m.width else f"丈 {m.length}"))
        for m in rows]
    return items, f"{label} の資材 {len(items)} 件"


def _background_url(plan: Any) -> str:
    """背景画像のURL。`<img>`/`<image>` から読ませるための入口。"""
    return "/api/layout/map/background" if plan.background.image else ""


# ------------------------------------------------------------------
# JSON
# ------------------------------------------------------------------
def to_dict(view: LayoutViewModel) -> dict[str, Any]:
    return {
        "available": view.available,
        "unavailable_message": view.unavailable_message,
        "view_box": view.view_box,
        "width": view.width,
        "height": view.height,
        "background": view.background,
        "background_x": view.background_x,
        "background_y": view.background_y,
        "background_scale": view.background_scale,
        "shelves": [_shelf_dict(s) for s in view.shelves],
        "bases": [_shelf_dict(s) for s in view.bases],
        "areas": [_shelf_dict(s) for s in view.areas],
        "base_point": view.base_point,
        "base_points": view.base_points,
        "kind": view.kind,
        "width_text": view.width_text,
        "length_text": view.length_text,
        "result": view.result,
        "found": view.found,
        "handoff": view.handoff,
        "selected": view.selected,
        "materials": [{"category": m.category, "size": m.size}
                      for m in view.materials],
        "materials_note": view.materials_note,
        "editing": view.editing,
        "unplaced": view.unplaced,
        "dirty": view.dirty,
    }


def _shelf_dict(shelf: Shelf) -> dict[str, Any]:
    return {"name": shelf.name, "x": shelf.x, "y": shelf.y,
            "w": shelf.w, "h": shelf.h, "state": shelf.state,
            "category": shelf.category, "distance": shelf.distance}
