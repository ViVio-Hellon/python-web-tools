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
手元の実績を「未送信」に戻して送り直します(送信IDで二重にならない。送信ID の無い
実績は、送る前に送信ID を付け、共有に同じ中身の実績があればそれとつなぐ。VER4.8.119)。
**共有を読めなかったときは送り直さない**(読めないことを「表が無い」にしない)。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import config, db, pattern_copies, source_db, trouble
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
    except sqlite3.Error as exc:
        if "no such table" in str(exc).lower():
            return out                      # 表がまだ無い(古いDB)
        # **数えられないのに「無い」と言わない。** 言うと総入れ替えの取り込みが走り、
        # 送れていない実績を消す(`outbox_sync._not_unknown` と同じ考え)
        log.warning("送れていない実績を数えられませんでした(取り込みは見送ります): %s", exc)
        return {"数えられなかった実績": 1}
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
        if not send_id:
            # **送信ID の無い実績(送信ID ができる前の実績・取り込んだ実績)は、付けてから送る。**
            # 送ったあと控える前に落ちても、次は送信ID で見つけて二重に足さない(VER4.8.119)
            send_id = uuid.uuid4().hex
            with conn:
                conn.execute(f"UPDATE {h} SET 送信ID = ? WHERE 実績ID = ?", (send_id, local_id))
            header["送信ID"] = send_id
        details = store._details(conn, local_id)
        sign = pattern_copies.detail_signatures(conn, [local_id]).get(local_id, "")
        try:
            with source.transaction() as tx:
                found = tx.query(f"SELECT 実績ID FROM {h} WHERE 送信ID = ?",
                                 (send_id,)) if send_id else []
                same = (None if found else
                        pattern_copies.same_in(tx._conn, header, sign))
                if found:
                    # 送れていたが、控える前に落ちた。控えるだけ
                    new_id = int(found[0]["実績ID"])
                elif same is not None:
                    # **共有に同じ中身の実績がもうある**(登録日時・ヘッダ・明細が同じ)。足さずに
                    # それとつなぐ。送信ID の無い頃に送った実績を送り直したとき(VER4.8.119)。
                    # 送り直しが起きたしるしなので、画面の記録にも出す(VER4.8.120)
                    new_id = same
                    trouble.once(log, f"実績の二重|{source.path}", trouble.KIND_STOPPED_DUPLICATE,
                                 "同じ実績が共有にあるので足しませんでした(送り直しを止めた): "
                                 "手元%s → 取り込み元%s(%s)", local_id, new_id,
                                 pattern_copies.label(header))
                else:
                    # **番号はこちらで振る。** VBA(Access)から作られた取り込み元の
                    # 表は 実績ID がただの列(自動採番ではない)なので、入れないと
                    # 空のまま入る。鍵(BEGIN IMMEDIATE)を取ってから数えるので、
                    # 別の端末と同じ番号にはならない
                    new_id = int(tx.query(
                        f"SELECT COALESCE(MAX(実績ID), 0) + 1 AS n FROM {h}")[0]["n"])
                    try:
                        tx.insert(config.TBL_PT_HEADER, {
                            "実績ID": new_id,
                            **{n: header.get(n) for n in SOURCE_HEADER_NAMES}})
                    except sqlite3.IntegrityError:
                        # 共有の守り(同じ実績は1件だけ。VER4.8.120)が断った。登録日時・ヘッダが同じで
                        # 明細だけ違う実績が共有にある。足さずにそれとつなぐ(送り直しを止めた)
                        twin = pattern_copies.same_header_in(tx._conn, header)
                        if twin is None:
                            raise
                        trouble.once(log, f"実績の二重|{source.path}", trouble.KIND_STOPPED_DUPLICATE,
                                     "同じ登録日時・中身の実績が共有にあるので足しませんでした"
                                     "(明細だけ違う): 手元%s → 取り込み元%s(%s)", local_id, twin,
                                     pattern_copies.label(header))
                        new_id = twin
                        details = [[] for _ in store.DETAIL_TABLES]
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
        trouble.failed(log, f"実績の送信|{source.path}", trouble.KIND_UNSENDABLE,
                       f"共有({Path(source.path).name})への実績の送信", exc)
        return result
    # 共有の側の守り(同じ実績は1件だけ)。付けられなくても送ることは止めない(VER4.8.120)
    pattern_copies.ensure_guard(source)
    _push_deletes(conn, source, result)   # 先に消す(消したものに回数を足さない)
    _push_new(conn, source, result)
    _push_usage(conn, source, result)
    if result.total:
        log.info("実績を取り込み元へ反映: 追加%s 使用回数%s 削除%s",
                 result.sent, result.usage, result.deleted)
    # しくじりが続いたら画面の記録に出す(VER4.8.120。`trouble`)
    key = f"実績の送信|{source.path}"
    if result.errors:
        trouble.failed(log, key, trouble.KIND_UNSENDABLE,
                       f"共有({Path(source.path).name})への実績の送信", " / ".join(result.errors[:3]))
    else:
        trouble.ok(log, key)
    return result


