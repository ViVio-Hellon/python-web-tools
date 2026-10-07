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
                  source_key="取込元管理番号",
                  # 取り消した発注を確認済みにしない・確認済みを取り消さない。
                  # 手元の画面でも止めているが、相手の印は取り込むまで
                  # 手元に無いので、送り先でもう一度確かめる
                  mark_blockers=(("確認済み", "取り消し済"),
                                 ("取り消し済", "確認済み")),
                  match_columns=("登録日時", "LotNo", "品名"),
                  number_column="管理番号",
                  optional_columns=("送信端末", "発注キー")),
    WriteBackSpec(sqlite_table=config.TBL_STOCK_HISTORY,
                  access_table=config.TBL_STOCK_HISTORY,
                  key_column="id",
                  number_column="管理番号",
                  on_insert=lambda tx, values: apply_stock(tx, values)),
    # 資材選択の「使用する」。**全端末の合計で人気度を出す**ために集める。
    # 足すだけで、あとから変わる値は無い(印は無い)
    WriteBackSpec(sqlite_table=config.TBL_BOARD_USAGE,
                  access_table=config.TBL_BOARD_USAGE,
                  key_column="管理番号",
                  number_column="管理番号"),
    # 発注ごとの現場⇔倉庫のやり取り。足すだけ(書き直さない)
    WriteBackSpec(sqlite_table=config.TBL_ORDER_COMMENT,
                  access_table=config.TBL_ORDER_COMMENT,
                  key_column="管理番号",
                  number_column="管理番号"),
    # 相手がコメントを見たか(誰が・いつ)。足すだけ
    WriteBackSpec(sqlite_table=config.TBL_ORDER_COMMENT_SEEN,
                  access_table=config.TBL_ORDER_COMMENT_SEEN,
                  key_column="管理番号",
                  number_column="管理番号"),
    # 切断依頼(送った紙面)と、その状態(受け取った・切った・取り消し)。どちらも足すだけ
    WriteBackSpec(sqlite_table=config.TBL_CUT_REQUEST,
                  access_table=config.TBL_CUT_REQUEST,
                  key_column="管理番号",
                  number_column="管理番号"),
    WriteBackSpec(sqlite_table=config.TBL_CUT_REQUEST_EVENT,
                  access_table=config.TBL_CUT_REQUEST_EVENT,
                  key_column="管理番号",
                  number_column="管理番号"),
]

