"""ロット一覧の表示内容

画面が出すものを全部ここで決めます。JS は受け取った値を並べるだけで、
「この列は右寄せか」「いま何で並んでいるか」「なぜ0件か」を判断しません。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .. import lot_query as q
from ..logging_utils import get_logger

log = get_logger("presenters.lot_list")

# 検索欄の下に常時出す案内。**入口は1つ**であることを言葉でも示す。
# 以前は番号を打つ専用の欄が別にあり、入口が2つあった ── どちらに
# 打つのが正しいかを利用者が判断しなければならず、導線として弱い
SEARCH_HINT = ("ロット番号を打つと1件に絞れた時点で自動で開きます。"
               "用途・材質・寸法でも絞れます。")

# 表の使い方。ダブルクリックは見ただけでは分からない操作なので、
# 覚えていることを前提にしない(§1.5 認識は再生に勝る)。
# キーボードでも同じことができる、と併記するのが要点
ROW_HINT = "行をダブルクリック(またはEnter)で詳細を開きます。"


# 列の幅。行の高さを 42px に固定する(`table--rich`)ため、幅は
# 中身任せにせずこちらで割り付ける。
#
# **8列とも並べ替えに使うので、1つも畳まない。** 畳んだ値では
# 並べ替えの見出しを置けなくなる ── 見た目のために機能を落とさない。
# 代わりに「読む列は広く、数字は狭く」で配る。
@dataclass
class HeaderView:
    key: str
    label: str
    numeric: bool = False
    sorted: str = ""      # "" / "asc" / "desc"


@dataclass
class RowView:
    lot_no: str
    values: list[str] = field(default_factory=list)
    # いま作業中のロットかどうか。**出どころは `work_context` ただ1つ**で、
    # 画面が別に覚えることはしない
    current: bool = False


@dataclass
class LotListViewModel:
    available: bool = True
    unavailable_message: str = ""

    headers: list[HeaderView] = field(default_factory=list)
    rows: list[RowView] = field(default_factory=list)

    conditions: list[dict[str, str]] = field(default_factory=list)
    text: str = ""
    saved: list[str] = field(default_factory=list)
    page_size: int = q.DEFAULT_PAGE_SIZE
    page_sizes: list[int] = field(default_factory=lambda: list(q.PAGE_SIZES))

    total: int = 0
    shown: int = 0
    # 件数の言い回しはサーバが持つ。「200件」とだけ出すと、それが全部なのか
    # 切られたのかが読めない
    count_note: str = ""
    # 0件のときに何をすればよいか。押した人が次の手を打てるように
    empty_why: str = ""

    can_save: bool = False
    save_why: str = ""

    # 検索欄の下に出す案内。**入口が1つしかないことを言葉でも示す**
    search_hint: str = ""
    # 表の使い方。ダブルクリックは見ただけでは分からない操作なので、
    # 覚えていることを前提にしない(認識は再生に勝る)
    row_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "unavailable_message": self.unavailable_message,
            "headers": [{"key": h.key, "label": h.label,
                         "numeric": h.numeric, "sorted": h.sorted}
                        for h in self.headers],
            "rows": [{"lot_no": r.lot_no, "values": r.values,
                      "current": r.current} for r in self.rows],
            "conditions": self.conditions,
            "text": self.text,
            "saved": self.saved,
            "page_size": self.page_size,
            "page_sizes": self.page_sizes,
            "total": self.total,
            "shown": self.shown,
            "count_note": self.count_note,
            "empty_why": self.empty_why,
            "can_save": self.can_save,
            "save_why": self.save_why,
            "search_hint": self.search_hint,
            "row_hint": self.row_hint,
        }


def build(session: Any, conn: sqlite3.Connection, *,
          available: bool = True, current_lot: str = "") -> LotListViewModel:
    view = LotListViewModel(available=available, page_size=session.page_size)

    if not available:
        view.unavailable_message = (
            "仕掛台帳が未取り込みです。設定画面から取り込んでください。")
        return view

    view.headers = [
        HeaderView(key=c.key, label=c.label,
                   numeric=(c.kind == q.TYPE_NUMBER),
                   sorted=("desc" if session.descending else "asc")
                   if c.key == session.sort else "")
        for c in q.COLUMNS
    ]

    rows = session.rows(conn)
    view.rows = [
        RowView(lot_no=str(row["ロット番号"]),
                values=[q.format_value(c, row[c.source]) for c in q.COLUMNS],
                current=bool(current_lot) and str(row["ロット番号"]) == current_lot)
        for row in rows
    ]

    view.conditions = [c.to_dict() for c in session.conditions]
    view.text = session.text
    view.saved = sorted(session.saved())

    view.total = session.total(conn)
    view.shown = len(view.rows)
    view.count_note = _count_note(view.shown, view.total, session.page_size)
    view.empty_why = _empty_why(session) if not view.rows else ""

    view.can_save = bool(session.conditions)
    view.save_why = "" if view.can_save else "先に条件を1つ以上足してください。"
    view.search_hint = SEARCH_HINT
    view.row_hint = ROW_HINT if view.rows else ""
    return view


def _count_note(shown: int, total: int, page_size: int) -> str:
    """「全部出ているのか、切られたのか」が分かる言い方にする。"""
    if total == 0:
        return "0 件"
    if shown < total:
        return f"{total} 件中 {shown} 件を表示(表示件数 {page_size})"
    return f"{total} 件"


def _empty_why(session: Any) -> str:
    """0件の理由。**何をすれば見つかるか**まで言う。"""
    if session.conditions and session.text:
        return ("条件と検索語の両方に合うロットがありません。"
                "検索語を消すか、条件を1つ外してみてください。")
    if session.conditions:
        return "この条件に合うロットがありません。条件を1つ外してみてください。"
    if session.text:
        return f"「{session.text}」を含むロットがありません。"
    return "仕掛ロットが1件もありません。設定画面から取り込んでください。"
