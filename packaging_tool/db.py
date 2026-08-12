"""DB共通アクセス層 (VBA `UFdaily` 標準モジュールのADOラッパー群の移植)

対応関係の目安:
    VBA                         -> Python
    ---------------------------------------------------------------
    adoConnection()              -> get_connection()
    ExecuteSQLWithRetry()         -> execute_with_retry() (sqlite_toolkit)
    GetRecordsArrSafe/GetFieldsArrSafe (ReadArrWithRetry)
                                  -> fetch_all() (sqlite_toolkit)
    adoSQL() (UPDATEのみ対応)      -> update_record() (sqlite_toolkit)
    IsLockError()                 -> sqlite_toolkit.is_lock_error()
    BuildSQLLiteral() / AdoTypeToCategory()
                                  -> 不要(sqlite3のプレースホルダ`?`が
                                     型に応じたエスケープ/バインドを自動で
                                     行うため、SQL文字列を手組みする
                                     BuildSQLLiteral相当のロジックは
                                     Python版では丸ごと不要になる)
    NowDBString()                  -> now_db_string() (sqlite_toolkit)
    SanitizeForDB()                 -> sanitize_for_db() (sqlite_toolkit)

【Access -> SQLite 型差異メモ】
    - Yes/No (Boolean)   -> INTEGER (0/1)
    - オートナンバー(Long) -> INTEGER PRIMARY KEY (SQLiteのrowidに直結)
    - 日付/時刻型         -> TEXT (ISO8601 "YYYY-MM-DD HH:MM:SS"、
                              文字列比較でソート可能な形式を維持)
    - 通貨/倍精度浮動小数点 -> REAL
    - テキスト型(短/長)    -> TEXT (SQLiteは可変長で長さ制限を持たない)
    SQLiteは動的型付け(型アフィニティ)のため、Access ADOのように
    フィールドごとの型を事前取得してSQLリテラルを作り分ける必要が無い。

【sqlite_toolkit との役割分担】
テーブル名やスキーマに依存しないSQLite操作(リトライ付き実行・読み取り・
簡易CRUD・ロック判定)は `sqlite_toolkit` に切り出してあり、他のVBA移行
プロジェクトでもそのまま使い回せる。このファイルに残っているのは、
「このツールのDBファイルをどこに置くか」「`schema.sql` の適用」
「過去のスキーマ変更への移行」といった、このプロジェクト固有の事情
だけ。`execute_with_retry`/`fetch_all` 等はここから再エクスポートして
いるので、既存の呼び出し元(`db.fetch_all(...)` 等)は変更不要。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from . import config, sqlite_toolkit
from .logging_utils import get_logger
from .sqlite_toolkit import ExecResult, UpdateResult, now_db_string, sanitize_for_db

log = get_logger("db")


# ------------------------------------------------------------------
# 接続
# ------------------------------------------------------------------
def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """SQLite接続を1本開いて返す。

    VBA版 `adoConnection` はファイル種別ごとにProviderを切り替えていたが、
    Python版はSQLite専用なのでその分岐は不要。ネットワーク共有上の
    .dbファイルを複数端末から開く運用を想定し、ロック待ち(busy_timeout)を
    設定して短時間の競合は自動で吸収する。
    """
    path = Path(db_path) if db_path is not None else config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    # isolation_level はデフォルト("" = DEFERRED)のままにする。
    # これによりINSERT/UPDATE/DELETEの前で暗黙にトランザクションが開始され、
    # `with conn:` ブロックの終了時にcommit/rollbackされるようになる
    # (isolation_level=Noneのautocommitモードにすると `with conn:` が
    # 何もグループ化しなくなり、複数文をまたぐ原子性が失われるため注意)。
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")  # ms。VBA版の ConnectionTimeout=10秒 に相当
    sqlite_toolkit.enable_wal(conn)
    return conn


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """`with db.connect() as conn:` の形で使う簡易コンテキストマネージャ。"""
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def apply_schema(conn: sqlite3.Connection) -> None:
    """`schema.sql` を適用してテーブルが無ければ作成する(冪等)。"""
    _migrate_before_schema(conn)
    schema_path = Path(__file__).resolve().parent / "schema.sql"
    conn.executescript(schema_path.read_text(encoding="utf-8"))


def _migrate_before_schema(conn: sqlite3.Connection) -> None:
    """`CREATE TABLE IF NOT EXISTS` では直せない形の変更を先に片付ける。

    仕掛ロットは当初 `ロット番号 TEXT PRIMARY KEY` にしていたが、実データでは
    ロット番号がBOX工程ごとに重複する(2046行中527ロット)ため取り込めなかった。
    このテーブルは仕掛台帳から丸ごと取り込み直す一時テーブルなので、
    古い形が残っていれば捨てて作り直してよい(利用者が入れた値は無い)。

    仕掛引当も同じ理由で作り直す。引当番号を数値(REAL)で持っていたため
    8桁の番号が 6.0717e+07 と指数表記になっていた。TEXTに変える。

    仕掛ロットはさらに、試験指示票の要否判定に使っていた列を
    `試験NO` から `品質グレード_表面処理` へ差し替えた(VBA
    `AdvanceCheck` の実クエリには試験NO列自体が含まれておらず、
    以前の判定根拠が誤りだったため)。
    """
    _drop_if_old_shape(conn, "仕掛ロット",
                       ("ロット番号        TEXT PRIMARY KEY",
                        "ロット番号 TEXT PRIMARY KEY",
                        "試験NO"),
                       "新しい形(代理キー・品質グレード_表面処理)")
    _drop_if_old_shape(conn, "仕掛引当",
                       ("引当番号     REAL", "引当番号 REAL"),
                       "引当番号を文字列に")


def _drop_if_old_shape(conn: sqlite3.Connection, table: str,
                       markers: tuple[str, ...], why: str) -> None:
    """古い定義のまま残っているテーブルを捨てる(取り込み直しで復元される)。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,)).fetchone()
    if row is None:
        return
    ddl = (row[0] if not isinstance(row, sqlite3.Row) else row["sql"]) or ""
    if any(marker in ddl for marker in markers):
        log.info("%sを%s作り直します。再取り込みが必要です", table, why)
        conn.execute(f"DROP TABLE [{table}]")
        conn.commit()


