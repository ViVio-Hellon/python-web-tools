"""マスタ管理の面の表示内容

設定画面の「マスタ管理」の面に出すものを組み立てる。
判断は `master_admin` が済ませてあり、ここは**並べ方と言葉**だけを持つ。

【共有フォルダに触るのは、その面を開いたときだけ】
設定画面は毎日何度も開く。開くたびに共有の sqlite3 を19回数えに行くと、
設定を1つ変えたいだけの人まで待たされる。だから最初の描画では
**共有に触らない分**(直せるかどうか・どこへ書くか)だけを出し、
中身は面を開いたときに読む(`browse`)。§4.6「待たせ方」。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import config, data_sync, master_admin


@dataclass
class MasterViewModel:
    # --- 共有に触らずに分かること ---
    can_edit: bool = False
    edit_why: str = ""
    source_dir: str = ""

    # --- 中身(面を開いてから読む) ---
    loaded: bool = False
    source: str = ""
    source_note: str = ""       # 書き先のファイル(ファイルの名前まで)
    source_why: str = ""        # 梱包資材マスタが見つからない理由(候補が多すぎる など)
    tables: list[master_admin.TableInfo] = field(default_factory=list)
    table: str = ""
    query: str = ""
    columns: list[master_admin.Column] = field(default_factory=list)
    page: Optional[master_admin.Page] = None
    message: str = ""


def frame(conn: Optional[sqlite3.Connection]) -> MasterViewModel:
    """最初の描画ぶん。**共有フォルダには触らない。**"""
    allowed, why = master_admin.can_edit(conn)
    return MasterViewModel(can_edit=allowed, edit_why=why,
                           source_dir=str(config.master_db_dir()))


def browse(conn: sqlite3.Connection, *, table: str = "", query: str = "",
           sort: str = "", sort_dir: str = "asc",
           path: Optional[Path] = None, message: str = "") -> MasterViewModel:
    """面を開いたとき / 直したあとに返す、まるごとの状態。

    直したあとも同じものを返す(設計.md §1)。「保存しました」だけを
    返すと、**本当に入ったかどうか**を画面が別に確かめに行くことになる。
    """
    view = frame(conn)
    view.message = message
    found = path or data_sync.find_material_db()
    view.loaded = True
    view.source = str(found) if found else ""
    if found is None:
        from .. import sync_sources
        view.source_why = sync_sources.material_db_missing_why()
    view.tables = master_admin.tables(found)

    view.table = _pick(view.tables, table)
    view.query = query or ""
    if not view.table:
        view.page = master_admin.page(None, "", query=view.query)
        return view

    # **表ごとに取り込み元が違う。** パレット適合閾値の7表は別ファイル
    # なので、梱包資材マスタを渡すと「在るのに空」に見える
    if path is None:
        found = master_admin.source_for(view.table) or found
        view.source = str(found) if found else ""

    # `frame()` はまだ表を知らない時点の判定(既定は mode:material 有無
    # だけを見る)。アクセス権限マスタだけの逃げ道(mode:field+パスワード)
    # は表が決まらないと判断できないので、表が決まった時点で引き直す
    view.can_edit, view.edit_why = master_admin.can_edit(conn, view.table)

    view.source_note = _where(found)
    view.page = master_admin.page(found, view.table, query=view.query,
                                  sort=sort, sort_dir=sort_dir)
    # 打ち込める欄は、**取り込み元に本当にある列**だけにする。
    # 上流がまだ足していない列を出すと、保存の瞬間に断られる
    view.columns = master_admin.columns(conn, view.table, view.page.columns,
                                        source_path=found)
    if (not view.columns and not view.page.missing and not view.page.error
            and view.page.editable):
        # 表はある(missing=False)のに1つも打ち込めない ── たいてい
        # 列名が想定と違う。空の編集窓を出すだけでは分からないので、
        # ここで理由を足す(既存の `why` は「見るだけの表」用なので、
        # 直せる表のこの状況では元々空)
        mismatch = master_admin.column_mismatch_why(view.table, view.page.columns)
        if mismatch:
            view.page.why = mismatch
            # sqlite3 はテキストエディタで直せない。列名を直す他の
            # 手段が無い前提で、ここから作り直せることを画面に伝える
            view.page.rebuildable = True
    return view


def _where(found: Optional[Path]) -> str:
    """書き先の言い方。直すと**このファイル**が変わる、と名前まで言う。"""
    if found is None:
        return ""
    from .. import sync_sources
    return (sync_sources.where_written(found)
            + "。手元の写しではなく元のファイルを直すので、他の端末にも効きます。")


def _pick(tables: list[master_admin.TableInfo], wanted: str) -> str:
    """出す表を決める。

    指定が無い / 取り込み元に無いときは**先頭**にする ── 空の面を出して
    「自分で選べ」とするより、いちばん触る表を開いておくほうが早い。
    """
    names = [t.table for t in tables]
    if wanted and wanted in names:
        return wanted
    return names[0] if names else ""


def to_dict(view: MasterViewModel) -> dict[str, Any]:
    return {
        "can_edit": view.can_edit,
        "edit_why": view.edit_why,
        "source_dir": view.source_dir,
        "loaded": view.loaded,
        "source": view.source,
        "source_note": view.source_note,
        "source_why": view.source_why,
        "tables": [t.to_dict() for t in view.tables],
        "table": view.table,
        "query": view.query,
        # 型の言い方はサーバが持つ。画面側で「整数」と書き分けない
        "columns": [{**c.to_dict(),
                     "kind_label": master_admin.KIND_LABEL.get(c.kind, c.kind)}
                    for c in view.columns],
        "page": view.page.to_dict() if view.page else None,
        "row_key": master_admin.ROW_KEY,
        "message": view.message,
    }
