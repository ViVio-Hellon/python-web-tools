"""中身を見る ── 表の一覧と、1ページぶんの行

**直せない表も出す。** 中身を確かめられることと書き換えられることは
別の話で、見られないと「取り込めているのか」を誰も確かめられない。

行は `ROW_LIMIT` 件ずつ。取り込み元の暗黙の `rowid` を `__行` という
名前で持ち回る ── 手元の管理番号は取り込みのときに振り直しているので、
取り込み元の行を指さない。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# **他の段は名前ではなくモジュールで呼ぶ。** 差し替え(試験の stub)の
# 当て先が持ち主の1か所で済む
from . import config, data_sync, db, import_specs, source_db
from . import master_common
from .logging_utils import get_logger
from .master_common import (BY_TABLE, MANAGED, REFUSE_ALREADY,
                            REFUSE_BAD_VALUE, REFUSE_NOT_ALLOWED,
                            REFUSE_NOT_CREATABLE, REFUSE_NOT_EDITABLE,
                            REFUSE_NO_ROW, REFUSE_NO_SOURCE,
                            REFUSE_WRITE_FAILED, ROW_KEY, ROW_LIMIT,
                            STAMP_COLUMNS, Managed, Result, _follow, _label,
                            _write_failed, can_edit, source_dir, source_for,
                            source_label, view_only_why)
from . import master_columns
from . import master_schema

log = get_logger("master_admin.browse")

@dataclass
class TableInfo:
    """一覧に出す表1つ。"""

    table: str
    label: str
    mark: str
    note: str
    rows: int
    editable: bool
    why: str = ""
    # 取り込み元に無い(が、作れる)表。**隠さずに出す**
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "label": self.label, "mark": self.mark,
                "note": self.note, "rows": self.rows,
                "editable": self.editable, "why": self.why,
                "missing": self.missing}


def tables(path: Optional[Path],
           threshold_path: Optional[Path] = None) -> list[TableInfo]:
    """取り込み元にある表ぜんぶ。**直せないものも出す。**

    直せる表だけを出すと「あるはずの表が無い」に見えます。中身を
    確かめるのは全部の表でできるので、並べたうえで直せるかどうかを
    札にします。並びは `MANAGED` が先(触る頻度の順)、残りは名前順。

    **作れる表は、取り込み元に無くても並びに出します。** 出さないと
    「無い表は画面にも無い」になり、アクセス権限を一度も入れていない
    端末では、モードを決める場所がどこにも見えません。

    **取り込み元は1つではありません。** パレット適合閾値の7表は別の
    ファイル(`PalletThresholdMaster.sqlite3`)にあるので、そちらの
    件数はそちらを数えます ── 梱包資材マスタだけを見ていると、
    在るのに「無い」と出ます(`source_for`)。
    """
    if path is None:
        return []
    counts = source_db.table_counts(path)
    if not counts:
        return []
    if threshold_path is None:
        threshold_path = data_sync.find_threshold_db()
    if threshold_path is not None and threshold_path != path:
        for name, rows in source_db.table_counts(threshold_path).items():
            if name in import_specs.THRESHOLD_TABLES:
                counts[name] = rows
    out: list[TableInfo] = []
    for managed in MANAGED:
        if managed.table not in counts:
            if master_schema.can_create(managed.table):
                out.append(TableInfo(
                    table=managed.table, label=managed.table,
                    mark=managed.mark, note=managed.note, rows=0,
                    editable=True, missing=True,
                    why=master_schema._create_why(managed.table)))
            continue
        out.append(TableInfo(
            table=managed.table, label=managed.table, mark=managed.mark,
            note=managed.note, rows=counts[managed.table], editable=True))
    # 「表を持ってくる」で足した表は、直せる表として資材課の表のすぐ後に出す。
    # 記録の表そのもの(`BROUGHT_REGISTRY`)は出さない(中身は表の名前だけ)
    brought = master_common.brought_tables(path)
    for name in sorted(n for n in counts if n in brought and n not in BY_TABLE):
        out.append(TableInfo(
            table=name, label=name, mark="足", note="Access から持ってきた表",
            rows=counts[name], editable=True))
    for name in sorted(n for n in counts if n not in BY_TABLE and n not in brought
                       and n != master_common.BROUGHT_REGISTRY):
        out.append(TableInfo(
            table=name, label=name, mark="他", note="", rows=counts[name],
            editable=False, why=view_only_why(name)))
    return out

@dataclass
class Page:
    """1つの表の中身(の一部)。"""

    table: str
    label: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    editable: bool = False
    why: str = ""
    note: str = ""
    error: str = ""
    # 取り込み元に無い(が、作れる)表
    missing: bool = False
    # 取り込み元にはあるが、列名が想定と1つも合わず、作り直せば直る表
    rebuildable: bool = False
    # いま並び替えている列。空なら既定(rowid、取り込み順)
    sort: str = ""
    sort_dir: str = "asc"
    # この表を消せるか(このツールが使わない表だけ)と、消す前に言うこと
    droppable: bool = False
    drop_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "label": self.label,
                "columns": self.columns, "rows": self.rows,
                "total": self.total, "shown": len(self.rows),
                "editable": self.editable, "why": self.why,
                "note": self.note, "error": self.error,
                "missing": self.missing, "rebuildable": self.rebuildable,
                "row_key": ROW_KEY,
                "sort": self.sort, "sort_dir": self.sort_dir,
                "droppable": self.droppable, "drop_note": self.drop_note}


def page(path: Optional[Path], table: str, *, query: str = "",
         sort: str = "", sort_dir: str = "asc",
         limit: int = ROW_LIMIT) -> Page:
    """表の中身を読む。**取り込み元から直に読む。**

    手元の写しではなく元を読むのは、直したあと「本当に入ったか」を
    ここで確かめられるようにするためです。写しを見せると、書き込みが
    失敗していても画面上は直ったように見えます。

    `sort` は列名(見出しクリック)。**取り込み元に実在する列だけ**を
    許す ── 列名をそのまま `ORDER BY` に組み込むので、絞り込み
    (`_filter`)と同じく許可リストで確かめてから使う。無効な指定は
    黙って既定(`rowid`)に戻す(拒否すると押しただけで断られる画面になる)。
    """
    managed = BY_TABLE.get(table)
    brought = table in master_common.brought_tables(path)
    view = Page(table=table,
                label=table,
                editable=managed is not None or brought,
                why="" if brought else view_only_why(table))
    if path is None:
        from . import sync_sources
        view.error = sync_sources.material_db_missing_why()
        return view

    names = source_db.columns(path, table)
    if not names:
        if master_schema.can_create(table):
            # **断りではなく、次にできること。** ここで「ありません」と
            # だけ言うと、直しようが無い故障に見える
            view.missing = True
            view.why = master_schema._create_why(table)
            return view
        view.error = f"{table} は取り込み元にありません。"
        return view
    view.columns = names
    if not master_schema.drop_why(table, path):
        view.droppable = True
        view.drop_note = master_schema.drop_note(table, path)

    where, params = _filter(names, query)
    order, sort_col = _order(names, sort, sort_dir)
    view.sort = sort_col
    view.sort_dir = "desc" if sort_dir == "desc" else "asc"
    quoted = source_db.quote_identifier(table)
    try:
        count = source_db.read_query(
            path, f"SELECT COUNT(*) AS n FROM {quoted}{where}", params)
        view.total = int(count[0]["n"]) if count else 0
        view.rows = source_db.read_query(
            path,
            f'SELECT rowid AS "{ROW_KEY}", * FROM {quoted}{where}'
            f" {order} LIMIT ?", [*params, max(1, limit)])
    except source_db.SourceError as exc:
        view.rows = []
        view.error = str(exc)
        return view

    hidden = view.total - len(view.rows)
    if hidden > 0:
        # **黙って切らない。** 絞り込みの手があることまで言う
        view.note = (f"{view.total}件のうち {len(view.rows)}件を出しています"
                     f"(ほか {hidden}件)。絞り込むと目当ての行が出ます。")
    return view


def _order(names: list[str], sort: str, sort_dir: str) -> tuple[str, str]:
    """見出しクリックの並び替え。

    `sort` が実在の列でなければ、押していないのと同じ(`rowid` の
    既定順)へ静かに戻す ── マスタの列は取り込み元の都合で増減するので、
    もう無い列を指した並び替えを断ると「さっきまで押せたのに」が起きる。
    """
    if sort and sort in names:
        direction = "DESC" if sort_dir == "desc" else "ASC"
        quoted = source_db.quote_identifier(sort)
        blank, numeric, text = _sort_parts(quoted)
        # **数値は数値として、空欄は最後に並べる**(Excel と同じ並び)。
        #
        # 取り込み元の表は列に型が付いていないことが多い(変換ツールは
        # `CREATE TABLE x ("幅", ...)` と型を書かずに作る)。値が文字で
        # 入っているので、そのまま ORDER BY すると辞書順になり
        #     (空) 1000 (空) 1100 1100.5 12A 200 95
        # のように「200 が 1100 より後ろ」「空欄が途中に混ざる」並びに
        # なっていた ── 見出しを押しても並び替わっていないように見える。
        #
        #   1. 空欄(NULL・空文字・空白だけ)は向きに関係なく最後
        #   2. 数値と文字が混ざる列は、昇順で 数値→文字、降順で 文字→数値
        #   3. 数値は大きさで、文字は文字で比べる
        #   4. 同値が並ぶと表示順がページごとに揺れるので、rowidで確定させる
        return (f"ORDER BY {blank} ASC,"
                f" (CASE WHEN {numeric} THEN 0 ELSE 1 END) {direction},"
                f" (CASE WHEN {numeric} THEN CAST({quoted} AS REAL) END) {direction},"
                f" {text} {direction}, rowid ASC"), sort
    return "ORDER BY rowid ASC", ""


def _sort_parts(quoted: str) -> tuple[str, str, str]:
    """並び替えに使う式 3 つ(空欄か / 数値か / 比べる文字)。

    「数値か」は、数値で入っている値に加え、**文字で入っていても数値の
    形をしている値**(`1100`, `-5`, `0.25`)を数値とみなす。`12A`・
    `1-2`・`1.2.3` のように数値以外が混ざるものは文字のまま。
    """
    # NULL も空文字として扱う(空欄どうしは取り込み順で並ぶ。NULL と空文字で
    # 向きによって前後が入れ替わる、を起こさない)
    text = f"COALESCE(TRIM(CAST({quoted} AS TEXT)), '')"
    digits = f"LTRIM({text}, '+-')"
    blank = f"({text} = '')"
    numeric = (f"(typeof({quoted}) IN ('integer', 'real')"
               f" OR ({digits} <> ''"
               f" AND {digits} NOT GLOB '*[^0-9.]*'"
               f" AND {digits} GLOB '*[0-9]*'"
               f" AND {digits} NOT GLOB '*.*.*'"
               f" AND LENGTH({text}) - LENGTH({digits}) <= 1))")
    return blank, numeric, text


def _filter(names: list[str], query: str) -> tuple[str, list[Any]]:
    """絞り込みの条件。**どの列でもいい**ので、全部の列を見る。

    どの列に何が入っているかを覚えていなくても引けるようにする。
    """
    text = (query or "").strip()
    if not text:
        return "", []
    conds = " OR ".join(
        f"CAST({source_db.quote_identifier(n)} AS TEXT) LIKE ?" for n in names)
    return f" WHERE ({conds})", [f"%{text}%"] * len(names)
