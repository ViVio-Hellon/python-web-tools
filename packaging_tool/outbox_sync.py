"""SQLite -> Access 書き戻し汎用エンジン

特定のテーブル名や業務に一切依存しない。「送信したい行が増えるSQLite
テーブル」と「送り先のAccessテーブル」の対応を `WriteBackSpec` で渡す
だけで、以下をまとめて面倒みる。

    - 二重取得の防止(claim/予約): 背景スレッドが同時に走っても、
      同じ行を2度は拾わない
    - 中断からの再開: 送信の途中で落ちて「送信中」のまま残った予約は、
      一定時間後に拾い直す。そのとき送信IDは新規採番せず引き継ぐ
    - 二重登録の防止: 送信IDをAccess側の一意インデックスに賭ける。
      再送で同じ行がもう一度INSERTされても、Access側が弾いてくれる
      (弾かれたら「失敗」ではなく「既に届いていた」として扱う)
    - Access側に送信ID列を用意できない環境でも、書き戻し自体は
      止めない(その場合は旧来どおり「予約による二重防止」だけで動く)
    - 1行の失敗で全体を止めない

他のVBA移行ツールでも、`WriteBackSpec` のリストを用意して
`write_back(conn, access, specs)` を呼ぶだけで使い回せる。
このファイル自体は業務固有のテーブル名を一切知らない。

【Access側の送信ID列について】
`write_back` は呼ぶたびに `ensure_op_id_column` で列とインデックスの
有無を確かめるが、結果(使えた/使えなかった)は端末内でキャッシュする。
ALTER TABLE / CREATE INDEX は失敗しうる操作なので、毎回リトライして
無駄な失敗ログを積み上げたり、ロック競合を増やしたりしないためである。
ロック競合など「今回はたまたま判定できなかった」場合だけキャッシュ
せず、次回また試す。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import source_db
from .logging_utils import get_logger

log = get_logger("outbox_sync")

# 同期記録テーブル(このエンジンが手元のDB内に持つ管理テーブル)。
# 業務テーブルの名前は一切ここに出てこない。
#
# **名前は変えない。** 取り込み元が Access だったころの名残だが、
# 中身は「どの行を送り済みか」で、消すと送った行をもう一度送ってしまう
# (送信IDの一意索引が止めるとはいえ、無駄な再送は避けたい)
SYNC_LOG_TABLE = "Access同期記録"

# 同期記録の状態。送信前に「送信中」で予約し、送れたら「済」にする
SYNC_SENDING = "送信中"
SYNC_DONE = "済"

# 「送信中」のまま放置された予約を、何分たったら拾い直すか。
# アプリが落ちる・端末が落ちる等で予約だけ残ることがある
STALE_CLAIM_MINUTES = 10

# Access側に足す送信ID列の既定名。テーブルごとに変えたい理由がなければ
# このままでよい
DEFAULT_OP_ID_COLUMN = "送信ID"


@dataclass(frozen=True)
class WriteBackSpec:
    """書き戻し対象1テーブル分の定義。呼び出し側(アプリ層)が組み立てる。

    sqlite_table   : ローカルSQLite側のテーブル名(行が増えていく方)
    access_table   : 送り先のAccessテーブル名
    key_column     : 両テーブルで共通の主キー列名(整数、オートナンバー等)
    op_id_column   : Access側に足す送信ID列の名前
    use_op_id_guard: Access側テーブルに送信ID列/一意インデックスを
                     作成してよいか。他チーム管理のテーブル等、
                     スキーマを触りたくない送り先では False にする
                     (その場合は予約による二重防止だけで動く)
    mark_columns   : あとから付ける印の列(確認済み・確認日時など)。
                     **行を足したあとに手元で変わる値**を送り直すため。
                     空なら印の送り直しはしない(足すだけのテーブル)
    mark_pending   : 「まだ送れていない印がある」を表す手元の列。
                     送れたら空に戻す
    source_key     : 送り先の行番号を覚えてある手元の列。取り込みで
                     受け取った行がこれを持つ。手元で作った行は持たない
                     ので、そちらは送信IDで行を決める
    """

    sqlite_table: str
    access_table: str
    key_column: str
    op_id_column: str = DEFAULT_OP_ID_COLUMN
    use_op_id_guard: bool = True
    mark_columns: tuple[str, ...] = ()
    mark_pending: str = ""
    source_key: str = ""

    @property
    def marks_enabled(self) -> bool:
        return bool(self.mark_columns and self.mark_pending)


@dataclass
class WriteBackResult:
    sent: dict[str, int] = field(default_factory=dict)
    # 送り直した印の件数(確認済みにした等)。**足した行とは別に数える**
    # ── 「3件送りました」の3が、新しい発注なのか確認の印なのかで
    # 読む人のすることが変わる
    marked: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def total(self) -> int:
        return sum(self.sent.values())

    @property
    def total_marked(self) -> int:
        return sum(self.marked.values())

    @property
    def ok(self) -> bool:
        return not self.errors and not self.skipped_reason

    def summary(self) -> str:
        if self.skipped_reason:
            return self.skipped_reason
        if not self.total and not self.total_marked and not self.errors:
            return "送る新しいデータはありませんでした。"
        lines = [f"取り込み元へ{self.total}件を反映しました。"]
        for table, count in self.sent.items():
            lines.append(f"  {table}: {count}件")
        if self.total_marked:
            lines.append(f"確認・取消の印を{self.total_marked}件送りました。")
        if self.errors:
            lines.append("")
            lines.append("送れなかったもの:")
            lines.extend(f"  {e}" for e in self.errors)
        return "\n".join(lines)


# ------------------------------------------------------------------
# 同期記録テーブル
# ------------------------------------------------------------------
def ensure_sync_table(conn: sqlite3.Connection) -> None:
    """同期済みを覚えておく表。無ければ作る。全テーブル共通で1つだけ持つ。"""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS [{SYNC_LOG_TABLE}] ("
        " テーブル名 TEXT NOT NULL,"
        " 行ID       INTEGER NOT NULL,"
        " 送信ID     TEXT,"
        " 同期日時   TEXT NOT NULL DEFAULT (datetime('now','localtime')),"
        f" 状態       TEXT NOT NULL DEFAULT '{SYNC_DONE}',"
        " PRIMARY KEY (テーブル名, 行ID))")
    # 既存DBに列が無ければ足す(古い記録は「送信ID未採番・送信済み」扱い)
    columns = {row[1] for row in conn.execute(
        f"PRAGMA table_info([{SYNC_LOG_TABLE}])")}
    if "状態" not in columns:
        conn.execute(f"ALTER TABLE [{SYNC_LOG_TABLE}] ADD COLUMN"
                     f" 状態 TEXT NOT NULL DEFAULT '{SYNC_DONE}'")
    if "送信ID" not in columns:
        conn.execute(f"ALTER TABLE [{SYNC_LOG_TABLE}] ADD COLUMN 送信ID TEXT")
    conn.commit()


def mark_all_sent(conn: sqlite3.Connection, spec: WriteBackSpec) -> int:
    """いまテーブルにある行を、**送り済み**として記録し直す。

    総入れ替えの取り込みの**直後に**呼びます。取り込んだ行は
    取り込み元から来たものなので、こちらから送る必要がありません。

    【呼ばないとどうなるか ── 取り込みのたびに倍になります】
    取り込みは総入れ替えで、管理番号は手元で振り直されます。
    同期記録には**古い行ID**が残るので、取り込んだ行はどれも
    「まだ送っていない」に見えます。次の取り込みはその前に書き戻しを
    走らせるので、取り込み元から来た行が**新しい行としてもう一度**
    送られます。3件 → 6件 → 12件、と起動のたびに倍になります
    (現場のファイルで踏みました)。

    【古い記録を消すのも同じくらい大事】
    番号が振り直されたあとの古い行IDは、**別の行を指します**。
    残すと、こちらで新しく登録した行が「送信済み」に化けて、
    二度と送られなくなります。

    呼んでよい前提は、`import_master` が守っています ── 未送信の行が
    残っているテーブルは取り込みません(送っていない分を消さないため)。
    つまりここへ来た時点で、テーブルの中身は全部取り込み元のものです。
    """
    ensure_sync_table(conn)
    with conn:
        conn.execute(f"DELETE FROM [{SYNC_LOG_TABLE}] WHERE テーブル名 = ?",
                     (spec.sqlite_table,))
        cursor = conn.execute(
            f"INSERT INTO [{SYNC_LOG_TABLE}] (テーブル名, 行ID, 状態)"
            f" SELECT ?, [{spec.key_column}], ? FROM [{spec.sqlite_table}]",
            (spec.sqlite_table, SYNC_DONE))
    marked = cursor.rowcount or 0
    log.info("%s: 取り込んだ %s件を送信済みとして記録しました",
             spec.sqlite_table, marked)
    return marked


def pending_rows(conn: sqlite3.Connection, spec: WriteBackSpec) -> list[sqlite3.Row]:
    """まだ送れていない行を拾う(予約はしない)。件数確認用。"""
    ensure_sync_table(conn)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        f"SELECT * FROM [{spec.sqlite_table}] WHERE [{spec.key_column}] NOT IN"
        f" (SELECT 行ID FROM [{SYNC_LOG_TABLE}]"
        f"  WHERE テーブル名 = ? AND 状態 = ?)"
        f" ORDER BY [{spec.key_column}]",
        (spec.sqlite_table, SYNC_DONE)).fetchall()


def unpushed_mark_rows(conn: sqlite3.Connection,
                       spec: WriteBackSpec) -> list[sqlite3.Row]:
    """印を付けたのに、まだ送り先へ送れていない行。

    **総入れ替えの前に必ず見る。** 送れていない印は手元にしか無いので、
    先に消してしまうと二度と戻らない(印を付けた人には、押したはずの
    ものが翌朝消えているようにしか見えない)。
    """
    if not spec.marks_enabled:
        return []
    conn.row_factory = sqlite3.Row
    return conn.execute(
        f"SELECT * FROM [{spec.sqlite_table}]"
        f" WHERE [{spec.mark_pending}] IS NOT NULL"
        f"   AND [{spec.mark_pending}] <> ''"
        f" ORDER BY [{spec.key_column}]").fetchall()


def unpushed_mark_tables(conn: sqlite3.Connection,
                         specs: Iterable[WriteBackSpec]) -> dict[str, int]:
    """印を送れていない行が残っているテーブル(`unsent_tables` の印版)。"""
    remaining: dict[str, int] = {}
    for spec in specs:
        try:
            rows = unpushed_mark_rows(conn, spec)
        except sqlite3.Error:
            continue
        if rows:
            remaining[spec.sqlite_table] = len(rows)
    return remaining


def mark_pending(conn: sqlite3.Connection, spec: WriteBackSpec,
                 row_id: int) -> None:
    """この行の印をまだ送っていない、と覚える。

    印を付ける業務の処理(確認済みにする等)から呼ぶ。送るのは
    `push_marks`。
    """
    if not spec.marks_enabled:
        return
    conn.execute(
        f"UPDATE [{spec.sqlite_table}] SET [{spec.mark_pending}] = '1'"
        f" WHERE [{spec.key_column}] = ?", (row_id,))


def _clear_mark_pending(conn: sqlite3.Connection, spec: WriteBackSpec,
                        row_id: int) -> None:
    """この行の印は送り終わった、と覚え直す。"""
    if not spec.marks_enabled:
        return
    conn.execute(
        f"UPDATE [{spec.sqlite_table}] SET [{spec.mark_pending}] = ''"
        f" WHERE [{spec.key_column}] = ?", (row_id,))


def _mark_target(conn: sqlite3.Connection, spec: WriteBackSpec,
                 row: sqlite3.Row) -> Optional[dict[str, Any]]:
    """送り先のどの行に印を付けるか。**決まらなければ None。**

    2通りある。取り込みで受け取った行は送り先の行番号を覚えている。
    手元で作って送った行は覚えていないので、送信IDで決める
    (送信IDは送るときに採番して、その行だけに書いてある)。
    """
    if spec.source_key and spec.source_key in row.keys():
        value = row[spec.source_key]
        if value not in (None, ""):
            return {spec.key_column: int(value)}
    found = conn.execute(
        f"SELECT 送信ID FROM [{SYNC_LOG_TABLE}]"
        " WHERE テーブル名 = ? AND 行ID = ? AND 状態 = ?",
        (spec.sqlite_table, int(row[spec.key_column]), SYNC_DONE)).fetchone()
    op_id = found[0] if found else None
    if op_id:
        return {spec.op_id_column: op_id}
    return None


def push_marks(conn: sqlite3.Connection,
               source: "source_db.SourceConnection",
               spec: WriteBackSpec) -> tuple[int, list[str]]:
    """手元で付けた印を、送り先の同じ行へ書き戻す。

    足すのではなく**書き換える**ので、行を取り違えると別の発注に印が
    付く。どの行かが決まらない行は送らずに残す(`_mark_target`)。
    1行も書き換えられなかったときも、送れたことにしない ── 送り先から
    行が消えている場合があり、印だけ手元から消えると追えなくなる。
    """
    if not spec.marks_enabled:
        return 0, []
    rows = unpushed_mark_rows(conn, spec)
    if not rows:
        return 0, []

    pushed, errors = 0, []
    for row in rows:
        row_id = int(row[spec.key_column])
        where = _mark_target(conn, spec, row)
        if where is None:
            errors.append(
                f"{spec.access_table}(行{row_id}): 送り先のどの行か決められません"
                "(取り込み元の行番号も送信IDも分かりません)")
            continue
        values = {c: row[c] for c in spec.mark_columns if c in row.keys()}
        try:
            changed = source.update(spec.access_table, values, where)
        except source_db.SourceError as exc:
            log.warning("%s の印を送れませんでした: %s", spec.access_table, exc)
            errors.append(f"{spec.access_table}(行{row_id}): {exc}")
            continue
        if not changed:
            errors.append(
                f"{spec.access_table}(行{row_id}): 送り先に該当する行が"
                "ありません")
            continue
        _clear_mark_pending(conn, spec, row_id)
        conn.commit()
        pushed += 1
    if pushed:
        log.info("%s: 印を%s件書き戻しました", spec.access_table, pushed)
    return pushed, errors


def unsent_tables(conn: sqlite3.Connection,
                   specs: Iterable[WriteBackSpec]) -> dict[str, int]:
    """複数specの中で、まだ送っていない行が残っているものを返す。

    総入れ替え取り込みの前に「未送信を失わないか」を確認する用途。
    """
    remaining: dict[str, int] = {}
    for spec in specs:
        try:
            rows = pending_rows(conn, spec)
        except sqlite3.Error:
            continue
        if rows:
            remaining[spec.sqlite_table] = len(rows)
    return remaining


def claim_rows(conn: sqlite3.Connection,
               spec: WriteBackSpec) -> tuple[list[sqlite3.Row], dict[int, str]]:
    """送る行を予約してから返す。行IDごとの送信IDも一緒に返す。

    【なぜ予約が要るか】
    書き戻しは複数の操作の直後に裏で走りうる。予約せずに「未送信の行」を
    毎回SELECTすると、同時に走った処理同士が同じ行を拾って二重に
    INSERTしてしまう。ここで先に予約しておけば、スレッドでも別プロセス
    でも同じ行を2度は拾わない(予約はひとつのトランザクションで行う)。

    送信IDは予約時に採番し、期限切れの予約を拾い直すときは同じIDを
    使い回す(=Accessに届いていたかもしれない行だけ、IDを引き継ぐ)。
    確実に失敗したとわかった行は `release_claim` で予約ごと外すので、
    そちらは次回このIDを見ることはなく、新しいIDで送られる。
    """
    ensure_sync_table(conn)
    conn.row_factory = sqlite3.Row
    op_ids: dict[int, str] = {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1) 期限切れの予約を拾い直す。送信IDは引き継ぐ(採番し直さない)
        stale = conn.execute(
            f"SELECT 行ID, 送信ID FROM [{SYNC_LOG_TABLE}]"
            f" WHERE テーブル名 = ? AND 状態 = ?"
            f" AND 同期日時 < datetime('now','localtime',?)",
            (spec.sqlite_table, SYNC_SENDING,
             f"-{STALE_CLAIM_MINUTES} minutes")).fetchall()
        for record in stale:
            row_id = int(record["行ID"])
            op_id = record["送信ID"] or uuid.uuid4().hex
            op_ids[row_id] = op_id
            conn.execute(
                f"UPDATE [{SYNC_LOG_TABLE}]"
                " SET 送信ID = ?, 同期日時 = datetime('now','localtime')"
                " WHERE テーブル名 = ? AND 行ID = ?",
                (op_id, spec.sqlite_table, row_id))
        if stale:
            log.warning("%s: 送信中のまま残っていた予約を%s件拾い直します"
                        "(同じ送信IDで再送するのでAccess側が重複を弾きます)",
                        spec.sqlite_table, len(stale))

        # 2) まだ記録の無い行を新規に予約する(送信IDを新規採番)
        fresh = conn.execute(
            f"SELECT [{spec.key_column}] AS 行ID FROM [{spec.sqlite_table}]"
            f" WHERE [{spec.key_column}] NOT IN"
            f" (SELECT 行ID FROM [{SYNC_LOG_TABLE}] WHERE テーブル名 = ?)",
            (spec.sqlite_table,)).fetchall()
        for record in fresh:
            row_id = int(record["行ID"])
            op_id = uuid.uuid4().hex
            op_ids[row_id] = op_id
            conn.execute(
                f"INSERT INTO [{SYNC_LOG_TABLE}] (テーブル名, 行ID, 送信ID, 状態)"
                " VALUES (?, ?, ?, ?)",
                (spec.sqlite_table, row_id, op_id, SYNC_SENDING))

        # 3) 予約できた行の実データをまとめて取り直す
        rows: list[sqlite3.Row] = []
        if op_ids:
            ids = list(op_ids)
            marks = ", ".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM [{spec.sqlite_table}]"
                f" WHERE [{spec.key_column}] IN ({marks})"
                f" ORDER BY [{spec.key_column}]", ids).fetchall()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return rows, op_ids


def mark_synced(conn: sqlite3.Connection, spec: WriteBackSpec,
                row_ids: Iterable[int]) -> None:
    """送れた行を「済」にする。予約が無くても記録する(手動送信など)。

    `INSERT OR REPLACE` ではなく `ON CONFLICT ... DO UPDATE`(UPSERT)に
    しているのは、予約時に採番した送信IDを消さないため
    (REPLACEは行を作り直すので既存の送信ID列がNULLに戻ってしまう)。
    要SQLite 3.24以降。
    """
    ensure_sync_table(conn)
    with conn:
        conn.executemany(
            f"INSERT INTO [{SYNC_LOG_TABLE}] (テーブル名, 行ID, 状態)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(テーブル名, 行ID) DO UPDATE SET"
            " 状態 = excluded.状態, 同期日時 = datetime('now','localtime')",
            [(spec.sqlite_table, int(i), SYNC_DONE) for i in row_ids])


def release_claim(conn: sqlite3.Connection, spec: WriteBackSpec,
                  row_ids: Iterable[int]) -> None:
    """送れなかったことが確実な行の予約を外す(次回すぐ新しい送信IDで拾える)。

    ここに渡すのは、Accessへの送信で `AccessError` を捕まえて「届いて
    いない」と判断できた行**だけ**にすること(型不一致・列不足など、
    待っても直らない失敗)。記録ごと削除するので、次回は真新しい予約
    として拾われ、送信IDも採番し直される――届いていないと分かっている
    のでそれで安全。

    逆に「Accessに届いたかどうか分からない行」(想定外の例外で処理を
    抜けた場合など)はここに渡さず、「送信中」のまま残すこと。
    `claim_rows` の期限切れ拾い直しが同じ送信IDで再送し、Access側の
    一意インデックスに二重登録を弾かせるのが、その場合の唯一の安全策
    になる。
    """
    ids = [int(i) for i in row_ids]
    if not ids:
        return
    ensure_sync_table(conn)
    with conn:
        conn.executemany(
            f"DELETE FROM [{SYNC_LOG_TABLE}]"
            " WHERE テーブル名 = ? AND 行ID = ? AND 状態 = ?",
            [(spec.sqlite_table, i, SYNC_SENDING) for i in ids])


# ------------------------------------------------------------------
# 送り先テーブルの準備(送信ID列 / 一意インデックス)
# ------------------------------------------------------------------
# ensure_op_id_column の結果(使えた/使えなかった)を端末内で使い回す
# キャッシュ。key: (取り込み元のパス, access_table, op_id_column)
# 値: (確かめたときのファイルの姿(大きさ, 更新時刻), 使えたか)
#
# **ファイルの姿も覚える。** 以前はパスだけで覚えていたので、取り込み元が
# 変換し直したファイルに差し替えられ(変換では索引が付いてこない)ても、
# 動いているツールは「索引はある」と思い込んだまま作り直さなかった
_op_id_column_cache: dict[tuple[str, str, str], tuple[Optional[tuple[int, int]], bool]] = {}


def _file_stamp(path: Path) -> Optional[tuple[int, int]]:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def _try_ddl(source: "source_db.SourceConnection", sql: str,
            label: str) -> Optional[bool]:
    """DDLを1つ実行する。戻り値は3通り。

    True  : 成功した、または「もう存在する」ので実質OK
    False : 権限不足など、待っても直らない失敗(以後は諦めて素通りする)
    None  : ロック競合など一時的な失敗(今回は判定できない、次回また試す)
    """
    try:
        source.execute(sql)
        log.info("%s: 完了しました", label)
        return True
    except source_db.SourceError as exc:
        message = str(exc)
        if source_db.is_lock_error(message):
            log.debug("%s: ロック競合のため今回は見送ります(次回再試行)", label)
            return None
        if source_db.is_already_exists_error(message):
            log.debug("%s: 既にあります", label)
            return True
        log.warning(
            "%s: できませんでした(送信IDによる重複防止なしで送信を続けます): %s",
            label, exc)
        return False


def ensure_op_id_column(source: "source_db.SourceConnection",
                        spec: WriteBackSpec) -> bool:
    """送り先テーブルに送信ID列と一意インデックスがあるかを確かめる。

    無ければ作る。列を足し忘れた端末で書き戻すとINSERTが「そんな列は
    無い」で全滅しかねないので、使えるかどうかを毎回(ただしキャッシュ
    しつつ)確認する。作れない環境でも書き戻し自体は止めたくないので、
    戻り値の真偽だけを `write_back` に返す
    (False のときは送信ID列を省いて、旧来どおり予約による二重防止
    だけで送る)。
    """
    cache_key = (str(source.path), spec.access_table, spec.op_id_column)
    cached = _op_id_column_cache.get(cache_key)
    if cached is not None and cached[0] == _file_stamp(source.path):
        return cached[1]

    column_ok = _try_ddl(
        source,
        f"ALTER TABLE {source_db.quote_identifier(spec.access_table)}"
        f" ADD COLUMN {source_db.quote_identifier(spec.op_id_column)} TEXT",
        f"{spec.access_table} への {spec.op_id_column} 列の追加")
    if column_ok is None:
        return False

    index_ok = _create_op_id_index(source, spec)
    if index_ok is None:
        return False

    ready = column_ok and index_ok
    # 作った後の姿で覚える(作ったこと自体でファイルの更新時刻が進むため)
    _op_id_column_cache[cache_key] = (_file_stamp(source.path), ready)
    return ready


def index_name_for(spec: WriteBackSpec) -> str:
    """送信IDの一意インデックスの名前。状態の確認からも使う。"""
    return f"IX_{spec.access_table}_{spec.op_id_column}"


def _create_op_id_index(source: "source_db.SourceConnection",
                        spec: WriteBackSpec) -> Optional[bool]:
    """送信IDの一意インデックスを作る。作れなければ**1度だけ直して**試す。

    【空文字が混ざると、二度と作れなくなる】
    一意インデックスは NULL どうしを別物として扱うので、送信IDが未採番
    (NULL)の古い行がいくつあっても作れます。ところが**空文字は別物では
    ありません** ── 2行以上が `''` だと作成が一意制約で落ち、しかも
    データを直さないかぎり毎回落ち続けます。

    そうなると重複防止は**黙って切れたまま**になります(`_try_ddl` は
    警告を1行出すだけで、以後は諦めて素通りする)。送信IDが空のまま
    増え続け、次に同じ行を送り直したときに二重登録が起きます。

    空文字は送信IDとして意味を持ちません(採番するのは uuid4 の16進)。
    **未採番と同じ NULL に寄せて**から、もう一度だけ作ります ── これで
    古い行の意味は変わらず、インデックスだけが通ります。
    """
    sql = (f"CREATE UNIQUE INDEX IF NOT EXISTS "
           f"{source_db.quote_identifier(index_name_for(spec))}"
           f" ON {source_db.quote_identifier(spec.access_table)}"
           f" ({source_db.quote_identifier(spec.op_id_column)})")
    label = f"{spec.access_table} への一意インデックスの作成"

    try:
        source.execute(sql)
        log.info("%s: 完了しました", label)
        return True
    except source_db.SourceError as exc:
        message = str(exc)
        if source_db.is_lock_error(message):
            log.debug("%s: ロック競合のため今回は見送ります(次回再試行)", label)
            return None
        if source_db.is_already_exists_error(message):
            log.debug("%s: 既にあります", label)
            return True
        if not source_db.is_duplicate_error(message):
            log.warning("%s: できませんでした"
                        "(送信IDによる重複防止なしで送信を続けます): %s",
                        label, exc)
            return False

    # 一意制約で落ちた。空文字が混ざっているなら、そこを未採番(NULL)に
    # 寄せてから**1度だけ**やり直す
    blanks = _blank_op_ids_to_null(source, spec)
    if not blanks:
        log.warning("%s: 送信IDに重複した値があるため作れません"
                    "(送信IDによる重複防止なしで送信を続けます)", label)
        return False

    log.info("%s: 送信IDが空の行を%s件だけ未採番に直しました", label, blanks)
    return _try_ddl(source, sql, label + "(空欄を直したあと)")


def _blank_op_ids_to_null(source: "source_db.SourceConnection",
                          spec: WriteBackSpec) -> int:
    """送信IDが空文字の行を NULL に寄せる。戻り値は直した件数。

    **触るのはこのツールが足した列の、空欄だけ**です。業務の値は
    1つも動かしません。
    """
    column = source_db.quote_identifier(spec.op_id_column)
    table = source_db.quote_identifier(spec.access_table)
    try:
        return source.execute(
            f"UPDATE {table} SET {column} = NULL WHERE {column} = ''")
    except source_db.SourceError as exc:
        log.warning("%s の送信IDの空欄を直せませんでした: %s",
                    spec.access_table, exc)
        return 0


@dataclass
class GuardState:
    """重複送信ガードがいま効いているか。**読むだけ**(DDLは走らせない)。

    効いていないことは、いまはログの1行にしか出ません。ガードが切れた
    まま気づかないと、再送のたびに二重登録が起きます。画面から読める
    ようにするためだけの型です。
    """

    table: str
    has_column: bool = False
    has_index: bool = False
    blanks: int = 0              # 送信IDが空文字の行(これが2つ以上だと作れない)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.has_column and self.has_index and not self.error

    def why(self) -> str:
        """効いていない理由と、次にできること。効いていれば空。"""
        if self.error:
            return f"確かめられませんでした({self.error})"
        if not self.has_column:
            return (f"{self.table} に送信ID列がありません。"
                    "次の「取り込み元へ反映」で足します。")
        if not self.has_index:
            if self.blanks > 1:
                return (f"{self.table} の送信IDが空の行が{self.blanks}件あり、"
                        "一意インデックスを作れません。次の取り込みか"
                        "「取り込み元へ反映」で空欄を未採番に直して作ります。")
            return (f"{self.table} に送信IDの一意インデックスがありません。"
                    "次の取り込み(起動時の自動取り込みを含む)か"
                    "「取り込み元へ反映」で作ります。")
        return ""


def guard_state(source: "source_db.SourceConnection",
                spec: WriteBackSpec) -> GuardState:
    """重複送信ガードの状態を**読むだけ**で調べる。

    `ensure_op_id_column` と違い、列も索引も作りません ── 状態を見に
    行っただけで取り込み元の形が変わるのは、見る側の期待と違います。
    """
    state = GuardState(table=spec.access_table)
    if not spec.use_op_id_guard:
        state.has_column = state.has_index = True   # 使わない約束なので問わない
        return state
    table = source_db.quote_identifier(spec.access_table)
    column = source_db.quote_identifier(spec.op_id_column)
    try:
        state.has_column = any(
            row.get("name") == spec.op_id_column
            for row in source.query(f"PRAGMA table_info({table})"))
        if state.has_column:
            want = index_name_for(spec)
            state.has_index = any(
                row.get("name") == want
                for row in source.query(f"PRAGMA index_list({table})"))
            found = source.query(
                f"SELECT COUNT(*) AS 件数 FROM {table} WHERE {column} = ''")
            state.blanks = int(found[0]["件数"]) if found else 0
    except source_db.SourceError as exc:
        state.error = str(exc)
    return state


# ------------------------------------------------------------------
# 書き戻し本体
# ------------------------------------------------------------------
def write_back(conn: sqlite3.Connection,
               source: "source_db.SourceConnection",
               specs: Iterable[WriteBackSpec]) -> WriteBackResult:
    """specsに列挙された各テーブルについて、手元 → 送り先へ送る。

    呼び出し側(アプリ層)が「ファイルが見つかるか」「開けるか」を先に
    確認し、開いた接続をここへ渡す。このエンジンは接続の成否や
    業務固有のファイル探索を一切知らない。
    """
    result = WriteBackResult()
    for spec in specs:
        op_id_ready = spec.use_op_id_guard and ensure_op_id_column(source, spec)
        local_only = {c for c in (spec.mark_pending, spec.source_key) if c}
        try:
            rows, op_ids = claim_rows(conn, spec)
        except sqlite3.Error as exc:
            result.errors.append(f"{spec.sqlite_table}: {exc}")
            continue
        if not rows:
            _push_marks_into(result, conn, source, spec)
            continue

        sent = 0
        # ここに入るのは「確実に失敗した」とわかった行だけ(release_claim参照)
        failed: list[int] = []
        try:
            for row in rows:
                row_id = int(row[spec.key_column])
                # **手元にしか無い列は送らない。** 送り先にその列は無く、
                # 混ぜると1行も入らなくなる(印の管理用に手元へ足した列)
                values = {k: row[k] for k in row.keys()
                          if k != spec.key_column and k not in local_only}
                if op_id_ready:
                    values[spec.op_id_column] = op_ids[row_id]
                try:
                    source.insert(spec.access_table, values)
                except source_db.SourceError as exc:
                    if op_id_ready and source_db.is_duplicate_error(str(exc)):
                        # 前回の送信が届いていた(送信ID重複)。エラーではなく成功
                        log.info("%s 行%s は既に届いていました(送信ID重複)",
                                 spec.access_table, row_id)
                    else:
                        log.warning("%s の1行を送れませんでした: %s",
                                    spec.access_table, exc)
                        result.errors.append(
                            f"{spec.access_table}(行{row_id}): {exc}")
                        failed.append(row_id)
                        continue
                mark_synced(conn, spec, [row_id])
                # いま送った行には印も一緒に乗っている(INSERTの値に
                # 含まれている)ので、送り直す必要はない
                _clear_mark_pending(conn, spec, row_id)
                sent += 1
        except Exception:
            # 想定外の例外。ここまでに確実に失敗したとわかった分だけ予約を
            # 戻し、残り(未処理の行・届いたかもしれない行)は
            # 「送信中」のまま残す。期限切れ拾い直しが同じ送信IDで後日
            # 再送し、送り先の一意インデックスが二重登録を防ぐ
            release_claim(conn, spec, failed)
            log.exception("%s: 書き戻し中に想定外の例外", spec.sqlite_table)
            raise
        release_claim(conn, spec, failed)
        if sent:
            result.sent[spec.sqlite_table] = sent
        # 送り終わってから印を送る。**順番が逆だとできない** ── まだ
        # 送っていない行は送り先に無く、印を付ける相手がいない
        _push_marks_into(result, conn, source, spec)
    return result


def _push_marks_into(result: WriteBackResult, conn: sqlite3.Connection,
                     source: "source_db.SourceConnection",
                     spec: WriteBackSpec) -> None:
    """`push_marks` を呼んで、結果を書き戻しの集計へ足す。"""
    if not spec.marks_enabled:
        return
    pushed, errors = push_marks(conn, source, spec)
    if pushed:
        result.marked[spec.sqlite_table] = pushed
    result.errors.extend(errors)