# 共有にまだ無ければ、**最初に送る端末が作る**表。
#
# ボード使用実績は Access 時代には無かった表で、共有の梱包資材マスタには
# 入っていない。作らないと送れず、送れない行が残るあいだはその表の
# 取り込みも止まる(= いつまでも端末ごとの数のまま)。
# 番号は共有側で自動で振る(INTEGER PRIMARY KEY)。送信ID列と一意索引は
# ほかの表と同じく `ensure_op_id_column` が足す。
SHARED_TABLE_DDL: dict[str, str] = {
    config.TBL_BOARD_USAGE: (
        f'CREATE TABLE IF NOT EXISTS "{config.TBL_BOARD_USAGE}" ('
        " 管理番号 INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ボード幅 INTEGER NOT NULL DEFAULT 0, ボード丈 INTEGER NOT NULL DEFAULT 0,"
        " ボードタイプ TEXT NOT NULL DEFAULT '', 枚数 INTEGER NOT NULL DEFAULT 0,"
        " 切断後幅 INTEGER NOT NULL DEFAULT 0, 切断後丈 INTEGER NOT NULL DEFAULT 0,"
        " 製品幅 INTEGER NOT NULL DEFAULT 0, 製品丈 INTEGER NOT NULL DEFAULT 0,"
        " パレット幅 INTEGER NOT NULL DEFAULT 0, パレット丈 INTEGER NOT NULL DEFAULT 0,"
        " ロット番号 TEXT NOT NULL DEFAULT '', 使用日時 TEXT NOT NULL DEFAULT '')"),
    config.TBL_ORDER_COMMENT: (
        f'CREATE TABLE IF NOT EXISTS "{config.TBL_ORDER_COMMENT}" ('
        " 管理番号 INTEGER PRIMARY KEY AUTOINCREMENT,"
        " 発注キー TEXT NOT NULL DEFAULT '', コメントID TEXT NOT NULL DEFAULT '',"
        " 書いた端末 TEXT NOT NULL DEFAULT '', 書いた側 TEXT NOT NULL DEFAULT '',"
        " 本文 TEXT NOT NULL DEFAULT '', 書いた日時 TEXT NOT NULL DEFAULT '')"),
    config.TBL_ORDER_COMMENT_SEEN: (
        f'CREATE TABLE IF NOT EXISTS "{config.TBL_ORDER_COMMENT_SEEN}" ('
        " 管理番号 INTEGER PRIMARY KEY AUTOINCREMENT,"
        " コメントID TEXT NOT NULL DEFAULT '', 見た端末 TEXT NOT NULL DEFAULT '',"
        " 見た側 TEXT NOT NULL DEFAULT '', 見た日時 TEXT NOT NULL DEFAULT '')"),
    config.TBL_CUT_REQUEST: (
        f'CREATE TABLE IF NOT EXISTS "{config.TBL_CUT_REQUEST}" ('
        " 管理番号 INTEGER PRIMARY KEY AUTOINCREMENT,"
        " 依頼ID TEXT NOT NULL DEFAULT '', LotNo TEXT NOT NULL DEFAULT '',"
        " 要約 TEXT NOT NULL DEFAULT '', 送った端末 TEXT NOT NULL DEFAULT '',"
        " 送った日時 TEXT NOT NULL DEFAULT '', 題名 TEXT NOT NULL DEFAULT '',"
        " 紙面 TEXT NOT NULL DEFAULT '', 差し替え元 TEXT NOT NULL DEFAULT '')"),
    config.TBL_CUT_REQUEST_EVENT: (
        f'CREATE TABLE IF NOT EXISTS "{config.TBL_CUT_REQUEST_EVENT}" ('
        " 管理番号 INTEGER PRIMARY KEY AUTOINCREMENT,"
        " 依頼ID TEXT NOT NULL DEFAULT '', 状態 TEXT NOT NULL DEFAULT '',"
        " 端末 TEXT NOT NULL DEFAULT '', 側 TEXT NOT NULL DEFAULT '',"
        " 日時 TEXT NOT NULL DEFAULT '')"),
}


# 共有にあとから足す列。**送るものがある端末が足す**(表と同じ)。
#
# 送信端末 … どの現場が送った発注か。取り消しはその端末だけ
#            (`warehouse_service.cancel_order`)。足せなかった共有へも
#            発注は届ける(`optional_columns`)── 取り消しの絞り込みが
#            効かないだけで、発注が止まるよりよい
SHARED_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    config.TBL_WAREHOUSE_ORDER: (("送信端末", "TEXT"), ("発注キー", "TEXT")),
}