# ------------------------------------------------------------------
# 受け取る
# ------------------------------------------------------------------
@dataclass
class ImportOutcome:
    imported: int = 0              # 取り込んだ(足した・中身ごと入れ直した)実績(ヘッダ)の数
    skipped_reason: str = ""       # 入れ替えなかった理由
    missing: bool = False          # 取り込み元に実績の表が無かった
    error: str = ""
    updated: int = 0               # 使用回数・更新日時だけ直した実績
    removed: int = 0               # 取り込み元から消えたので消した実績
    unchanged: int = 0             # 変わっていなかった実績(書かない)

    @property
    def in_sync(self) -> int:
        """取り込み元と同じになった実績の数(取り込みの結果に出す数。ほかの表の「取り込んだ件数」と同じ意味)。"""
        return self.imported + self.updated + self.unchanged


# ヘッダのこの列だけが違うなら、ヘッダだけ直す(明細は読まない)。読んだ回数は全端末で毎回増える。
# 更新日時が変わったら明細ごと入れ直す(保存し直したのかもしれない。1件ぶんの明細なので軽い)
HEADER_ONLY_COLUMNS = frozenset({"使用回数"})
# 明細を `WHERE 実績ID IN (...)` で引く1回の数。足す実績が全体の半分を超えたら表ごと読む
DETAIL_CHUNK = 400


def _same(now: Any, was: Any) -> bool:
    """取り込み元と手元の値が同じか。共有の列は型が無く数が文字のことがある('1030' と 1030 は同じ)。"""
    if now is None or was is None:
        return (now is None or now == "") and (was is None or was == "")
    if now == was:
        return True
    try:
        return float(now) == float(was)
    except (TypeError, ValueError):
        return str(now) == str(was)


def import_from(conn: sqlite3.Connection, path: Path) -> ImportOutcome:
    """取り込み元の実績を手元へ取り込む(中身は `_import_from`)。

    **しくじりが続いたら画面の記録に出す**(VER4.8.120。`trouble`)。1回きりで次に読めたものは
    その日のログにだけ書く。続いていたものが直ったら「直った」も出す。
    """
    outcome = _import_from(conn, path)
    key = f"実績の取り込み|{path}"
    if outcome.error:
        trouble.failed(log, key, trouble.KIND_UNREADABLE,
                       f"共有({path.name})からの実績の取り込み", outcome.error)
    elif not outcome.skipped_reason:
        trouble.ok(log, key)
    return outcome


