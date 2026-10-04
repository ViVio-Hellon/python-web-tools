"""梱包資材マスタの管理 ── 中身を見る / 直す

【なぜ要るのか】
梱包資材マスタは起動のたびに**黙って読まれるだけ**でした。中に何が
入っているかを見る手立ても、間違いを直す手立ても画面にありません。
値が違っていても、現場には「パレットが出ない」「ボードの候補が
足りない」という形でしか現れず、原因に辿り着けません。
確かめる場所と、直す場所をここに作ります。

【どこを直すのか ── 取り込み元**だけ**】
取り込みは総入れ替えです(`data_sync.import_tables`)。手元のDBを
直しても、次の取り込みで消えます。**同じ事実を2か所に持たない**ので、
書き先は取り込み元(共有フォルダの sqlite3)ただ1つにして、書いた
あとにその表だけ取り込み直します。

    画面 → 取り込み元へ書く → その表だけ取り込み直す → 手元が追いつく

【行をどう指すのか】
sqlite3 の暗黙の `rowid` を使います。取り込みのとき管理番号は手元で
振り直している(`import_specs` の冒頭)ので、**手元の管理番号は
取り込み元の行を指しません**。取り込み元の行は取り込み元の言葉で
指す必要があります。

【誰が直せるのか】
資材モードを持つ端末だけです。マスタは資材課のもので、現場から
書き換えられると「何が正しいのか」が分からなくなります。
ただし**まだ誰も登録されていないとき**は塞ぎません ── アクセス権限
マスタの最初の1行をここから入れられなければ、資材モードに入る手立てが
どこにもなくなるからです(本ツールの権限の行が1行でもあれば、その時点で
閉まります。ほかのツールの行だけでは閉まりません)。

【役割ごとに分けてある】
ここは**外向けの1枚窓**で、中身は役割ごとのモジュールが持つ。

    master_common   どの表を、誰が、どこで直せるか。書いたあとの
                    取り込み直しまで
    master_columns  列の形。何を打ち込めるか、列名が合っているか
    master_schema   表そのものを作る・作り直す・列を足す
    master_browse   中身を見る(表の一覧と1ページぶんの行)
    ここ            行を直す・足す・消す

**このツール固有なのは `master_common` の `MANAGED` と
`VIEW_ONLY_WHY` だけ。** 他のツールへ持っていくときは、そこと
`import_specs`(列の対応表)を差し替えれば残りはそのまま動く。
"""
from __future__ import annotations

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config, db, import_specs, source_db
from .logging_utils import get_logger

# ------------------------------------------------------------------
# **ここは外向けの1枚窓。** 中身は役割ごとのモジュールが持つ。
#
#     master_common   どの表を、誰が、どこで直せるか。書いたあとの
#                     取り込み直しまで
#     master_columns  列の形。何を打ち込めるか、列名が合っているか
#     master_schema   表そのものを作る・作り直す・列を足す
#     master_browse   中身を見る(表の一覧と1ページぶんの行)
#     ここ            行を直す・足す・消す
#
# **他の段は名前ではなくモジュールで呼ぶ** ── 差し替え(試験の stub)の
# 当て先が持ち主の1か所で済む(`data_sync` を分けたときの教訓)。
# ------------------------------------------------------------------
from . import master_browse, master_columns, master_common, master_schema
from .master_common import (  # noqa: F401
    ADDED_REGISTRY, BY_TABLE, DEFAULT_VIEW_ONLY, MANAGED, REFUSE_ALREADY,
    REFUSE_BAD_VALUE, REFUSE_NEED_CONFIRM, added_columns,
    REFUSE_NOT_ALLOWED, REFUSE_NOT_CREATABLE, REFUSE_NOT_EDITABLE,
    REFUSE_NO_ROW, REFUSE_NO_SOURCE, REFUSE_WRITE_FAILED, ROW_KEY, ROW_LIMIT,
    STAMP_COLUMNS, VIEW_ONLY_WHY, Managed, Result, _follow, _label,
    _write_failed, can_edit, source_dir, source_for, source_label,
    view_only_why)
from .master_columns import (  # noqa: F401
    KIND_LABEL, Column, column_mismatch_why, columns,
    expected_column_names)
from .master_schema import (  # noqa: F401
    ADD_KINDS, add_column, add_column_note, add_column_why, can_create,
    can_rebuild, create_table, creatable_tables, drop_note, drop_table,
    drop_why, rebuild_table, tool_tables)
from .master_browse import Page, TableInfo, page, tables  # noqa: F401

log = get_logger("master_admin")


# ==================================================================
# 直す
# ==================================================================
@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""


