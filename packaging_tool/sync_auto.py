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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, db, import_diag, import_specs, source_db
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
    """元ファイルが前回の取り込み以降に更新されているか(理由は `import_reason`)。"""
    return import_reason(conn, path)[0]


def import_reason(conn: sqlite3.Connection, path: Path) -> tuple[bool, str]:
    """元ファイルが前回の取り込み以降に更新されているか。

    毎回の起動で1万件超を読み直すのは無駄なので、更新されたファイルだけ
    取り込む。仕掛台帳は日々更新されるので結果的にほぼ毎回読み、
    マスタは変わったときだけ読む、という自然な動きになる。
    """
    _ensure_stamp_table(conn)
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        return False, f"見に行けません({exc})"
    row = conn.execute(
        f"SELECT 更新時刻, 取込日時 FROM [{STAMP_TABLE}] WHERE ファイル = ?",
        (str(path),)).fetchone()
    now = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    if row is None:
        return True, f"この場所から取り込んだ記録が無い(ファイルの更新 {now})"
    then = datetime.fromtimestamp(float(row[0])).strftime("%Y-%m-%d %H:%M:%S")
    if mtime > float(row[0]) + 1.0:
        return True, f"更新されている(前回取り込んだ時点 {then} → いま {now})"
    # **いちばん見落としやすい場合。** 中身を差し替えても、更新時刻が
    # 前回より新しくならない写し方(時刻を保ったコピーなど)だとここに来る
    return False, (f"前回取り込んだ時点({then}、取り込んだのは {row[1]})から"
                   f"更新時刻が進んでいない(いま {now})。中身を差し替えたのに"
                   "読まれないときは、設定画面から手で取り込んでください")


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
    """起動時の取り込み(本体は `_auto_import`)。**読んだか見送ったかを
    診断記録に残す** ── 見送ったときはふだんのログに1行も出ないため。"""
    with import_diag.run("起動時の自動取り込み"
                         + ("(強制)" if force else "")) as outer:
        result = _auto_import(conn, force=force, progress=progress)
        if outer:
            import_diag.write("  まとめ: " + (result.summary().replace("\n", "\n    ")
                                              if (result.imported or result.errors)
                                              else "読んだ表はありません"))
    return result


def _auto_import(conn: sqlite3.Connection, *, force: bool = False,
                 progress: Optional[Progress] = None) -> ImportResult:
    """起動時に呼ぶ取り込み。更新されたファイルだけを読む。

    見つからないファイルや読めない環境では**黙って何もしない**。
    起動のたびに警告を出しても現場の役に立たないので、状態は
    設定画面で確認してもらう。

    【マスタは3ファイルある ── 3つとも見る】
    `import_master` が読むのは梱包資材マスタ・看板マスタ・パレット閾値
    マスタの**3ファイル**です。ところが以前は梱包資材マスタの更新時刻
    だけを見ていたので、

        資材課が看板マスタ(在庫薄)だけを直した
        資材課がパレット閾値マスタだけを直した

    という**いちばんよくある直し方**では、梱包資材マスタが動くまで
    起動時の取り込みが走りませんでした。現場からは「マスタは直したのに
    効かない」に見え、設定画面から手で「まとめて取り込み」を押すまで
    古い値で動き続けます。取り込んだ印も1ファイルぶんしか残しておらず、
    手で押しても次の起動の判定には使えていませんでした。

    どれか1つでも新しければ読み直し、**3つとも印を残します**。
    """
    result = ImportResult()

    master = sync_sources.find_material_db()
    # 3ファイルまとめて1回の `import_master` で読むので、判定も印も3つぶん
    master_group = [p for p in (master,
                                sync_sources.find_kanban_db(),
                                sync_sources.find_threshold_db())
                    if p is not None]
    import_diag.describe_file("梱包資材マスタ", master, [config.master_db_dir()])
    reasons = {p: import_reason(conn, p) for p in master_group}
    for p, (read, why) in reasons.items():
        import_diag.decision(p, read or force, "手で押した(強制)" if force else why)
    if master is not None and (force or any(r for r, _ in reasons.values())):
        log.info("自動取り込み(マスタ): %s", master)
        result.merge(sync_import.import_master(conn, master, progress=progress,
                                   progress_range=(0, 50)))
        for path in master_group:
            mark_imported(conn, path)

    found = sync_sources.find_lot_dbs()
    second = sync_sources.find_second_lot_dbs()
    for table, filename in config.LOT_DB_FILES.items():
        import_diag.describe_file(f"仕掛台帳 {filename}", found.get(table),
                                  sync_sources.lot_search_dirs())
    for table, path in found.items():
        # 2つ目のファイルが変わったときも読み直す(足す分が変わる)
        group = [p for p in (path, second.get(table)) if p is not None]
        decisions = {p: import_reason(conn, p) for p in group}
        for p, (read, why) in decisions.items():
            import_diag.decision(p, read or force, "手で押した(強制)" if force else why)
        if not force and not any(read for read, _why in decisions.values()):
            continue
        log.info("自動取り込み(仕掛台帳): %s", path)
        sync_import.import_lot_table(
            conn, table, path, second.get(table), result=result,
            progress=progress, progress_range=(50, 100))
        for p in group:
            mark_imported(conn, p)

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
