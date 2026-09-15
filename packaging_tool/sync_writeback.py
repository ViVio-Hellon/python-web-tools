"""書き戻し (手元 → 取り込み元)

行の予約・送信ID発行・二重登録防止・再送判定は、業務に依存しない
`outbox_sync` に任せている。ここに残すのは「取り込み元をどう探すか」
「接続できないときの日本語メッセージ」といった、このツール固有の事情
だけ。

**どのテーブルを書き戻すかの一覧(`WRITEBACK_SPECS`)もここが持つ。**
新しいテーブルを足したくなったら1行足すだけでよい。`key_column` を
間違えるとそのテーブルは一度も送られず、しかもSQLは失敗を握って空
リストを返すので「送るものが無い」ように見えて気づけない ── スキーマと
一致していることは `tests/test_data_sync.py` が固定する。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, db, import_specs, outbox_sync, source_db
from .logging_utils import get_logger
from .outbox_sync import WriteBackResult, WriteBackSpec
# **他の段は名前ではなくモジュールで呼ぶ。** 差し替え(試験の stub)の
# 当て先が持ち主の1か所で済む
from . import sync_sources
from .sync_sources import SyncError

log = get_logger("data_sync.writeback")

# 書き戻し対象テーブルの定義。新しいテーブルを書き戻したくなったら
# ここに1行足すだけでよい(行の予約・送信・二重防止は outbox_sync に
# 任せているので、そちら側は無改造で動く)。
#
# key_column を間違えると、そのテーブルは**一度も送られない**。
# しかもSQLは失敗を握って空リストを返すので、「送るものが無い」ように
# 見えて気づけない(実際、入出庫履歴が管理番号扱いになっていて一度も
# 送られていなかった)。スキーマと一致していることは
# tests/test_data_sync.py で固定する。
WRITEBACK_SPECS: list[WriteBackSpec] = [
    WriteBackSpec(sqlite_table=config.TBL_WAREHOUSE_ORDER,
                  access_table=config.TBL_WAREHOUSE_ORDER,
                  key_column="管理番号",
                  # **確認・取消の印はあとから付く。** 行を送ったあとに
                  # 手元で変わる値なので、足すだけでは共有へ届かない
                  # (届かないと、現場から確認状況が見えず、次の取り込みで
                  #  印が消える)
                  mark_columns=("確認済み", "確認日時",
                                "取り消し済", "取り消し日時"),
                  mark_pending="印未反映",
                  source_key="取込元管理番号"),
    WriteBackSpec(sqlite_table=config.TBL_STOCK_HISTORY,
                  access_table=config.TBL_STOCK_HISTORY,
                  key_column="id"),
]


# 重すぎて、何度も走らせるものではない。
ORDER_TABLES: tuple[str, ...] = tuple(
    spec.sqlite_table for spec in WRITEBACK_SPECS)

def _unsent_writeback_tables(conn: sqlite3.Connection) -> dict[str, int]:
    """書き戻し対象で、**まだ共有へ渡せていないもの**が残っているテーブル。

    2種類ある。どちらも総入れ替えで消えると取り返せない。

        まだ送っていない行   … 手元で登録した発注そのもの
        まだ送っていない印   … 確認済み・取消の印(あとから付く)

    印のほうを数え忘れると、「確認したのに翌朝消えている」が起きる。
    """
    remaining = dict(outbox_sync.unsent_tables(conn, WRITEBACK_SPECS))
    for table, count in outbox_sync.unpushed_mark_tables(
            conn, WRITEBACK_SPECS).items():
        remaining[table] = remaining.get(table, 0) + count
    return remaining

def write_back(conn: sqlite3.Connection,
               source_path: Optional[Path] = None) -> WriteBackResult:
    """手元で増えた行を取り込み元へ送る(倉庫発注・入出庫履歴)。

    取り込み元に届かない端末では**何もせずに理由だけ返す**。手元の登録は
    すでに済んでいるので現場は止まらず、次に繋がったときにまとめて送られる。
    """
    result = WriteBackResult()

    path = source_path or sync_sources.find_material_db()
    if path is None:
        result.skipped_reason = (
            "書き戻し先のファイルが見つかりません。"
            f"{config.master_db_dir()} に {config.MATERIAL_DB_NAME} を"
            "置くか、設定画面で置き場所を直してください。")
        return result

    try:
        source = source_db.connect(path)
    except source_db.SourceError as exc:
        result.skipped_reason = f"取り込み元を開けませんでした: {exc}"
        return result

    try:
        result = outbox_sync.write_back(conn, source, WRITEBACK_SPECS)
    finally:
        source.close()
    return result


def write_back_in_background(on_done: Optional[Callable[[WriteBackResult], None]] = None) -> None:
    """登録操作のあとに呼ぶ、邪魔をしない書き戻し。

    倉庫送信や受入/払出の直後に呼ぶ想定。**成功しても失敗しても
    画面には何も出さない**(SQLiteへの登録はもう終わっており、
    取り込み元への反映は遅れても取り返せるため)。結果はログに残り、
    送れなかった分は次に「取り込み元へ反映」を押したときにまとめて送られる。

    取り込み元に届かない端末でも、送れなかったことがログに残るだけで
    現場は止まらない。
    """
    def runner() -> None:
        try:
            with db.connect() as conn:
                result = write_back(conn)
            if result.total:
                log.info("取り込み元へ自動反映: %s件", result.total)
            for message in result.errors:
                log.warning("取り込み元へ反映できず: %s", message)
            if on_done is not None:
                on_done(result)
        except Exception:                       # noqa: BLE001 - 背景処理なので握る
            log.exception("取り込み元への自動反映で例外(手元の登録は完了しています)")

    threading.Thread(target=runner, daemon=True).start()
