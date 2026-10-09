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
from typing import Any, Callable, Iterable, Optional

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
    mark_blockers  : (印, 先に付いていたら負ける印) の組。送り先でもう
                     相手の印が付いていたら、こちらの印は送らない
                     (例: 取り消し済の発注に「確認済み」を付けない)
    unique_column  : 行番号も送信IDも当たらないときに、**中身より先に**見る
                     一意の列(発注の 発注キー)。中身で探すと同じ中身の行を
                     まとめて当ててしまう
    match_columns  : 行番号も送信IDも当たらないときに、同じ行を探す列
                     (送信IDを書けなかった時期に送った行のため)。
                     **2行以上当たったら印は付けない**(どれか決められない)
    on_insert      : 1行を足すのと**同じまとまりで**送り先に施す処理
                     (例: 入出庫履歴を足したら在庫数も動かす)。
                     呼ばれ方は on_insert(送り先のトランザクション, 足した値)。
                     足すのが二重と分かった(送信ID重複)ときは一緒に戻る
    optional_columns: 送り先に**あれば**送る列。無ければ外して送る
                     (あとから足した列。足せなかった共有へも行は届ける)
    number_column  : 送り先で番号を振る列(`MAX+1`)。送り先が Access から
                     変換した表だと、番号の列は自動で振られない
                     (型の無いただの列)。空のまま足すと、ほかの端末が
                     その行を番号で指せない
    """

    sqlite_table: str
    access_table: str
    key_column: str
    op_id_column: str = DEFAULT_OP_ID_COLUMN
    use_op_id_guard: bool = True
    mark_columns: tuple[str, ...] = ()
    mark_pending: str = ""
    source_key: str = ""
    mark_blockers: tuple[tuple[str, str], ...] = ()
    match_columns: tuple[str, ...] = ()
    unique_column: str = ""
    on_insert: Optional[Callable[[Any, dict[str, Any]], None]] = field(
        default=None, compare=False)
    number_column: str = ""
    optional_columns: tuple[str, ...] = ()

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
        return record_all_sent(conn, spec)


def record_all_sent(conn: sqlite3.Connection, spec: WriteBackSpec) -> int:
    """`mark_all_sent` の中身。**コミットしない** ── 取り込みと同じトランザクションで呼ぶ。

    【なぜ取り込みと同じトランザクションか】
    取り込み(総入れ替え)をコミットしてから印を付けると、その**あいだ**に
    裏の書き戻し(`claim_rows`)が走れば、取り込んだばかりの行がぜんぶ
    「未送信」に見えて、共有へもう一度送られる ── 表まるごと倍になる
    (現場の手元DBで、発注コメント・ボード使用実績が 7件 → 14件 と、
    同じ7件が同じ順で2回入っていた)。あいだに入れなくすれば起きない。
    同期記録の表は先に用意しておくこと(`ensure_sync_table` はコミットする)。
    """
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
        except sqlite3.Error as exc:
            if _not_unknown(spec, exc):
                continue
            remaining[spec.sqlite_table] = 1
            continue
        if rows:
            remaining[spec.sqlite_table] = len(rows)
    return remaining


def _not_unknown(spec: WriteBackSpec, exc: sqlite3.Error) -> bool:
    """数えられなかったとき、「送れていない分は無い」と言ってよいか。

    **言ってよいのは、手元にその表が無いときだけ**(無い表に送れていない行は無い)。
    それ以外(掴まれている・混んでいる など)で「無い」と答えると、総入れ替えの
    取り込みが走って、送れていない発注を消してしまう。分からないときは
    「ある」と答えて取り込みを見送らせる(次の回でまた数える)。
    """
    if "no such table" in str(exc).lower():
        return True
    log.warning("%s: 送れていない行を数えられませんでした(取り込みは見送ります): %s",
                spec.sqlite_table, exc)
    return False


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
    付く。行は送り先の行番号か送信IDで決め、当たらなければ中身
    (`match_columns`)で探す。

    【付いている印だけを送る ── 空で上書きしない】
    以前は印の列を4つとも手元の値で書き換えていた。手元で「確認済み」
    だけを付けた行は取消の列が空なので、**ほかの端末が先に付けた
    「取り消し済」を空で消していた**(現場が取り消した発注が、倉庫の
    確認で生き返る)。印は付けるだけで外す操作は無いので、空は送らない。

    【相手の印が先なら送らない(`mark_blockers`)】
    送り先で読んでから書くまでを1つのまとまり(BEGIN IMMEDIATE)にして、
    負ける印が先に付いていたら書かない。手元の印は送らずに下ろし、
    次の取り込みで共有の状態に揃う。

    【送り先に行が無いときは、印を下ろす】
    以前は「送れていない」のまま残した。残った印は発注の表の取り込みを
    止めるので、**その端末だけ発注一覧が二度と更新されなかった**
    (共有で行が消えた・変換し直しで送信IDが消えた、など)。行が無ければ
    印を付ける相手がいない。下ろして、次の取り込みで共有に揃える。
    """
    if not spec.marks_enabled:
        return 0, []
    rows = unpushed_mark_rows(conn, spec)
    if not rows:
        return 0, []

    pushed, errors = 0, []
    for row in rows:
        row_id = int(row[spec.key_column])
        values = {c: row[c] for c in spec.mark_columns
                  if c in row.keys() and row[c] not in (None, "")}
        if not values:
            _clear_mark_pending(conn, spec, row_id)
            conn.commit()
            continue
        try:
            with source.transaction() as tx:
                outcome = _put_marks(tx, spec, _mark_target(conn, spec, row),
                                     row, values)
        except source_db.SourceError as exc:
            log.warning("%s の印を送れませんでした: %s", spec.access_table, exc)
            errors.append(f"{spec.access_table}(行{row_id}): {exc}")
            continue
        _clear_mark_pending(conn, spec, row_id)
        conn.commit()
        if outcome is None:
            pushed += 1
        else:
            log.warning("%s(行%s): %s", spec.access_table, row_id, outcome)
            errors.append(f"{spec.access_table}(行{row_id}): {outcome}")
    if pushed:
        log.info("%s: 印を%s件書き戻しました", spec.access_table, pushed)
    return pushed, errors


