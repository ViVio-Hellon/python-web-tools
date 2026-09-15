"""起動時の自動取り込み

その日まだ取り込んでいなければ、起動のついでに取り込む。取り込んだ日は
手元DBの `Access取込記録` に残す ── 1日に何度も総入れ替えを走らせない
ため。

背後(スレッド)でも走らせられる。起動を待たせないためで、終わったか
どうかは `jobs` が画面へ伝える。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, db, import_specs, source_db
from .logging_utils import get_logger
# **他の段は名前ではなくモジュールで呼ぶ。** 差し替え(試験の stub)の
# 当て先が持ち主の1か所で済む
from . import sync_import, sync_sources
from .sync_import import ImportResult
from .sync_sources import Progress, _noop_progress

log = get_logger("data_sync.auto")

# ==================================================================
# 起動時の自動取り込み
# ==================================================================
STAMP_TABLE = "Access取込記録"


def _ensure_stamp_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS [{STAMP_TABLE}] ("
        " ファイル   TEXT PRIMARY KEY,"
        " 更新時刻   REAL NOT NULL,"
        " 取込日時   TEXT NOT NULL DEFAULT (datetime('now','localtime')))")
    conn.commit()


def needs_import(conn: sqlite3.Connection, path: Path) -> bool:
    """元ファイルが前回の取り込み以降に更新されているか。

    毎回の起動で1万件超を読み直すのは無駄なので、更新されたファイルだけ
    取り込む。仕掛台帳は日々更新されるので結果的にほぼ毎回読み、
    マスタは変わったときだけ読む、という自然な動きになる。
    """
    _ensure_stamp_table(conn)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    row = conn.execute(
        f"SELECT 更新時刻 FROM [{STAMP_TABLE}] WHERE ファイル = ?",
        (str(path),)).fetchone()
    return row is None or mtime > float(row[0]) + 1.0


def mark_imported(conn: sqlite3.Connection, path: Path) -> None:
    _ensure_stamp_table(conn)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO [{STAMP_TABLE}] (ファイル, 更新時刻)"
            " VALUES (?, ?)", (str(path), mtime))


def auto_import(conn: sqlite3.Connection, *, force: bool = False,
                progress: Optional[Progress] = None) -> ImportResult:
    """起動時に呼ぶ取り込み。更新されたファイルだけを読む。

    見つからないファイルや読めない環境では**黙って何もしない**。
    起動のたびに警告を出しても現場の役に立たないので、状態は
    設定画面で確認してもらう。
    """
    result = ImportResult()

    master = sync_sources.find_material_db()
    if master is not None and (force or needs_import(conn, master)):
        log.info("自動取り込み(マスタ): %s", master)
        result.merge(sync_import.import_master(conn, master, progress=progress,
                                   progress_range=(0, 50)))
        mark_imported(conn, master)

    for table, path in sync_sources.find_lot_dbs().items():
        if not force and not needs_import(conn, path):
            continue
        log.info("自動取り込み(仕掛台帳): %s", path)
        sync_import.import_tables(
            conn, path, {table: import_specs.LOT_IMPORT_SPECS[table]},
            source_table=import_specs.LOT_SOURCE_TABLE,
            required=import_specs.REQUIRED_KEY_COLUMNS,
            blank_is_missing=import_specs.BLANK_IS_MISSING,
            fallbacks=import_specs.NULL_FALLBACKS, result=result,
            progress=progress, progress_range=(50, 100))
        mark_imported(conn, path)

    return result


def auto_import_in_background(
        on_done: Optional[Callable[[ImportResult], None]] = None,
        progress: Optional[Progress] = None) -> bool:
    """起動直後に呼ぶ。画面を待たせずに裏で取り込む。

    SQLiteの接続はスレッドをまたげないので、作業スレッド側で開き直す。
    終わったら `on_done` を呼ぶので、呼び出し側は
    (tkinterなら `after` 経由で)画面を更新する。

    戻り値は**取り込みを始めたか**。設定でOFFのときは False を返し、
    `on_done` も呼ばない。呼び出し側は進捗表示を出す前にこれを見る
    (出しっぱなしで終わらないようにするため)。
    失敗しても `on_done` は必ず呼ぶ(進捗表示を確実に閉じるため)。
    """
    from . import user_settings
    if not user_settings.get(config.KEY_AUTO_IMPORT, config.AUTO_IMPORT_DEFAULT):
        log.info("起動時の自動取り込みは設定でOFFになっています")
        return False

    def runner() -> None:
        result = ImportResult()
        try:
            with db.connect() as conn:
                db.apply_schema(conn)
                result.merge(auto_import(conn, progress=progress))
            if result.total:
                log.info("起動時の自動取り込み: %s件", result.total)
        except Exception as exc:               # noqa: BLE001 - 起動を止めない
            log.exception("起動時の自動取り込みで例外(手動で取り込めます)")
            result.errors.append(f"起動時の自動取り込み: {exc}")
        finally:
            if on_done is not None:
                on_done(result)

    threading.Thread(target=runner, daemon=True).start()
    return True