def ensure_shared_tables(conn: sqlite3.Connection, source: Any) -> list[str]:
    """送るものがあるのに共有に無い表を作る。作った表の名前を返す。

    **送るものが無ければ触らない。** 共有は全員のファイルなので、
    用も無いのに書き換えない(更新時刻が変わると全端末が取り込み直す)。
    """
    made: list[str] = []
    try:
        names = set(source.table_names())
    except source_db.SourceError:
        return made
    for spec in WRITEBACK_SPECS:
        ddl = SHARED_TABLE_DDL.get(spec.access_table)
        if ddl is None or spec.access_table in names:
            continue
        try:
            if not outbox_sync.pending_rows(conn, spec):
                continue
            source.execute(ddl)
        except (sqlite3.Error, source_db.SourceError) as exc:
            log.warning("%s を取り込み元に作れませんでした: %s", spec.access_table, exc)
            continue
        log.info("%s を取り込み元に作りました", spec.access_table)
        made.append(spec.access_table)
    for spec in WRITEBACK_SPECS:
        added = SHARED_ADDED_COLUMNS.get(spec.access_table)
        if not added or spec.access_table not in names:
            continue
        try:
            have = {r["name"] for r in source.query(
                f"PRAGMA table_info({source_db.quote_identifier(spec.access_table)})")}
            missing = [(c, t) for c, t in added if c not in have]
            if not missing or not outbox_sync.pending_rows(conn, spec):
                continue
            for column, kind in missing:
                source.execute(
                    f"ALTER TABLE {source_db.quote_identifier(spec.access_table)}"
                    f" ADD COLUMN {source_db.quote_identifier(column)} {kind}")
                log.info("%s に %s 列を足しました", spec.access_table, column)
                made.append(f"{spec.access_table}.{column}")
        except (sqlite3.Error, source_db.SourceError) as exc:
            log.warning("%s に列を足せませんでした: %s", spec.access_table, exc)
    return made


def _is_list_managed(value: Any) -> bool:
    return str(value or "").strip() == "要"


