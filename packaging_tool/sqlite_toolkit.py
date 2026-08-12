"""SQLite汎用ヘルパー(ロック競合リトライ・簡易CRUD)

特定のプロジェクトのテーブル名やスキーマに一切依存しない、SQLite操作の
共通部分だけを集めたモジュール。ネットワーク共有上の.dbファイルを複数
端末から使う運用(VBA版のADO接続がそうだった)では、他端末が書いている
瞬間に読み書きがぶつかって `database is locked` になることがある。
待てば通る見込みがあるので、ここでは一律に「ロック競合のときだけ
少し待って再試行する」という方針で統一している。

    - `execute_with_retry` / `fetch_all` / `fetch_one` : リトライ付きSQL実行
    - `update_record` / `insert_record`                : 簡易CRUD
    - `is_lock_error` / `enable_wal`                    : ロック対策の部品
    - `now_db_string` / `sanitize_for_db`               : 文字列まわりの補助

他のVBA移行プロジェクトでも、このファイルをそのまま持っていって
`db.py` 側にはプロジェクト固有のスキーマ適用・接続先解決だけを残せば
使い回せる。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from .logging_utils import get_logger

log = get_logger("sqlite_toolkit")

# ロック競合時の既定リトライ回数/待機秒数。呼び出し側の都合(業務側の
# config 等)には依存させず、このモジュール単体で完結させる
DEFAULT_MAX_RETRY = 3
DEFAULT_RETRY_WAIT_SEC = 1.0

# sqlite3標準の"database is locked"系エラーメッセージ断片
LOCK_ERROR_SNIPPETS = ("database is locked", "database table is locked", "busy")


def is_lock_error(exc: BaseException) -> bool:
    """sqlite3のロック競合系エラーか判定する(VBA `IsLockError` 相当)。"""
    if not isinstance(exc, (sqlite3.OperationalError, sqlite3.DatabaseError)):
        return False
    msg = str(exc).lower()
    return any(snippet in msg for snippet in LOCK_ERROR_SNIPPETS)


def enable_wal(conn: sqlite3.Connection) -> None:
    """読み書きが同時でも待たされないようにする(WALモード)。

    取り込みは「DELETEしてから全行INSERT」を1つのトランザクションで行う
    ような使い方をすることがある。既定の rollback journal だと、その間
    ずっと**読み取り側が止まる**ため、裏で取り込んでいるあいだ画面の
    操作が固まってしまう。WALなら読み手は取り込み前の内容を見たまま
    進める。

    共有フォルダ上の.dbではWALが使えないことがある(共有メモリを
    置けない)。その場合は既定のまま続ける。
    """
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()
    except sqlite3.Error as exc:
        log.warning("WALを有効にできませんでした(既定のまま続行): %s", exc)
        return
    actual = (mode[0] if mode else "") or ""
    if str(actual).lower() != "wal":
        log.info("WALは使えませんでした(journal_mode=%s)。既定のまま続行します", actual)


# ------------------------------------------------------------------
# 汎用: リトライ付きSQL実行 (UPDATE/INSERT/DELETE用)
# ------------------------------------------------------------------
@dataclass
class ExecResult:
    ok: bool
    rowcount: int = 0
    error: Optional[str] = None
    lastrowid: Optional[int] = None


def execute_with_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
    *,
    caller_name: str = "SQL",
    max_retry: int = DEFAULT_MAX_RETRY,
    retry_wait_sec: float = DEFAULT_RETRY_WAIT_SEC,
) -> ExecResult:
    """VBA `ExecuteSQLWithRetry` の移植。ロック競合時のみリトライする。"""
    retry_count = 0
    while True:
        try:
            with conn:  # 成功時commit / 例外発生時rollback
                cur = conn.execute(sql, params)
            log.debug("%s: 実行成功 影響行数=%s", caller_name, cur.rowcount)
            return ExecResult(ok=True, rowcount=cur.rowcount, lastrowid=cur.lastrowid)
        except sqlite3.Error as exc:
            log.warning("%s: エラー %s SQL=%s", caller_name, exc, sql)
            if is_lock_error(exc) and retry_count < max_retry:
                retry_count += 1
                wait = retry_wait_sec * retry_count
                log.info("%s: ロック競合 リトライ %s回目 (%.1fs待機)", caller_name, retry_count, wait)
                time.sleep(wait)
                continue
            return ExecResult(ok=False, error=str(exc))


# ------------------------------------------------------------------
# 汎用: リトライ付き読み取り
# ------------------------------------------------------------------
def fetch_all(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] = (),
    *,
    caller_name: str = "SELECT",
    max_retry: int = DEFAULT_MAX_RETRY,
    retry_wait_sec: float = DEFAULT_RETRY_WAIT_SEC,
) -> Optional[list[sqlite3.Row]]:
    """VBA `GetRecordsArrSafe`/`ReadArrWithRetry` の移植。

    失敗時は `None` を返す(VBA版のEmpty相当)。該当行が無い場合は
    空リスト `[]` を返す(「クエリ自体は成功したが0件」と
    「クエリ失敗」を区別できるようにする)。
    """
    retry_count = 0
    while True:
        try:
            cur = conn.execute(sql, params)
            return cur.fetchall()
        except sqlite3.Error as exc:
            log.warning("%s: エラー %s SQL=%s", caller_name, exc, sql)
            if is_lock_error(exc) and retry_count < max_retry:
                retry_count += 1
                time.sleep(retry_wait_sec * retry_count)
                continue
            return None


def fetch_one(
    conn: sqlite3.Connection, sql: str, params: Sequence[Any] = (), *,
    caller_name: str = "SELECT", max_retry: int = DEFAULT_MAX_RETRY,
    retry_wait_sec: float = DEFAULT_RETRY_WAIT_SEC,
) -> Optional[sqlite3.Row]:
    rows = fetch_all(conn, sql, params, caller_name=caller_name,
                     max_retry=max_retry, retry_wait_sec=retry_wait_sec)
    if not rows:
        return None
    return rows[0]


# ------------------------------------------------------------------
# adoSQL 相当: 主キー指定によるレコード更新(存在確認つき)
# ------------------------------------------------------------------
@dataclass
class UpdateResult:
    ok: bool
    rowcount: int = 0
    # "success" / "not_found" / "error"
    reason: str = "success"
    error: Optional[str] = None


def update_record(
    conn: sqlite3.Connection,
    table: str,
    key_field: str,
    key_value: Any,
    values: Mapping[str, Any],
    *,
    where_clause: Optional[str] = None,
    where_params: Sequence[Any] = (),
    max_retry: int = DEFAULT_MAX_RETRY,
    retry_wait_sec: float = DEFAULT_RETRY_WAIT_SEC,
) -> UpdateResult:
    """VBA `adoSQL`(UPDATEモード)の移植。

    VBA版は「対象レコードが存在するか事前にCOUNTで確認」→
    「UPDATE実行」→「影響行数が0なら他端末による変更・削除とみなす」
    という2段階チェックを行っていた。同じ流れをトランザクション内で行う。

    `where_clause`/`where_params` を指定すると、主キー1件指定ではなく
    任意のWHERE句で更新対象を絞り込める(VBA版の `keySQLfilter` 相当)。
    """
    if where_clause is None:
        where_sql = f"[{key_field}] = ?"
        where_params = (key_value,)
    else:
        where_sql = where_clause

    set_sql = ", ".join(f"[{col}] = ?" for col in values.keys())
    sql = f"UPDATE [{table}] SET {set_sql} WHERE {where_sql}"
    params = (*values.values(), *where_params)

    retry_count = 0
    while True:
        try:
            with conn:
                exists = conn.execute(
                    f"SELECT COUNT(*) AS cnt FROM [{table}] WHERE {where_sql}", where_params
                ).fetchone()
                if exists["cnt"] == 0:
                    log.warning("update_record: 対象レコードなし table=%s where=%s", table, where_sql)
                    return UpdateResult(ok=False, reason="not_found")

                cur = conn.execute(sql, params)
            log.debug("update_record: 更新成功 table=%s 影響行数=%s", table, cur.rowcount)
            if cur.rowcount == 0:
                # 存在確認とUPDATEの間に他プロセスが変更・削除した場合
                return UpdateResult(ok=False, reason="not_found")
            return UpdateResult(ok=True, rowcount=cur.rowcount, reason="success")
        except sqlite3.Error as exc:
            log.warning("update_record: エラー %s SQL=%s", exc, sql)
            if is_lock_error(exc) and retry_count < max_retry:
                retry_count += 1
                time.sleep(retry_wait_sec * retry_count)
                continue
            return UpdateResult(ok=False, reason="error", error=str(exc))


def insert_record(
    conn: sqlite3.Connection, table: str, values: Mapping[str, Any], *,
    caller_name: Optional[str] = None,
    max_retry: int = DEFAULT_MAX_RETRY,
    retry_wait_sec: float = DEFAULT_RETRY_WAIT_SEC,
) -> ExecResult:
    """新規テーブル(StockHistory等)用のINSERTヘルパー。

    VBA版の adoSQL は UPDATE専用だったため直接対応する関数は無いが、
    INSERTが必須になる場面のために用意する。
    """
    cols = ", ".join(f"[{c}]" for c in values.keys())
    placeholders = ", ".join("?" for _ in values)
    sql = f"INSERT INTO [{table}] ({cols}) VALUES ({placeholders})"
    return execute_with_retry(conn, sql, tuple(values.values()),
                              caller_name=caller_name or f"INSERT {table}",
                              max_retry=max_retry, retry_wait_sec=retry_wait_sec)


# ------------------------------------------------------------------
# 汎用ユーティリティ
# ------------------------------------------------------------------
def now_db_string() -> str:
    """VBA `NowDBString` の移植。DB登録用の日時文字列を生成する。

    元のVBAは "yyyy/mm/dd hh:nn:ss" 形式だったが、SQLiteの日付関数や
    ISO8601ソートとの親和性を優先し "YYYY-MM-DD HH:MM:SS" 形式にする。
    どちらもゼロ埋めの文字列比較で正しくソートできる点は同じ。
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sanitize_for_db(s: str) -> str:
    """VBA `SanitizeForDB` の移植。

    VBA版はADOの `GetString` がタブ・独自改行区切りでレコードを
    直列化する実装だったため、値にタブ/改行が混じると列崩壊を起こす
    という制約があった。sqlite3のプレースホルダ経由の読み書きでは
    その問題は起きないが、表示上の見栄え(改行でレイアウトが崩れる等)
    を防ぐため、同じサニタイズを踏襲する。
    """
    s = s.replace("\t", " ")
    for br in ("\r\n", "\r", "\n"):
        s = s.replace(br, " ")
    return s.strip()
