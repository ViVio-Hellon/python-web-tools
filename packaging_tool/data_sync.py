"""取り込み元と手元DBのデータ同期

取り込み元(共有フォルダの sqlite3)と、手元の作業用DBのあいだで
2方向を扱う。

    取り込み元 → 手元   取り込み(マスタ・仕掛台帳)。起動時や手動で実行
    手元 → 取り込み元   書き戻し(倉庫発注・入出庫履歴)。登録のたびに反映

【ファイルの置き場所】
    梱包資材マスタ    : `config.master_db_dir()`
    仕掛台帳の3ファイル: `config.lot_db_dir()`(既定は社内共有のUNCパス)

どちらも設定画面で変えられる。届かない端末ではローカルの写しを指す。

【書き戻しについて】
書き戻すのは「このツールで新しく発生したデータ」だけで、マスタ類は
触らない。行の予約・送信ID発行・二重登録防止・再送判定は、業務に
依存しない `outbox_sync` に任せている。このファイルの役目は
「どのテーブルを書き戻すか」(`WRITEBACK_SPECS`)と「取り込み元を
どう見つけるか」だけ。

**取り込み元に届かなくても手元の登録は成功する**。同期は次に繋がった
ときへ持ち越す(現場を止めないことを優先する)。

【役割ごとに分けてある】
ここは**外向けの1枚窓**で、中身は役割ごとのモジュールが持つ。

    sync_sources    取り込み元を探す・読む・いつのものかを覚える
    sync_writeback  書き戻し(手元 → 取り込み元)と、その対象一覧
    sync_import     取り込み(取り込み元 → 手元)。総入れ替え
    sync_refresh    発注まわりだけの取り込み直し(開いたまま気づく)
    sync_auto       起動時の自動取り込み
    ここ            取り込めているかの診断

**段どうしは名前ではなくモジュールで呼ぶ**(`sync_sources.read_table(...)`)。
名前で取り込むと、差し替え(試験の stub)を持ち主に当てても取り込んだ側
には効かず、「止めたつもりの共有フォルダを実際に見に行く」ことになる。
"""
from __future__ import annotations

import sqlite3
import sys
from typing import Optional

from . import config, db, import_specs
from .logging_utils import get_logger

# ------------------------------------------------------------------
# **ここは外向けの1枚窓。** 中身は役割ごとのモジュールが持ち、呼ぶ側は
# このモジュールだけを見ればよいようにする。
#
#     sync_sources    取り込み元を探す・読む・いつのものかを覚える
#     sync_writeback  書き戻し(手元 → 取り込み元)と、その対象一覧
#     sync_import     取り込み(取り込み元 → 手元)。総入れ替え
#     sync_refresh    発注まわりだけの取り込み直し(開いたまま気づく)
#     sync_auto       起動時の自動取り込み
#     ここ            取り込めているかの診断
# ------------------------------------------------------------------
from . import sync_sources  # 診断はモジュール経由で呼ぶ(差し替えの当て先を1か所に)
from .sync_sources import (  # noqa: F401
    Progress, Rows, SyncError, _noop_progress, note_source_read, read_table,
    source_changed, source_stamp)


# 取り込み元を探す口は**モジュール経由で通す**。ハブが名前で抱えると、
# 持ち主(`sync_sources`)への差し替えがハブ越しの呼び出しに効かなくなり、
# 「試験では止めたつもりの共有フォルダを実際に見に行く」ことになる
def backend_name(*a, **k):
    return sync_sources.backend_name(*a, **k)


def find_material_db(*a, **k):
    return sync_sources.find_material_db(*a, **k)


def find_kanban_db(*a, **k):
    return sync_sources.find_kanban_db(*a, **k)


def find_threshold_db(*a, **k):
    return sync_sources.find_threshold_db(*a, **k)


def find_lot_dbs(*a, **k):
    return sync_sources.find_lot_dbs(*a, **k)


def list_tables(*a, **k):
    return sync_sources.list_tables(*a, **k)
from .sync_writeback import (  # noqa: F401
    ORDER_TABLES, WRITEBACK_SPECS, WriteBackResult, WriteBackSpec,
    _unsent_writeback_tables, write_back, write_back_in_background)
from .sync_import import (  # noqa: F401
    ImportResult, duplicate_count, import_all, import_lot_ledger,
    import_master, import_tables)
from .sync_refresh import OrderRefresh, refresh_orders  # noqa: F401
from .sync_auto import (  # noqa: F401
    STAMP_TABLE, auto_import, auto_import_in_background, mark_imported,
    needs_import)

log = get_logger("data_sync")


# ==================================================================
# 取り込み済みかの確認
# ==================================================================
# 「空だと業務にならない」テーブル。1つでも空なら取り込みができていない
ESSENTIAL_TABLES = ("PalletMaster", "BoardMaster", "CornerboardMaster")


def missing_master_tables(conn: sqlite3.Connection) -> list[str]:
    """空のままになっている主要テーブル。"""
    empty: list[str] = []
    for table in ESSENTIAL_TABLES:
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
        except sqlite3.Error:
            empty.append(table)
            continue
        if not count:
            empty.append(table)
    return empty