# 画面を開いたあとに、その行がほかで変わっていた(消えた・入れ替わった)
STALE_ROW = ("その行は、一覧を出したあとにほかで変わっています(消された・"
             "中身が入れ替わった など)。一覧を出し直してから、もう一度開いてください。")


def save_row(conn: sqlite3.Connection, table: str, row_key: Any,
             values: dict[str, Any], *,
             path: Optional[Path] = None,
             seen: Optional[dict[str, Any]] = None) -> Result:
    """1行を書き換える。`seen` は画面が見ていたその行(`_write_row`)。"""
    path, refused = _ready(conn, table, path)
    if refused:
        return refused

    clean, problem = _clean(conn, table, values, filling=False,
                            present=source_db.columns(path, table), source_path=path)
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)
    if not clean:
        return Result(False, "変える値がありません。", REFUSE_BAD_VALUE)

    q = source_db.quote_identifier
    sets = ", ".join(f"{q(k)} = ?" for k in clean)
    try:
        changed = _write_row(path, table, row_key, seen,
                             f"UPDATE {q(table)} SET {sets} WHERE rowid = ?",
                             [*clean.values(), row_key])
    except source_db.SourceError as exc:
        return _write_failed(table, exc)
    if not changed:
        return Result(False, STALE_ROW, REFUSE_NO_ROW)

    log.info("マスタを直しました: %s rowid=%s %s", table, row_key, sorted(clean))
    return Result(True, f"{_label(table)}の1行を直しました{_follow(conn, path, table)}")


def add_row(conn: sqlite3.Connection, table: str, values: dict[str, Any], *,
            path: Optional[Path] = None) -> Result:
    """1行足す。"""
    path, refused = _ready(conn, table, path)
    if refused:
        return refused

    clean, problem = _clean(conn, table, values, filling=True,
                            present=source_db.columns(path, table), source_path=path)
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)
    if not clean:
        return Result(False, "入れる値がありません。", REFUSE_BAD_VALUE)

    try:
        with source_db.connect(path) as src:
            src.insert(table, clean)
    except source_db.SourceError as exc:
        return _write_failed(table, exc)

    log.info("マスタに足しました: %s %s", table, sorted(clean))
    return Result(True, f"{_label(table)}に1行足しました{_follow(conn, path, table)}")


def delete_row(conn: sqlite3.Connection, table: str, row_key: Any, *,
               path: Optional[Path] = None,
               seen: Optional[dict[str, Any]] = None) -> Result:
    """1行消す。`seen` は画面が見ていたその行(`_write_row`)。"""
    path, refused = _ready(conn, table, path)
    if refused:
        return refused

    try:
        changed = _write_row(path, table, row_key, seen,
                             f"DELETE FROM {source_db.quote_identifier(table)}"
                             " WHERE rowid = ?", [row_key])
    except source_db.SourceError as exc:
        return _write_failed(table, exc)
    if not changed:
        return Result(False, STALE_ROW, REFUSE_NO_ROW)

    log.info("マスタから消しました: %s rowid=%s", table, row_key)
    return Result(True, f"{_label(table)}の1行を消しました{_follow(conn, path, table)}")


def _write_row(path: Path, table: str, row_key: Any, seen: Optional[dict[str, Any]],
               sql: str, params: list[Any]) -> int:
    """その行がまだ画面で見ていたとおりなら書く。書いた行数(0 = 変わっていた)。

    【なぜ rowid だけでは足りないか】
    行は取り込み元の `rowid` で指す。ところが rowid は**同じ番号が別の行に
    付き直る**ことがある:
    - 「表を持ってくる」の入れ替えで中身を消して入れ直すと、主キーの無い表
      (Access から変換した表はみなそう)は 1 から振り直される
    - 最後の行を消してから1行足すと、足した行に同じ番号が付く
    画面を開いたままほかでこれが起きると、古い画面から直した値が**別の行**に
    入る(通しの試験で起きた)。そこで、画面が見ていたその行の値(`seen`)が
    いまも同じときだけ書く。確かめと書き込みは1回で確定する(間に割り込ませない)。
    `seen` が無い呼び出し(画面を通らない道具)は番号だけで書く。
    """
    q = source_db.quote_identifier
    with source_db.connect(path) as src:
        with src.transaction() as tx:
            now = tx.query(f"SELECT * FROM {q(table)} WHERE rowid = ?", [row_key])
            if not now:
                return 0
            if seen is not None and not _same_row(now[0], seen):
                return 0
            return tx.execute(sql, params)


def _same_row(now: dict[str, Any], seen: dict[str, Any]) -> bool:
    """画面が見ていた値と、いまの値が同じか。画面から来た列だけを比べる。"""
    for name, was in seen.items():
        if name not in now:
            continue                        # 行の鍵(`ROW_KEY`)や、もう無い列
        if not _same_value(now[name], was):
            return False
    return True


