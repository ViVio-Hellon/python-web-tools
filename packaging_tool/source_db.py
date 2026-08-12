"""取り込み元の sqlite3 ファイルを読み書きする

【何のためにあるか】
梱包資材マスタと仕掛台帳は、**このツールの外**にある sqlite3 ファイルです。
手元の作業用DB(`config.DB_PATH`)とは別物なので、開き方も扱いも分けます。

    取り込み元(外)  … このモジュール。読むのが主で、書き戻しだけ書く
    作業用DB(手元)  … `db.py`。アプリが自由に読み書きする

【読むときは読み取り専用で開く】
`file:...?mode=ro` で開きます。共有フォルダのファイルを、取り込みの
つもりで**うっかり書き換えない**ためです。読み取り専用なら、上流の
仕組みが書いている最中でも壊しません。

【以前は Access(.accdb)でした】
ODBCドライバの有無で動いたり動かなかったりし、Linux では mdbtools が
要りました。取り込み元が sqlite3 になったので、その分岐は丸ごと不要です
── 標準ライブラリだけで、どの端末でも同じように読めます。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from typing import Any, Iterable, Optional

from .logging_utils import get_logger

log = get_logger("source_db")

# 取り込み元の拡張子。**この2つだけを探す**
SUFFIXES = (".sqlite3", ".db")

# 共有フォルダの上でロックを待つ時間。上流が書いている最中に当たっても、
# すぐ諦めずに少し待つ
BUSY_TIMEOUT_MS = 10_000


class SourceError(RuntimeError):
    """取り込み元を読み書きできなかった。"""


# ==================================================================
# エラーの種別
# ==================================================================
# **文言から推し量る**しかない層。sqlite3 は例外の型を細かく分けないので、
# ここで1か所にまとめて、呼び出し側が文字列を見ないようにする
def is_lock_error(message: str) -> bool:
    """他の誰かが書いている最中。時間をおけば通る見込みがある。"""
    lowered = (message or "").lower()
    return "locked" in lowered or "busy" in lowered


def is_duplicate_error(message: str) -> bool:
    """一意制約に当たった = **もう入っている**。二重送信の防止が効いた形。"""
    lowered = (message or "").lower()
    return "unique constraint" in lowered or "constraint failed" in lowered


def is_already_exists_error(message: str) -> bool:
    """列や索引が既にある。作ろうとして当たっただけなので失敗ではない。"""
    lowered = (message or "").lower()
    return "duplicate column" in lowered or "already exists" in lowered


def quote_identifier(name: str) -> str:
    """テーブル名・列名を囲む。日本語の名前がそのまま出てくるので必須。"""
    return '"' + str(name).replace('"', '""') + '"'


# ==================================================================
# 開く
# ==================================================================
def to_uri(path: Path) -> str:
    r"""`sqlite3` の URI にする。**共有フォルダ(UNC)を壊さない。**

    `Path.as_uri()` は `\\サーバ\共有\x.sqlite3` を
    `file://サーバ/共有/x.sqlite3` にします。この `//サーバ` を SQLite は
    **authority** として読み、空か `localhost` 以外は受け付けません:

        invalid uri authority: nlmsrvngy03

    取り込み元は**全部**共有フォルダに置くので、これに当たると
    マスタも仕掛台帳も1件も読めません(現場で踏みました)。

    authority は空のままにして、UNC は**パスとして**書きます:

        \\サーバ\共有\x.sqlite3  →  file:////サーバ/共有/x.sqlite3

    `file:` のあとの `//` が「authority は空」、続く `//サーバ/…` が
    パスです。Windows は `/` 区切りの UNC をそのまま受け付けます。
    日本語と空白は `as_uri()` と同じように百分率符号化します。
    """
    text = str(path)
    if text.startswith("\\\\") or text.startswith("//"):
        rest = text.replace("\\", "/").lstrip("/")
        return "file:////" + quote(rest, safe="/")
    return path.as_uri()


# 開き方の名前。**どれで開けたか**を診断に出す
WAY_URI = "URI(読み取り専用)"
WAY_PLAIN = "素のパス"
WAY_COPY = "手元への写し"

# 開くときに必ず通す1文。**開けたかどうかは、引けたかどうかで決める。**
_PROOF = "SELECT count(*) FROM sqlite_master"


def _connect(path: Path, *, read_only: bool) -> sqlite3.Connection:
    conn, _way = _open(path, read_only=read_only)
    return conn


def _open(path: Path, *, read_only: bool,
          attempts: Optional[list[tuple[str, str]]] = None,
          ) -> tuple[sqlite3.Connection, str]:
    r"""取り込み元を開く。**開けるまで手を変える。**

    【なぜ手を変えるのか】
    共有フォルダ(SMB)の上の sqlite3 は、置かれ方しだいで開けません。

        WAL で作られている … WAL は共有メモリ(`-shm`)を使う。SMB には
                             それが無いので、**読むだけでも開けない**
        誰かが掴んでいる   … 変換ツールや Access が開いたまま
        URI が通らない     … UNC・ドライブ割り当て・長いパス

    仕掛台帳は開けるのにマスタだけ開けない、という形で現れます
    (現場で踏みました)。同じフォルダ・同じ経路なので、違いは
    **そのファイルがどう作られたか**しかありません。

    【なぜ引いてみるのか】
    `sqlite3.connect()` はファイルを触りません。**開けたつもりのまま
    返ってきて、最初の問い合わせで初めて落ちます。** それでは次の手に
    移れないので、ここで1文引いて確かめてから返します。
    """
    path = Path(path)
    if not path.exists():
        raise SourceError(f"ファイルが見つかりません: {path}")
    # 絶対の道にするだけ。**共有には問い合わせない。**
    # `resolve()` は実体を開いて確かめに行くので、共有の上では
    # 1テーブルごとに往復が増えます。`..` を畳むのは文字の上でできます。
    resolved = Path(os.path.abspath(path))
    log_to = attempts if attempts is not None else []

    ways = [(WAY_URI, lambda: _try_uri(resolved, read_only=read_only)),
            (WAY_PLAIN, lambda: _try_plain(resolved, read_only=read_only))]
    if read_only:
        # 写しは**読むときだけ**。書き戻しを写しへ向けたら、書いたものが
        # どこにも残らない ── 開けないなら開けないと言うほうがまし
        ways.append((WAY_COPY, lambda: _try_copy(resolved)))

    for name, attempt in ways:
        try:
            conn = attempt()
        except (sqlite3.Error, OSError) as exc:
            log_to.append((name, str(exc)))
            continue
        log_to.append((name, ""))
        if name != WAY_URI:
            log.warning("%s は %s で開きました(%s)", path.name, name,
                        log_to[0][1] if log_to else "")
        return conn, name

    reasons = " / ".join(f"{n}: {why}" for n, why in log_to if why)
    raise SourceError(f"{path.name} を開けませんでした: {reasons}")


def _prepare(conn: sqlite3.Connection) -> sqlite3.Connection:
    """開いた直後の約束ごと。**引けることまで確かめる。**"""
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    conn.execute(_PROOF).fetchone()
    return conn


def _try_uri(resolved: Path, *, read_only: bool) -> sqlite3.Connection:
    """`file:...?mode=ro` で開く。ふだんはこれで通る。"""
    uri = to_uri(resolved) + ("?mode=ro" if read_only else "")
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        return _prepare(conn)
    except (sqlite3.Error, OSError):
        conn.close()
        raise


def _try_plain(resolved: Path, *, read_only: bool) -> sqlite3.Connection:
    """素のパスで開く。URI の書き方は環境で当たり外れが出うる。

    `mode=ro` の代わりに `query_only` を立てます。取り込み元へ
    書き込まないことは**譲れない**ので、読み取り専用は必ず掛けます。
    """
    conn = sqlite3.connect(str(resolved), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        if read_only:
            conn.execute("PRAGMA query_only = 1")
        return _prepare(conn)
    except (sqlite3.Error, OSError):
        conn.close()
        raise


def _try_copy(resolved: Path) -> sqlite3.Connection:
    """手元に写してから開く。**共有の上で開けないときの最後の手。**

    WAL のファイルは共有フォルダの上では開けません(共有メモリが
    要るため)。手元のディスクなら開けるので、写して読みます。
    `-wal` / `-shm` も一緒に写します ── 本体だけ写すと、直前に
    書かれた分が抜けた中身を読むことになります。

    **読み取り専用で開きます。** 写しを書き換えても誰にも届きません。
    """
    if not _looks_like_sqlite(resolved):
        # 中身が別物なら写しても開けない。**共有を無駄に往復させない**
        raise sqlite3.DatabaseError("sqlite3 のファイルではありません")
    copy = _copy_of(resolved)
    conn = sqlite3.connect(str(copy), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute("PRAGMA query_only = 1")
        return _prepare(conn)
    except (sqlite3.Error, OSError):
        conn.close()
        raise


# 写しの置き場。**同じファイルを何度も写さない** ── 取り込みは
# テーブルごとに開き直すので、19回写すと共有の往復だけで待たされる
_COPY_DIR: Optional[Path] = None
_COPIES: dict[str, tuple[tuple[int, int], Path]] = {}
_SIDECARS = ("-wal", "-shm", "-journal")


def _copy_of(resolved: Path) -> Path:
    """手元に写した実体。中身が変わっていなければ写し直さない。"""
    global _COPY_DIR
    stat = resolved.stat()
    stamp = (stat.st_size, stat.st_mtime_ns)
    key = str(resolved)
    known = _COPIES.get(key)
    if known is not None and known[0] == stamp and known[1].exists():
        return known[1]

    if _COPY_DIR is None:
        _COPY_DIR = Path(tempfile.mkdtemp(prefix="pkgsrc_"))
        atexit.register(shutil.rmtree, _COPY_DIR, True)
    copy = _COPY_DIR / f"{abs(hash(key)):x}{resolved.suffix}"
    for extra in _SIDECARS:
        side = Path(str(resolved) + extra)
        target = Path(str(copy) + extra)
        target.unlink(missing_ok=True)
        if side.exists():
            shutil.copyfile(side, target)
    shutil.copyfile(resolved, copy)
    _COPIES[key] = (stamp, copy)
    log.info("共有の上で開けないので手元へ写しました: %s → %s", resolved, copy)
    return copy


def is_readable(path: Path) -> bool:
    """sqlite3 として開けるか。**中身までは見ない**。

    拡張子が合っていても中身が別物ということはある(名前を変えただけの
    Access ファイル等)。開いてテーブル一覧が引けるかで判定する。
    """
    try:
        with _connect(Path(path), read_only=True) as conn:
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        return True
    except (SourceError, sqlite3.Error):
        return False


# ==================================================================
# 診断 ── なぜ開けないのかを、そのまま画面に出せる形で
# ==================================================================
@dataclass
class Probe:
    """1ファイルを開いてみた結果。**分かったことを全部持つ。**

    「sqlite3 として読めません」だけでは、現場も直せません。
    どこまで届いていて、何が拒まれたのかを出します。
    """

    path: str = ""
    exists: bool = False
    size: int = 0
    is_sqlite: bool = False          # 先頭16バイトが sqlite3 の印か
    opened_by: str = ""              # どの手で開けたか(開けなければ空)
    journal: str = ""                # WAL / delete / ...
    sidecars: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    attempts: list[tuple[str, str]] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.opened_by)


# sqlite3 のファイルの先頭にある印
MAGIC = b"SQLite format 3\x00"


def _looks_like_sqlite(path: Path) -> bool:
    """先頭の印だけを見る。**中身の正しさまでは見ない。**"""
    try:
        with open(path, "rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def probe(path: Path) -> Probe:
    """開いてみて、分かったことを返す。**例外は投げない。**"""
    path = Path(path)
    out = Probe(path=str(path))
    try:
        out.exists = path.exists()
    except OSError as exc:                       # 共有に届かない
        out.error = str(exc)
        return out
    if not out.exists:
        out.error = "ファイルがありません"
        return out

    try:
        out.size = path.stat().st_size
        with open(path, "rb") as handle:
            out.is_sqlite = handle.read(len(MAGIC)) == MAGIC
        out.sidecars = [e.lstrip("-") for e in _SIDECARS
                        if Path(str(path) + e).exists()]
    except OSError as exc:
        out.error = str(exc)
        return out

    try:
        conn, way = _open(path, read_only=True, attempts=out.attempts)
    except SourceError as exc:
        out.error = str(exc)
        return out
    out.opened_by = way
    try:
        with conn:
            out.journal = str(conn.execute(
                "PRAGMA journal_mode").fetchone()[0])
            out.tables = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    except sqlite3.Error as exc:                 # pragma: no cover - 開けた後
        out.error = str(exc)
    return out


# ==================================================================
# 読む
# ==================================================================
def list_tables(path: Path) -> list[str]:
    """このファイルにあるテーブルの一覧(sqlite の内部表は除く)。"""
    try:
        with _connect(Path(path), read_only=True) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
        return [r["name"] for r in rows]
    except (SourceError, sqlite3.Error) as exc:
        log.warning("テーブル一覧を引けません (%s): %s", path, exc)
        return []


def columns(path: Path, table: str) -> list[str]:
    """テーブルの列名。取り込み前に「その列があるか」を見るのに使う。"""
    try:
        with _connect(Path(path), read_only=True) as conn:
            rows = conn.execute(
                f"PRAGMA table_info({quote_identifier(table)})").fetchall()
        return [r["name"] for r in rows]
    except (SourceError, sqlite3.Error):
        return []


def read_query(path: Path, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """読み取り専用で開いて1文だけ引く。

    **書ける口をここに作らない。** `mode=ro` で開くので、渡された文が
    たとえ `UPDATE` でも通らない。絞り込みや行数の数え上げのように、
    表を丸ごと読むまでもない用に使う。
    """
    path = Path(path)
    try:
        with _connect(path, read_only=True) as conn:
            cursor = conn.execute(sql, tuple(params))
            names = [d[0] for d in cursor.description or []]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
    except SourceError:
        raise
    except sqlite3.Error as exc:
        raise SourceError(f"{path.name} を読めませんでした: {exc}") from exc


def table_counts(path: Path) -> dict[str, int]:
    """テーブルごとの行数。**1回開いて全部数える。**

    共有フォルダの上では開き直すたびに往復が要る。表の数だけ開くと
    それだけで待たされるので、1つの接続で数え切る。
    """
    path = Path(path)
    counts: dict[str, int] = {}
    try:
        with _connect(path, read_only=True) as conn:
            names = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
            for name in names:
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) AS n FROM {quote_identifier(name)}"
                    ).fetchone()
                    counts[name] = int(row["n"])
                except sqlite3.Error:
                    # 1つ数えられなくても残りは出す。見えないより見えるほうがよい
                    counts[name] = -1
    except (SourceError, sqlite3.Error) as exc:
        log.warning("行数を数えられません (%s): %s", path, exc)
        return {}
    return counts


def read_table(path: Path, table: str) -> list[dict[str, Any]]:
    """1テーブルを丸ごと読む。

    戻り値は行ごとの辞書。値は sqlite3 が返す型のまま(数値は数値)で、
    文字列化は `import_specs` の変換関数が引き受ける。
    """
    path = Path(path)
    try:
        with _connect(path, read_only=True) as conn:
            cursor = conn.execute(
                f"SELECT * FROM {quote_identifier(table)}")
            names = [d[0] for d in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
    except SourceError:
        raise
    except sqlite3.Error as exc:
        raise SourceError(
            f"{path.name} の {table} を読めませんでした: {exc}") from exc


# ==================================================================
# 書く(書き戻し)
# ==================================================================
class SourceConnection:
    """書き戻し用に開いた取り込み元。**書けるのはここだけ**。

    `outbox_sync` がこの形を期待する(`execute` / `insert` / `path`)。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._conn = _connect(self.path, read_only=False)

    # --- 後始末 ---
    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:                    # pragma: no cover - 閉じ損ね
            pass

    def __enter__(self) -> "SourceConnection":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- 読む ---
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        try:
            cursor = self._conn.execute(sql, tuple(params))
            names = [d[0] for d in cursor.description or []]
            return [dict(zip(names, row)) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            raise SourceError(str(exc)) from exc

    def table_names(self) -> list[str]:
        return [r["name"] for r in self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    # --- 書く ---
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """1文を実行して、影響のあった行数を返す。"""
        try:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cursor.rowcount
        except sqlite3.Error as exc:
            raise SourceError(str(exc)) from exc

    def insert(self, table: str, values: dict[str, Any]) -> int:
        """1行足す。**値はプレースホルダで渡す**(文字列に埋め込まない)。"""
        if not values:
            raise SourceError("入れる値がありません。")
        cols = ", ".join(quote_identifier(k) for k in values)
        marks = ", ".join("?" for _ in values)
        return self.execute(
            f"INSERT INTO {quote_identifier(table)} ({cols}) VALUES ({marks})",
            list(values.values()))

    def update(self, table: str, values: dict[str, Any],
               where: dict[str, Any]) -> int:
        """条件に合う行を書き換える。

        **`where` が空なら断る。** 空の `WHERE` は「全行を書き換える」で、
        取り込み元は共有のマスタなので、1回の取り違えで全員が困る。
        呼び手が渡し忘れたのか全件を狙ったのかはここでは分からないので、
        分からないほうに倒す。
        """
        if not values:
            raise SourceError("書き換える値がありません。")
        if not where:
            raise SourceError("どの行かが決まっていません。")
        sets = ", ".join(f"{quote_identifier(k)} = ?" for k in values)
        conds = " AND ".join(f"{quote_identifier(k)} = ?" for k in where)
        return self.execute(
            f"UPDATE {quote_identifier(table)} SET {sets} WHERE {conds}",
            list(values.values()) + list(where.values()))

    def delete(self, table: str, where: dict[str, Any]) -> int:
        """条件に合う行を消す。**`where` が空なら断る**(理由は `update` と同じ)。"""
        if not where:
            raise SourceError("どの行かが決まっていません。")
        conds = " AND ".join(f"{quote_identifier(k)} = ?" for k in where)
        return self.execute(
            f"DELETE FROM {quote_identifier(table)} WHERE {conds}",
            list(where.values()))


def connect(path: Path) -> SourceConnection:
    """書き戻し用に開く。読むだけなら `read_table` を使う。"""
    return SourceConnection(path)


# ==================================================================
# 探す
# ==================================================================
def find(directory: Path, *names: str) -> Optional[Path]:
    """フォルダの中から、名前が一致するファイルを探す。

    拡張子違い(`.sqlite3` / `.db`)も見る。上流の付け方に合わせて
    こちらが折れるほうが、現場でファイル名を直させるより早い。
    """
    directory = Path(directory)
    for name in names:
        stem = Path(name).stem
        for suffix in SUFFIXES:
            candidate = directory / f"{stem}{suffix}"
            try:
                if candidate.exists():
                    return candidate
            except OSError:                      # 共有に届かない
                return None
    return None


def list_source_files(directory: Path) -> list[Path]:
    """フォルダにある取り込み元らしいファイル。設定画面に出す。"""
    directory = Path(directory)
    found: list[Path] = []
    for suffix in SUFFIXES:
        try:
            found.extend(sorted(directory.glob(f"*{suffix}")))
        except OSError:
            return []
    return sorted(set(found))
