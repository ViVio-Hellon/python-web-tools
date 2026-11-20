"""メモページの保存(VER4.9.0。Excel 風のメモ。`app/routes/memo.py`・`app/static/js/views/memo.js`)

現場の決め(2026-11-20):
    保存先        **この PC の手元の DB**(`packaging_tool.db`。ほかの PC からは見えない)
    出す画面      現場モードだけ
    シートを消す  誰でも可(確認を出す)

【表】既存の表には触らない。2つだけ足す(`ensure_tables`。`db.apply_schema` から呼ばれる。冪等)
    メモシート … 1行1シート。名前・並び・行数・列数・列幅/行高(JSON)・版・消した日時
    メモセル   … 中身のあるセルだけ。値と書式(JSON)と、そのセルを最後に変えた版
                (数式は将来用の列 `数式` を持つ。いまは使わない)

【保存】画面は変わったものだけを貯めて1回で送る(`save`)。1つの取引で書くので、全部入るか、何も入らない。
シートの版は保存のたびに1つ進む。画面は読んだときの版を添えて送る:
    - 版が同じ                 … そのまま書く
    - 版が進んでいる(別の窓で書いた)
        - 同じセルを別の窓が変えていなければ、重ねて書く(セル単位で合わせる)
        - 同じセルがぶつかる・行列の足し引きがある … 書かずに `Conflict`(画面が読み直して選ぶ)

【消す】すぐには消さず「消した日時」を付ける(`TRASH_DAYS` 日は戻せる)。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from .logging_utils import get_logger
from .sqlite_toolkit import now_db_string

log = get_logger("memo_store")

SHEETS = "メモシート"
CELLS = "メモセル"

DEFAULT_ROWS = 100
DEFAULT_COLS = 26
MAX_ROWS = 20000
MAX_COLS = 200
MAX_TEXT = 32767                 # Excel の1セルの上限と同じ
TRASH_DAYS = 30
NAME_MAX = 31                    # Excel のシート名の上限と同じ
# 書式で受け付ける鍵(ほかは捨てる。画面から何でも DB に入れない)
FORMAT_KEYS = ("b", "i", "color", "bg", "align", "valign", "wrap", "bt", "br", "bb", "bl")


class MemoError(Exception):
    """断り(画面に出す文言)。"""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code


class Conflict(MemoError):
    """別の窓が先に書いた。`cells` はぶつかったセル(いまの DB の値)。"""

    def __init__(self, message: str, cells: list[dict[str, Any]], rev: int):
        super().__init__(message, "conflict")
        self.cells = cells
        self.rev = rev


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{SHEETS}" ('
        " シートID INTEGER PRIMARY KEY AUTOINCREMENT,"
        " 名前 TEXT NOT NULL DEFAULT '',"
        " 並び INTEGER NOT NULL DEFAULT 0,"
        f" 行数 INTEGER NOT NULL DEFAULT {DEFAULT_ROWS},"
        f" 列数 INTEGER NOT NULL DEFAULT {DEFAULT_COLS},"
        " 列幅 TEXT NOT NULL DEFAULT '{}',"
        " 行高 TEXT NOT NULL DEFAULT '{}',"
        " 版 INTEGER NOT NULL DEFAULT 1,"
        " 作った日時 TEXT NOT NULL DEFAULT '',"
        " 更新日時 TEXT NOT NULL DEFAULT '',"
        " 消した日時 TEXT)")
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS "{CELLS}" ('
        " シートID INTEGER NOT NULL,"
        " 行 INTEGER NOT NULL,"
        " 列 INTEGER NOT NULL,"
        " 値 TEXT NOT NULL DEFAULT '',"
        " 数式 TEXT NOT NULL DEFAULT '',"
        " 書式 TEXT NOT NULL DEFAULT '',"
        " 版 INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (シートID, 行, 列))")


# ------------------------------------------------------------------
# 読む
# ------------------------------------------------------------------
@dataclass
class Sheet:
    id: int
    name: str
    order: int
    rows: int
    cols: int
    col_widths: dict[str, int]
    row_heights: dict[str, int]
    rev: int
    updated_at: str
    cells: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self, *, with_cells: bool = True) -> dict[str, Any]:
        out = {"id": self.id, "name": self.name, "order": self.order, "rows": self.rows, "cols": self.cols,
               "col_widths": self.col_widths, "row_heights": self.row_heights, "rev": self.rev,
               "updated_at": self.updated_at}
        if with_cells:
            out["cells"] = self.cells
        return out


def _json(text: Any) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _sheet(row: sqlite3.Row | tuple) -> Sheet:
    r = tuple(row)
    return Sheet(id=r[0], name=r[1], order=r[2], rows=r[3], cols=r[4], col_widths=_json(r[5]),
                 row_heights=_json(r[6]), rev=r[7], updated_at=r[8] or "")


_SHEET_COLS = "シートID, 名前, 並び, 行数, 列数, 列幅, 行高, 版, 更新日時"


def list_sheets(conn: sqlite3.Connection) -> list[Sheet]:
    """消していないシート(並びの順)。1枚も無ければ「Sheet1」を作る(初めて開いたとき)。"""
    found = [_sheet(r) for r in conn.execute(
        f'SELECT {_SHEET_COLS} FROM "{SHEETS}" WHERE 消した日時 IS NULL ORDER BY 並び, シートID')]
    if not found:
        create_sheet(conn, "Sheet1")
        return list_sheets(conn)
    return found


def get_sheet(conn: sqlite3.Connection, sheet_id: int) -> Sheet:
    row = conn.execute(f'SELECT {_SHEET_COLS} FROM "{SHEETS}" WHERE シートID = ? AND 消した日時 IS NULL',
                       (sheet_id,)).fetchone()
    if row is None:
        raise MemoError("そのシートはありません(消されたか、別の窓で消しました)。", "not_found")
    sheet = _sheet(row)
    sheet.cells = [_cell_out(r) for r in conn.execute(
        f'SELECT 行, 列, 値, 書式 FROM "{CELLS}" WHERE シートID = ? AND NOT (値 = \'\' AND 書式 = \'\')'
        ' ORDER BY 行, 列', (sheet_id,))]
    return sheet


def _cell_out(r) -> dict[str, Any]:
    out: dict[str, Any] = {"r": r[0], "c": r[1], "v": r[2]}
    fmt = _json(r[3])
    if fmt:
        out["f"] = fmt
    return out


# ------------------------------------------------------------------
# シートの管理
# ------------------------------------------------------------------
def _clean_name(name: Any) -> str:
    text = " ".join(str(name or "").split())
    for ch in '\\/?*[]:':
        text = text.replace(ch, "")
    if not text:
        raise MemoError("シート名を入れてください。")
    if len(text) > NAME_MAX:
        raise MemoError(f"シート名は{NAME_MAX}文字までです。")
    return text


def _name_taken(conn: sqlite3.Connection, name: str, but: Optional[int] = None) -> bool:
    return conn.execute(f'SELECT 1 FROM "{SHEETS}" WHERE 消した日時 IS NULL AND 名前 = ? AND シートID <> ?',
                        (name, but or -1)).fetchone() is not None


def _unique(conn: sqlite3.Connection, base: str) -> str:
    name, n = base, 2
    while _name_taken(conn, name):
        suffix = f" ({n})"
        name = base[:NAME_MAX - len(suffix)] + suffix
        n += 1
    return name


def create_sheet(conn: sqlite3.Connection, name: str = "") -> Sheet:
    if not str(name or "").strip():
        n = conn.execute(f'SELECT COUNT(*) FROM "{SHEETS}"').fetchone()[0] + 1
        name = f"Sheet{n}"
    name = _unique(conn, _clean_name(name))
    order = (conn.execute(f'SELECT MAX(並び) FROM "{SHEETS}" WHERE 消した日時 IS NULL').fetchone()[0] or 0) + 1
    now = now_db_string()
    with conn:
        cur = conn.execute(f'INSERT INTO "{SHEETS}" (名前, 並び, 作った日時, 更新日時) VALUES (?, ?, ?, ?)',
                           (name, order, now, now))
    log.info("メモ: シートを作りました %s", name)
    return get_sheet(conn, cur.lastrowid)


def rename_sheet(conn: sqlite3.Connection, sheet_id: int, name: str) -> Sheet:
    get_sheet(conn, sheet_id)
    name = _clean_name(name)
    if _name_taken(conn, name, sheet_id):
        raise MemoError(f"「{name}」という名前のシートがもうあります。")
    with conn:
        conn.execute(f'UPDATE "{SHEETS}" SET 名前 = ?, 更新日時 = ? WHERE シートID = ?',
                     (name, now_db_string(), sheet_id))
    return get_sheet(conn, sheet_id)


def duplicate_sheet(conn: sqlite3.Connection, sheet_id: int) -> Sheet:
    src = get_sheet(conn, sheet_id)
    name = _unique(conn, (src.name[:NAME_MAX - 4] + " のコピー")[:NAME_MAX])
    now = now_db_string()
    with conn:
        conn.execute(f'UPDATE "{SHEETS}" SET 並び = 並び + 1 WHERE 消した日時 IS NULL AND 並び > ?', (src.order,))
        cur = conn.execute(
            f'INSERT INTO "{SHEETS}" (名前, 並び, 行数, 列数, 列幅, 行高, 作った日時, 更新日時)'
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, src.order + 1, src.rows, src.cols, json.dumps(src.col_widths), json.dumps(src.row_heights),
             now, now))
        conn.execute(f'INSERT INTO "{CELLS}" (シートID, 行, 列, 値, 数式, 書式, 版)'
                     f' SELECT ?, 行, 列, 値, 数式, 書式, 1 FROM "{CELLS}" WHERE シートID = ?',
                     (cur.lastrowid, sheet_id))
    return get_sheet(conn, cur.lastrowid)


def delete_sheet(conn: sqlite3.Connection, sheet_id: int) -> None:
    """消す(「消した日時」を付ける。`TRASH_DAYS` 日は戻せる)。最後の1枚は消さない。"""
    sheet = get_sheet(conn, sheet_id)
    if len(list_sheets(conn)) <= 1:
        raise MemoError("最後の1枚は消せません。中身を消すか、先にシートを足してください。", "last_sheet")
    with conn:
        conn.execute(f'UPDATE "{SHEETS}" SET 消した日時 = ? WHERE シートID = ?', (now_db_string(), sheet_id))
    log.info("メモ: シートを消しました %s(%s日は戻せます)", sheet.name, TRASH_DAYS)
    purge_old(conn)


def trashed(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{"id": r[0], "name": r[1], "deleted_at": r[2]} for r in conn.execute(
        f'SELECT シートID, 名前, 消した日時 FROM "{SHEETS}" WHERE 消した日時 IS NOT NULL ORDER BY 消した日時 DESC')]


def restore_sheet(conn: sqlite3.Connection, sheet_id: int) -> Sheet:
    row = conn.execute(f'SELECT 名前 FROM "{SHEETS}" WHERE シートID = ? AND 消した日時 IS NOT NULL',
                       (sheet_id,)).fetchone()
    if row is None:
        raise MemoError("戻せるシートがありません。", "not_found")
    name = _unique(conn, row[0])
    order = (conn.execute(f'SELECT MAX(並び) FROM "{SHEETS}" WHERE 消した日時 IS NULL').fetchone()[0] or 0) + 1
    with conn:
        conn.execute(f'UPDATE "{SHEETS}" SET 消した日時 = NULL, 名前 = ?, 並び = ? WHERE シートID = ?',
                     (name, order, sheet_id))
    return get_sheet(conn, sheet_id)


def purge_old(conn: sqlite3.Connection, now: Optional[datetime] = None) -> int:
    limit = ((now or datetime.now()) - timedelta(days=TRASH_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    ids = [r[0] for r in conn.execute(
        f'SELECT シートID FROM "{SHEETS}" WHERE 消した日時 IS NOT NULL AND 消した日時 < ?', (limit,))]
    if ids:
        marks = ",".join("?" * len(ids))
        with conn:
            conn.execute(f'DELETE FROM "{CELLS}" WHERE シートID IN ({marks})', ids)
            conn.execute(f'DELETE FROM "{SHEETS}" WHERE シートID IN ({marks})', ids)
    return len(ids)


def move_sheet(conn: sqlite3.Connection, sheet_id: int, to_index: int) -> list[Sheet]:
    sheets = list_sheets(conn)
    ids = [s.id for s in sheets]
    if sheet_id not in ids:
        raise MemoError("そのシートはありません。", "not_found")
    ids.remove(sheet_id)
    ids.insert(max(0, min(int(to_index), len(ids))), sheet_id)
    with conn:
        for i, sid in enumerate(ids, start=1):
            conn.execute(f'UPDATE "{SHEETS}" SET 並び = ? WHERE シートID = ?', (i, sid))
    return list_sheets(conn)


# ------------------------------------------------------------------
# 保存
# ------------------------------------------------------------------
def _fmt(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    clean = {k: value[k] for k in FORMAT_KEYS if k in value and value[k] not in (None, "", False)}
    for k, v in list(clean.items()):
        if isinstance(v, str) and len(v) > 40:
            clean.pop(k)
    return json.dumps(clean, ensure_ascii=False, sort_keys=True) if clean else ""


def _index(value: Any, limit: int, what: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise MemoError(f"{what}の番号が読めません。") from None
    if not 0 <= n < limit:
        raise MemoError(f"{what}の番号が範囲の外です({n})。")
    return n


def _sizes(value: Any) -> str:
    if not isinstance(value, dict):
        raise MemoError("列幅・行高の形が違います。")
    clean = {}
    for k, v in value.items():
        try:
            clean[str(int(k))] = max(4, min(2000, int(v)))
        except (TypeError, ValueError):
            continue
    return json.dumps(clean)


_STRUCT_OPS = ("insert_rows", "delete_rows", "insert_cols", "delete_cols")


def save(conn: sqlite3.Connection, sheet_id: int, base_rev: int, ops: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """変わったものをまとめて書く(1つの取引)。新しい版を返す。

    ops(画面で起きた順):
        {"op": "cells", "cells": [{"r", "c", "v", "f"?}, ...]}   値と書式(v が空で f も無ければセルを消す)
        {"op": "insert_rows", "at", "n"} / "delete_rows" / "insert_cols" / "delete_cols"
        {"op": "size", "rows"?, "cols"?, "col_widths"?, "row_heights"?}
    """
    ops = list(ops)
    sheet = get_sheet(conn, sheet_id)
    try:
        touched = [(int(c.get("r", -1)), int(c.get("c", -1)))
                   for op in ops if op.get("op") == "cells" for c in (op.get("cells") or [])]
    except (TypeError, ValueError, AttributeError) as exc:
        raise MemoError(f"送られた内容が読めません({exc})。") from exc
    structural = any(op.get("op") in _STRUCT_OPS for op in ops)
    if base_rev != sheet.rev:
        if structural:
            raise Conflict("別の窓でこのシートが変わっています。読み直してから、もう一度行・列を足し引きしてください。",
                           [], sheet.rev)
        if touched:
            # 読んだあとに変わったセル・行列の足し引き
            later = conn.execute(f'SELECT 行, 列, 値, 書式 FROM "{CELLS}" WHERE シートID = ? AND 版 > ?',
                                 (sheet_id, base_rev)).fetchall()
            mine = set(touched)
            clash = [_cell_out(r) for r in later if (r[0], r[1]) in mine]
            if clash:
                raise Conflict(f"別の窓で同じセルが変わっています({len(clash)}か所)。", clash, sheet.rev)
    new_rev = sheet.rev + 1
    rows, cols = sheet.rows, sheet.cols
    try:
        with conn:
            for op in ops:
                kind = op.get("op")
                if kind == "cells":
                    for c in op.get("cells") or []:
                        r = _index(c.get("r"), MAX_ROWS, "行")
                        k = _index(c.get("c"), MAX_COLS, "列")
                        value = "" if c.get("v") is None else str(c.get("v"))
                        if len(value) > MAX_TEXT:
                            raise MemoError(f"1つのセルに入るのは{MAX_TEXT}文字までです。")
                        fmt = _fmt(c.get("f"))
                        rows, cols = max(rows, r + 1), max(cols, k + 1)
                        if value == "" and not fmt:
                            conn.execute(f'DELETE FROM "{CELLS}" WHERE シートID = ? AND 行 = ? AND 列 = ?',
                                         (sheet_id, r, k))
                            # 消したことも版に残す(別の窓とのぶつかりを見るため)
                            conn.execute(f'INSERT INTO "{CELLS}" (シートID, 行, 列, 値, 書式, 版) VALUES (?, ?, ?, ?, ?, ?)',
                                         (sheet_id, r, k, "", "", new_rev))
                        else:
                            conn.execute(
                                f'INSERT INTO "{CELLS}" (シートID, 行, 列, 値, 書式, 版) VALUES (?, ?, ?, ?, ?, ?)'
                                " ON CONFLICT(シートID, 行, 列) DO UPDATE SET 値 = excluded.値,"
                                " 書式 = excluded.書式, 版 = excluded.版",
                                (sheet_id, r, k, value, fmt, new_rev))
                elif kind in _STRUCT_OPS:
                    at = int(op.get("at", 0))
                    n = max(1, min(int(op.get("n", 1)), 5000))
                    axis = "行" if kind.endswith("rows") else "列"
                    limit = MAX_ROWS if axis == "行" else MAX_COLS
                    if kind.startswith("insert"):
                        if (rows if axis == "行" else cols) + n > limit:
                            raise MemoError(f"{axis}は{limit}までです。")
                        _shift(conn, sheet_id, axis, at, n)
                        if axis == "行":
                            rows += n
                        else:
                            cols += n
                    else:
                        conn.execute(f'DELETE FROM "{CELLS}" WHERE シートID = ? AND {axis} >= ? AND {axis} < ?',
                                     (sheet_id, at, at + n))
                        _shift(conn, sheet_id, axis, at + n, -n)
                        if axis == "行":
                            rows = max(1, rows - n)
                        else:
                            cols = max(1, cols - n)
                    widths = sheet.col_widths if axis == "列" else sheet.row_heights
                    moved = _shift_sizes(widths, at, n if kind.startswith("insert") else -n)
                    if axis == "列":
                        sheet.col_widths = moved
                    else:
                        sheet.row_heights = moved
                    conn.execute(f'UPDATE "{CELLS}" SET 版 = ? WHERE シートID = ?', (new_rev, sheet_id))
                elif kind == "size":
                    if "rows" in op:
                        rows = max(1, min(int(op["rows"]), MAX_ROWS))
                    if "cols" in op:
                        cols = max(1, min(int(op["cols"]), MAX_COLS))
                    if "col_widths" in op:
                        sheet.col_widths = json.loads(_sizes(op["col_widths"]))
                    if "row_heights" in op:
                        sheet.row_heights = json.loads(_sizes(op["row_heights"]))
                else:
                    raise MemoError(f"知らない操作です({kind})。")
            # 空のセルの印は版を残すだけ。古いものは片付ける(この保存で付けた印は残す)
            conn.execute(f'DELETE FROM "{CELLS}" WHERE シートID = ? AND 値 = \'\' AND 書式 = \'\' AND 版 < ?',
                         (sheet_id, new_rev))
            conn.execute(f'UPDATE "{SHEETS}" SET 行数 = ?, 列数 = ?, 列幅 = ?, 行高 = ?, 版 = ?, 更新日時 = ?'
                         " WHERE シートID = ?",
                         (rows, cols, json.dumps(sheet.col_widths), json.dumps(sheet.row_heights), new_rev,
                          now_db_string(), sheet_id))
    except (ValueError, TypeError) as exc:
        raise MemoError(f"送られた内容が読めません({exc})。") from exc
    return {"rev": new_rev, "rows": rows, "cols": cols, "saved_at": now_db_string()}


def _shift(conn: sqlite3.Connection, sheet_id: int, axis: str, start: int, delta: int) -> None:
    """`start` 以降の行(列)を `delta` だけずらす。主キーがぶつからない順に動かす。"""
    order = "DESC" if delta > 0 else "ASC"
    rows = conn.execute(f'SELECT rowid FROM "{CELLS}" WHERE シートID = ? AND {axis} >= ? ORDER BY {axis} {order}',
                        (sheet_id, start)).fetchall()
    for (rowid,) in rows:
        conn.execute(f'UPDATE "{CELLS}" SET {axis} = {axis} + ? WHERE rowid = ?', (delta, rowid))


def _shift_sizes(sizes: dict[str, int], at: int, delta: int) -> dict[str, int]:
    out: dict[str, int] = {}
    for k, v in sizes.items():
        i = int(k)
        if delta < 0 and at <= i < at - delta:
            continue
        out[str(i + delta if i >= (at if delta > 0 else at - delta) else i)] = v
    return out