def apply_stock(tx: Any, values: dict[str, Any]) -> None:
    """入出庫履歴を1行送るとき、共有の PalletMaster の在庫数も同じだけ動かす。

    【なぜ要るか】
    受入・払出は手元の PalletMaster を書き換えるが、PalletMaster は
    書き戻しの対象ではなかった。**在庫数は共有へ届かず、ほかの端末からは
    見えず、次の取り込み(総入れ替え)で手元からも消えていた。**
    (実データ: 共有の入出庫履歴にある受入7件の位置が、共有の
     PalletMaster には1行も無い)

    VBA は共有の PalletMaster を直接書き換えていた。ここでは履歴を
    送るのと同じまとまりで**増減を**当てる ── 値で上書きしないので、
    2台が同じ棚に入れても両方の数が足される。手順は手元の
    `pallet_service.receive` / `issue` と同じ:

        受入 … 幅・丈・位置が同じ行があれば足す。無ければ行を作る
        払出 … 引く。0 になり リスト管理 が「要」でなければ行を消す
               (共有に行が無ければ何もしない。マイナスにはしない)

    共有の値は文字列で入っている(Access から変換したため)ので、
    数として比べる。
    """
    kind = str(values.get("区分") or "")
    try:
        width, length = int(values["幅"]), int(values["丈"])
        qty = int(values.get("数量") or 0)
    except (KeyError, TypeError, ValueError):
        return
    if kind not in ("受入", "払出") or qty <= 0:
        return
    position = str(values.get("位置") or "")
    now = values.get("更新日時") or db.now_db_string()
    table = source_db.quote_identifier("PalletMaster")
    found = tx.query(
        f"SELECT rowid AS _rid, 在庫数, リスト管理 FROM {table}"
        " WHERE CAST(幅 AS INTEGER) = ? AND CAST(丈 AS INTEGER) = ?"
        "   AND TRIM(COALESCE(位置, '')) = TRIM(?)"
        " ORDER BY rowid LIMIT 1", (width, length, position))
    row = found[0] if found else None
    stock = int(float(row["在庫数"])) if row and str(row["在庫数"] or "").strip() else 0

    if kind == "受入":
        if row is not None:
            tx.execute(f"UPDATE {table} SET 在庫数 = ?, 更新日時 = ? WHERE rowid = ?",
                       (stock + qty, now, row["_rid"]))
            return
        columns = {r["name"] for r in tx.query(f"PRAGMA table_info({table})")}
        new = {"幅": width, "丈": length, "巾適合min": 0, "巾適合max": 0,
               "丈適合min": 0, "丈適合max": 0,
               "業界": values.get("業界") or "一般", "記号": values.get("記号") or "",
               "位置": position, "在庫数": qty, "リスト管理": "", "桁数": 0,
               "脚数": 0, "コード": "", "単位": "", "備考": values.get("備考") or "",
               "更新日時": now}
        if "管理番号" in columns:
            # 共有の 管理番号 は型の無い列(自動では振られない)。
            # 空のままだと取り込み側で行を見分けられないので、続きを振る
            top = tx.query(f"SELECT MAX(CAST(管理番号 AS INTEGER)) AS n FROM {table}")
            new["管理番号"] = int(top[0]["n"] or 0) + 1
        tx.insert("PalletMaster", {k: v for k, v in new.items() if k in columns})
        return

    if row is None or stock < qty:
        # **足りないのに払い出された。** 現場が複数台あると、同じ棚から
        # ほぼ同時に払い出したとき、どちらの端末も手元では在庫があった
        # ように見える(互いの払出が共有に届く前)。数はマイナスにしないが、
        # 黙って0にすると「1台しか無いのに2台出た」が後から追えないので、
        # 共有の履歴の備考に残す
        note = f"[共有の在庫が足りませんでした: 在庫{stock}・払出{qty}]"
        log.warning("払出が共有の在庫を超えました: %s×%s 位置%s 在庫%s 払出%s",
                    width, length, position, stock, qty)
        if values.get("送信ID"):
            history = source_db.quote_identifier(config.TBL_STOCK_HISTORY)
            tx.execute(f"UPDATE {history} SET 備考 = TRIM(COALESCE(備考, '') || ' ' || ?)"
                       " WHERE 送信ID = ?", (note, values["送信ID"]))
    if row is None:
        return
    left = max(stock - qty, 0)
    if left == 0 and not _is_list_managed(row["リスト管理"]):
        tx.execute(f"DELETE FROM {table} WHERE rowid = ?", (row["_rid"],))
    else:
        tx.execute(f"UPDATE {table} SET 在庫数 = ?, 更新日時 = ? WHERE rowid = ?",
                   (left, now, row["_rid"]))


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
    # 入出庫履歴が送れていないうちは、その在庫数も共有に無い
    # (`apply_stock` は履歴と一緒に当たる)。PalletMaster を入れ替えると
    # 受入・払出した在庫数が消えるので、こちらも待たせる
    if config.TBL_STOCK_HISTORY in remaining:
        remaining[config.TBL_PALLET_MASTER] = remaining[config.TBL_STOCK_HISTORY]
    # 実績(ヘッダ+明細)は専用の送り方(`pattern_sync`)。まだ送れていない
    # 実績・読んだ回数・削除も、総入れ替えで消えると取り返せない
    from . import pattern_sync
    patterns = pattern_sync.pending_total(conn)
    if patterns:
        remaining[config.TBL_PT_HEADER] = patterns
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
        ensure_shared_tables(conn, source)
        result = outbox_sync.write_back(conn, source, WRITEBACK_SPECS)
        _push_patterns(conn, source, result)
    finally:
        source.close()
    return result


def ensure_guards(path: Path) -> list[str]:
    """取り込み元に二重登録の防止(送信ID列と一意インデックス)を用意する。

    返すのは表ごとの結果の1行(取り込み診断と画面のまとめに出す)。

    【なぜ取り込みのたびに見るのか】
    以前は書き戻し(`write_back`)のときにしか作っていなかった。取り込み元は
    Access から**変換し直したファイルに差し替えられ**、そのとき索引は付いて
    こない。送る行が無ければ書き戻しは走らないので、設定画面に「二重登録の
    防止: 効いていません」が出たまま、何をしても消えなかった。
    マスタを取り込むのは、まさにファイルが差し替わったときなので、そこで作る。
    """
    lines: list[str] = []
    try:
        source = source_db.connect(path)
    except source_db.SourceError as exc:
        return [f"二重登録の防止: 取り込み元を開けないので確かめられません({exc})"]
    try:
        names = set(source.table_names())
        for spec in WRITEBACK_SPECS:
            if not spec.use_op_id_guard or spec.access_table not in names:
                continue
            before = outbox_sync.guard_state(source, spec)
            if before.ok:
                continue
            outbox_sync.ensure_op_id_column(source, spec)
            after = outbox_sync.guard_state(source, spec)
            if after.ok:
                lines.append(f"二重登録の防止: {spec.access_table} に送信IDの"
                             "一意インデックスを作りました")
            else:
                lines.append(f"二重登録の防止: {spec.access_table} は用意できません"
                             f"でした ── {after.why()}")
    finally:
        source.close()
    for line in lines:
        log.info("%s", line)
    return lines


