"""取り込み (取り込み元 → 手元) ── **総入れ替え**

手元のDBは取り込み元の写しで、取り込みは1テーブルまるごとの入れ替え。
だから**手元を直しても次の取り込みで消える** ── 直すときは取り込み元を
直す(`master_admin`)。

送っていない書き戻しがある表は、入れ替える前に送る。順番を逆にすると
手元の未送信分が消える。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config, db, import_diag, import_specs, outbox_sync, source_db
from .logging_utils import get_logger
# **他の段は名前ではなくモジュールで呼ぶ。** こうしておくと、差し替え
# (試験の stub)の当て先が持ち主の1か所で済む ── 名前で取り込むと、
# 取り込んだ側それぞれに当てないと効かない
from . import sync_sources
from . import sync_writeback
from .sync_sources import Progress, SyncError, _noop_progress
from .sync_writeback import WRITEBACK_SPECS

log = get_logger("data_sync.import")

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
    # **取り込んだが、元のファイルに無い列があった**(その列は空で入っている)。
    # 以前は `errors` に入れていたので、12,509件入ったのに「失敗」「取り込め
    # ませんでした」と出て、どれが読めていないのか分からなかった(現場の声:
    # 「どれがよめてねぇの？」)。取り込み自体は済んでいるので失敗にはせず、
    # 何が空なのかを別の見出しで言う。鍵の列が無いときは取り込まないので、
    # そちらはこれまでどおり `errors`
    warnings: list[str] = field(default_factory=list)
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
        self.warnings.extend(other.warnings)
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
        if self.warnings:
            lines.append("")
            lines.append("取り込みましたが、元のファイルに無い列は空のままです"
                         "(取り込み元の変換で列が変わったのかもしれません):")
            lines.extend(f"  {w}" for w in self.warnings)
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
        # 診断記録用に、取り込む前の手元の姿を控える(`import_diag`)
        before = import_diag.local_count(conn, table)
        lots_before = (import_diag.lot_numbers(conn, table)
                       if table in import_specs.LOT_IMPORT_SPECS else None)
        try:
            rows = sync_sources.read_table(source_path, source_table or table)
        except SyncError as exc:
            if table in optional:
                # 無くてよい表。失敗として数えると、毎回「失敗」と出て
                # 本当に直すべき問題が埋もれる
                log.info("%s: 取り込み元にありません(任意)", table)
                result.missing_optional.append(table)
                import_diag.write(f"  [{table}] 取り込み元にありません(任意)")
                continue
            log.warning("%s: 読み取り失敗 %s", table, exc)
            result.errors.append(f"{table}: {exc}")
            import_diag.table_report(
                table, source_path, source_rows=0, missing=[], lacking_ok=[],
                extra=[], imported=None, skipped={}, skipped_samples=[],
                before=before, after=before, error=f"読めません: {exc}")
            # 段の文言(step_message)は変えず、いまの段が失敗したとだけ伝える。
            # そうしないと、レーンの各段は最後の段しか失敗を示せず、
            # 「取り込みは失敗したのに、どの段も完了のまま」に見える
            notify(step_pct, step_message, ok=False)
            continue
        if not rows:
            result.imported[table] = 0
            # **0行の表は手元を消さない**(総入れ替えしない)。診断に残す
            import_diag.write(f"  [{table}] ← {Path(source_path).name}  元が0行"
                              f"(手元の {before}件はそのまま)")
            continue

        # **元に無い列があれば先に言う。**
        # `row.get()` は無い列を None にするので、上流が列名を変えても
        # 取り込み自体は「成功」してしまい、中身だけが空になる。
        # 実際、仕掛ロットの列名が半角カナから全角に変わっていた写しで
        # 2,159件が**ロット番号だけ空**のまま入り、一覧が1件になった。
        present = set(rows[0].keys())
        may_lack = import_specs.OPTIONAL_COLUMNS.get(table, frozenset())
        missing = [src for _col, src, _conv in spec
                   if src not in present and src not in may_lack]
        lacking_ok = sorted(src for _col, src, _conv in spec
                            if src not in present and src in may_lack)
        if lacking_ok:
            log.info("%s: 元に無い列(無くても動く): %s", table, lacking_ok)
        wanted = {src for _col, src, _conv in spec}
        extra = sorted(present - wanted)

        def report(**kw: Any) -> None:
            import_diag.table_report(
                table, source_path, source_rows=len(rows), missing=missing,
                lacking_ok=lacking_ok, extra=extra, before=before,
                lots_before=lots_before, **kw)
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
                report(imported=None, skipped={}, skipped_samples=[], after=before,
                       error=f"鍵の列 {', '.join(lost_keys)} が無い(手元はそのまま)")
                continue
            result.warnings.append(note)
            log.warning("%s: 元に無い列: %s", table, missing)

        columns = [c[0] for c in spec]
        placeholders = ", ".join("?" for _ in columns)
        col_list = ", ".join(f"[{c}]" for c in columns)
        keys = required.get(table, ())
        blanks = blank_is_missing.get(table, ())

        imported = skipped = 0
        why_skipped = {"鍵が無い": 0, "鍵が空欄": 0}
        samples: list[str] = []
        try:
            with conn:
                conn.execute(f"DELETE FROM [{table}]")
                for number, row in enumerate(rows, start=1):
                    values = {col: conv(row.get(src)) for col, src, conv in spec}
                    lacking = [k for k in keys if values[k] is None]
                    blank = [k for k in blanks if str(values[k]).strip() == ""]
                    if lacking or blank:
                        skipped += 1
                        why_skipped["鍵が無い" if lacking else "鍵が空欄"] += 1
                        if len(samples) < import_diag.SAMPLE:
                            samples.append(f"{number}行目({','.join(lacking or blank)})")
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
            report(imported=None, skipped={}, skipped_samples=[],
                   after=import_diag.local_count(conn, table),
                   error=f"書き込み中のエラー: {exc}(手元は元のまま)")
            continue

        result.imported[table] = imported
        if skipped:
            result.skipped[table] = skipped
        report(imported=imported, skipped=why_skipped, skipped_samples=samples,
               after=import_diag.local_count(conn, table),
               lots_after=(import_diag.lot_numbers(conn, table)
                           if lots_before is not None else None))
        log.info("%s: %s件取り込み(%s件スキップ)", table, imported, skipped)
    notify(end_pct, "")
    return result


def _import_patterns(conn: sqlite3.Connection, path: Path,
                     result: ImportResult) -> None:
    """実績を取り込む。取り込み元に表がまだ無いのは失敗ではない。"""
    from . import pattern_sync
    outcome = pattern_sync.import_from(conn, path)
    if outcome.error:
        result.errors.append(f"{config.TBL_PT_HEADER}: {outcome.error}")
    elif outcome.skipped_reason:
        result.errors.append(
            f"{config.TBL_PT_HEADER}: 取り込みを見送りました({outcome.skipped_reason})")
    elif not outcome.missing:
        result.imported[config.TBL_PT_HEADER] = outcome.imported


def _import_master(conn: sqlite3.Connection, source_path: Optional[Path] = None,
                  *, kanban_path: Optional[Path] = None,
                  threshold_path: Optional[Path] = None,
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
    `sync_sources.find_kanban_db()`)から読む。見つからなければテーブルごとに8件の
    エラーを出すのではなく、案内を1件だけ `notes` に足す
    (アクセス権限と違い機能そのものが任意なわけではないが、
    分けたばかりで置き場所が未設定の端末が多いうちは
    「毎回失敗」に見せないため)。

    【パレット閾値マスタも別ファイル】
    `import_specs.THRESHOLD_TABLES`(PalletDakeThreshold ほか7表)は
    PalletThresholdMaster.sqlite3 から読む。移植元(VBA)が閾値だけを
    別ファイルに持っていて、現場の写しもその形で配られているため
    (現場の指摘:「取り込み元にパレット閾値の条件がないのでテーブルを
    読み込めていない」)。見つからないときの扱いは看板マスタと同じ。
    """
    path = source_path or sync_sources.find_material_db()
    import_diag.describe_file("梱包資材マスタ", path, [config.master_db_dir()])
    if path is not None:
        import_diag.write("    表と行数: " + _counts_text(path))
    if path is None:
        result = ImportResult()
        result.errors.append(sync_sources.material_db_missing_why())
        return result

    log.info("マスタ取り込み開始: %s", path)
    apart = import_specs.KANBAN_TABLES | frozenset(import_specs.THRESHOLD_TABLES)
    specs = {t: s for t, s in import_specs.IMPORT_SPECS.items()
             if t not in apart}
    kanban_specs = {t: s for t, s in import_specs.IMPORT_SPECS.items()
                    if t in import_specs.KANBAN_TABLES}
    threshold_specs = {t: s for t, s in import_specs.IMPORT_SPECS.items()
                       if t in import_specs.THRESHOLD_TABLES}
    result = ImportResult()

    unsent = sync_writeback._unsent_writeback_tables(conn)
    if unsent:
        flushed = sync_writeback.write_back(conn, path)
        unsent = sync_writeback._unsent_writeback_tables(conn)
        for table in unsent:
            specs.pop(table, None)
            reason = flushed.skipped_reason or "取り込み元へ送れなかった行があります"
            result.errors.append(
                f"{table}: 未送信の行が残っているため取り込みを見送りました"
                f"({reason})")
            log.warning("%s: 未送信 %s件のため総入れ替えを見送り", table, unsent[table])

    # 梱包資材マスタ / 看板マスタ / 閾値マスタで進捗の帯を分ける
    # (テーブル数の比で配分)
    start_pct, end_pct = progress_range
    total_tables = max(len(specs) + len(kanban_specs) + len(threshold_specs), 1)
    span = end_pct - start_pct
    split_pct = start_pct + span * len(specs) // total_tables
    split2_pct = split_pct + span * len(kanban_specs) // total_tables

    result = import_tables(
        conn, path, specs,
        required=import_specs.REQUIRED_KEY_COLUMNS,
        blank_is_missing=import_specs.BLANK_IS_MISSING,
        optional=import_specs.OPTIONAL_TABLES,
        fallbacks=import_specs.NULL_FALLBACKS, result=result,
        progress=progress, progress_range=(start_pct, split_pct))

    kanban_source = kanban_path or sync_sources.find_kanban_db()
    import_diag.describe_file("看板マスタ", kanban_source, [config.kanban_db_dir()])
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
            progress=progress, progress_range=(split_pct, split2_pct))

    threshold_source = threshold_path or sync_sources.find_threshold_db()
    import_diag.describe_file("パレット閾値マスタ", threshold_source,
                              [config.master_db_dir()])
    if threshold_source is None:
        log.info("パレット閾値マスタが見つかりません(%s): %s",
                 config.threshold_db_dir(), config.THRESHOLD_DB_NAME)
        result.notes.append(
            f"{config.THRESHOLD_DB_NAME} が見つかりません(探した場所: "
            f"{config.threshold_db_dir()})。見つかるとパレット適合閾値の"
            "7テーブルが取り込まれます。いまは手元に入れてある"
            "基準表の初期値で動いています。")
    else:
        log.info("パレット閾値マスタ取り込み: %s", threshold_source)
        result = import_tables(
            conn, threshold_source, threshold_specs,
            required=import_specs.REQUIRED_KEY_COLUMNS,
            blank_is_missing=import_specs.BLANK_IS_MISSING,
            fallbacks=import_specs.NULL_FALLBACKS, result=result,
            progress=progress, progress_range=(split2_pct, end_pct))

    # 実績(ヘッダ+明細)は専用の取り込み(`pattern_sync`)。送れていない
    # ものが残っていれば、上の関門と同じく入れ替えない
    if config.TBL_PT_HEADER not in unsent:
        _import_patterns(conn, path, result)

    # 二重登録の防止(送信IDの一意インデックス)。取り込み元が変換し直した
    # ファイルに差し替わると索引が消えるので、取り込むたびに確かめて作る
    # (`sync_writeback.ensure_guards`)。作れなくても取り込みは続ける
    try:
        guard_lines = sync_writeback.ensure_guards(path)
    except Exception as exc:                       # noqa: BLE001 - 取り込みは止めない
        log.exception("二重登録の防止を用意できませんでした")
        guard_lines = [f"二重登録の防止: 用意できませんでした({exc})"]
    for line in guard_lines:
        import_diag.write(f"  {line}")
        result.notes.append(line)

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

    # いまの姿で読んだ、と覚えておく。**起動直後の見張りが、取り込んだ
    # ばかりのものをもう一度取り込みに行かないため**
    sync_sources.note_source_read(path)
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

