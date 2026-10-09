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
from typing import Iterable, Any, Callable, Optional

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

    既定のファイル名(拡張子違いも可)で見つからなければ、そのフォルダの
    sqlite3 のうち、**梱包資材マスタの候補がただ1つのときだけ**それを使う
    (利用者がファイル名を変えていても動くようにするため)。

    【候補が2つ以上なら選ばない】
    以前は、候補のうち名前順で最初のものを**黙って**使っていた。書き込み
    (表を持ってくる・マスタ管理)もそのファイルへ行くので、関係の無い
    ファイルに表を足しかねない(現場の声:「関係ないファイルにテーブル
    追加したりしてたの？」)。どれが梱包資材マスタかは推し量らず、見つから
    ない扱いにして理由を言う(`material_db_missing_why`)。
    """
    directory = Path(directory or config.master_db_dir())
    named = source_db.find(directory, config.MATERIAL_DB_NAME)
    if named is not None:
        return named
    found = material_candidates(directory)
    if len(found) == 1:
        log.info("梱包資材マスタとして %s を使います(既定の名前のファイルが無いため)",
                 found[0].name)
        return found[0]
    if found:
        log.warning("梱包資材マスタを決められません。候補が %s 個あります: %s",
                    len(found), ", ".join(f.name for f in found))
    return None


def material_candidates(directory: Path) -> list[Path]:
    """梱包資材マスタかもしれないファイル。**役目の決まったファイルは外す。**

    仕掛台帳(SIKA で始まる。**名前ではなく頭で外す** ── 上流が SIKALOTNOW →
    SIKALOT と改名したとき古い名前のファイルが残った)、看板マスタ、
    パレット閾値マスタ、置き換え待ちの `.pending_` は梱包資材マスタではない。
    """
    known = {Path(config.KANBAN_DB_NAME).stem, Path(config.THRESHOLD_DB_NAME).stem}
    return [f for f in source_db.list_source_files(directory)
            if not f.stem.upper().startswith("SIKA") and f.stem not in known
            and ".pending_" not in f.name]


def material_db_missing_why(directory: Optional[Path] = None) -> str:
    """梱包資材マスタが見つからないときの言い方。候補が多すぎるならそう言う。"""
    directory = Path(directory or config.master_db_dir())
    found = material_candidates(directory)
    if len(found) > 1:
        return (f"梱包資材マスタを決められません。{directory} に"
                f" {Path(config.MATERIAL_DB_NAME).stem}.sqlite3 が無く、"
                f"sqlite3 が {len(found)} 個あります({'、'.join(f.name for f in found)})。"
                f"使うファイルの名前を {Path(config.MATERIAL_DB_NAME).stem}.sqlite3 に"
                "してください(関係の無いファイルに書かないよう、推し量って選びません)。")
    return (f"梱包資材マスタが見つかりません({directory})。"
            "「取り込み元」で置き場所を確かめてください。")


def where_written(path: Optional[Path]) -> str:
    """書き先のファイルを、**ファイルの名前まで**言う文。

    「取り込んだのに .sqlite3 が変わっていない」(現場の声)は、見ている
    ファイルと書いたファイルが違うと起きる。フォルダだけでは、同じ
    フォルダに並んだどのファイルかが分からない。既定の名前のファイルが
    無くて別のファイルを使っているとき(`find_material_db`)と、同じ
    フォルダにほかの sqlite3 が並んでいるときは、それも言う。
    """
    if path is None:
        return ""
    path = Path(path)
    said = f"書き先のファイル: {path}"
    if path.stem != Path(config.MATERIAL_DB_NAME).stem:
        said += (f"(フォルダに {Path(config.MATERIAL_DB_NAME).stem}.sqlite3 が無いので、"
                 "このファイルを梱包資材マスタとして使っています)")
    # 役目の決まっているほかの取り込み元(仕掛台帳・看板・パレット閾値)は数えない
    others = [f.name for f in material_candidates(path.parent) if f.name != path.name]
    if others:
        said += (f"。同じフォルダのほかのファイル({'、'.join(others)})には書いていません")
    return said


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

    **設定した場所だけを見る**(1つ目 → 2つ目。2つ目は設定していれば)。
    """
    found: dict[str, Path] = {}
    # フォルダを明示されたときはそこだけを見る。省略時は
    # 「1つ目 → 2つ目(設定していれば)」の順に探す。
    # **ファイルごとに**探すので、1つ目に無いファイルだけ2つ目から読む
    candidates = ([Path(directory)] if directory is not None else lot_search_dirs())
    for table, filename in config.LOT_DB_FILES.items():
        for base in candidates:
            path = source_db.find(base, filename)
            if path is not None:
                found[table] = path
                break
    return found

def describe_dir(base: Path, wanted: Iterable[str]) -> str:
    """そのフォルダで何が見えたか(見つからないときに**理由まで**言うため)。

    「見つかりません」だけだと、フォルダに届いていないのか、届いたが名前が違うのか、
    現場からは区別できない(ファイルは置いてあるのに「見つかりません」と出た)。
    """
    import os
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return f"{base}: フォルダがありません"
    except OSError as exc:
        return f"{base}: フォルダを開けません({exc.__class__.__name__}: {exc})"
    files = sorted(n for n in names if Path(n).suffix.lower() in source_db.SUFFIXES)
    hits = [n for n in files
            if Path(n).stem.lower() in {Path(w).stem.lower() for w in wanted}]
    if hits:
        return f"{base}: {', '.join(hits)} が見えています"
    shown = ", ".join(files[:8]) + (" …" if len(files) > 8 else "")
    return f"{base}: 開けましたが該当するファイルがありません(ある取り込み元: {shown or 'なし'})"


def lot_search_dirs() -> list[Path]:
    """仕掛台帳を探すフォルダの順(1つ目 → 2つ目。2つ目は設定していれば)。

    **設定していない場所は見ない。** 以前は予備としてマスタのフォルダも見ていたが、
    画面からは「設定していない道を探している」ようにしか見えなかった(現場の声:
    「仕掛台帳用の2つ目は設定なしなら探さないでしょ?」)。
    """
    return config.lot_db_dirs()


def find_second_lot_dbs() -> dict[str, Path]:
    """2つ目の置き場所にある仕掛台帳のファイル。設定していなければ空。

    1つ目のファイルに**目当てのロットが無い**ときのために、取り込みのあとで
    ここから足りない分を足す(`sync_import.merge_second_lot`)。
    """
    second = config.lot_db_dir2()
    if second is None:
        return {}
    found: dict[str, Path] = {}
    for table, filename in config.LOT_DB_FILES.items():
        path = source_db.find(second, filename)
        if path is not None:
            found[table] = path
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
