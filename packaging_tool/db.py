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
    _migrate_after_schema(conn)
    _seed_thresholds(conn)


def _seed_thresholds(conn: sqlite3.Connection) -> None:
    """パレット適合閾値の表が空なら基準表を入れる。

    **他のマスタと違って、空のままでは動きません。** 閾値の帯は
    0〜999999を隙間なく覆っている必要があり、0件だと適合範囲の再計算が
    まるごと止まります。表を作った直後に必ず使える状態にしておきます。

    入れるのは空のときだけなので、資材課がマスタ管理画面で直した値を
    起動のたびに書き戻すことはありません(`pallet_threshold.seed`)。
    """
    from . import pallet_threshold
    pallet_threshold.seed(conn)


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
    # 一覧に出す列を増やした(現場の依頼)。既にある端末の表には増えないので、
    # ここで作り直す。**新しい列を足すたびにここへ書き足す**
    _drop_if_missing_columns(conn, "仕掛ロット",
                             ("前々工程実績_枚本数", "前工程実績_枚本数",
                              "BOX最終実績_設備名", "BOX最終実績_板厚",
                              "BOX最終実績_板幅", "BOX最終実績_板丈",
                              "BOX最終実績_枚本数"),
                             "前工程まわりと BOX最終実績")
    _park_old_board_usage(conn)


# 移行のあいだだけ置いておく古いボード使用実績の名前。
# `_park_old_board_usage` が退避し、`_migrate_after_schema` が中身を
# 新しい表へ移して消す
_BOARD_USAGE_PARKED = "ボード使用実績_移行中"


def _park_old_board_usage(conn: sqlite3.Connection) -> None:
    """古い形のボード使用実績を脇へ退ける。**捨てない。**

    以前は 幅×丈×タイプ ごとの集計1行(`使用回数` / `最終使用日時`)
    でした。製品とパレットの寸法も一緒に残すことになったので、
    **1行 = 1回の使用**の記録に作り変えます。

    ここでは名前を変えるだけで、中身を移すのは `schema.sql` が新しい
    表を作ったあと(`_migrate_after_schema`)です。**DDLを2か所に
    書かない**ため ── ここで新しい表を作ってしまうと、`schema.sql` を
    直したときに片方だけ古くなります。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("ボード使用実績",)).fetchone()
    if row is None:
        return
    ddl = (row[0] if not isinstance(row, sqlite3.Row) else row["sql"]) or ""
    if "最終使用日時" not in ddl:
        return                                    # もう新しい形
    conn.execute(f"DROP TABLE IF EXISTS [{_BOARD_USAGE_PARKED}]")
    conn.execute(f"ALTER TABLE [ボード使用実績] RENAME TO [{_BOARD_USAGE_PARKED}]")
    log.info("ボード使用実績を新しい形に作り変えます(古い集計は引き継ぎます)")


def _migrate_after_schema(conn: sqlite3.Connection) -> None:
    """`schema.sql` を当てたあとに片付けること。"""
    _move_old_board_usage(conn)
    _add_order_mark_columns(conn)
    # 実績(スナップショット)の表。名前が `config.TBL_PT_*` の仮の値なので、
    # schema.sql に書かずに定数から作る(名前を直すのが1か所で済む)
    from . import pattern_store
    pattern_store.ensure_tables(conn)


# 発注テーブルにあとから足した列。`CREATE TABLE IF NOT EXISTS` は
# **すでにある表には何もしない**ので、前の版から入れ替えた端末には
# 足しに行く必要がある
_ORDER_ADDED_COLUMNS = (("取込元管理番号", "INTEGER"), ("印未反映", "TEXT"),
                        ("送信端末", "TEXT"), ("発注キー", "TEXT"))


def _add_order_mark_columns(conn: sqlite3.Connection) -> None:
    """確認の印を共有へ送るために足した2列を、古い手元DBにも足す。

    列が無いままだと、確認した印が共有へ届かず、次の取り込みで消えます
    (VER2.58.0 より前はそうなっていました)。
    """
    have = {r[1] for r in conn.execute(
        "PRAGMA table_info([資材パレット注文管理])")}
    if not have:
        return                                    # 表そのものが無い
    for column, kind in _ORDER_ADDED_COLUMNS:
        if column in have:
            continue
        conn.execute(
            f"ALTER TABLE [資材パレット注文管理] ADD COLUMN [{column}] {kind}")
        log.info("資材パレット注文管理 に %s 列を足しました", column)
    conn.commit()


def _move_old_board_usage(conn: sqlite3.Connection) -> None:
    """退けておいた古い集計を、新しい記録の表へ移す。

    古い行は「累計で何枚」しか持っていないので、**1行にまとめて**
    移します。製品・パレットの寸法は当時記録していないので0のまま
    ── 0は「分からない」の意味で、後から見たときに
    「このぶんは古い形で積まれた」と読み取れます。
    """
    found = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_BOARD_USAGE_PARKED,)).fetchone()
    if found is None:
        return
    moved = conn.execute(
        "INSERT INTO [ボード使用実績] "
        "(ボード幅, ボード丈, ボードタイプ, 枚数, 使用日時) "
        "SELECT ボード幅, ボード丈, ボードタイプ, 使用回数, 最終使用日時 "
        f"FROM [{_BOARD_USAGE_PARKED}] WHERE 使用回数 > 0").rowcount
    conn.execute(f"DROP TABLE [{_BOARD_USAGE_PARKED}]")
    conn.commit()
    log.info("古いボード使用実績を%s件引き継ぎました", moved)


def _drop_if_missing_columns(conn: sqlite3.Connection, table: str,
                             columns: tuple[str, ...], why: str) -> None:
    """列が増えた取り込み用テーブルを作り直す。

    `CREATE TABLE IF NOT EXISTS` は**すでにある表には何もしません**。
    列を足しても、既存の端末では増えないまま ── 一覧に新しい列を出す
    側だけが先に新しくなり、`no such column` で表が引けなくなります。

    仕掛台帳の表は取り込み元から丸ごと入れ直す**写し**なので、捨てて
    作り直してよい(利用者が手で入れた値は1つもありません)。次の
    取り込みで戻ります ── `_drop_if_old_shape` と同じ考え方で、
    見る向きが逆なだけです(古い印があれば捨てる/新しい列が無ければ捨てる)。
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,)).fetchone()
    if row is None:
        return
    ddl = (row[0] if not isinstance(row, sqlite3.Row) else row["sql"]) or ""
    missing = [c for c in columns if c not in ddl]
    if missing:
        log.info("%sに列を足すため作り直します(%s)。再取り込みが必要です",
                 table, why)
        conn.execute(f"DROP TABLE [{table}]")
        conn.commit()


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
