"""取り込み元を探す・読む ── どこにある、いつのものか

共有フォルダの sqlite3(梱包資材マスタ・かんばん・基準表・仕掛台帳)を
**探すところまで**を持つ。読んだ中身をどう手元へ入れるかは
`sync_import`、書き戻しは `sync_writeback`。

取り込み元の**姿**(大きさと更新時刻)もここで覚える。開きも読みもせず
「変わったか」だけを知りたい場面(発注の見張り)があり、共有への往復を
軽くするため。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, db, import_specs, outbox_sync, source_db
from .logging_utils import get_logger
from .outbox_sync import WriteBackResult, WriteBackSpec

log = get_logger("data_sync.sources")

# 取り込み元の1テーブルを読んだ結果
# 取り込み元の1行。値は sqlite3 が返す型のまま(数値は数値)で、
# 文字列化は `import_specs` の変換関数が引き受ける
Rows = list[dict[str, object]]

class SyncError(RuntimeError):
    """同期の失敗。利用者に見せる日本語のメッセージを持つ。"""


# 進捗の通知先。VBA `frmProgress.UpdateProgress(pct, msg)` と同じ形
#   progress(何%か, いま何をしているか, その段は問題なく進んでいるか)
# 3つ目は省略可(既定True)。**文言を変えずに**「いまの段で問題が
# 起きた」とだけ伝えたいとき(1テーブルの読み込み失敗など)に使う ──
# 文言を変えると新しい段が始まった扱いになり、失敗した段が
# 別の段にすり替わってしまう(`packaging_tool/jobs.py` の `_progress`)。
Progress = Callable[..., None]


def _noop_progress(_pct: int, _msg: str, ok: bool = True) -> None:
    """進捗の受け取り手がいないときの既定。"""


# ==================================================================
# 取り込み元を読む
# ==================================================================
def backend_name() -> str:
    """読み取り方式の名前(画面や診断に出す)。

    以前は環境によって ODBC / mdbtools を切り替えていた。取り込み元が
    sqlite3 になったので**分岐そのものが無い** ── どの端末でも同じ。
    """
    return "sqlite3"


def read_table(path: Path, table: str) -> Rows:
    """取り込み元の1テーブルを読む。

    読めなかったときは `SyncError` に包み直す。**1テーブル欠けただけで
    取り込み全体を落とさない**ため ── 呼び出し側はテーブルごとに拾って、
    残りを続ける(欠けたことは結果の `errors` に出る)。
    """
    try:
        return source_db.read_table(path, table)
    except source_db.SourceError as exc:
        raise SyncError(str(exc)) from exc


def list_tables(path: Path) -> list[str]:
    """取り込み元のテーブル一覧。"""
    return source_db.list_tables(path)


# ==================================================================
# ファイルの探索
# ==================================================================
def find_material_db(directory: Optional[Path] = None) -> Optional[Path]:
    """梱包資材マスタの sqlite3 をフォルダから探す。

    既定のファイル名(拡張子違いも可)で見つからなければ、そのフォルダに
    ある sqlite3 のうち仕掛台帳(SIKA*)以外で最初のものを使う。
    利用者がファイル名を変えていても動くようにするため。
    """
    directory = Path(directory or config.master_db_dir())
    named = source_db.find(directory, config.MATERIAL_DB_NAME)
    if named is not None:
        return named
    # **名前ではなく頭で外す。** 以前はこの3ファイルの名前とぴったり
    # 一致するものだけを外していたが、上流がファイル名を変えたときに
    # (SIKALOTNOW → SIKALOT)、**古い名前で残っているファイルが
    # 梱包資材マスタとして拾われる**。仕掛台帳はどれも SIKA で始まるので、
    # 上の説明どおり頭で見る
    for path in source_db.list_source_files(directory):
        if path.stem.upper().startswith("SIKA"):
            continue
        log.info("梱包資材マスタとして %s を使います", path.name)
        return path
    return None


def find_kanban_db(directory: Optional[Path] = None) -> Optional[Path]:
    """看板マスタの sqlite3 をフォルダから探す。

    既定の置き場所は梱包資材マスタと同じ共有フォルダにしてあるため、
    `find_material_db` と同じ「名前が合わなければフォルダ内の他の
    sqlite3 を使う」自動判別はしない ── 同じフォルダに梱包資材マスタ
    自身が並んでいることがあり、緩い一致だとそちらを誤って拾いかねない。
    名前(拡張子違いは可)が一致したときだけ返す。
    """
    directory = Path(directory or config.kanban_db_dir())
    return source_db.find(directory, config.KANBAN_DB_NAME)


def find_threshold_db(directory: Optional[Path] = None) -> Optional[Path]:
    """パレット閾値マスタの sqlite3 をフォルダから探す。

    看板マスタと同じく、**名前が一致したときだけ**返す ── 梱包資材
    マスタと同じフォルダに置かれることがあり、緩い一致だとそちらを
    誤って拾いかねない。
    """
    directory = Path(directory or config.threshold_db_dir())
    return source_db.find(directory, config.THRESHOLD_DB_NAME)


def find_lot_dbs(directory: Optional[Path] = None) -> dict[str, Path]:
    """仕掛台帳の3ファイルを探す。見つかったものだけ返す。

    共有フォルダに届かない端末もあるので、起動フォルダに置かれた
    コピーも探す(共有 → 起動フォルダ の順)。
    """
    found: dict[str, Path] = {}
    # フォルダを明示されたときはそこだけを見る。省略時だけ
    # 「共有 → 起動フォルダ」の順に探す
    candidates = ([Path(directory)] if directory is not None
                  else [config.lot_db_dir(), config.master_db_dir()])
    for table, filename in config.LOT_DB_FILES.items():
        for base in candidates:
            path = source_db.find(base, filename)
            if path is not None:
                found[table] = path
                break
    return found

# 最後に取り込んだときの取り込み元の姿(大きさと更新時刻)。**プロセスに
# 1つ。** モードごとに別プロセスなので、端末の中で混ざることはない
_stamp_lock = threading.Lock()
_last_stamp: dict[str, tuple[int, int]] = {}


def source_stamp(path: Path) -> Optional[tuple[int, int]]:
    """取り込み元の姿。共有の上にあるので、**開かずに分かるものだけ**見る。"""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def note_source_read(path: Path) -> None:
    """いまの姿で取り込んだ、と覚える。"""
    stamp = source_stamp(path)
    if stamp is None:
        return
    with _stamp_lock:
        _last_stamp[str(path)] = stamp


def source_changed(path: Path) -> bool:
    """前に取り込んだときから変わったか。

    **一度も取り込んでいなければ「変わった」**と答える。分からないのに
    「変わっていない」と答えると、届いた発注を見落とす方向に倒れる。
    """
    stamp = source_stamp(path)
    if stamp is None:
        return False                              # 届いていない。見に行けない
    with _stamp_lock:
        return _last_stamp.get(str(path)) != stamp
