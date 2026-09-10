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
"""
from __future__ import annotations

import sqlite3
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, db, import_specs, outbox_sync, source_db
from .outbox_sync import WriteBackResult, WriteBackSpec
from .logging_utils import get_logger

log = get_logger("data_sync")

# 取り込み元の1テーブルを読んだ結果
# 取り込み元の1行。値は sqlite3 が返す型のまま(数値は数値)で、
# 文字列化は `import_specs` の変換関数が引き受ける
Rows = list[dict[str, object]]

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
                  key_column="管理番号"),
    WriteBackSpec(sqlite_table=config.TBL_STOCK_HISTORY,
                  access_table=config.TBL_STOCK_HISTORY,
                  key_column="id"),
]


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


# ==================================================================
# 取り込み (取り込み元 → 手元)
# ==================================================================
@dataclass
class ImportResult:
    """1回の取り込みの結果。画面に出すためのまとめ。"""

    imported: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # 失敗ではないが伝えたいこと(任意テーブルが元に無い、など)。
    # `errors` に混ぜると取り込みが「失敗」と出て、直すべき問題が埋もれる
    notes: list[str] = field(default_factory=list)
    # 取り込み元に無かった任意テーブル。**1行にまとめて言う**ため
    # `notes` とは別に持つ。閾値マスタを足して8表になり、1表1行だと
    # 案内だけで8行になって、本当に直すべき問題が埋もれた
    missing_optional: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.imported.values())

    @property
    def ok(self) -> bool:
        return not self.errors

    def merge(self, other: "ImportResult") -> "ImportResult":
        self.imported.update(other.imported)
        self.skipped.update(other.skipped)
        self.errors.extend(other.errors)
        self.notes.extend(other.notes)
        self.missing_optional.extend(other.missing_optional)
        return self

    def summary(self) -> str:
        if not self.imported and self.errors:
            return "取り込めませんでした:\n" + "\n".join(self.errors)
        lines = [f"{len(self.imported)}テーブル / 合計{self.total}件を取り込みました。"]
        for table, count in self.imported.items():
            note = f"({self.skipped[table]}件スキップ)" if self.skipped.get(table) else ""
            lines.append(f"  {table}: {count}件{note}")
        if self.errors:
            lines.append("")
            lines.append("次のテーブルは取り込めませんでした:")
            lines.extend(f"  {e}" for e in self.errors)
        if self.missing_optional:
            lines.append("")
            lines.append(f"  取り込み元に無かった表({len(self.missing_optional)}件、"
                         f"無くても動きます): "
                         + ", ".join(self.missing_optional))
        if self.notes:
            lines.append("")
            lines.extend(f"  {n}" for n in self.notes)
        return "\n".join(lines)


def import_tables(
    conn: sqlite3.Connection,
    source_path: Path,
    specs: dict[str, list[tuple[str, str, Callable[[Optional[str]], Any]]]],
    *,
    source_table: str = "",
    required: Optional[dict[str, tuple[str, ...]]] = None,
    blank_is_missing: Optional[dict[str, tuple[str, ...]]] = None,
    optional: Optional[frozenset[str]] = None,
    fallbacks: Optional[dict[Any, Any]] = None,
    result: Optional[ImportResult] = None,
    progress: Optional[Progress] = None,
    progress_range: tuple[int, int] = (0, 100),
) -> ImportResult:
    """取り込み元の表を手元へ総入れ替えで取り込む。

    `specs` は「手元の列名, 取り込み元の列名, 変換関数」の並び。
    1テーブルが失敗しても他は続ける(現場では「全部止まる」より
    「入るものは入る」ほうが役に立つ)。

    `required` は値が `None` の行を、`blank_is_missing` は加えて
    **空文字の行も**落とす。`to_text` は空を `""` で返すので、
    権限コードのように空では意味を成さない列は後者で撥ねる。
    """
    result = result or ImportResult()
    required = required or {}
    blank_is_missing = blank_is_missing or {}
    optional = optional or frozenset()
    fallbacks = fallbacks or {}
    notify = progress or _noop_progress
    start_pct, end_pct = progress_range
    span = max(end_pct - start_pct, 0)

    for index, (table, spec) in enumerate(specs.items()):
        # テーブル単位で進める。読み取りが一番時間を食うので、その前に出す
        step_pct = start_pct + span * index // len(specs) if specs else start_pct
        step_message = f"{table} を読み込み中..."
        if specs:
            notify(step_pct, step_message)
        try:
            rows = read_table(source_path, source_table or table)
        except SyncError as exc:
            if table in optional:
                # 無くてよい表。失敗として数えると、毎回「失敗」と出て
                # 本当に直すべき問題が埋もれる
                log.info("%s: 取り込み元にありません(任意)", table)
                result.missing_optional.append(table)
                continue
            log.warning("%s: 読み取り失敗 %s", table, exc)
            result.errors.append(f"{table}: {exc}")
            # 段の文言(step_message)は変えず、いまの段が失敗したとだけ伝える。
            # そうしないと、レーンの各段は最後の段しか失敗を示せず、
            # 「取り込みは失敗したのに、どの段も完了のまま」に見える
            notify(step_pct, step_message, ok=False)
            continue
        if not rows:
            result.imported[table] = 0
            continue

        # **元に無い列があれば先に言う。**
        # `row.get()` は無い列を None にするので、上流が列名を変えても
        # 取り込み自体は「成功」してしまい、中身だけが空になる。
        # 実際、仕掛ロットの列名が半角カナから全角に変わっていた写しで
        # 2,159件が**ロット番号だけ空**のまま入り、一覧が1件になった。
        present = set(rows[0].keys())
        missing = [src for _col, src, _conv in spec if src not in present]
        if missing:
            key_sources = {src for col, src, _conv in spec
                           if col in required.get(table, ())}
            lost_keys = [m for m in missing if m in key_sources]
            note = (f"{table}: 元のファイルに無い列があります: "
                    f"{', '.join(missing)}")
            if lost_keys:
                # 鍵の列が無いなら**入れない**。空の鍵で総入れ替えすると、
                # それまで使えていたデータまで消える
                result.errors.append(
                    note + f" ── {', '.join(lost_keys)} が無いので取り込みません")
                log.warning("%s: 鍵の列が無いため取り込みを見送りました: %s",
                            table, lost_keys)
                notify(step_pct, step_message, ok=False)
                continue
            result.errors.append(note + " ── その列は空で取り込みます")
            log.warning("%s: 元に無い列: %s", table, missing)
            notify(step_pct, step_message, ok=False)

        columns = [c[0] for c in spec]
        placeholders = ", ".join("?" for _ in columns)
        col_list = ", ".join(f"[{c}]" for c in columns)
        keys = required.get(table, ())
        blanks = blank_is_missing.get(table, ())

        imported = skipped = 0
        try:
            with conn:
                conn.execute(f"DELETE FROM [{table}]")
                for row in rows:
                    values = {col: conv(row.get(src)) for col, src, conv in spec}
                    if any(values[k] is None for k in keys):
                        skipped += 1
                        continue
                    if any(str(values[k]).strip() == "" for k in blanks):
                        skipped += 1
                        continue
                    conn.execute(
                        f"INSERT INTO [{table}] ({col_list}) VALUES ({placeholders})",
                        [values[col] if values[col] is not None else fallbacks.get(conv)
                         for col, _src, conv in spec])
                    imported += 1
        except sqlite3.Error as exc:
            log.exception("%s: 取り込み中にエラー", table)
            result.errors.append(f"{table}: {exc}")
            notify(step_pct, step_message, ok=False)
            continue

        result.imported[table] = imported
        if skipped:
            result.skipped[table] = skipped
        log.info("%s: %s件取り込み(%s件スキップ)", table, imported, skipped)
    notify(end_pct, "")
    return result


def import_master(conn: sqlite3.Connection, source_path: Optional[Path] = None,
                  *, kanban_path: Optional[Path] = None,
                  progress: Optional[Progress] = None,
                  progress_range: tuple[int, int] = (0, 100)) -> ImportResult:
    """梱包資材マスタを取り込む。パスを省略すると設定のフォルダから探す。

    取り込みは総入れ替えなので、**まだ送っていない行が
    消えてしまわないように**、書き戻し対象のテーブルは先に送ってから
    読み直す。送れなかったときはそのテーブルだけ取り込みを見送る
    (取り込み元が正となるが、こちらの未送信分を失うほうが困る)。

    【看板マスタは別ファイル】
    `import_specs.KANBAN_TABLES`(Form状態管理・看板_*)は梱包資材マスタ
    ではなく看板マスタ.sqlite3(`kanban_path` 省略時は
    `find_kanban_db()`)から読む。見つからなければテーブルごとに8件の
    エラーを出すのではなく、案内を1件だけ `notes` に足す
    (アクセス権限と違い機能そのものが任意なわけではないが、
    分けたばかりで置き場所が未設定の端末が多いうちは
    「毎回失敗」に見せないため)。
    """
    path = source_path or find_material_db()
    if path is None:
        result = ImportResult()
        result.errors.append(
            f"梱包資材マスタが見つかりません。{config.master_db_dir()} に "
            f"{config.MATERIAL_DB_NAME} を置いてください。")
        return result

    log.info("マスタ取り込み開始: %s", path)
    specs = {t: s for t, s in import_specs.IMPORT_SPECS.items()
             if t not in import_specs.KANBAN_TABLES}
    kanban_specs = {t: s for t, s in import_specs.IMPORT_SPECS.items()
                    if t in import_specs.KANBAN_TABLES}
    result = ImportResult()

    unsent = _unsent_writeback_tables(conn)
    if unsent:
        flushed = write_back(conn, path)
        unsent = _unsent_writeback_tables(conn)
        for table in unsent:
            specs.pop(table, None)
            reason = flushed.skipped_reason or "取り込み元へ送れなかった行があります"
            result.errors.append(
                f"{table}: 未送信の行が残っているため取り込みを見送りました"
                f"({reason})")
            log.warning("%s: 未送信 %s件のため総入れ替えを見送り", table, unsent[table])

    # 梱包資材マスタと看板マスタで進捗の帯を分ける(テーブル数の比で配分)
    start_pct, end_pct = progress_range
    split_pct = start_pct + (end_pct - start_pct) * len(specs) // max(
        len(specs) + len(kanban_specs), 1)

    result = import_tables(
        conn, path, specs,
        required=import_specs.REQUIRED_KEY_COLUMNS,
        blank_is_missing=import_specs.BLANK_IS_MISSING,
        optional=import_specs.OPTIONAL_TABLES,
        fallbacks=import_specs.NULL_FALLBACKS, result=result,
        progress=progress, progress_range=(start_pct, split_pct))

    kanban_source = kanban_path or find_kanban_db()
    if kanban_source is None:
        log.info("看板マスタが見つかりません(%s): %s",
                 config.kanban_db_dir(), config.KANBAN_DB_NAME)
        result.notes.append(
            f"{config.KANBAN_DB_NAME} が見つかりません(探した場所: "
            f"{config.kanban_db_dir()})。見つかると Form状態管理・"
            "看板_*(在庫薄警告)の8テーブルが取り込まれます。")
    else:
        log.info("看板マスタ取り込み: %s", kanban_source)
        result = import_tables(
            conn, kanban_source, kanban_specs,
            required=import_specs.REQUIRED_KEY_COLUMNS,
            blank_is_missing=import_specs.BLANK_IS_MISSING,
            fallbacks=import_specs.NULL_FALLBACKS, result=result,
            progress=progress, progress_range=(split_pct, end_pct))

    # **取り込んだ行は、取り込み元から来た行。** 送り返す必要はない。
    #
    # ここを飛ばすと取り込みのたびに倍になる。総入れ替えで管理番号が
    # 振り直されるのに同期記録は古い行IDのままなので、取り込んだ行が
    # どれも「未送信」に見え、次の取り込みの前の書き戻しが**新しい行と
    # してもう一度**送ってしまう(3件 → 6件 → 12件)。
    #
    # ここへ来た時点で、これらのテーブルに未送信の行は無い ── 上の
    # `unsent` の関門が、残っているテーブルを取り込みから外している。
    for spec in WRITEBACK_SPECS:
        if spec.sqlite_table in result.imported:
            outbox_sync.mark_all_sent(conn, spec)

    # **中身が同じ行がないか、読んだついでに数える。**
    # 取り込みのたびに倍になる不具合(〜VER2.2.0)は、現場からは見えて
    # いなかった。ここで数えておけば、同じ種類の事故を早く見つけられる
    for spec in WRITEBACK_SPECS:
        if spec.sqlite_table not in result.imported:
            continue
        same = duplicate_count(conn, spec.sqlite_table, spec.key_column)
        if same:
            result.notes.append(
                f"{spec.sqlite_table}: 中身が同じ行が {same}件 あります"
                "(scripts/dedupe_writeback.py で数える / 消せます)")

    # 取り込んだ直後に適合範囲を計算し直す。
    #
    # 取り込み元の値は `RunUpdatePalletAll` が走ったあとだと現物サイズより
    # 広がっていることがあり(上限のクランプが抜けているため)、そのまま
    # 使うと現物に載らないパレットが検索に乗ってしまう。こちらで計算し直せば
    # 取り込み元がどうなっていても手元の値は正しくなる。
    if "PalletMaster" in result.imported and result.imported["PalletMaster"]:
        from . import pallet_service
        summary = pallet_service.recompute_fit_ranges(conn)
        if not summary.ok:
            result.errors.append(f"適合範囲の再計算: {summary.error}")
        else:
            # 内訳まで残す。「取り込んだのに候補が出ない」の問い合わせで、
            # 何件がどう直ったのかをログだけで追えるようにする
            log.info("適合範囲を再計算しました(%s行): %s", summary.total,
                     " / ".join(f"{name}={count}"
                                for name, count in summary.counts()))
    return result


def duplicate_count(conn: sqlite3.Connection, table: str,
                    key_column: str) -> int:
    """**中身が同じ行**の数(1件目は数えない)。

    比べないのは「送るときに振られる番号」だけ ── 管理番号と送信ID。
    それ以外の列が全部一致していれば、同じ行が2回入っているとみなす。
    """
    try:
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info([{table}])")]
    except sqlite3.Error:                         # pragma: no cover
        return 0
    compare = [c for c in columns if c not in (key_column, "送信ID")]
    if not compare:
        return 0
    names = ", ".join(f"[{c}]" for c in compare)
    try:
        row = conn.execute(
            f"SELECT COALESCE(SUM(n - 1), 0) FROM"
            f" (SELECT COUNT(*) AS n FROM [{table}]"
            f"  GROUP BY {names} HAVING n > 1)").fetchone()
    except sqlite3.Error:                         # pragma: no cover
        return 0
    return int(row[0] or 0)


def _unsent_writeback_tables(conn: sqlite3.Connection) -> dict[str, int]:
    """書き戻し対象で、まだ送っていない行が残っているテーブル。"""
    return outbox_sync.unsent_tables(conn, WRITEBACK_SPECS)


def import_lot_ledger(conn: sqlite3.Connection, directory: Optional[Path] = None,
                      *, progress: Optional[Progress] = None,
                      progress_range: tuple[int, int] = (0, 100)) -> ImportResult:
    """仕掛台帳(SIKALOT/SIKAHIKI/SIKAODR)を取り込む。

    3ファイルともテーブル名は「仕掛」なので、1ファイルずつ対応する
    SQLiteテーブルへ入れる。見つからないファイルは飛ばす
    (共有に届かない端末でも、届いた分だけは最新になる)。
    """
    result = ImportResult()
    found = find_lot_dbs(directory)
    if not found:
        result.errors.append(
            f"仕掛台帳が見つかりません。{config.lot_db_dir()} または"
            f" {config.master_db_dir()} に3ファイルを置いてください。")
        return result

    start_pct, end_pct = progress_range
    span = max(end_pct - start_pct, 0)
    for index, (table, path) in enumerate(found.items()):
        log.info("仕掛台帳 取り込み: %s → %s", path.name, table)
        import_tables(
            conn, path, {table: import_specs.LOT_IMPORT_SPECS[table]},
            source_table=import_specs.LOT_SOURCE_TABLE,
            required=import_specs.REQUIRED_KEY_COLUMNS,
            blank_is_missing=import_specs.BLANK_IS_MISSING,
            fallbacks=import_specs.NULL_FALLBACKS,
            result=result, progress=progress,
            progress_range=(start_pct + span * index // len(found),
                            start_pct + span * (index + 1) // len(found)))
    missing = set(config.LOT_DB_FILES) - set(found)
    for table in missing:
        result.errors.append(
            f"{config.LOT_DB_FILES[table]} が見つからないため {table} は更新していません")
    return result


def import_all(conn: sqlite3.Connection,
               *, progress: Optional[Progress] = None) -> ImportResult:
    """マスタと仕掛台帳をまとめて取り込む(画面の「取り込み」ボタン用)。

    片方が見つからなくてももう片方は取り込む。どちらが入って
    どちらが入らなかったかは結果のまとめに出る。
    """
    # マスタは件数が多いので前半、仕掛台帳を後半に割り当てる
    result = import_master(conn, progress=progress, progress_range=(0, 50))
    result.merge(import_lot_ledger(conn, progress=progress, progress_range=(50, 100)))
    return result


# ==================================================================
# 書き戻し (手元 → 取り込み元)
# ==================================================================
# 行の予約・送信ID発行・二重登録防止・再送判定は、業務に依存しない
# `outbox_sync` に任せている。このファイルに残すのは「取り込み元を
# どう探すか」「接続できないときの日本語メッセージ」といった、この
# ツール固有の事情だけ。
def write_back(conn: sqlite3.Connection,
               source_path: Optional[Path] = None) -> WriteBackResult:
    """手元で増えた行を取り込み元へ送る(倉庫発注・入出庫履歴)。

    取り込み元に届かない端末では**何もせずに理由だけ返す**。手元の登録は
    すでに済んでいるので現場は止まらず、次に繋がったときにまとめて送られる。
    """
    result = WriteBackResult()

    path = source_path or find_material_db()
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
        result = outbox_sync.write_back(conn, source, WRITEBACK_SPECS)
    finally:
        source.close()
    return result


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


# ==================================================================
# 起動時の自動取り込み
# ==================================================================
STAMP_TABLE = "Access取込記録"


def _ensure_stamp_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS [{STAMP_TABLE}] ("
        " ファイル   TEXT PRIMARY KEY,"
        " 更新時刻   REAL NOT NULL,"
        " 取込日時   TEXT NOT NULL DEFAULT (datetime('now','localtime')))")
    conn.commit()


def needs_import(conn: sqlite3.Connection, path: Path) -> bool:
    """元ファイルが前回の取り込み以降に更新されているか。

    毎回の起動で1万件超を読み直すのは無駄なので、更新されたファイルだけ
    取り込む。仕掛台帳は日々更新されるので結果的にほぼ毎回読み、
    マスタは変わったときだけ読む、という自然な動きになる。
    """
    _ensure_stamp_table(conn)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    row = conn.execute(
        f"SELECT 更新時刻 FROM [{STAMP_TABLE}] WHERE ファイル = ?",
        (str(path),)).fetchone()
    return row is None or mtime > float(row[0]) + 1.0


def mark_imported(conn: sqlite3.Connection, path: Path) -> None:
    _ensure_stamp_table(conn)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO [{STAMP_TABLE}] (ファイル, 更新時刻)"
            " VALUES (?, ?)", (str(path), mtime))


def auto_import(conn: sqlite3.Connection, *, force: bool = False,
                progress: Optional[Progress] = None) -> ImportResult:
    """起動時に呼ぶ取り込み。更新されたファイルだけを読む。

    見つからないファイルや読めない環境では**黙って何もしない**。
    起動のたびに警告を出しても現場の役に立たないので、状態は
    設定画面で確認してもらう。
    """
    result = ImportResult()

    master = find_material_db()
    if master is not None and (force or needs_import(conn, master)):
        log.info("自動取り込み(マスタ): %s", master)
        result.merge(import_master(conn, master, progress=progress,
                                   progress_range=(0, 50)))
        mark_imported(conn, master)

    for table, path in find_lot_dbs().items():
        if not force and not needs_import(conn, path):
            continue
        log.info("自動取り込み(仕掛台帳): %s", path)
        import_tables(
            conn, path, {table: import_specs.LOT_IMPORT_SPECS[table]},
            source_table=import_specs.LOT_SOURCE_TABLE,
            required=import_specs.REQUIRED_KEY_COLUMNS,
            blank_is_missing=import_specs.BLANK_IS_MISSING,
            fallbacks=import_specs.NULL_FALLBACKS, result=result,
            progress=progress, progress_range=(50, 100))
        mark_imported(conn, path)

    return result


def auto_import_in_background(
        on_done: Optional[Callable[[ImportResult], None]] = None,
        progress: Optional[Progress] = None) -> bool:
    """起動直後に呼ぶ。画面を待たせずに裏で取り込む。

    SQLiteの接続はスレッドをまたげないので、作業スレッド側で開き直す。
    終わったら `on_done` を呼ぶので、呼び出し側は
    (tkinterなら `after` 経由で)画面を更新する。

    戻り値は**取り込みを始めたか**。設定でOFFのときは False を返し、
    `on_done` も呼ばない。呼び出し側は進捗表示を出す前にこれを見る
    (出しっぱなしで終わらないようにするため)。
    失敗しても `on_done` は必ず呼ぶ(進捗表示を確実に閉じるため)。
    """
    from . import user_settings
    if not user_settings.get(config.KEY_AUTO_IMPORT, config.AUTO_IMPORT_DEFAULT):
        log.info("起動時の自動取り込みは設定でOFFになっています")
        return False

    def runner() -> None:
        result = ImportResult()
        try:
            with db.connect() as conn:
                db.apply_schema(conn)
                result.merge(auto_import(conn, progress=progress))
            if result.total:
                log.info("起動時の自動取り込み: %s件", result.total)
        except Exception as exc:               # noqa: BLE001 - 起動を止めない
            log.exception("起動時の自動取り込みで例外(手動で取り込めます)")
            result.errors.append(f"起動時の自動取り込み: {exc}")
        finally:
            if on_done is not None:
                on_done(result)

    threading.Thread(target=runner, daemon=True).start()
    return True


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
             f"読み取り方式: {backend_name()}"]

    material = find_material_db()
    lines.append(f"梱包資材マスタ: {material or '(起動フォルダに見つかりません)'}")
    lines.append(f"  探した場所: {config.master_db_dir()}")
    if material is not None:
        # 取り込みが空になる原因はたいてい「テーブル名が違う」なので、
        # そのファイルに実際どのテーブルがあるのかを出しておく。
        # 看板マスタ側のテーブル(KANBAN_TABLES)は別ファイルの持ち物
        # なので、ここには数えない(数えると毎回「不足」に見える)
        try:
            names = list_tables(material)
        except Exception:                  # noqa: BLE001 - 案内なので握る
            names = []
        if names:
            expect = [t for t in import_specs.IMPORT_SPECS
                      if t not in import_specs.KANBAN_TABLES]
            missing = [t for t in expect if t not in names]
            lines.append(f"  テーブル: {len(names)}個")
            if missing:
                lines.append(f"  ※取り込み対象なのに無い: {', '.join(missing)}")

    kanban = find_kanban_db()
    lines.append(f"看板マスタ: {kanban or '(見つかりません)'}")
    lines.append(f"  探した場所: {config.kanban_db_dir()}")
    if kanban is not None:
        try:
            names = list_tables(kanban)
        except Exception:                  # noqa: BLE001 - 案内なので握る
            names = []
        if names:
            missing = [t for t in import_specs.KANBAN_TABLES if t not in names]
            lines.append(f"  テーブル: {len(names)}個")
            if missing:
                lines.append(f"  ※取り込み対象なのに無い: {', '.join(missing)}")

    lots = find_lot_dbs()
    lines.append(f"仕掛台帳: {len(lots)}/3 ファイル")
    for table, filename in config.LOT_DB_FILES.items():
        found = lots.get(table)
        lines.append(f"  {filename}: {found or '(見つかりません)'}")
    lines.append(f"  探した場所: {config.lot_db_dir()} → {config.master_db_dir()}")

    lines.append(f"書き戻し先: {material or '(見つかりません)'}")
    return "\n".join(lines)