def _push_patterns(conn: sqlite3.Connection, source: Any,
                   result: WriteBackResult) -> None:
    """実績(保存・読んだ回数・削除)も同じ取り込み元へ渡す。

    **発注の書き戻しとは別に失敗させる。** 実績が送れなくても、発注は
    送れたと数える(逆も同じ)。
    """
    from . import pattern_sync
    try:
        pushed = pattern_sync.push(conn, source)
    except Exception as exc:                     # noqa: BLE001 - 発注の結果は残す
        log.exception("実績の書き戻しで例外")
        result.errors.append(f"{config.TBL_PT_HEADER}: {exc}")
        return
    if pushed.sent:
        result.sent[config.TBL_PT_HEADER] = pushed.sent
    # 読んだ回数と削除は「確認・取消の印」とは別物なので、印には数えない
    if pushed.usage or pushed.deleted:
        result.sent[f"{config.TBL_PT_HEADER}(使用回数・削除)"] = (
            pushed.usage + pushed.deleted)
    result.errors.extend(f"{config.TBL_PT_HEADER}: {e}" for e in pushed.errors)


# 送れていない分を送り直しに行く間隔の下限(秒)。心拍(15秒ごと)のたびに行くと、
# 共有に届かない端末で同じ失敗を15秒ごとに繰り返すだけになる
RETRY_MIN_SEC = 30
_retry_lock = threading.Lock()
_retry_at = 0.0
_retry_running = False


def retry_unsent_in_background(*, now: Optional[float] = None) -> bool:
    """**送れていない分があれば、送り直しに行く**(心拍から呼ぶ)。行ったら True。

    以前は「次に何か登録したとき」にしか送り直さなかった。共有フォルダが一時的に
    見えない間に発注を1件だけ出すと、その端末で次に何かを登録するまで**倉庫に
    届かないまま**になっていた(通し点検: 共有を戻して90秒たっても届かず、
    別の発注を送った時についでに届いた)。画面には「送信しました」と出ているので、
    現場は届いたと思っている。

    重いことはしない: 間隔(`RETRY_MIN_SEC`)を空け、同時に2つは走らせず、
    送れていない分が無ければ共有には触らない。
    """
    import time as _time
    global _retry_at, _retry_running
    moment = _time.monotonic() if now is None else now
    with _retry_lock:
        if _retry_running or moment - _retry_at < RETRY_MIN_SEC:
            return False
        _retry_at = moment
        _retry_running = True

    def runner() -> None:
        global _retry_running
        try:
            with db.connect() as conn:
                waiting = _unsent_writeback_tables(conn)
                if not waiting:
                    return
                log.info("送れていない分を送り直します: %s", waiting)
                result = write_back(conn)
            if result.total:
                log.info("送れていなかった分を送りました: %s件", result.total)
            if result.skipped_reason:
                log.info("まだ送れません: %s", result.skipped_reason)
            for message in result.errors:
                log.warning("取り込み元へ反映できず: %s", message)
        except Exception:                       # noqa: BLE001 - 背景処理なので握る
            log.exception("送れていない分の送り直しで例外(手元の登録は残っています)")
        finally:
            with _retry_lock:
                _retry_running = False

    threading.Thread(target=runner, daemon=True, name="writeback-retry").start()
    return True


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
