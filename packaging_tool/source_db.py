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
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from typing import Any, Callable, Iterable, Iterator, Optional

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


# 取り込み元の文字の入れ方。**ファイル単位で1つに決める**(下記 `sniff`)
ENCODING_UTF8 = "utf-8"
ENCODING_CP932 = "cp932"

# 見極めに読む量。**全部は読まない** ── 共有フォルダの上の数万行を
# 開くたびに舐めると、開くだけで待たされる
SNIFF_TABLES = 12
SNIFF_ROWS = 200
# 日本語を含む値のうち、UTF-8として読めないものがこれを超えたらCP932。
# 半分にしてあるのは、**どちらの側の事故も1件では起こさない**ため ──
# CP932のファイルなら日本語の値はほぼ全部が読めず、UTF-8のファイルなら
# 読めないのは壊れた値だけなので、実際の値は0付近か1付近に寄る
SNIFF_CP932_RATIO = 0.5


def sniff_encoding(conn: sqlite3.Connection) -> str:
    """このファイルの文字が、UTF-8で入っているのかCP932で入っているのか。

    【値ごとに判定してはいけない】
    「まずUTF-8、駄目ならCP932」と1値ずつ試すのは**間違い**だった。
    CP932のバイト列が、たまたま正しいUTF-8としても読めることがある ──
    例えば `燿　` は CP932 で `e0 a0 81 40`、これはUTF-8としても妥当で
    `ࠁ@` と読めてしまう。例外も出ず `�` も出ないので、**静かに化ける。**
    現場の「文字化けが治っていない」の残りはこれだった。

    【ファイル単位なら決められる】
    変換ツールは1つのファイルを1つの入れ方で書く。そして**本物のUTF-8の
    ファイルには、UTF-8として読めないバイト列が1つも無い。** だから
    「読めない値が1つでもあればCP932」と決めてよい。
    値ごとの当てずっぽうと違って、この判定は取りこぼさない。
    """
    # **表と列の名前は、切り替える前に集める。** `text_factory` は
    # `PRAGMA table_info` の戻り(列名・型)にも効くので、bytes にしたまま
    # 引くと列名が `b'用途名'`、型が `b'TEXT'` になり、TEXT列が1つも
    # 見つからない ── 判定は素通りして「UTF-8」を返してしまう
    plan: list[tuple[str, list[str]]] = []
    with identifiers_as_utf8(conn):
        try:
            names = [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND name NOT LIKE 'sqlite_%' LIMIT ?", (SNIFF_TABLES,))]
        except sqlite3.Error:
            return ENCODING_UTF8
        for table in names:
            try:
                # **型で列を絞らない。** 実際の変換ツールは
                # `CREATE TABLE x ("列1", "列2")` と**型を書かずに**表を作る。
                # 「TEXT型の列だけ」を見ていたころは、そういうファイルでは
                # 1列も検査せず、判定が素通りしていた(現物で踏んだ)。
                # 値の型は読んでみれば分かる ── bytes で返るものが文字
                columns = [r[1] for r in conn.execute(
                    f"PRAGMA table_info({quote_identifier(table)})")]
            except sqlite3.Error:
                continue
            if columns:
                plan.append((table, columns))
    if not plan:
        return ENCODING_UTF8

    # **1つの値で決めない。** UTF-8で作られたファイルに壊れた値が1つ
    # 混ざっているだけで全体をCP932と読むと、その1件を助けるために
    # 残り全部を化けさせることになる。日本語を含む値だけを数えて多数決:
    #   CP932のファイル … 日本語の値はほぼ全部がUTF-8として読めない
    #   UTF-8のファイル … 読めないのは壊れた値だけ(あってもごく僅か)
    japanese = failed = 0
    saved = conn.text_factory
    conn.text_factory = bytes          # 決める前に例外で止まらないように
    try:
        for table, columns in plan:
            picked = ", ".join(quote_identifier(c) for c in columns)
            try:
                rows = conn.execute(
                    f"SELECT {picked} FROM {quote_identifier(table)} LIMIT ?",
                    (SNIFF_ROWS,)).fetchall()
            except sqlite3.Error:
                continue               # 引けない表は飛ばす。判定は続ける
            for row in rows:
                for value in row:
                    if not isinstance(value, bytes) or value.isascii():
                        continue       # ASCIIはどちらでも同じ。判断材料にしない
                    japanese += 1
                    try:
                        value.decode(ENCODING_UTF8)
                    except UnicodeDecodeError:
                        failed += 1
    finally:
        conn.text_factory = saved

    if japanese and failed / japanese > SNIFF_CP932_RATIO:
        log.info("取り込み元は CP932 と判断しました"
                 "(日本語を含む %s件のうち %s件がUTF-8として読めません)",
                 japanese, failed)
        return ENCODING_CP932
    return ENCODING_UTF8


@contextmanager
def identifiers_as_utf8(conn: sqlite3.Connection):
    """表名・列名を読むあいだだけ、UTF-8で読むようにする。

    **SQLiteの識別子は必ずUTF-8。** 中のデータがCP932で入っていても、
    表名と列名はUTF-8で保持されている。ところが `sqlite_master` の
    `name` や `PRAGMA table_info` の戻りは**値として**返るので、
    CP932で読む `text_factory` を掛けたままだと識別子まで CP932 として
    読まれ、`ﾛｯﾄ番号` が `ﾛｯﾄ逡ｪ蜿ｷ` になる ── その名前で引こうとして
    「そんな列は無い」になり、表が丸ごと空で取り込まれる。

    (`cursor.description` の列名は `text_factory` を通らないので無事。
    影響を受けるのは、識別子を**値として**引くこの2つだけ。)
    """
    saved = conn.text_factory
    conn.text_factory = str            # 既定=UTF-8
    try:
        yield conn
    finally:
        conn.text_factory = saved


def make_text_factory(encoding: str) -> Callable[[bytes], str]:
    """決めた入れ方でTEXT列を読む関数。

    決めたほうで読めない値だけ、もう一方を試す(表の中に1行だけ別の
    入れ方が混ざっている、という壊れ方があるため)。どちらでも読めない
    バイト列は元のファイルが壊れている ── **黙って `�` に潰さず**、
    そうしたことを記録に残す(`decode_failures`)。
    """
    other = ENCODING_CP932 if encoding == ENCODING_UTF8 else ENCODING_UTF8

    def decode(raw: bytes) -> str:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
        try:
            return raw.decode(other)
        except UnicodeDecodeError:
            decode_failures.append(raw[:64])
            log.warning("どちらの入れ方でも読めないバイト列があります: %s",
                        raw[:32].hex())
            return raw.decode(encoding, "replace")

    return decode


# どちらでも読めなかったバイト列。**黙って潰さない**ための控え。
# 取り込みのたびに見て、あれば結果に添える(`data_sync`)
decode_failures: list[bytes] = []


def decode_text(raw: bytes) -> str:
    """入れ方を決めずに読む(判定できなかったときの保険)。

    ふだんは `sniff_encoding` で決めた `make_text_factory` を使う。
    """
    return make_text_factory(ENCODING_UTF8)(raw)


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

    ways = []
    if read_only:
        # **読むときは、まず手元へ写す。**
        #
        # 共有のファイルを開いたままにすると、**上流が置き換えられない**。
        # Windows は開いているファイルの差し替えを拒むので、変換する側は
        # 新しいほうを `SIKALOT.pending_20260914_091528.sqlite3` のような
        # 名前で置いたまま去る ── 現場の共有に溜まっていたのはこれ。
        # 写してしまえば共有を触るのは数ミリ秒で済み、上流は自由に
        # 差し替えられる。
        #
        # 写しは中身が変わったときだけ作り直す(`_copy_of`)ので、
        # 19表の取り込みでも共有への往復は1回。
        #
        # 写しは**読むときだけ**。書き戻しを写しへ向けたら、書いたものが
        # どこにも残らない ── 開けないなら開けないと言うほうがまし
        ways.append((WAY_COPY, lambda: _try_copy(resolved)))
    ways += [(WAY_URI, lambda: _try_uri(resolved, read_only=read_only)),
             (WAY_PLAIN, lambda: _try_plain(resolved, read_only=read_only))]

    for name, attempt in ways:
        try:
            conn = attempt()
        except (sqlite3.Error, OSError) as exc:
            log_to.append((name, str(exc)))
            continue
        log_to.append((name, ""))
        # **1手目で開けたなら普通のこと。** 折れた手があったときだけ言う
        # (読むときの1手目は手元への写しで、それが当たり前の経路)
        failed = [(n, why) for n, why in log_to if why]
        if failed:
            log.warning("%s は %s で開きました(%s)", path.name, name,
                        " / ".join(f"{n}: {why}" for n, why in failed))
        return conn, name

    reasons = " / ".join(f"{n}: {why}" for n, why in log_to if why)
    raise SourceError(f"{path.name} を開けませんでした: {reasons}")


def _prepare(conn: sqlite3.Connection) -> sqlite3.Connection:
    """開いた直後の約束ごと。**引けることまで確かめる。**"""
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    # **読めない字で表ごと落とさない**、そして**静かに化けさせない**。
    # 既定のままだと UTF-8 として読めない TEXT 列で例外が飛んで表ごと
    # 見送られ、値ごとに読み方を当てにいくと CP932 が UTF-8 としても
    # 読めてしまう場合に黙って化ける(`sniff_encoding` の説明)。
    # ファイル単位で1つに決めてから読む
    conn.text_factory = make_text_factory(sniff_encoding(conn))
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
    """手元に写してから開く。**読むときの、これがふだんの手。**

    以前は「共有の上で開けないときの最後の手」でしたが、順番を
    入れ替えました(`_open` の説明)── 共有のファイルを開いたままに
    すると上流が差し替えられず、`.pending_` が溜まるためです。

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

# 写しの置き場の名前。**掃除するために名前を決めておく**(下記)
_COPY_PREFIX = "pkgsrc_"


def sweep_old_copies(keep_hours: float = 6.0) -> int:
    r"""前の起動が置いていった写しを掃除する。消したフォルダ数を返す。

    【なぜ要るのか】
    写しは `atexit` で消す約束になっていますが、**その約束が果たされる
    のは行儀よく終わったときだけ**です。現場の止め方はそうなりません:

        stop.bat        … `SIGTERM` / `TerminateProcess` で落とす
        コンソールを×  … 同じく後始末は走らない
        自動終了・強制終了・電源断

    1起動ぶんの写しは仕掛台帳を含めて **16MB 以上**あり(SIKALOT だけで
    13.8MB)、`%TEMP%` は Windows が勝手に空けてはくれません。日に数回
    起動すれば**月に数GB**が静かに積もります。VER2.76.1 で「読むときは
    まず手元へ写す」に変えてから、これが毎回起きるようになりました。

    【掃除の仕方】
    自分のフォルダと、**まだ新しいもの**は残します ── 同じPCで
    現場モードと資材モードを並べて開くことがあり、動いている側のものを
    消しに行くと読んでいる最中のファイルを抜くことになります。
    Windows では開かれているファイルは消せずに例外になるので、
    そこは黙って見送ります(消せないのは使われている証拠)。
    """
    import time

    now = time.time()
    removed = 0
    try:
        candidates = list(Path(tempfile.gettempdir()).glob(f"{_COPY_PREFIX}*"))
    except OSError:
        return 0
    for folder in candidates:
        if folder == _COPY_DIR or not folder.is_dir():
            continue
        try:
            if now - folder.stat().st_mtime < keep_hours * 3600:
                continue
        except OSError:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        if not folder.exists():
            removed += 1
    if removed:
        log.info("前の起動が残した取り込み元の写しを %d 件片づけました", removed)
    return removed


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
        _COPY_DIR = Path(tempfile.mkdtemp(prefix=_COPY_PREFIX))
        atexit.register(shutil.rmtree, _COPY_DIR, True)
    copy = _COPY_DIR / f"{abs(hash(key)):x}{resolved.suffix}"
    for extra in _SIDECARS:
        Path(str(copy) + extra).unlink(missing_ok=True)
    if not _snapshot(resolved, copy):
        stamp = _raw_copy(resolved, copy, stamp)
    _COPIES[key] = (stamp, copy)
    # **「開けないので」とは言わない。** 読むときは最初から写します
    # (`_open`)。失敗したように読める行がログに並ぶと、うまくいって
    # いるのに原因を探すことになります
    log.info("読むために手元へ写しました: %s → %s", resolved, copy)
    return copy


def _snapshot(resolved: Path, copy: Path) -> bool:
    """**書いている途中を写さない**写し方。写せたら True。

    【なぜファイルのまま写さないのか】
    現場の端末が何台もあると、発注・受払・使用実績を送るたびに共有の
    ファイルが書き換わる。書いている最中にファイルをそのまま写すと、
    **半分だけ書かれた姿**が写り「database disk image is malformed」で
    読めない(書き込みを続けながら写す試験で 1,397回中24回)。その回の
    取り込みは表ごと失敗する。

    【どう防ぐか】
    sqlite3 の**読むための鍵**を取ってから写す。鍵を持っているあいだは
    ほかの端末が書き終える(確定する)ことができないので、写るのは
    書き終わった姿だけ。書いている端末が居れば、書き終わるまで待つ
    (busy_timeout。待ちきれなければ False を返して、ファイルのまま写す)。

    `Connection.backup()` は使わない。ほかの端末が鍵を持ち続けると
    **いつまでも待ち続けて戻らない**(待つ上限が無い)ため。
    """
    try:
        src = sqlite3.connect(to_uri(resolved) + "?mode=ro", uri=True,
                              timeout=BUSY_TIMEOUT_MS / 1000,
                              isolation_level=None)
    except sqlite3.Error:
        return False
    try:
        src.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        src.execute("BEGIN")
        src.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()   # 読む鍵を取る
        shutil.copyfile(resolved, copy)
        src.execute("COMMIT")
        return True
    except (sqlite3.Error, OSError) as exc:
        # WAL のファイルは共有の上では開けない(共有メモリが要る)。
        # 待ちきれなかったときも同じ。ファイルのまま写す(`_raw_copy`)
        log.debug("鍵を取って写せませんでした(ファイルのまま写します): %s", exc)
        copy.unlink(missing_ok=True)
        return False
    finally:
        src.close()


def _raw_copy(resolved: Path, copy: Path,
              stamp: tuple[int, int]) -> tuple[int, int]:
    """ファイルのまま写す(backup で開けないとき)。写した姿を返す。

    **写しているあいだに書き換わったら、写し直す。** 途中の姿を
    掴んだかもしれないため(`_snapshot` の説明)。何度やっても
    落ち着かないときは最後の写しを使う ── 読めなければ取り込みが
    その表を見送るだけで、共有には何も起きない。
    """
    import time

    for attempt in range(5):
        for extra in _SIDECARS:
            side = Path(str(resolved) + extra)
            if side.exists():
                shutil.copyfile(side, Path(str(copy) + extra))
        shutil.copyfile(resolved, copy)
        after = resolved.stat()
        now = (after.st_size, after.st_mtime_ns)
        if now == stamp:
            return stamp
        stamp = now
        time.sleep(0.2 * (attempt + 1))
    log.warning("%s は写しているあいだも書き換わり続けました", resolved.name)
    return stamp


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
    # 中の文字をどちらの入れ方と判断したか(`sniff_encoding`)。
    # **判定を画面に出しておく。** 文字化けの問い合わせが来たとき、
    # 判定を誤ったのか元のファイルが壊れているのかが、これだけで分かる
    encoding: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.opened_by)

    @property
    def failures(self) -> list[tuple[str, str]]:
        """**実際に折れた手**だけ。試さなかった手はここに入らない。"""
        return [(name, why) for name, why in self.attempts if why]

    @property
    def normal(self) -> bool:
        """ふだんどおりに開けたか。

        **「どの手で開けたか」では判断しない。** 読むときの1手目は
        手元への写しなので(`_open`)、`opened_by` を `WAY_URI` と
        比べると**うまくいっている状態が毎回「要確認」になります** ──
        現場の「取り込めているはずなのに要確認になる」がこれでした。

        普通かどうかを決めるのは**折れた手があったかどうか**だけです。
        """
        return self.ok and not self.failures


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
            with identifiers_as_utf8(conn):
                out.tables = [r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                    " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            out.encoding = sniff_encoding(conn)
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
            with identifiers_as_utf8(conn):
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
            with identifiers_as_utf8(conn):
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
            with identifiers_as_utf8(conn):
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



    @contextmanager
    def transaction(self) -> Iterator["SourceTransaction"]:
        """まとめて確定する。途中で失敗したら**全部戻す**。

        書き込みの鍵は最初に取る(`BEGIN IMMEDIATE`)── 読んでから書く間に
        別の端末が割り込むと、送り済みかどうかの確かめが古くなる。
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise SourceError(str(exc)) from exc
        try:
            yield SourceTransaction(self._conn)
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SourceError(str(exc)) from exc
        except BaseException:
            self._conn.rollback()
            raise


class SourceTransaction:
    """`SourceConnection.transaction()` の中で使う手。**途中で確定しない。**

    `SourceConnection.execute` は1文ごとに確定するので、ヘッダと明細を
    まとめて足す(片方だけ残してはいけない)ときはこちらを使う。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cursor = self._conn.execute(sql, tuple(params))
        names = [d[0] for d in cursor.description or []]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        return self._conn.execute(sql, tuple(params)).rowcount

    def insert(self, table: str, values: dict[str, Any]) -> int:
        """1行足して、**付いた番号(rowid)**を返す。"""
        if not values:
            raise SourceError("入れる値がありません。")
        cols = ", ".join(quote_identifier(k) for k in values)
        marks = ", ".join("?" for _ in values)
        cursor = self._conn.execute(
            f"INSERT INTO {quote_identifier(table)} ({cols}) VALUES ({marks})",
            list(values.values()))
        return int(cursor.lastrowid or 0)


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