def _import_from(conn: sqlite3.Connection, path: Path) -> ImportOutcome:
    """取り込み元の実績を手元へ取り込む。**変わった実績だけ書く**(VER4.8.118)。

    前は毎回**総入れ替え**だった。共有の梱包資材マスタは、どの端末が発注・受払・コメントを
    送っても変わるので、そのたびに全端末が実績をまるごと入れ直していた。現場の写しは実績
    37,161件・明細 427,424行で、1回に手元でも7秒(こちらの速い機械で。ラインの端末と共有ではもっとかかる)、そのあいだ手元のDBを
    書きっぱなしにしていた(資材選択は画面を作るたびにこれを待っていた。現場の声
    「検索一つに15分とかかかる事がある」)。

    いまは取り込み元のヘッダを読んで手元と比べ、
        足された実績       … ヘッダと明細を足す(明細はその実績の分だけ読む)
        中身が変わった実績 … ヘッダと明細を入れ直す
        読んだ回数だけ違う … ヘッダの その列だけ直す
        消えた実績         … 手元からも消す
    変わっていなければ**何も書かない**。

    **送れていないものが残っていれば入れ替えない。** 取り込み元に表が無ければ、手元の実績を
    消さずに「未送信」へ戻す(次の書き戻しで送り直す)。
    """
    outcome = ImportOutcome()
    left = pending(conn)
    if left:
        outcome.skipped_reason = (
            "まだ取り込み元へ送れていない実績があります("
            + "・".join(f"{k}{v}件" for k, v in left.items()) + ")")
        return outcome

    # **読めないことを「表が無い」にしない**(VER4.8.119)。前は表の一覧が読めないと空が返り、
    # 「実績の表が無い」と見て手元の実績を送り直していた ── 共有を読めない回のたびに、
    # 送信ID の無い実績が1件ずつ増えた(現場の共有で 2件が 37,154件に。`pattern_copies`)
    try:
        tables = set(source_db.list_tables(path, strict=True))
    except source_db.SourceError as exc:
        outcome.error = str(exc)
        log.warning("実績を取り込めませんでした(共有を読めない。手元の実績は送り直しません): %s", exc)
        return outcome
    if not tables:
        # 表が1つも無いのは、作っている途中か別物。梱包資材マスタとは見ない
        outcome.error = f"{path.name} に表が1つもありません(作っている途中か、別のファイル)"
        log.warning("実績を取り込めませんでした: %s。手元の実績は送り直しません", outcome.error)
        return outcome
    if config.TBL_PT_HEADER not in tables:
        outcome.missing = True
        with conn:
            n = conn.execute(
                f"UPDATE {_q(config.TBL_PT_HEADER)} SET 取込元実績ID = NULL "
                "WHERE 取込元実績ID IS NOT NULL").rowcount
        if n:
            trouble.once(log, f"実績の送り直し|{path}", trouble.KIND_RESENT,
                         "取り込み元に実績の表がありません。手元の%s件を送り直します(%s)", n, path)
        return outcome

    try:
        headers = source_db.read_table(path, config.TBL_PT_HEADER)
    except source_db.SourceError as exc:
        outcome.error = str(exc)
        return outcome
    names = list(SOURCE_HEADER_NAMES)
    source: dict[int, dict[str, Any]] = {}
    for row in headers:
        pid = store.pt_long(row.get("実績ID"))
        if pid > 0:
            source[pid] = row
    h = _q(config.TBL_PT_HEADER)
    local = {int(r["取込元実績ID"]): dict(r) for r in conn.execute(
        f"SELECT 実績ID, 取込元実績ID, {', '.join(_q(n) for n in names)} FROM {h}"
        " WHERE 取込元実績ID IS NOT NULL")}

    added = [pid for pid in source if pid not in local]
    gone = [pid for pid in local if pid not in source]
    renew: list[int] = []
    header_only: dict[int, list[str]] = {}
    for pid in source.keys() & local.keys():
        diff = [n for n in names if not _same(source[pid].get(n), local[pid].get(n))]
        if not diff:
            outcome.unchanged += 1
        elif set(diff) <= HEADER_ONLY_COLUMNS:
            header_only[pid] = diff
        else:
            renew.append(pid)
    if not (added or gone or renew or header_only):
        log.debug("実績は変わっていません(%s件)", outcome.unchanged)
        return outcome

    need = sorted(set(added) | set(renew))
    try:
        details = _read_details(path, tables, need, whole=len(need) * 2 > len(source))
    except source_db.SourceError as exc:
        outcome.error = str(exc)
        return outcome

    try:
        with conn:
            # **書く直前に、同じトランザクションの中でもう一度確かめる。**
            # 上で確かめてから取り込み元を読むあいだに保存された実績を消さないため
            # (IMMEDIATE で書き込みを先に押さえる)
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            left = pending(conn)
            if left:
                outcome.skipped_reason = (
                    "まだ取り込み元へ送れていない実績があります("
                    + "・".join(f"{k}{v}件" for k, v in left.items()) + ")")
                return outcome
            for pid in gone + renew:
                local_id = local[pid]["実績ID"]
                for table, _cols, _order in store.DETAIL_TABLES:
                    conn.execute(f"DELETE FROM {_q(table)} WHERE 実績ID = ?", (local_id,))
                conn.execute(f"DELETE FROM {h} WHERE 実績ID = ?", (local_id,))
            outcome.removed = len(gone)
            for pid, diff in header_only.items():
                conn.execute(f"UPDATE {h} SET {', '.join(f'{_q(n)} = ?' for n in diff)} WHERE 実績ID = ?",
                             [*(source[pid].get(n) for n in diff), local[pid]["実績ID"]])
            outcome.updated = len(header_only)
            taken = {int(r[0]) for r in conn.execute(f"SELECT 実績ID FROM {h}")}
            for pid in need:
                row = source[pid]
                # 手元の番号は取り込み元と同じにする。**この端末で保存して送った実績**が同じ番号を
                # 手元で使っていれば、番号は手元で振る(取込元実績ID で取り込み元とつなぐ)
                cols = [*names, "取込元実績ID", "使用回数未反映"]
                values = [*[row.get(n) for n in names], pid, 0]
                if pid not in taken:
                    cols, values = ["実績ID", *cols], [pid, *values]
                cursor = conn.execute(
                    f"INSERT INTO {h} ({', '.join(_q(c) for c in cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})", values)
                local_id = pid if pid not in taken else int(cursor.lastrowid)
                taken.add(local_id)
                for (table, columns, _order) in store.DETAIL_TABLES:
                    dcols = ["実績ID", *[n for n, _ in columns]]
                    for drow in details.get(table, {}).get(pid, []):
                        conn.execute(
                            f"INSERT INTO {_q(table)} ({', '.join(_q(c) for c in dcols)}) "
                            f"VALUES ({', '.join('?' for _ in dcols)})",
                            [local_id, *[drow.get(c) for c in dcols[1:]]])
                outcome.imported += 1
    except sqlite3.Error as exc:
        outcome.error = str(exc)
        outcome.imported = outcome.updated = outcome.removed = 0
        log.exception("実績の取り込みに失敗しました(手元は元のまま)")
        return outcome
    log.info("実績を取り込み直しました: 足した・入れ直した %s件 / 回数だけ直した %s件 / 消した %s件"
             "(変わらない %s件)", outcome.imported, outcome.updated, outcome.removed, outcome.unchanged)
    from . import master_state
    master_state.touch([config.TBL_PT_HEADER])
    return outcome