# 文字化けの跡。UTF-8として読めなかった字がここに置き換わっている
REPLACEMENT = "�"


def mojibake_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """**手元のDBに残っている文字化け**を、表ごとに数える。

    取り込み元をUTF-8として決め打ちで読んでいたころ(〜VER2.19.0)は、
    読めない字が `�` に置き換わったまま手元へ入っていた。読む側は
    直した(`source_db.decode_text`)が、**入ってしまった字は戻らない** ──
    置き換わった時点で元のバイトが失われているため。

    つまり版を上げただけでは画面の文字化けは消えず、**取り込み直すまで
    残る**。放っておくと「直したと言われたのに直っていない」になるので、
    こちらから見つけて言う(設定画面の「いまの状態」)。
    """
    from . import import_specs

    tables = list(import_specs.IMPORT_SPECS) + list(import_specs.LOT_IMPORT_SPECS)
    found: dict[str, int] = {}
    for table in tables:
        try:
            # 型で絞らない(`source_db.sniff_encoding` と同じ理由)。
            # 数値の列に文字の照合を掛けても当たらないだけで害は無い
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info([{table}])")]
        except sqlite3.Error:
            continue
        if not columns:
            continue
        where = " OR ".join(f"[{c}] LIKE ?" for c in columns)
        try:
            count = conn.execute(
                f"SELECT COUNT(*) FROM [{table}] WHERE {where}",
                [f"%{REPLACEMENT}%"] * len(columns)).fetchone()[0]
        except sqlite3.Error:
            continue
        if count:
            found[table] = count
    return found


# ==================================================================
# 診断
# ==================================================================
def describe_environment() -> str:
    """いまの取り込み環境を1画面にまとめる(設定画面や起動時の案内用)。"""
    lines = ["=== データ取り込みの状態 ===",
             f"Python: {sys.version.split()[0]}",
             f"読み取り方式: {sync_sources.backend_name()}"]

    material = sync_sources.find_material_db()
    lines.append(f"梱包資材マスタ: {material or '(起動フォルダに見つかりません)'}")
    lines.append(f"  探した場所: {config.master_db_dir()}")
    if material is not None:
        # 取り込みが空になる原因はたいてい「テーブル名が違う」なので、
        # そのファイルに実際どのテーブルがあるのかを出しておく。
        # 看板マスタ側のテーブル(KANBAN_TABLES)は別ファイルの持ち物
        # なので、ここには数えない(数えると毎回「不足」に見える)
        try:
            names = sync_sources.list_tables(material)
        except Exception:                  # noqa: BLE001 - 案内なので握る
            names = []
        if names:
            expect = [t for t in import_specs.IMPORT_SPECS
                      if t not in import_specs.KANBAN_TABLES]
            missing = [t for t in expect if t not in names]
            lines.append(f"  テーブル: {len(names)}個")
            if missing:
                lines.append(f"  ※取り込み対象なのに無い: {', '.join(missing)}")

    kanban = sync_sources.find_kanban_db()
    lines.append(f"看板マスタ: {kanban or '(見つかりません)'}")
    lines.append(f"  探した場所: {config.kanban_db_dir()}")
    if kanban is not None:
        try:
            names = sync_sources.list_tables(kanban)
        except Exception:                  # noqa: BLE001 - 案内なので握る
            names = []
        if names:
            missing = [t for t in import_specs.KANBAN_TABLES if t not in names]
            lines.append(f"  テーブル: {len(names)}個")
            if missing:
                lines.append(f"  ※取り込み対象なのに無い: {', '.join(missing)}")

    # **3ファイルとも出す。** 閾値マスタを足したとき(VER2.6x)ここに
    # 書き足しそびれていたので、`--check` は「見ている場所」を1つ隠した
    # まま答えていた ── 診断が黙っている場所は、探しに行けない
    threshold = sync_sources.find_threshold_db()
    lines.append(f"パレット閾値マスタ: {threshold or '(見つかりません)'}")
    lines.append(f"  探した場所: {config.threshold_db_dir()}")
    if threshold is None:
        lines.append("  ※手元の基準表の初期値で動きます"
                     "(資材課が直した値は届きません)")
    else:
        try:
            names = sync_sources.list_tables(threshold)
        except Exception:                  # noqa: BLE001 - 案内なので握る
            names = []
        if names:
            missing = [t for t in import_specs.THRESHOLD_TABLES
                       if t not in names]
            lines.append(f"  テーブル: {len(names)}個")
            if missing:
                lines.append(f"  ※取り込み対象なのに無い: {', '.join(missing)}")

    lots = sync_sources.find_lot_dbs()
    lines.append(f"仕掛台帳: {len(lots)}/3 ファイル")
    for table, filename in config.LOT_DB_FILES.items():
        found = lots.get(table)
        lines.append(f"  {filename}: {found or '(見つかりません)'}")
    lines.append(f"  探した場所: {config.lot_db_dir()} → {config.master_db_dir()}")

    lines.append(f"書き戻し先: {material or '(見つかりません)'}")
    return "\n".join(lines)
