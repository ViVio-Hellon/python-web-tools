"""書き戻す表にできた**同じ内容の行**を数える / 消す(`--fix`)

設定画面の「--fix を実行(重複を消す)」・「いまの状態」の警告・`scripts/dedupe_writeback.py`
の中身。**「同じ」の決め方はここ1か所だけ**(以前は警告・共有の片付け・注文の片付けで
3通りあり、警告は出るのに消えない、が起きた)。

【何を重複とみなすか】
**取り込みと同じ形にそろえてから**、比べなくてよい列(管理番号・送信ID など。`IGNORED`)を
除いた全部の列が一致する行。Access から来た行(`2026/08/11`・厚 `140.000`・空欄 NULL)と
ツールが送った行(`2026-08-11`・`140.0`・`''`)は、そろえると同じになる。値そのものが
違う行(丈 2502.5 と 2502 など)は**同じ行ではない**(下の「切り捨ての写し」だけは別に拾う)。
確認・取り消しの印が違う行も別の行
(どちらの印が正しいか機械では決めない)。

一致した組の中で**いちばん小さい番号だけを残し**、残りを消す(先に入った行が本物で、
あとから増えたのが写し。発注コメントの `#番号` も小さいほうを指している)。

【切り捨ての写し(共有だけ)】
VER4.8.3 より前は、取り込みで幅・丈の小数を切り捨て、その値を共有へ送り直していた。共有には
「丈 2502.5 の元の行」と「丈 2502 の写し」が残る(現場の判断: 消す)。値が違うので上の決め方では
同じ行にならないため、別に拾う(`truncated_copies`)。拾うのは次を全部満たす行だけ:
  - 幅・丈(`TRUNCATED_COLUMNS`)以外の列が、ある行と全部同じ(登録日時の秒・確認の印まで)
  - その行の幅・丈は小数を持ち、こちらは**その小数を切り捨てた値**(2502.5 → 2502)
小数のある元の行を残し、写しを消す。VER4.8.3 からは切り捨てないので、新しくはできない。

【共有と手元】
`run_fix` は 共有を片付ける → 手元を取り込み直す → 手元に残った重なりも消す、の順。
共有に表が無い(取り込んでも入れ替わらない)手元の重なりも、これで消える。

【消す前に必ず控えを取る】
共有を書き換える前に、同じフォルダへ `<名前>.bak-YYYYMMDDHHMMSS.sqlite3` として丸ごと写す。
数え直しと削除は**1つの書き込みトランザクション**で行う ── 数えてから消すまでのあいだに
ほかの端末が書いた行を、古い数えで消さないため。
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import import_specs, source_db
from .logging_utils import get_logger

log = get_logger("dedupe")

# 中身の比較から外す列。**送るときに振られる番号**と、手元だけで持つ控え(共有での番号・
# 印をまだ送っていないか・行の出どころ)。どれも中身ではない
IGNORED = ("管理番号", "id", "送信ID", "取込元管理番号", "印未反映", "作成元")
ROW = "__行"            # 行を指す番号(rowid)を入れておく名前


@dataclass
class Group:
    """重なっている1組。`rows` は組の行(先頭が残す行)。"""

    rows: list[dict[str, Any]]
    names: list[str]

    def describe(self, width: int = 6) -> str:
        first = self.rows[0]
        return " / ".join(f"{n}={first.get(n)}" for n in self.names[:width])

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
    truncated: list[int] = field(default_factory=list)  # 切り捨ての写し(rowid。drop とは重ならない)

    @property
    def remove(self) -> list[int]:
        """消す行ぜんぶ(同じ内容の写し + 切り捨ての写し)。"""
        return sorted({*self.drop, *self.truncated})


def _columns(table: str, sample: dict[str, Any], *, shared: bool) -> list[tuple[str, str, Callable]]:
    """比べる列 (見出し, 行から読む列名, そろえ方)。取り込みの決め(`IMPORT_SPECS`)に合わせる。

    共有の行は取り込み元の列名で、手元の行は手元の列名で読む。決めの無い表は、その行に
    ある列をそのまま比べる。
    """
    spec = import_specs.IMPORT_SPECS.get(table)
    if spec is None:
        return [(n, n, lambda v: v) for n in sample if n not in IGNORED and n != ROW]
    return [(col, src if shared else col, conv) for col, src, conv in spec if col not in IGNORED]


def _key(row: dict[str, Any], columns: list[tuple[str, str, Callable]]) -> tuple:
    """取り込みと同じ形にそろえた中身(空は取り込みと同じ既定値に寄せる)。"""
    out = []
    for _name, read, conv in columns:
        value = conv(row.get(read))
        if value is None:
            value = import_specs.NULL_FALLBACKS.get(conv)
        out.append(value)
    return tuple(out)


def group_rows(rows: Iterable[dict[str, Any]], table: str = "", *, shared: bool = True,
               keep_first: Optional[Callable[[dict[str, Any]], Any]] = None
               ) -> tuple[list[int], list[Group], int]:
    """行(`ROW` に rowid を入れたもの)を中身で組にする。

    戻り値は (消してよい rowid, 2行以上の組, 全体の行数)。組の中で残すのは、`keep_first` の
    小さい順の先頭(既定は番号の小さい行 = 先に入った行)。
    """
    rows = sorted(rows, key=lambda r: r[ROW])
    if not rows:
        return [], [], 0
    columns = _columns(table, rows[0], shared=shared)
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_key(row, columns), []).append(row)
    order = keep_first or (lambda r: r[ROW])
    many = [Group(sorted(members, key=order), [c[0] for c in columns])
            for members in groups.values() if len(members) > 1]
    drop = [row[ROW] for g in many for row in g.rows[1:]]
    return sorted(drop), many, len(rows)


# 以前、取り込みで小数を切り捨てていた列(`import_specs.to_number` に変える前は整数にしていた)
TRUNCATED_COLUMNS = ("幅", "丈")


def _integral(value: Any) -> bool:
    return isinstance(value, int) or (isinstance(value, float) and value.is_integer())


def truncated_copies(rows: Iterable[dict[str, Any]], table: str = "", *,
                     shared: bool = True) -> list[int]:
    """**切り捨ての写し**の rowid(丈 2502.5 の行があるときの、ほかが全部同じ丈 2502 の行)。"""
    rows = list(rows)
    if not rows:
        return []
    columns = _columns(table, rows[0], shared=shared)
    at = [i for i, (name, _r, _c) in enumerate(columns) if name in TRUNCATED_COLUMNS]
    if not at:
        return []

    def cut(key: tuple) -> tuple:
        return tuple(math.trunc(v) if i in at and isinstance(v, (int, float)) else v
                     for i, v in enumerate(key))

    keys = [(row, _key(row, columns)) for row in rows]
    # 小数を持つ元の行を、切り捨てた姿で覚える
    originals = {cut(key) for _row, key in keys
                 if any(isinstance(key[i], float) and not key[i].is_integer() for i in at)}
    return sorted(row[ROW] for row, key in keys
                  if all(_integral(key[i]) for i in at) and cut(key) in originals)


def _read(conn: Any, table: str) -> list[dict[str, Any]]:
    sql = f"SELECT rowid AS {ROW}, * FROM {source_db.quote_identifier(table)}"
    if isinstance(conn, sqlite3.Connection):
        cursor = conn.execute(sql)
        names = [d[0] for d in cursor.description or []]
        return [dict(zip(names, r)) for r in cursor.fetchall()]
    return conn.query(sql)


def count_conn(conn: Any, table: str, *, shared: bool = True) -> TableCount:
    """開いた接続(`sqlite3.Connection` / `SourceTransaction`)で1つの表を数える。

    `shared=False` は手元の作業用DB(列名が手元のもの)。
    """
    rows = _read(conn, table)
    drop, groups, total = group_rows(rows, table, shared=shared)
    return TableCount(table, total, drop, groups, truncated=_extra(rows, table, drop, shared))


def _extra(rows: list[dict[str, Any]], table: str, drop: list[int], shared: bool) -> list[int]:
    """切り捨ての写し(共有だけ。手元は取り込み直しで共有の姿になる)。"""
    if not shared:
        return []
    done = set(drop)
    return [r for r in truncated_copies(rows, table, shared=True) if r not in done]


def _tables(tables: Optional[Iterable[str]]) -> list[str]:
    if tables is not None:
        return list(tables)
    from .sync_writeback import WRITEBACK_SPECS
    return [s.sqlite_table for s in WRITEBACK_SPECS]


def count_path(path: Path, tables: Optional[Iterable[str]] = None, *,
               shared: bool = True) -> list[TableCount]:
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
        drop, groups, total = group_rows(rows, table, shared=shared)
        out.append(TableCount(table, total, drop, groups,
                              truncated=_extra(rows, table, drop, shared)))
    return out


def backup(path: Path) -> Path:
    """丸ごとの控え。**書いている途中の姿を写さない**よう SQLite の backup で取る。"""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    copy = path.with_name(f"{path.stem}.bak-{stamp}{path.suffix}")
    # 元は**読むだけで開く**(無ければ作らない。VER4.8.122)。既定の開き方だと、共有のマスタが一瞬
    # 見えなくなったときに空のマスタを作り、その空を控えにしていた
    src = sqlite3.connect(source_db.to_uri(Path(path)) + "?mode=ro", uri=True)
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
    truncated: dict[str, int] = field(default_factory=dict)  # removed のうち切り捨ての写し
    patterns: list[str] = field(default_factory=list)       # 消した実績の写しの組(何の写しか)
    compacted: str = ""                                     # 詰め直しの結果の1行(しなかったときは空)
    error: str = ""

    @property
    def total(self) -> int:
        return sum(self.removed.values())

    def summary(self) -> str:
        if self.error:
            return f"共有の重複を消せませんでした: {self.error}"
        if not self.total:
            return "\n".join(["共有の梱包資材マスタに、重複はありませんでした。"]
                             + ([self.compacted] if self.compacted else []))
        lines = [f"共有の梱包資材マスタから、重複を {self.total}件 消しました"
                 f"(控え: {self.backup})。"]
        lines += [f"  {t}: {n}件" + (f"(うち切り捨ての写し {self.truncated[t]}件。小数のある元の行を残しました)"
                                       if self.truncated.get(t) else "")
                  for t, n in self.removed.items() if n]
        if self.patterns:
            lines.append("  実績の写し(いちばん小さい実績ID を残しました。明細の行数は上の表ごとの数):")
            lines += [f"    {text}" for text in self.patterns]
        if self.compacted:
            lines.append(self.compacted)
        return "\n".join(lines)


def count_patterns(path: Path) -> "pattern_copies.Copies":
    """共有の実績の写し(`pattern_copies`。VER4.8.119)。読むだけ(手元へ写して読む)。"""
    from . import pattern_copies
    try:
        with source_db._opened(Path(path), read_only=True) as conn:
            return pattern_copies.find(conn)
    except sqlite3.Error as exc:
        raise source_db.SourceError(f"実績を読めません: {exc}") from exc


# 詰め直すのは、空いた場所がこれ以上あるとき(小さな空きのために共有を書き直さない)
COMPACT_MIN_BYTES = 1024 * 1024


def compact(path: Path) -> str:
    """共有の梱包資材マスタを**詰め直す**(SQLite の VACUUM)。結果の1行を返す(しなかったときは空)。

    行を消しても、消した場所は空きとして残り、ファイルは小さくならない。端末は共有が変わるたびに
    ファイルを丸ごと手元へ写して読むので、空きの分まで毎回写すことになる(現場の共有は写しを消しても
    28.6MB。詰め直すと 1MB)。中身は変わらない。

    ほかの端末が読んでいる・書いているときは詰め直せない(待つのは上限まで)。そのときは見送り、
    次に「--fix を実行」を押したときにもう一度試す。
    """
    try:
        size_before = Path(path).stat().st_size
        with source_db.connect(path) as src:
            [row] = src.query("PRAGMA freelist_count")
            free_pages = int(list(row.values())[0])
            [row] = src.query("PRAGMA page_size")
            page_size = int(list(row.values())[0])
            if free_pages * page_size < COMPACT_MIN_BYTES:
                return ""
            src.execute("VACUUM")
        size_after = Path(path).stat().st_size
    except (OSError, source_db.SourceError, sqlite3.Error) as exc:
        log.warning("共有の梱包資材マスタを詰め直せませんでした(次の --fix でもう一度試します): %s", exc)
        return f"共有のファイルの詰め直しは見送りました({exc})。次に「--fix を実行」を押したときにもう一度試します。"
    from . import trouble
    trouble.once(log, f"詰め直し|{path}", trouble.KIND_CLEANED,
                 "共有の梱包資材マスタを詰め直しました: %sKB → %sKB(%s)",
                 size_before // 1024, size_after // 1024, path, every=0)
    return (f"共有のファイルを詰め直しました: {size_before / 1048576:.1f}MB → {size_after / 1048576:.1f}MB"
            "(消した行の空きを除いた。中身は変わりません)。")


def fix_shared(path: Path, tables: Optional[Iterable[str]] = None) -> FixResult:
    """共有の重複を消す。消すものがあれば**控えを取ってから**、数え直しと削除を1つの
    書き込みトランザクションで行う。消すものが無ければ何も書かない(控えも取らない)。

    表を指定しないときは、実績の写し(同じ1件の実績が何件も入ったもの。`pattern_copies`)も消す。
    """
    from . import pattern_copies
    result = FixResult()
    names = _tables(tables)
    patterns = tables is None
    try:
        counted = count_path(path, names)
        copies = count_patterns(path) if patterns else pattern_copies.Copies()
    except source_db.SourceError as exc:
        return FixResult(ok=False, error=str(exc))
    # **読めなかった表があるのに「重複はありませんでした」と言わない**
    broken = [f"{c.table}: {c.error}" for c in counted if c.error]
    if broken:
        return FixResult(ok=False, error="共有を読めませんでした(" + " / ".join(broken) + ")")
    before = [c for c in counted if c.remove]
    if not before and not copies.drop:
        # 消すものが無くても、前の片付けで空いた場所が残っていれば詰め直す(VER4.8.119 で片付けた共有など)
        if patterns:
            result.compacted = compact(path)
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
                if not counted.remove:
                    continue
                quoted = source_db.quote_identifier(table)
                for row_id in counted.remove:
                    tx.execute(f"DELETE FROM {quoted} WHERE rowid = ?", (row_id,))
                result.removed[table] = len(counted.remove)
                if counted.truncated:
                    result.truncated[table] = len(counted.truncated)
            if patterns:
                # 実績の写しも、鍵を取ったあとで数え直してから消す
                try:
                    copies = pattern_copies.find(tx._conn)
                    gone = pattern_copies.remove(tx._conn, copies)
                except sqlite3.Error as exc:
                    raise source_db.SourceError(f"実績の写しを消せません: {exc}") from exc
                if gone:
                    result.patterns = copies.describe()
                    result.removed.update(gone)
    except source_db.SourceError as exc:
        log.warning("共有の重複を消せませんでした: %s", exc)
        return FixResult(ok=False, backup=result.backup,
                         error=f"{exc}(何も消していません。控え: {result.backup})")
    from . import trouble
    trouble.once(log, f"重複を消した|{path}", trouble.KIND_CLEANED,
                 "共有の重複を消しました: %s(控え %s)", result.removed, result.backup, every=0)
    if patterns:
        # 消した行の場所は空きとして残り、ファイルは小さくならない。詰め直す(VER4.8.121)
        result.compacted = compact(path)
    if result.patterns:
        # 写しが消えたので、共有の側の守り(同じ実績は1件だけ)をすぐ付ける(VER4.8.120)
        pattern_copies.forget_guard()
        try:
            with source_db.connect(path) as src:
                pattern_copies.ensure_guard(src)
        except source_db.SourceError as exc:
            log.warning("片付けのあと、実績の守りを付けられませんでした(次の取り込みで付けます): %s", exc)
    return result


def local_duplicates(conn: sqlite3.Connection) -> dict[str, int]:
    """手元の作業用DBの重なり(設定画面の警告と同じ数え方)。"""
    from . import config, pattern_copies
    out = {}
    for table in _tables(None):
        try:
            counted = count_conn(conn, table, shared=False)
        except sqlite3.Error:
            continue                                     # 表が無い(古い手元DB)
        if counted.drop:
            out[table] = len(counted.drop)
    copies = pattern_copies.local_count(conn)
    if copies:
        out[config.TBL_PT_HEADER] = copies
    return out


def fix_local(conn: sqlite3.Connection) -> dict[str, int]:
    """手元の作業用DBの重なりを消す。消した件数を表ごとに返す。

    **もう共有と行き来が済んだ行を残す**(取り込んだ行・送った行)。まだ送っていない写しの
    ほうを消す ── 逆にすると、消さずに残した写しがもう一度送られる。
    共有に表が無い(取り込みで入れ替わらない)手元の重なりも、ここで消える。
    """
    from . import outbox_sync, sync_writeback
    removed: dict[str, int] = {}
    outbox_sync.ensure_sync_table(conn)
    for spec in sync_writeback.WRITEBACK_SPECS:
        table = spec.sqlite_table
        try:
            rows = _read(conn, table)
        except sqlite3.Error:
            continue
        done = {int(r[0]) for r in conn.execute(
            f"SELECT 行ID FROM [{outbox_sync.SYNC_LOG_TABLE}] WHERE テーブル名 = ? AND 状態 = ?",
            (table, outbox_sync.SYNC_DONE))}

        def settled_first(row: dict[str, Any]) -> tuple:
            settled = (row.get(spec.origin_column) == outbox_sync.ORIGIN_IMPORTED
                       or int(row[spec.key_column]) in done)
            return (0 if settled else 1, row[ROW])

        drop, _groups, _total = group_rows(rows, table, shared=False, keep_first=settled_first)
        if not drop:
            continue
        with conn:
            conn.executemany(f"DELETE FROM [{table}] WHERE rowid = ?", [(r,) for r in drop])
        removed[table] = len(drop)
        log.info("手元の重複を消しました: %s %s件", table, len(drop))
    return removed


@dataclass
class FixRun:
    """設定画面の「--fix を実行」1回分(共有の重複を消す → 取り込み直す → 手元の重複を消す)。"""

    fixed: FixResult
    imported: Any = None                                 # `ImportResult`(取り込まなかったときは None)
    local_removed: dict[str, int] = field(default_factory=dict)
    local_left: dict[str, int] = field(default_factory=dict)   # それでも手元に残る重なり

    @property
    def ok(self) -> bool:
        return (self.fixed.ok and self.imported is not None
                and bool(getattr(self.imported, "ok", True)) and not self.local_left)

    def summary(self) -> str:
        lines = [self.fixed.summary()]
        if self.imported is None:
            lines.append("取り込み直しはしていません(共有の重複を消せなかったため)。")
            return "\n".join(lines)
        if self.local_removed:
            lines.append("この端末の重複を消しました: "
                         + "、".join(f"{t} {n}件" for t, n in self.local_removed.items()))
        if self.local_left:
            # **残っているのに「片付きました」と言わない**
            lines.append("この端末にはまだ重複が残っています: "
                         + "、".join(f"{t} {n}件" for t, n in self.local_left.items()))
        else:
            lines.append("この端末の重複も無くなりました。ほかの端末も、次の取り込み"
                         "(起動時・倉庫連携の見張り・設定の取り込み)で直ります。")
        lines += ["", "【取り込み直しの結果】", self.imported.summary()]
        return "\n".join(lines)


def run_fix(conn: sqlite3.Connection, path: Optional[Path] = None,
            progress: Any = None) -> FixRun:
    """`--fix`: 共有の重複を消し(控えを取ってから)、取り込み直し、手元の重複も消す。

    **共有の重複を消せなかったら取り込み直さない** ── 取り込むと、重なったままの共有で
    手元をまた埋めるだけになる。
    """
    from . import data_sync
    path = path or data_sync.find_material_db()
    if path is None:
        from . import sync_sources
        return FixRun(FixResult(ok=False, error=sync_sources.material_db_missing_why()))
    fixed = fix_shared(path)
    if not fixed.ok:
        return FixRun(fixed)
    kw = {"progress": progress} if progress is not None else {}
    imported = data_sync.import_master(conn, path, **kw)
    removed = fix_local(conn)
    return FixRun(fixed, imported, removed, local_duplicates(conn))