def _is_set(value: Any) -> bool:
    """印が付いているか。空・0 は付いていない。"""
    return value not in (None, "") and str(value).strip() not in ("0", "False", "false")


def _put_marks(tx: "source_db.SourceTransaction", spec: WriteBackSpec,
               where: Optional[dict[str, Any]], row: sqlite3.Row,
               values: dict[str, Any]) -> Optional[str]:
    """送り先の行に印を書く。書けなかったら理由(書けたら None)。"""
    table = source_db.quote_identifier(spec.access_table)

    def rows_by(cond: dict[str, Any]) -> tuple[list[dict[str, Any]], str, list[Any]]:
        sql = " AND ".join(f"{source_db.quote_identifier(k)} = ?" for k in cond)
        params = list(cond.values())
        return tx.query(f"SELECT * FROM {table} WHERE {sql}", params), sql, params

    found: list[dict[str, Any]] = []
    if where:
        found, cond_sql, cond_params = rows_by(where)
    if (not found and spec.unique_column and spec.unique_column in row.keys()
            and row[spec.unique_column] not in (None, "")):
        # 一意の鍵(発注キー)を中身より先に見る
        try:
            found, cond_sql, cond_params = rows_by({spec.unique_column: row[spec.unique_column]})
        except source_db.SourceError:
            found = []                     # 送り先にその列が無い(古い共有)
    if not found and spec.match_columns and all(
            c in row.keys() and row[c] not in (None, "") for c in spec.match_columns):
        # 送信IDを書けなかった時期に送った行・変換し直しで送信IDが
        # 消えた行は、中身で探す
        found, cond_sql, cond_params = rows_by(
            {c: row[c] for c in spec.match_columns})
        if len(found) > 1:
            # **同じ中身の行が2つ以上ある。** 以前は当たった行すべてに印を書いて
            # いた(同じ秒に同じロット・品名で2回送ると、片方の確認で両方が
            # 確認済みになる)。どれか決められないので書かない
            return (f"送り先に同じ中身の行が{len(found)}行あり、どれに付けるか決められません。"
                    "印は送らず、次の取り込みで共有に合わせます")
    if not found:
        return ("送り先に行がありません(共有側で消えています)。"
                "印は送らず、次の取り込みで共有に合わせます")
    for flag, blocker in spec.mark_blockers:
        if flag in values and any(_is_set(r.get(blocker)) for r in found):
            return (f"送り先では先に「{blocker}」が付いていたため、"
                    f"「{flag}」は送りませんでした。次の取り込みで共有に合わせます")
    sets = ", ".join(f"{source_db.quote_identifier(k)} = ?" for k in values)
    tx.execute(f"UPDATE {table} SET {sets} WHERE {cond_sql}",
               list(values.values()) + cond_params)
    return None


def unsent_tables(conn: sqlite3.Connection,
                   specs: Iterable[WriteBackSpec]) -> dict[str, int]:
    """複数specの中で、まだ送っていない行が残っているものを返す。

    総入れ替え取り込みの前に「未送信を失わないか」を確認する用途。
    """
    remaining: dict[str, int] = {}
    for spec in specs:
        try:
            rows = pending_rows(conn, spec)
        except sqlite3.Error as exc:
            if not _not_unknown(spec, exc):
                remaining[spec.sqlite_table] = 1     # 分からない = 送れていない分があるかも
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
        if spec.optional_columns:
            # 送り先に無い列は送らない。混ぜると1行も入らなくなる
            try:
                have = {r["name"] for r in source.query(
                    f"PRAGMA table_info({source_db.quote_identifier(spec.access_table)})")}
            except source_db.SourceError:
                have = set()
            local_only |= {c for c in spec.optional_columns if c not in have}
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
                    if spec.on_insert is None and not spec.number_column:
                        source.insert(spec.access_table, values)
                    else:
                        # 足すのと、それに伴う処理(番号・在庫数)は1つの
                        # まとまり。二重と分かれば両方とも戻る
                        with source.transaction() as tx:
                            _number_row(tx, spec, values)
                            tx.insert(spec.access_table, values)
                            if spec.on_insert is not None:
                                spec.on_insert(tx, values)
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


def _number_row(tx: "source_db.SourceTransaction", spec: WriteBackSpec,
                values: dict[str, Any]) -> None:
    """送り先で番号が自動で振られない列なら、`MAX+1` を入れる(VBAと同じ)。

    送り先の表を先に書き込みの鍵ごと押さえてある(BEGIN IMMEDIATE)ので、
    2台が同時に送っても同じ番号にはならない。
    """
    if not spec.number_column:
        return
    table = source_db.quote_identifier(spec.access_table)
    info = {r["name"]: r for r in tx.query(f"PRAGMA table_info({table})")}
    column = info.get(spec.number_column)
    if column is None:
        return
    if column["pk"] and str(column["type"] or "").upper() == "INTEGER":
        return                     # 自動で振られる(INTEGER PRIMARY KEY)
    name = source_db.quote_identifier(spec.number_column)
    top = tx.query(f"SELECT MAX(CAST({name} AS INTEGER)) AS n FROM {table}")
    values[spec.number_column] = int(top[0]["n"] or 0) + 1


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
