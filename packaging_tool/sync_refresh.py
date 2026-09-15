"""開いたままでも、届いた発注に気づくための取り込み直し

取り込みは起動時と手動だけだった。資材の端末を朝から開きっぱなしに
すると、**その朝の発注一覧を一日出し続ける** ── 現場が昼に出した発注は、
閉じて開き直すまで出てこない。

ここでは**発注まわりの表だけ**を取り込み直す。マスタ全部の総入れ替えは
重すぎて、何度も走らせるものではない。

`only_if_changed` を立てると、取り込み元の姿(大きさと更新時刻)を見て、
変わっていなければ何もしない ── 見に行くだけなら共有への往復は軽い。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config, db, import_specs, outbox_sync, source_db
from .logging_utils import get_logger
# **他の段は名前ではなくモジュールで呼ぶ。** 差し替え(試験の stub)の
# 当て先が持ち主の1か所で済む
from . import sync_import, sync_sources, sync_writeback
from .sync_sources import Progress, _noop_progress
from .sync_writeback import ORDER_TABLES, WRITEBACK_SPECS

log = get_logger("data_sync.refresh")

@dataclass
class OrderRefresh:
    """発注の取り込み直し1回分。画面に出す文はここで作る。"""

    looked: bool = False          # 取り込み元を見に行けたか
    changed: bool = False         # 前回から変わっていたか
    imported: int = 0             # 入れ直した行数
    tables: int = 0               # 入れ直せた表の数
    errors: list[str] = field(default_factory=list)
    reason: str = ""              # 見に行けなかった理由

    @property
    def updated(self) -> bool:
        """画面を出し直す必要があるか。

        **表が1つ読めなかったからといって、出し直さないのは違う。**
        発注は入れ替わっているのに古い一覧を出したままになる。
        1つでも入れ替えられたなら出し直す。
        """
        return self.changed and self.tables > 0

    def message(self) -> str:
        if not self.looked:
            return self.reason or "取り込み元を見に行けませんでした。"
        if not self.tables and self.errors:
            return "取り込み元を見に行きましたが、取り込めませんでした。"
        if not self.changed:
            return "取り込み元を見ました。新しいものはありません。"
        return f"取り込み元から{self.imported}件を取り込みました。"


def refresh_orders(conn: sqlite3.Connection,
                   source_path: Optional[Path] = None,
                   *, only_if_changed: bool = False) -> OrderRefresh:
    """発注まわりの表だけを取り込み直す。

    `only_if_changed` は見張り用。取り込み元が前回から変わっていなければ
    **開かずに帰る**ので、共有への往復はファイルの姿を見るだけで済む。

    総入れ替えなので、送れていない行・送れていない印がある表は入れ替え
    ない(`import_master` と同じ関門を通す)。先に書き戻しを走らせて、
    それでも残るものがあれば、その表は今回見送る。
    """
    result = OrderRefresh()
    path = source_path or sync_sources.find_material_db()
    if path is None:
        result.reason = (
            "取り込み元が見つかりません。設定画面で置き場所を確かめてください。")
        return result

    # **届いていないのを「見た」と言わない。** 共有へ繋がらない端末では
    # ここで止まる。一覧は手元のもののままで、画面には理由を出す
    if sync_sources.source_stamp(path) is None:
        result.reason = (f"取り込み元に届きませんでした({path})。"
                         "共有に繋がっているか確かめてください。")
        return result

    result.looked = True
    changed = sync_sources.source_changed(path)
    if only_if_changed and not changed:
        return result

    specs = {t: s for t, s in import_specs.IMPORT_SPECS.items()
             if t in ORDER_TABLES}
    unsent = sync_writeback._unsent_writeback_tables(conn)
    if unsent:
        sync_writeback.write_back(conn, path)
        unsent = sync_writeback._unsent_writeback_tables(conn)
    for table in unsent:
        specs.pop(table, None)
        result.errors.append(
            f"{table}: まだ取り込み元へ送れていないものがあるため、"
            "取り込みを見送りました")
    if not specs:
        return result

    imported = sync_import.import_tables(
        conn, path, specs,
        required=import_specs.REQUIRED_KEY_COLUMNS,
        blank_is_missing=import_specs.BLANK_IS_MISSING,
        optional=import_specs.OPTIONAL_TABLES,
        fallbacks=import_specs.NULL_FALLBACKS)
    for spec in WRITEBACK_SPECS:
        if spec.sqlite_table in imported.imported:
            outbox_sync.mark_all_sent(conn, spec)

    result.imported = imported.total
    result.tables = len(imported.imported)
    result.errors.extend(imported.errors)
    # **「変わっていた」は取り込み元の姿で決める。** 押されたから取り込んだ
    # だけのときに「新しいものがありました」と言うと、押すたびに何かが
    # 起きたように見える
    result.changed = changed
    # **1つでも読めたなら、その姿で読んだと覚える。**
    #
    # 「1つも失敗しなかったら」にすると、取り込み元に無い表が1つでも
    # あるかぎり覚えないまま ── 見張りが同じものを何度でも取り込み直す
    # (実際にそうなっていた。共有に パレット入出庫履歴 が無い環境で、
    #  60秒ごとに総入れ替えが走る)。読めなかった表は次も読めないので、
    #  待っても変わらない。何も読めなかったときだけ、次にやり直す
    if result.tables:
        sync_sources.note_source_read(path)
    log.info("発注を取り込み直しました: %s件(変化=%s)", result.imported, changed)
    return result
