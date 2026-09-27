"""列の形 ── 何を打ち込めるか、取り込み元と食い違っていないか

打ち込める列は**取り込みが読む列**(`import_specs`)と同じ。取り込みが
読まない列を直しても、次の取り込みで消えるだけなので出さない。

`column_mismatch_why` は、表はあるのに列名が想定と違う場合の説明。
1つも打ち込めない表になるので、黙って空の表を出さずに理由を言う。
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

log = get_logger("master_admin.columns")

@dataclass(frozen=True)
class Column:
    """直せる列1つ。"""

    name: str
    kind: str               # "int" | "real" | "text"
    required: bool = False
    stamp: bool = False     # 空欄なら今の日時が入る
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind,
                "required": self.required, "note": self.note}


# 画面に出す型の名前。**サーバが言葉を持つ**(画面側で書き分けない)
KIND_LABEL = {"int": "整数", "real": "小数", "text": "文字"}


def _brought_columns(path: Path, table: str) -> list[Column]:
    """「表を持ってくる」で足した表の列。**取り込み元の表の定義**から作る。

    この表はこのツールが取り込まない(`IMPORT_SPECS` に無い)ので、型も
    必須かどうかも、足したときの定義(Access の型、変換ツール経由なら型なし)
    がそのまま答え。`INTEGER PRIMARY KEY` の列(番号が自動で振られる)は空欄で通す。

    **型の書いていない列は、いま入っている値に合わせる**(`_kind_by_values`)。
    変換ツールは列に型を書かないが、値は読み取り方しだいで数にも文字にもなる
    (pyodbc なら数、access_parser なら文字)。数ばかりの列へ打った 20 を文字で
    入れると、同じ列に数と文字が混ざって並べ替えがずれる。
    """
    try:
        rows = source_db.read_query(
            path, f"PRAGMA table_info({source_db.quote_identifier(table)})")
    except source_db.SourceError as exc:
        log.warning("%s の列を引けません: %s", table, exc)
        return []
    out: list[Column] = []
    for row in rows:
        declared = str(row.get("type") or "")
        auto_number = bool(row.get("pk")) and "INT" in declared.upper()
        stamp = row["name"] in STAMP_COLUMNS
        required = (bool(row.get("notnull")) and row.get("dflt_value") is None
                    and not auto_number and not stamp)
        note = ("空欄なら番号が自動で入ります" if auto_number
                else "空欄なら今の日時が入ります" if stamp else "")
        kind = _kind_of(declared) if declared else _kind_by_values(path, table, row["name"])
        out.append(Column(name=row["name"], kind=kind,
                          required=required, stamp=stamp, note=note))
    return out


def _kind_by_values(path: Path, table: str, column: str) -> str:
    """型の書いていない列の型を、入っている値から決める。空の列・混ざった列は文字。"""
    q = source_db.quote_identifier
    try:
        rows = source_db.read_query(
            path, f"SELECT typeof({q(column)}) AS t, COUNT(*) AS n FROM {q(table)}"
                  f" WHERE {q(column)} IS NOT NULL GROUP BY 1")
    except source_db.SourceError:
        return "text"
    seen = {r["t"] for r in rows}
    if seen == {"integer"}:
        return "int"
    if seen and seen <= {"integer", "real"}:
        return "real"
    return "text"


def columns(conn: sqlite3.Connection, table: str,
            present: Optional[Iterable[str]] = None, *,
            source_path: Optional[Path] = None) -> list[Column]:
    """その表で直せる列。

    出どころは3つとも既にあるものを読むだけ ── **同じ事実を書き足さない**。

        どの列を扱うか   … `import_specs.IMPORT_SPECS`(取り込みが読む列)
        どんな型か       … 手元のスキーマ(`schema.sql`)
        本当にあるか     … 取り込み元の列(`present`)

    取り込み元にしか無い列(管理番号など)はここに出ません。触らずに
    そのまま残します。逆に**取り込み元に無い列も出しません** ── 出すと
    打ち込めてしまい、保存の瞬間に「そんな列は無い」と断られます。
    上流が列を足していないことは取り込みの結果が言うので、ここでは黙って
    外します。
    """
    spec = import_specs.IMPORT_SPECS.get(table, [])
    if not spec:
        # 持ってきた表は、取り込み元の定義から列を作る
        if source_path is not None and table in master_common.brought_tables(source_path):
            return _brought_columns(source_path, table)
        return []
    allowed = None if present is None else set(present)
    info: dict[str, sqlite3.Row] = {}
    try:
        for row in conn.execute(
                f"PRAGMA table_info({source_db.quote_identifier(table)})"):
            info[row["name"]] = row
    except sqlite3.Error as exc:                 # pragma: no cover - 通常は無い
        log.warning("%s の列を引けません: %s", table, exc)
        return []

    out: list[Column] = []
    for local, source, _conv in spec:
        row = info.get(local)
        if row is None:
            continue
        if allowed is not None and source not in allowed:
            continue
        kind = _kind_of(str(row["type"]))
        stamp = local in STAMP_COLUMNS
        # 既定値のある列は空欄で通してよい。NULL も既定も無い列だけが必須
        required = bool(row["notnull"]) and row["dflt_value"] is None and not stamp
        out.append(Column(
            name=local, kind=kind, required=required, stamp=stamp,
            note="空欄なら今の日時が入ります" if stamp else ""))
    return out


def expected_column_names(table: str) -> list[str]:
    """この表で取り込みが読もうとする、取り込み元の列名。

    `IMPORT_SPECS` の `source` 側(取り込み元での呼び名)を並べただけ。
    `columns()` が0件を返したとき、「取り込み元にこの表はあるのに、
    なぜ1つも打ち込めないのか」を具体的に言うために使う。
    """
    return [source for _local, source, _conv in import_specs.IMPORT_SPECS.get(table, [])]


def column_mismatch_why(table: str, present: Iterable[str]) -> str:
    """**表はある。列名が期待と違うので、1つも打ち込めない。**

    取り込み元に表そのものは存在する(`missing=False`)のに、`columns()`が
    0件を返すのは、たいていこれが原因。「まだ取り込まれていません」
    (`_create_why`)とは別の話で、専用の理由を出さないと、編集の窓が
    ただ空になって何も打てない画面にしか見えない
    (現場の声:「1行足す」を押しても入力欄が1つも出てこない)。
    """
    expected = expected_column_names(table)
    if not expected:
        return ""
    have = set(present)
    if have & set(expected):
        # 一部でも一致していれば、これは別の状況(型違い等)。ここでは
        # 「1つも無い」ときだけに絞る
        return ""
    return (f"{table} は取り込み元にありますが、列名が想定と違うため"
           f"1つも打ち込めません。このツールが読む列名は "
           + " / ".join(expected) +
           f" です。取り込み元の実際の列名({', '.join(present) or '(列が無い)'})"
           "と見比べて、列名を合わせてください。")




def _kind_of(declared: str) -> str:
    upper = declared.upper()
    if "INT" in upper:
        return "int"
    if "REAL" in upper or "FLOA" in upper or "DOUB" in upper:
        return "real"
    return "text"