# ------------------------------------------------------------------
# 汎用SQLite操作(sqlite_toolkit)の薄い再エクスポート
#
# 実体は sqlite_toolkit にある(業務非依存で他プロジェクトにも使い回せる)。
# ここでは既存の呼び出し元(`db.fetch_all(...)` 等)がそのまま動くように
# 同名の関数を用意しつつ、リトライ回数/待機秒数だけこのプロジェクトの
# `config.MAX_RETRY`/`config.RETRY_BASE_WAIT_SEC` に合わせている。
# ------------------------------------------------------------------
def execute_with_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
    *,
    caller_name: str = "SQL",
    max_retry: int = config.MAX_RETRY,
) -> ExecResult:
    return sqlite_toolkit.execute_with_retry(
        conn, sql, params, caller_name=caller_name,
        max_retry=max_retry, retry_wait_sec=config.RETRY_BASE_WAIT_SEC)


def fetch_all(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
    *,
    caller_name: str = "SELECT",
    max_retry: int = config.MAX_RETRY,
) -> Optional[list[sqlite3.Row]]:
    return sqlite_toolkit.fetch_all(
        conn, sql, params, caller_name=caller_name,
        max_retry=max_retry, retry_wait_sec=config.RETRY_BASE_WAIT_SEC)


def fetch_one(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = (), *, caller_name: str = "SELECT"
) -> Optional[sqlite3.Row]:
    return sqlite_toolkit.fetch_one(
        conn, sql, params, caller_name=caller_name,
        retry_wait_sec=config.RETRY_BASE_WAIT_SEC)


def update_record(
    conn: sqlite3.Connection,
    table: str,
    key_field: str,
    key_value: Any,
    values: Mapping[str, Any],
    *,
    where_clause: Optional[str] = None,
    where_params: Sequence[Any] = (),
    max_retry: int = config.MAX_RETRY,
) -> UpdateResult:
    return sqlite_toolkit.update_record(
        conn, table, key_field, key_value, values,
        where_clause=where_clause, where_params=where_params,
        max_retry=max_retry, retry_wait_sec=config.RETRY_BASE_WAIT_SEC)


def insert_record(
    conn: sqlite3.Connection, table: str, values: Mapping[str, Any], *, caller_name: Optional[str] = None,
) -> ExecResult:
    return sqlite_toolkit.insert_record(
        conn, table, values, caller_name=caller_name,
        retry_wait_sec=config.RETRY_BASE_WAIT_SEC)
