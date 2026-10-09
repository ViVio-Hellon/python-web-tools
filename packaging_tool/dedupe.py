"""書き戻す表にできた**同じ内容の行**を数える / 片付ける

設定画面(「取り込み」の面の「同じ内容の行」)と `scripts/dedupe_writeback.py` の中身。

【何を重複とみなすか】
**管理番号と送信IDを除いた全部の列が一致する行**。どちらも「送るときに振られる番号」で、
中身ではない。一致した組の中で**いちばん小さい番号だけを残し**、残りを消す(先に入った行が
本物で、あとから増えたのが写し)。

【共有と手元】
設定画面の警告は手元の作業用DBを数えている。手元は取り込みのたびに共有の中身で入れ替わる
ので、片付けるのは**共有**。共有を片付けてから取り込み直せば手元も直る。共有に無く手元に
だけあるなら、取り込み直すだけでよい(`fix_shared` は消すものが無ければ何も書かない)。

【消す前に必ず控えを取る】
共有を書き換える前に、同じフォルダへ `<名前>.bak-YYYYMMDDHHMMSS.sqlite3` として丸ごと写す。
数え直しと削除は**1つの書き込みトランザクション**で行う ── 数えてから消すまでのあいだに
ほかの端末が書いた行を、古い数えで消さないため。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import source_db
from .logging_utils import get_logger

log = get_logger("dedupe")

# 中身の比較から外す列。**送るときに振られる番号**であって中身ではない
IGNORED = ("管理番号", "id", "送信ID")
ROW = "__行"            # 行を指す番号(rowid)を入れておく名前


@dataclass
class Group:
    """重なっている1組。`rows` は組の行(先頭が残す行)。"""

    rows: list[dict[str, Any]]
    names: list[str]

    def describe(self, width: int = 6) -> str:
        first = self.rows[0]
        return " / ".join(f"{n}={first[n]}" for n in self.names[:width])

    def ids(self) -> list[str]:
        out = []
        for row in self.rows:
            ids = " ".join(f"{n}={row[n]}" for n in IGNORED if n in row)
            out.append(ids or f"rowid={row[ROW]}")
        return out


@dataclass
class TableCount:
    table: str
    total: int = 0
    drop: list[int] = field(default_factory=list)      # 消してよい行(rowid)
    groups: list[Group] = field(default_factory=list)
    error: str = ""                                     # 数えられなかった理由
    missing: bool = False                               # その表がまだ無い(数えるものが無い)


def group_rows(rows: Iterable[dict[str, Any]]) -> tuple[list[int], list[Group], int]:
    """行(`ROW` に rowid を入れたもの)を中身で組にする。

    戻り値は (消してよい rowid, 2行以上の組, 全体の行数)。組の中の先頭(先に入った行)は残す。
    """
    rows = sorted(rows, key=lambda r: r[ROW])
    if not rows:
        return [], [], 0
    names = [n for n in rows[0] if n not in IGNORED and n != ROW]
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(tuple(row[n] for n in names), []).append(row)
    many = [Group(members, names) for members in groups.values() if len(members) > 1]
    drop = [row[ROW] for g in many for row in g.rows[1:]]
    return sorted(drop), many, len(rows)


def count_conn(conn: Any, table: str) -> TableCount:
    """開いた接続(`sqlite3.Connection` / `SourceTransaction`)で1つの表を数える。"""
    quoted = source_db.quote_identifier(table)
    sql = f"SELECT rowid AS {ROW}, * FROM {quoted}"
    if isinstance(conn, sqlite3.Connection):
        cursor = conn.execute(sql)
        names = [d[0] for d in cursor.description or []]
        rows = [dict(zip(names, r)) for r in cursor.fetchall()]
    else:
        rows = conn.query(sql)
    drop, groups, total = group_rows(rows)
    return TableCount(table, total, drop, groups)


def _tables(tables: Optional[Iterable[str]]) -> list[str]:
    if tables is not None:
        return list(tables)
    from .sync_writeback import WRITEBACK_SPECS
    return [s.sqlite_table for s in WRITEBACK_SPECS]


def count_path(path: Path, tables: Optional[Iterable[str]] = None) -> list[TableCount]:
    """ファイル(共有の梱包資材マスタ・手元の作業用DB)の表ごとの重なり。読むだけ。

    表が無い・読めないときは `error` に理由を入れて続ける(1つで全部を止めない)。
    """
    out = []
    for table in _tables(tables):
        try:
            rows = source_db.read_query(
                path, f"SELECT rowid AS {ROW}, * FROM {source_db.quote_identifier(table)}")
        except source_db.SourceError as exc:
            # 表がまだ無いのは失敗ではない(最初に送った端末が作る表がある)
            if "no such table" in str(exc).lower():
                out.append(TableCount(table, missing=True))
            else:
                out.append(TableCount(table, error=str(exc)))
            continue
        drop, groups, total = group_rows(rows)
        out.append(TableCount(table, total, drop, groups))
    return out


def backup(path: Path) -> Path:
    """丸ごとの控え。**書いている途中の姿を写さない**よう SQLite の backup で取る。"""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    copy = path.with_name(f"{path.stem}.bak-{stamp}{path.suffix}")
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return copy


@dataclass
class FixResult:
    """共有を片付けた結果。"""

    ok: bool = True
    backup: str = ""                                     # 控えの置き場所(消さなかったときは空)
    removed: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def total(self) -> int:
        return sum(self.removed.values())

    def summary(self) -> str:
        if self.error:
            return f"共有の重なりを片付けられませんでした: {self.error}"
        if not self.total:
            return "共有の梱包資材マスタに、同じ内容の行はありませんでした。"
        lines = [f"共有の梱包資材マスタから、同じ内容の行を {self.total}件 消しました"
                 f"(控え: {self.backup})。"]
        lines += [f"  {t}: {n}件" for t, n in self.removed.items() if n]
        return "\n".join(lines)


def fix_shared(path: Path, tables: Optional[Iterable[str]] = None) -> FixResult:
    """共有の重なりを消す。消すものがあれば**控えを取ってから**、数え直しと削除を1つの
    書き込みトランザクションで行う。消すものが無ければ何も書かない(控えも取らない)。"""
    result = FixResult()
    names = _tables(tables)
    try:
        before = [c for c in count_path(path, names) if c.drop]
    except source_db.SourceError as exc:
        return FixResult(ok=False, error=str(exc))
    if not before:
        return result
    try:
        result.backup = str(backup(path))
    except (OSError, sqlite3.Error) as exc:
        # **控えが取れないなら消さない。**
        return FixResult(ok=False, error=f"控えを取れませんでした({exc})。何も消していません")
    try:
        with source_db.connect(path) as conn, conn.transaction() as tx:
            for table in names:
                try:
                    counted = count_conn(tx, table)
                except sqlite3.Error:
                    continue                             # 表が無い
                if not counted.drop:
                    continue
                quoted = source_db.quote_identifier(table)
                for row_id in counted.drop:
                    tx.execute(f"DELETE FROM {quoted} WHERE rowid = ?", (row_id,))
                result.removed[table] = len(counted.drop)
    except source_db.SourceError as exc:
        log.warning("共有の重なりを消せませんでした: %s", exc)
        return FixResult(ok=False, backup=result.backup,
                         error=f"{exc}(何も消していません。控え: {result.backup})")
    log.info("共有の重なりを消しました: %s(控え %s)", result.removed, result.backup)
    return result


@dataclass
class Cleanup:
    """設定画面の「片付けて取り込み直す」1回分(共有を片付ける → 手元を取り込み直す)。"""

    fixed: FixResult
    imported: Any = None                                 # `ImportResult`(取り込まなかったときは None)
    local_left: dict[str, int] = field(default_factory=dict)   # 取り込み直した後も手元に残る重なり

    @property
    def ok(self) -> bool:
        return (self.fixed.ok and self.imported is not None
                and bool(getattr(self.imported, "ok", True)) and not self.local_left)

    def summary(self) -> str:
        lines = [self.fixed.summary()]
        if self.imported is None:
            lines.append("取り込み直しはしていません(共有を片付けられなかったため)。")
            return "\n".join(lines)
        lines += ["", self.imported.summary()]
        if self.local_left:
            # **残っているのに「片付きました」と言わない。** 取り込みを見送った表
            # (まだ送っていない行がある)は手元が入れ替わらない
            lines += ["", "この端末にはまだ同じ内容の行が残っています: "
                      + "、".join(f"{t} {n}件" for t, n in self.local_left.items())
                      + "。まだ送っていない行があると、その表は取り込み直しを見送ります。"
                        "送れたあとにもう一度押してください。"]
        else:
            lines += ["", "この端末の同じ内容の行も無くなりました。ほかの端末も、次の取り込み"
                          "(起動時・倉庫連携の見張り・設定の取り込み)で直ります。"]
        return "\n".join(lines)


def local_duplicates(conn: sqlite3.Connection) -> dict[str, int]:
    """手元の作業用DBの重なり(設定画面の警告と同じ数え方)。"""
    from . import data_sync
    out = {}
    for spec in data_sync.WRITEBACK_SPECS:
        same = data_sync.duplicate_count(conn, spec.sqlite_table, spec.key_column)
        if same:
            out[spec.sqlite_table] = same
    return out


def cleanup(conn: sqlite3.Connection, path: Optional[Path] = None,
            progress: Any = None) -> Cleanup:
    """共有の重なりを片付けて(無ければ何もしない)、梱包資材マスタを取り込み直す。

    **共有を片付けられなかったら取り込み直さない** ── 取り込むと、重なったままの共有で
    手元をまた埋めるだけになる。
    """
    from . import data_sync
    path = path or data_sync.find_material_db()
    if path is None:
        from . import sync_sources
        return Cleanup(FixResult(ok=False, error=sync_sources.material_db_missing_why()))
    fixed = fix_shared(path)
    if not fixed.ok:
        return Cleanup(fixed)
    kw = {"progress": progress} if progress is not None else {}
    imported = data_sync.import_master(conn, path, **kw)
    return Cleanup(fixed, imported, local_duplicates(conn))
