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
    _unsent_writeback_tables, retry_unsent_in_background, write_back,
    write_back_in_background)
from .sync_import import (  # noqa: F401
    ImportResult, duplicate_count, import_all, import_lot_ledger,
    import_master, import_tables)
from .sync_refresh import OrderRefresh, refresh_orders  # noqa: F401


# ==================================================================
# 画面の要求から呼ぶ取り込み直し(VER4.8.115)── 待つのは上限まで
# ==================================================================
# 画面を出す前に待つ秒数。共有が遅いときは手元の値で出す(取り込みは背景で続く)
PAGE_WAIT_SEC = 2.0
# 設定画面を出す前に待つ秒数。設定画面で発注の取り込み直しが効くのはボード人気度だけなので短く
SETTINGS_WAIT_SEC = 1.0
# 送る・確かめる・取り消すの前に待つ秒数。ほかの端末が先に動かしたことを知りたいので少し長く
ACTION_WAIT_SEC = 5.0
# 「取り込み直す」を押したときに待つ秒数
BUTTON_WAIT_SEC = 10.0
# 間に合わなかったときの一言(倉庫連携の「取り込み直す」・見張り)
PENDING_MESSAGE = ("取り込み元の応答を待っています(背景で取り込みを続けています。"
                   "届いたら一覧が変わります)。")


def _catch_up_work(force: bool):
    """背景の1本の中身。`refresh_orders` はこのモジュールの名前で呼ぶ ── 試験が
    `data_sync.refresh_orders` を差し替えたら、ここにも効くように。"""
    def work(conn):
        return sys.modules[__name__].refresh_orders(conn, only_if_changed=not force)
    return work


_flights = None


def _flight(force: bool):
    global _flights
    if _flights is None:
        from .bounded import FileFlights
        _flights = {False: FileFlights("発注の取り込み直し", _catch_up_work(False)),
                    True: FileFlights("発注の取り込み直し(押された)", _catch_up_work(True))}
    return _flights[force]


def catch_up(wait: float = PAGE_WAIT_SEC, *, force: bool = False,
             conn: Optional[sqlite3.Connection] = None) -> Optional[OrderRefresh]:
    """発注まわりを取り込み直す。**待つのは `wait` 秒まで**(VER4.8.115。`bounded` の説明)。

    `conn` は画面の要求の接続(そのDBファイルを背景の1本が開き直す。省略すれば手元のDB)。
    間に合えばその結果を返す。間に合わなければ `None`(取り込みは背景で続く。終われば
    `master_state` の世代が進んで、開いている画面が読み直す)。同時に2本は走らせない。

    前の回で待つのをやめたあとに終わった取り込みが「変わった」なら、それも含めて返す
    (倉庫連携の見張りが、背景で入った発注を描き直せるように)。
    """
    outcome = _flight(force).run(conn, wait)
    late = outcome.late or _flight(not force).take_late(conn)
    if outcome.error is not None:
        raise outcome.error
    got = outcome.value if outcome.done else None
    if late is not None and getattr(late, "updated", False):
        if got is None or not got.updated:
            return late
    return got


def pending_refresh() -> OrderRefresh:
    """`catch_up` が間に合わなかったときに画面へ返すもの(見に行けていないとは言わない)。"""
    return OrderRefresh(looked=True, pending=True, reason=PENDING_MESSAGE)


# 共有フォルダに届かず、この端末に預かっただけのときに添える一言
QUEUED_NOTE = ("(共有フォルダに届かないため、相手にはまだ見えていません。"
               "つながると自動で送ります)")


def queued_note() -> str:
    """**共有フォルダが見えないときだけ** `QUEUED_NOTE` を返す(見えていれば空)。

    確認・取消・コメント・受入/払出・切断依頼の状態は、どれも手元に書いてから
    背景で共有へ送る。共有が見えない間に「確認済みにしました」とだけ言うと、
    押した人は相手にも見えていると思う(通し点検: 発注の送信で同じことが
    起きていた)。手元では済んでいるので失敗にはせず、そう言い添える。
    """
    return "" if shared_reachable() else QUEUED_NOTE


# 「共有の取り込み元が見えるか」を、送ったあとの一言のために待つ上限(秒。VER4.8.115)
REACH_WAIT_SEC = 1.0
_reach_flight = None


def shared_reachable(wait: float = REACH_WAIT_SEC) -> bool:
    """共有の取り込み元が見えるか。**待つのは `wait` 秒まで**(VER4.8.115。`bounded` の説明)。

    送った・確かめたあとに「まだ相手に届いていません」を添えるかを決めるためだけに見る。
    共有が応答しないと、ファイルがあるかを確かめるだけで要求が返らなかった(錠を持ったまま)。
    間に合わなければ「見えない」とみなす(送るのは背景が続ける。届いていないと言うほうが正直)。
    """
    global _reach_flight
    from .bounded import Flight
    if _reach_flight is None:
        _reach_flight = Flight("取り込み元が見えるか",
                               lambda: sync_sources.find_material_db() is not None)
    outcome = _reach_flight.run(wait)
    return bool(outcome.done and outcome.value)
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
    # 1つ目 → 2つ目(設定していれば)。前は2つ目に梱包資材マスタのフォルダを出していた(VER4.8.78 で直した)
    lines.append(f"  探した場所: {' → '.join(str(d) for d in sync_sources.lot_search_dirs())}")

    lines.append(f"書き戻し先: {material or '(見つかりません)'}")
    return "\n".join(lines)