def _import_lot_ledger(conn: sqlite3.Connection, directory: Optional[Path] = None,
                      *, progress: Optional[Progress] = None,
                      progress_range: tuple[int, int] = (0, 100)) -> ImportResult:
    """仕掛台帳(SIKALOT/SIKAHIKI/SIKAODR)を取り込む。

    3ファイルともテーブル名は「仕掛」なので、1ファイルずつ対応する
    SQLiteテーブルへ入れる。見つからないファイルは飛ばす
    (共有に届かない端末でも、届いた分だけは最新になる)。
    """
    result = ImportResult()
    found = sync_sources.find_lot_dbs(directory)
    second = sync_sources.find_second_lot_dbs() if directory is None else {}
    searched = ([directory] if directory is not None
                else sync_sources.lot_search_dirs())
    for table, filename in config.LOT_DB_FILES.items():
        import_diag.describe_file(f"仕掛台帳 {filename}", found.get(table), searched)
    if not found:
        result.errors.append(
            f"仕掛台帳が見つかりません。{' / '.join(str(d) for d in searched)} の"
            "どこかに3ファイルを置いてください。")
        return result

    start_pct, end_pct = progress_range
    span = max(end_pct - start_pct, 0)
    for index, (table, path) in enumerate(found.items()):
        import_lot_table(
            conn, table, path, second.get(table), result=result, progress=progress,
            progress_range=(start_pct + span * index // len(found),
                            start_pct + span * (index + 1) // len(found)))
    missing = set(config.LOT_DB_FILES) - set(found)
    for table in missing:
        result.errors.append(
            f"{config.LOT_DB_FILES[table]} が見つからないため {table} は更新していません")
    return result


# 2つ目の置き場所から足すときの「同じもの」の見分け方(手元の列名)。
# ロットの表はロット番号、受注の表は受注番号。1つ目にあるものは1つ目を正とする
LOT_MERGE_KEYS: dict[str, str] = {
    "仕掛ロット": "ロット番号",
    "仕掛引当": "ロット番号",
    "仕掛受注": "受注番号",
}


# BOX最終実績寸法の候補(`仕掛ロット_2つ目`)。1つ目のロットで BOX最終実績_板厚・板幅・板丈 の
# どれかが空(取り込みで空欄は 0 になる)なら、2つ目の SIKALOT の同じロットの行を控える。
# 1つ目と2つ目の SIKALOT は**列の中身が違う**ことがあるので、取り込み元の列名でその行を読み、
# BOX最終実績_* に1つでも値があればそれ、無ければ BOX実績_* を使う。どちらも空なら控えない
# (参照パス2専用の SIKALOT には BOX実績_板厚・板幅・板丈 の列が無く、BOX最終実績_板丈 は 0)
BOX_CHOICE_TABLE = "仕掛ロット_2つ目"
BOX_DIMENSIONS = ("板厚", "板幅", "板丈")
BOX_CHOICE_SOURCES = ("BOX最終実績", "BOX実績")


def _same_file(a: Optional[Path], b: Optional[Path]) -> bool:
    if a is None or b is None:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


def import_lot_table(conn: sqlite3.Connection, table: str, path: Path,
                     second: Optional[Path] = None, *, result: ImportResult,
                     progress: Optional[Progress] = None,
                     progress_range: tuple[int, int] = (0, 100)) -> None:
    """仕掛台帳の1表を取り込む。2つ目の置き場所があれば、足りない分をそこから足す。

    1つ目のファイルが**読めなかった**ときは、2つ目のファイルを1つ目として読む
    (ファイルが無いときと同じ扱い。手元の古い中身に足すだけにしない)。
    """
    spec = import_specs.LOT_IMPORT_SPECS[table]
    log.info("仕掛台帳 取り込み: %s → %s", path.name, table)
    if table == "仕掛ロット":
        # 候補は1つ目・2つ目の今の中身から作り直す(前の取り込みの候補を残さない)
        with conn:
            conn.execute(f"DELETE FROM [{BOX_CHOICE_TABLE}]")
    mine = ImportResult()
    import_tables(
        conn, path, {table: spec},
        source_table=import_specs.LOT_SOURCE_TABLE,
        required=import_specs.REQUIRED_KEY_COLUMNS,
        blank_is_missing=import_specs.BLANK_IS_MISSING,
        fallbacks=import_specs.NULL_FALLBACKS,
        result=mine, progress=progress, progress_range=progress_range)
    if second is not None and not _same_file(second, path):
        if table not in mine.imported:
            log.warning("%s: 1つ目(%s)が読めないので、2つ目(%s)から読みます",
                        table, path, second)
            mine.warnings.append(f"{table}: 1つ目の置き場所のファイルが読めないため、"
                                 f"2つ目({second})から読みました")
            import_tables(
                conn, second, {table: spec},
                source_table=import_specs.LOT_SOURCE_TABLE,
                required=import_specs.REQUIRED_KEY_COLUMNS,
                blank_is_missing=import_specs.BLANK_IS_MISSING,
                fallbacks=import_specs.NULL_FALLBACKS, result=mine)
            if table in mine.imported:
                # 2つ目で読めた。1つ目の「読めません」は残さない(入ったのに失敗に見える)
                mine.errors = [e for e in mine.errors if not e.startswith(f"{table}:")]
        else:
            merge_second_lot(conn, table, second, mine)
    result.merge(mine)


def merge_second_lot(conn: sqlite3.Connection, table: str, path: Path,
                     result: ImportResult) -> int:
    """2つ目の置き場所のファイルから、**1つ目に無いもの**だけを手元に足す。足した行数。

    1つ目にファイルはあるが、目当てのロット(受注)が入っていないとき、2つ目にあれば
    それを使う(現場の声)。同じロット・受注が両方にあれば1つ目を正とする
    (2つ目の行は足さない)。読めなければ一言残して何もしない(1つ目の分は入っている)。

    1つ目にロットはあるが BOX最終実績の寸法が空のときは、2つ目の同じロットの行を**候補として
    控える**(`collect_box_choices`)。どれを使うかはロット情報の画面で選ぶ(勝手に埋めない)。
    """
    spec = import_specs.LOT_IMPORT_SPECS[table]
    key = LOT_MERGE_KEYS[table]
    try:
        rows = sync_sources.read_table(path, import_specs.LOT_SOURCE_TABLE)
    except SyncError as exc:
        result.warnings.append(f"{table}: 2つ目の置き場所のファイルを読めませんでした({exc})")
        import_diag.write(f"  [{table}] 2つ目 {path} を読めません: {exc}")
        return 0
    fallbacks = import_specs.NULL_FALLBACKS
    have = {str(r[0]) for r in conn.execute(f"SELECT DISTINCT [{key}] FROM [{table}]")}
    columns = [c[0] for c in spec]
    col_list = ", ".join(f"[{c}]" for c in columns)
    marks = ", ".join("?" for _ in columns)
    added = 0
    keys: set[str] = set()
    try:
        with conn:
            for row in rows:
                values = {col: conv(row.get(src)) for col, src, conv in spec}
                value = values.get(key)
                if value is None or str(value).strip() == "" or str(value) in have:
                    continue
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO [{table}] ({col_list}) VALUES ({marks})",
                    [values[col] if values[col] is not None else fallbacks.get(conv)
                     for col, _src, conv in spec])
                if cur.rowcount:
                    added += 1
                    keys.add(str(value))
    except sqlite3.Error as exc:
        log.exception("%s: 2つ目から足す途中でエラー", table)
        result.warnings.append(f"{table}: 2つ目の置き場所から足せませんでした({exc})")
        return 0
    if table == "仕掛ロット":
        collect_box_choices(conn, rows, result)
    if added:
        result.imported[table] = result.imported.get(table, 0) + added
        what = "受注" if key == "受注番号" else "ロット"
        result.notes.append(f"{table}: 1つ目に無い{what} {len(keys):,}件"
                            f"({added:,}行)を2つ目の置き場所から足しました")
    import_diag.write(f"  [{table}] ← 2つ目 {Path(path).name}({path.parent}) "
                      f"1つ目に無い分 {added}行 を足した")
    log.info("%s: 2つ目(%s)から %s行 足しました", table, path, added)
    return added


def collect_box_choices(conn: sqlite3.Connection, rows: list, result: ImportResult) -> int:
    """BOX最終実績寸法が空のロットについて、2つ目の SIKALOT の同じロットの行を候補に控える。

    控えた候補の数を返す。手元の `仕掛ロット`(1つ目 + 2つ目から足したロット)のうち、
    BOX最終実績_板厚・板幅・板丈 のどれかが 0 のロットだけが対象。同じ設備名・同じ寸法の
    行は1つにまとめる。
    """
    blank = " OR ".join(f"COALESCE([BOX最終実績_{d}], 0) <= 0" for d in BOX_DIMENSIONS)
    need = {str(r[0]) for r in conn.execute(
        f"SELECT DISTINCT [ロット番号] FROM [仕掛ロット] WHERE {blank}")}
    if not need:
        return 0
    lot_src = next(src for col, src, _conv in import_specs.LOT_IMPORT_SPECS["仕掛ロット"]
                   if col == "ロット番号")
    seen: set[tuple] = set()
    values: list[tuple] = []
    for row in rows:
        lot = import_specs.to_text(row.get(lot_src))
        if not lot or lot not in need:
            continue
        for source in BOX_CHOICE_SOURCES:
            dims = [import_specs.to_real(row.get(f"{source}_{d}")) or 0.0 for d in BOX_DIMENSIONS]
            # **どれか1つでも値があれば候補にする。** 2つ目の実データ(参照パス2専用)は
            # BOX最終実績_板丈 が全行 0 で、3つそろうのを待つと1件も出ない。0 は 0 のまま見せ、
            # 使えるか(資材展開に板幅・板丈が要る)は画面で分かるようにする
            if any(v > 0 for v in dims):
                break
        else:
            continue                      # この行には寸法が1つも無い
        equipment = import_specs.to_text(row.get("BOX設計_設備名")) or ""
        box_no = import_specs.to_text(row.get("BOX番号")) or ""
        key = (lot, equipment, *dims)
        if key in seen:
            continue
        seen.add(key)
        values.append((lot, box_no, equipment, *dims, source))
    if not values:
        return 0
    try:
        with conn:
            conn.executemany(
                f"INSERT INTO [{BOX_CHOICE_TABLE}] (ロット番号, BOX番号, BOX設計_設備名,"
                " 板厚, 板幅, 板丈, 出どころ) VALUES (?, ?, ?, ?, ?, ?, ?)", values)
    except sqlite3.Error as exc:
        log.exception("BOX最終実績の候補を控えられませんでした")
        result.warnings.append(f"仕掛ロット: 2つ目の BOX最終実績の候補を控えられませんでした({exc})")
        return 0
    lots = len({v[0] for v in values})
    result.notes.append(f"仕掛ロット: 1つ目で BOX最終実績(板厚・板幅・板丈)が空のロット {lots:,}件に、"
                        f"2つ目の置き場所の行 {len(values):,}件を候補として控えました"
                        "(ロット情報の画面で選べます)")
    import_diag.write(f"  [仕掛ロット] ← 2つ目 BOX最終実績が空のロット {lots}件 の候補 {len(values)}件")
    log.info("BOX最終実績が空のロット %s件 に2つ目の候補 %s件", lots, len(values))
    return len(values)


def _counts_text(path: Path) -> str:
    counts = source_db.table_counts(path)
    return ", ".join(f"{k} {v:,}" for k, v in counts.items()) or "(数えられません)"


def _with_diag(label: str, work: Callable[[], ImportResult]) -> ImportResult:
    """診断記録の見出しを付けて取り込む。いちばん外側なら記録の場所を知らせる。"""
    with import_diag.run(label) as outer:
        result = work()
        if outer:
            import_diag.write("  まとめ: " + result.summary().replace("\n", "\n    "))
            # ログフォルダは隠しフォルダの中なので、**画面から開ける道**を言う
            result.notes.append("取り込みの記録は、この下の「取り込みの記録」の「記録を見る」で"
                                f"開けます(ファイル: {import_diag.path_for()})")
    return result


def import_master(conn: sqlite3.Connection, source_path: Optional[Path] = None,
                  **kw: Any) -> ImportResult:
    """梱包資材マスタを取り込む(本体は `_import_master`。説明もそちら)。"""
    return _with_diag("マスタの取り込み",
                      lambda: _import_master(conn, source_path, **kw))


def import_lot_ledger(conn: sqlite3.Connection, directory: Optional[Path] = None,
                      **kw: Any) -> ImportResult:
    """仕掛台帳を取り込む(本体は `_import_lot_ledger`。説明もそちら)。"""
    return _with_diag("仕掛台帳の取り込み",
                      lambda: _import_lot_ledger(conn, directory, **kw))


def import_all(conn: sqlite3.Connection,
               *, progress: Optional[Progress] = None) -> ImportResult:
    """マスタと仕掛台帳をまとめて取り込む(画面の「取り込み」ボタン用)。

    片方が見つからなくてももう片方は取り込む。どちらが入って
    どちらが入らなかったかは結果のまとめに出る。
    """
    def work() -> ImportResult:
        # マスタは件数が多いので前半、仕掛台帳を後半に割り当てる
        result = import_master(conn, progress=progress, progress_range=(0, 50))
        result.merge(import_lot_ledger(conn, progress=progress,
                                       progress_range=(50, 100)))
        return result
    return _with_diag("まとめて取り込み", work)
