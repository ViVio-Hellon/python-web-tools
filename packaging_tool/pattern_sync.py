"""実績(スナップショット)を全端末で共有する ── 取り込み元との行き来

VBA は共有の Access(梱包資材マスタ)へ直接書くので、保存すればすぐ全端末
から見えます。Python版は手元の SQLite で動くので、ここで取り込み元
(梱包資材マスタ.sqlite3)と行き来させます。

【なぜ汎用の書き戻し(`outbox_sync`)に乗せないのか】
実績は**ヘッダ1行と明細3種を実績IDでつないだ1組**です。取り込み元で
付く実績IDは手元の番号と違うので、送るときに明細の実績IDを振り直す
必要があり、しかも**1組まとめて**入らないといけません(ヘッダだけ届く・
明細が半分だけ、を作らない)。1表ずつ送る汎用の仕組みでは守れません。

【送る(`push`)】
    保存した実績   … 取り込み元で1組まとめて足し、付いた番号を
                     手元の `取込元実績ID` に控える。**送信IDで二重を防ぐ**
                     (送れたが控える前に落ちた → 次は送信IDで見つけて控えるだけ)
    読んだ回数     … `使用回数未反映` の分だけ取り込み元の使用回数に足す
                     (上書きしない。別の端末で同時に読んでも数が消えない)
    消した実績     … `実績削除待ち` のぶん、取り込み元からも消す
                     (消さないと次の取り込みで戻ってくる)

【受け取る(`import_from`)】取り込みは総入れ替えです。**送れていないものが
1つでも残っていれば入れ替えない**(発注と同じ関門)── 入れ替えると、
まだ届いていない実績・読んだ回数・削除が消えてしまいます。

取り込み元に実績の表が無ければ、最初に送るときに作ります(表の名前は
`config.TBL_PT_*`)。表が無くなっていた(取り込み元を作り直した等)ときは、
手元の実績を「未送信」に戻して送り直します(送信IDで二重にならない)。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config, db, source_db
from . import pattern_store as store
from .logging_utils import get_logger

log = get_logger("pattern_sync")

# 取り込み元へ送る(取り込み元にもある)ヘッダの列
SOURCE_HEADER_NAMES: tuple[str, ...] = tuple(n for n, _ in store.HEADER_COLUMNS)


def _q(name: str) -> str:
    return source_db.quote_identifier(name)


# ------------------------------------------------------------------
# まだ送れていないもの
# ------------------------------------------------------------------
def pending(conn: sqlite3.Connection) -> dict[str, int]:
    """まだ取り込み元へ渡せていないもの。無ければ空。"""
    h = _q(config.TBL_PT_HEADER)
    out: dict[str, int] = {}
    try:
        unsent = conn.execute(
            f"SELECT COUNT(*) FROM {h} WHERE 取込元実績ID IS NULL").fetchone()[0]
        usage = conn.execute(
            f"SELECT COUNT(*) FROM {h} WHERE 取込元実績ID IS NOT NULL "
            "AND COALESCE(使用回数未反映, 0) > 0").fetchone()[0]
        deleted = conn.execute(
            f"SELECT COUNT(*) FROM {_q(store.TBL_PT_DELETED)}").fetchone()[0]
    except sqlite3.Error:
        return out                          # 表がまだ無い(古いDB)
    if unsent:
        out["未送信の実績"] = unsent
    if usage:
        out["未反映の使用回数"] = usage
    if deleted:
        out["未反映の削除"] = deleted
    return out


def pending_total(conn: sqlite3.Connection) -> int:
    return sum(pending(conn).values())


# ------------------------------------------------------------------
# 送る
# ------------------------------------------------------------------
@dataclass
class PushResult:
    sent: int = 0          # 足した実績
    usage: int = 0         # 使用回数を足した実績
    deleted: int = 0       # 消した実績
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.sent + self.usage + self.deleted


def _source_columns(tx: source_db.SourceTransaction, table: str) -> set[str]:
    return {r["name"] for r in tx.query(f"PRAGMA table_info({_q(table)})")}


def ensure_source_tables(source: source_db.SourceConnection) -> None:
    """取り込み元に実績の表を作る(あれば何もしない)。

    VBA が作った表に `送信ID` が無ければ足す(送り直しの二重防止に要る)。
    """
    with source.transaction() as tx:
        existing = {r["name"] for r in tx.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if config.TBL_PT_HEADER in existing:
            if "送信ID" not in _source_columns(tx, config.TBL_PT_HEADER):
                tx.execute(f"ALTER TABLE {_q(config.TBL_PT_HEADER)} ADD COLUMN 送信ID TEXT")
        sqls = store.create_sql(config.TBL_PT_HEADER, store.HEADER_COLUMNS,
                                header=True)
        for table, columns, _order in store.DETAIL_TABLES:
            sqls += store.create_sql(table, columns, header=False)
        for sql in sqls:
            tx.execute(sql)


def _push_new(conn: sqlite3.Connection, source: source_db.SourceConnection,
              result: PushResult) -> None:
    h = _q(config.TBL_PT_HEADER)
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM {h} WHERE 取込元実績ID IS NULL ORDER BY 実績ID")]
    for header in rows:
        local_id = header["実績ID"]
        send_id = header.get("送信ID")
        details = store._details(conn, local_id)
        try:
            with source.transaction() as tx:
                found = tx.query(f"SELECT 実績ID FROM {h} WHERE 送信ID = ?",
                                 (send_id,)) if send_id else []
                if found:
                    # 送れていたが、控える前に落ちた。控えるだけ
                    new_id = int(found[0]["実績ID"])
                else:
                    # **番号はこちらで振る。** VBA(Access)から作られた取り込み元の
                    # 表は 実績ID がただの列(自動採番ではない)なので、入れないと
                    # 空のまま入る。鍵(BEGIN IMMEDIATE)を取ってから数えるので、
                    # 別の端末と同じ番号にはならない
                    new_id = int(tx.query(
                        f"SELECT COALESCE(MAX(実績ID), 0) + 1 AS n FROM {h}")[0]["n"])
                    tx.insert(config.TBL_PT_HEADER, {
                        "実績ID": new_id,
                        **{n: header.get(n) for n in SOURCE_HEADER_NAMES}})
                    for (table, columns, _order), rows_ in zip(store.DETAIL_TABLES, details):
                        names = [n for n, _ in columns]
                        for row in rows_:
                            tx.insert(table, {"実績ID": new_id,
                                              **{n: row.get(n) for n in names}})
        except source_db.SourceError as exc:
            result.errors.append(f"実績ID {local_id}: {exc}")
            log.warning("実績を送れませんでした: 実績ID=%s %s", local_id, exc)
            continue
        with conn:
            conn.execute(f"UPDATE {h} SET 取込元実績ID = ? WHERE 実績ID = ?",
                         (new_id, local_id))
        result.sent += 1
        log.info("実績を取り込み元へ送りました: 手元%s → 取り込み元%s", local_id, new_id)


def _push_usage(conn: sqlite3.Connection, source: source_db.SourceConnection,
                result: PushResult) -> None:
    h = _q(config.TBL_PT_HEADER)
    rows = conn.execute(
        f"SELECT 実績ID, 取込元実績ID, 使用回数未反映 FROM {h} "
        "WHERE 取込元実績ID IS NOT NULL AND COALESCE(使用回数未反映, 0) > 0").fetchall()
    for local_id, source_id, add in rows:
        # **送る前に、手元から引いて自分の分にする。** 同じ端末で書き戻しが
        # 2本同時に走ると(操作のあとの裏の送信と、倉庫連携の見張りなど)、
        # 以前はどちらも同じ「未反映 1」を読んで足し、共有の使用回数が
        # 走った本数ぶん増えていた(遅い共有を真似た試験で +1 のはずが +4)。
        # 引けた1本だけが送る。送った分だけ引くので、送っているあいだに
        # 増えた分は残る
        with conn:
            took = conn.execute(
                f"UPDATE {h} SET 使用回数未反映 = 使用回数未反映 - ? "
                "WHERE 実績ID = ? AND 使用回数未反映 >= ?",
                (add, local_id, add)).rowcount
        if not took:
            continue                       # ほかの書き戻しが持っていった
        try:
            source.execute(
                f"UPDATE {h} SET 使用回数 = COALESCE(使用回数, 0) + ?, "
                "更新日時 = ? WHERE 実績ID = ?",
                (add, db.now_db_string(), source_id))
        except source_db.SourceError as exc:
            # 届かなかった分は戻す。次の書き戻しがまた送る
            with conn:
                conn.execute(f"UPDATE {h} SET 使用回数未反映 = 使用回数未反映 + ? "
                             "WHERE 実績ID = ?", (add, local_id))
            result.errors.append(f"実績ID {local_id} の使用回数: {exc}")
            continue
        result.usage += 1


def _push_deletes(conn: sqlite3.Connection, source: source_db.SourceConnection,
                  result: PushResult) -> None:
    h = _q(config.TBL_PT_HEADER)
    rows = conn.execute(
        f"SELECT rowid, 取込元実績ID, 送信ID FROM {_q(store.TBL_PT_DELETED)}").fetchall()
    for rowid, source_id, send_id in rows:
        try:
            with source.transaction() as tx:
                ids = [source_id] if source_id is not None else []
                if send_id:
                    ids += [r["実績ID"] for r in tx.query(
                        f"SELECT 実績ID FROM {h} WHERE 送信ID = ?", (send_id,))]
                for pid in set(ids):
                    for table, _cols, _order in store.DETAIL_TABLES:
                        tx.execute(f"DELETE FROM {_q(table)} WHERE 実績ID = ?", (pid,))
                    tx.execute(f"DELETE FROM {h} WHERE 実績ID = ?", (pid,))
        except source_db.SourceError as exc:
            result.errors.append(f"実績の削除: {exc}")
            continue
        with conn:
            conn.execute(f"DELETE FROM {_q(store.TBL_PT_DELETED)} WHERE rowid = ?",
                         (rowid,))
        result.deleted += 1


def push(conn: sqlite3.Connection,
         source: source_db.SourceConnection) -> PushResult:
    """手元の実績(保存・読んだ回数・削除)を取り込み元へ渡す。

    **1件の失敗で全体を止めない**(送れた分は送れたと控える)。
    送るものが無ければ取り込み元に触らない(表も作らない)。
    """
    result = PushResult()
    if not pending(conn):
        return result
    try:
        ensure_source_tables(source)
    except source_db.SourceError as exc:
        result.errors.append(f"実績の表を用意できませんでした: {exc}")
        return result
    _push_deletes(conn, source, result)   # 先に消す(消したものに回数を足さない)
    _push_new(conn, source, result)
    _push_usage(conn, source, result)
    if result.total:
        log.info("実績を取り込み元へ反映: 追加%s 使用回数%s 削除%s",
                 result.sent, result.usage, result.deleted)
    return result


# ------------------------------------------------------------------
# 受け取る
# ------------------------------------------------------------------
@dataclass
class ImportOutcome:
    imported: int = 0              # 取り込んだ実績(ヘッダ)の数
    skipped_reason: str = ""       # 入れ替えなかった理由
    missing: bool = False          # 取り込み元に実績の表が無かった
    error: str = ""


def import_from(conn: sqlite3.Connection, path: Path) -> ImportOutcome:
    """取り込み元の実績を手元へ総入れ替えで取り込む。

    **送れていないものが残っていれば入れ替えない。** 取り込み元に表が
    無ければ、手元の実績を消さずに「未送信」へ戻す(次の書き戻しで送り直す)。
    """
    outcome = ImportOutcome()
    left = pending(conn)
    if left:
        outcome.skipped_reason = (
            "まだ取り込み元へ送れていない実績があります("
            + "・".join(f"{k}{v}件" for k, v in left.items()) + ")")
        return outcome

    try:
        tables = set(source_db.list_tables(path))
    except source_db.SourceError as exc:
        outcome.error = str(exc)
        return outcome
    if config.TBL_PT_HEADER not in tables:
        outcome.missing = True
        with conn:
            n = conn.execute(
                f"UPDATE {_q(config.TBL_PT_HEADER)} SET 取込元実績ID = NULL "
                "WHERE 取込元実績ID IS NOT NULL").rowcount
        if n:
            log.warning("取り込み元に実績の表がありません。手元の%s件を送り直します", n)
        return outcome

    try:
        headers = source_db.read_table(path, config.TBL_PT_HEADER)
        details = [source_db.read_table(path, table) if table in tables else []
                   for table, _cols, _order in store.DETAIL_TABLES]
    except source_db.SourceError as exc:
        outcome.error = str(exc)
        return outcome

    names = list(SOURCE_HEADER_NAMES)
    try:
        with conn:
            for table, _cols, _order in store.DETAIL_TABLES:
                conn.execute(f"DELETE FROM {_q(table)}")
            conn.execute(f"DELETE FROM {_q(config.TBL_PT_HEADER)}")
            for row in headers:
                pid = store.pt_long(row.get("実績ID"))
                if pid <= 0:
                    continue
                cols = ["実績ID", *names, "取込元実績ID", "使用回数未反映"]
                values = [pid, *[row.get(n) for n in names], pid, 0]
                conn.execute(
                    f"INSERT INTO {_q(config.TBL_PT_HEADER)} "
                    f"({', '.join(_q(c) for c in cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})", values)
                outcome.imported += 1
            for (table, columns, _order), rows in zip(store.DETAIL_TABLES, details):
                cols = ["実績ID", *[n for n, _ in columns]]
                for row in rows:
                    conn.execute(
                        f"INSERT INTO {_q(table)} ({', '.join(_q(c) for c in cols)}) "
                        f"VALUES ({', '.join('?' for _ in cols)})",
                        [row.get(c) for c in cols])
    except sqlite3.Error as exc:
        outcome.error = str(exc)
        outcome.imported = 0
        log.exception("実績の取り込みに失敗しました(手元は元のまま)")
        return outcome
    log.info("実績を取り込みました: %s件", outcome.imported)
    return outcome


def push_to(conn: sqlite3.Connection, path: Optional[Path]) -> PushResult:
    """取り込み元の場所を受け取って送る(開けなければ理由を返す)。"""
    result = PushResult()
    if path is None:
        result.errors.append("取り込み元が見つかりません。")
        return result
    try:
        source = source_db.connect(path)
    except source_db.SourceError as exc:
        result.errors.append(f"取り込み元を開けませんでした: {exc}")
        return result
    try:
        return push(conn, source)
    finally:
        source.close()


__all__: list[Any] = ["pending", "pending_total", "push", "push_to",
                      "import_from", "ensure_source_tables"]