def _same_value(now: Any, was: Any) -> bool:
    # 画面との行き来は JSON なので、数は数・文字は文字のまま戻る。
    # ただし 1 と 1.0 は同じと見る(JSON で区別が消えることがある)
    if now is None or was is None:
        return now is None and was is None
    if isinstance(now, (int, float)) and isinstance(was, (int, float)) \
            and not isinstance(was, bool):
        return float(now) == float(was)
    return str(now) == str(was)


def _ready(conn: sqlite3.Connection, table: str, path: Optional[Path],
           ) -> tuple[Optional[Path], Optional[Result]]:
    """書く前に通す関門。通れば `(ファイル, None)`、通らなければ理由。

    順番に意味がある ── **権限 → 表 → 届くか → 表が本当にあるか**。
    届かないことを先に言うと、権限が無い人に「共有が落ちている」と
    読ませてしまう。

    【表が本当にあるかを、ここでも確かめる理由】
    `MANAGED`(=`BY_TABLE`)に載っているだけでは、**取り込み元に実在する
    とは限らない**(`アクセス権限` はあとから足した表で、無い端末が
    普通にある)。ここを確かめずに書こうとすると、`source_db.columns`
    が空を返し、`_clean` がどの値も「取り込み元に無い列」として
    黙って弾く。結果、押した人には「入れる値がありません」としか
    見えず、**表が無いこと**という本当の理由に辿り着けない
    (現場の声)。画面側(`master.js`)にも同じ防御を置いているが、
    直接APIを叩かれた場合や、二重にタブを開いていた場合のために、
    ここでも確かめる。
    """
    allowed, why = can_edit(conn, table)
    if not allowed:
        return None, Result(False, why, REFUSE_NOT_ALLOWED)
    found = path or source_for(table)
    if table not in BY_TABLE and table not in master_common.brought_tables(found):
        if found is not None and not source_db.columns(found, table):
            # 開いていた表を、ほかの端末が消した。「直さない表」と言うと、
            # 表があるのに断られたように読める
            return None, Result(False, f"{_label(table)}はもう梱包資材マスタにありません"
                                       "(ほかで消されました)。一覧を出し直してください。",
                                REFUSE_NO_ROW)
        return None, Result(False, view_only_why(table) or "直せない表です。",
                            REFUSE_NOT_EDITABLE)
    if found is None:
        return None, Result(False,
                            f"{source_label(table)}が見つかりません。"
                            f"{source_dir(table)} を確かめてください。",
                            REFUSE_NO_SOURCE)
    present = source_db.columns(found, table)
    if not present:
        if master_schema.can_create(table):
            return None, Result(
                False,
                f"{_label(table)}はまだ取り込み元にありません。"
                "先に「取り込み元に作る」で表を作ってください。",
                REFUSE_NOT_CREATABLE)
        return None, Result(False, f"{_label(table)}は取り込み元にありません。",
                            REFUSE_NOT_CREATABLE)
    mismatch = master_columns.column_mismatch_why(table, present)
    if mismatch:
        return None, Result(
            False,
            f"{_label(table)}は列名が想定と違うため、1つも打ち込めません。"
            "「列名を直して作り直す」で表を作り直してください。",
            REFUSE_NOT_CREATABLE)
    return found, None

def _clean(conn: sqlite3.Connection, table: str, values: dict[str, Any],
           *, filling: bool,
           present: Optional[Iterable[str]] = None,
           source_path: Optional[Path] = None) -> tuple[dict[str, Any], str]:
    """画面から来た値を、取り込み元へ入れられる形にする。

    `filling` が真なら新しい行なので、送られてこなかった必須の列も
    見る。偽なら書き換えなので、**送られてきた列だけ**を触る
    (送っていない列を消さないため)。
    """
    out: dict[str, Any] = {}
    for column in master_columns.columns(conn, table, present, source_path=source_path):
        if column.name not in values and not filling:
            continue
        raw = str(values.get(column.name, "")).strip()
        if raw == "":
            if column.stamp:
                out[column.name] = db.now_db_string()
                continue
            if column.required:
                return {}, f"「{column.name}」は空にできません。"
            # 空欄は「無し」。取り込みのときに手元の既定値で埋まる
            out[column.name] = None
            continue
        if column.kind == "int":
            try:
                out[column.name] = int(float(raw))
            except (ValueError, OverflowError):
                return {}, f"「{column.name}」は{KIND_LABEL['int']}で入れてください。"
        elif column.kind == "real":
            try:
                out[column.name] = float(raw)
            except ValueError:
                return {}, f"「{column.name}」は{KIND_LABEL['real']}で入れてください。"
        else:
            out[column.name] = raw
    return out, ""