def _read_details(path: Path, tables: set[str], ids: list[int], *,
                  whole: bool) -> dict[str, dict[int, list[dict[str, Any]]]]:
    """取り込み元の明細を、実績IDごとに。`whole` なら表ごと読む(足す実績が多いとき)。"""
    out: dict[str, dict[int, list[dict[str, Any]]]] = {}
    if not ids:
        return out
    wanted = set(ids)
    for table, _cols, _order in store.DETAIL_TABLES:
        if table not in tables:
            continue
        rows: list[dict[str, Any]] = []
        if whole:
            rows = source_db.read_table(path, table)
        else:
            for i in range(0, len(ids), DETAIL_CHUNK):
                chunk = ids[i:i + DETAIL_CHUNK]
                rows += source_db.read_query(
                    path, f"SELECT * FROM {_q(table)} WHERE CAST(実績ID AS INTEGER) IN "
                          f"({', '.join('?' for _ in chunk)}) ORDER BY rowid", chunk)
        found = out.setdefault(table, {})
        for row in rows:
            pid = store.pt_long(row.get("実績ID"))
            if pid in wanted:
                found.setdefault(pid, []).append(row)
    return out


# 実績だけを取り込み直したときの、取り込み元の姿(`refresh_if_changed`)
_seen_stamp: dict[str, tuple[int, int]] = {}


def refresh_if_changed(conn: sqlite3.Connection,
                       path: Optional[Path] = None) -> Optional[ImportOutcome]:
    """取り込み元が前に見たときから変わっていれば、**実績だけ**取り込み直す。

    【なぜ要るか】
    実績の取り込みは、起動時と、発注の取り込み直し(倉庫連携・簡易在庫・設定の
    見張り)に乗っているだけだった。資材選択を開いたままの端末には、ほかの端末が
    保存した実績が**いつまでも出なかった**(2台での通し試験)。VBA は一覧を
    開くたびに共有を直接読むので、保存すればすぐ見える。
    資材選択が実績の一覧を作るたびにここを通す。変わっていなければファイルの
    姿(大きさ・更新時刻)を見るだけで帰る。
    """
    from . import sync_sources
    path = path or sync_sources.find_material_db()
    if path is None:
        return None
    stamp = sync_sources.source_stamp(path)
    if stamp is None or _seen_stamp.get(str(path)) == stamp:
        return None
    outcome = import_from(conn, path)
    if not outcome.error and not outcome.skipped_reason:
        _seen_stamp[str(path)] = stamp
    return outcome


# 資材選択が実績の一覧を作る前に待つ秒数(VER4.8.118)。資材選択は操作のたびに画面を作り直すので短く。
# 間に合わなければ手元の実績で一覧を出し、取り込みは背景で続ける(次の操作で新しい一覧になる)
from .bounded import wait_seconds as _wait_seconds  # noqa: E402
PAGE_WAIT_SEC = _wait_seconds(1.0)
_flights = None


def refresh_bounded(conn: sqlite3.Connection, wait: float = PAGE_WAIT_SEC) -> bool:
    """`refresh_if_changed` を**待つ上限つきで**(`bounded` の説明)。取り込んだら True。

    前は資材選択の要求の中で取り込んでいた(画面を作るたびに、共有が変わっていれば実績を
    まるごと入れ直すのを待っていた)。走っている最中なら待たない。
    """
    global _flights
    import sys
    from .bounded import FileFlights
    if _flights is None:
        _flights = FileFlights("実績の取り込み直し",
                               lambda c: sys.modules[__name__].refresh_if_changed(c))
    outcome = _flights.run(conn, wait, wait_if_running=0.0)
    if outcome.error is not None:
        log.warning("実績を取り込み直せませんでした(手元の実績で出します): %s", outcome.error)
        return False
    got = outcome.value if outcome.done else outcome.late
    return bool(got and (got.imported or got.updated or got.removed))


def forget_seen() -> None:
    """試験用。次の `refresh_if_changed` で必ず見に行く。"""
    _seen_stamp.clear()


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
