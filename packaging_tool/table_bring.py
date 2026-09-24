"""表だけ持ってくる(Access で作った表を、梱包資材マスタへ足す)

【なぜ要るのか】
表を作るのは Access のほうが向いている(列の型や主キーを画面で決められ、
中身もその場で確かめられる)。ところが、表を1つ足すたびに
「Access → sqlite3 を丸ごと変換 → 差し替え」をすると、

    このツールが sqlite3 に書いた行(倉庫発注・入出庫履歴・パレット実績)
    のうち Access に無いものが消える。ツール側は「送り済み」と覚えて
    いるので送り直さない。送信IDの一意インデックスも消える

ということが、差し替えのたびに黙って起きる。

そこで、変換したファイルから **いまの梱包資材マスタに無い表だけ** を
写す。今ある表・行・索引には触らないので、ツールが書いた分は消えない。

【守っていること】
- **無い表しか作らない。** 同じ名前の表があれば断る(上書きも合流もしない)
- 1つの表は **作る+中身を入れる を1回で確定** する。途中で落ちたら何も残らない
- 書く前に、梱包資材マスタを手元の `backup` へ写しておく
- 何をしたかは画面と取り込みの記録(`import_diag`)に出す
"""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import import_diag, source_db, sync_sources
from .logging_utils import get_logger

log = get_logger("table_bring")

REFUSE_NO_FILE = "no_file"
REFUSE_NO_DEST = "no_dest"
REFUSE_SAME_FILE = "same_file"
REFUSE_NOTHING = "nothing"
REFUSE_EXISTS = "exists"
REFUSE_WRITE_FAILED = "write_failed"


@dataclass
class Candidate:
    """持ってくるファイルの中の表1つ。"""

    name: str
    rows: int = 0
    columns: list[str] = field(default_factory=list)
    exists: bool = False          # 梱包資材マスタにもう同じ名前の表がある

    @property
    def can_bring(self) -> bool:
        return not self.exists


@dataclass
class Plan:
    """中を見た結果。持ってこられる表と、もうある表。"""

    source: str = ""
    dest: str = ""
    ok: bool = False
    message: str = ""
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def new_tables(self) -> list[Candidate]:
        return [c for c in self.candidates if c.can_bring]


@dataclass
class BringResult:
    ok: bool
    message: str
    reason: str = ""
    brought: list[tuple[str, int]] = field(default_factory=list)
    backup: str = ""


def _dest() -> Optional[Path]:
    return sync_sources.find_material_db()


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a) == str(b)


def plan(source_path: str) -> Plan:
    """持ってくるファイルの中を見る。**書かない。**"""
    out = Plan(source=str(source_path or "").strip())
    if not out.source:
        out.message = "持ってくるファイルを選んでください。"
        return out
    src = Path(out.source)
    if not src.is_file():
        out.message = f"ファイルが見つかりません: {src}"
        return out
    dest = _dest()
    if dest is None:
        out.message = "梱包資材マスタが見つかりません。「取り込み元」で置き場所を確かめてください。"
        return out
    out.dest = str(dest)
    if _same(src, dest):
        out.message = ("選んだのは、いま使っている梱包資材マスタそのものです。"
                       "Access から変換した別のファイルを選んでください。")
        return out
    try:
        names = source_db.list_tables(src)
        existing = set(source_db.list_tables(dest))
        counts = source_db.table_counts(src)
    except (source_db.SourceError, sqlite3.Error) as exc:
        out.message = f"ファイルを読めません: {exc}"
        return out
    for name in names:
        out.candidates.append(Candidate(
            name=name, rows=max(counts.get(name, 0), 0),
            columns=source_db.columns(src, name), exists=name in existing))
    out.ok = True
    new = len(out.new_tables)
    out.message = (f"{len(names)}表のうち、梱包資材マスタに無い表が {new} 個あります。"
                   if new else
                   f"{len(names)}表とも、梱包資材マスタにもうあります。持ってくる表はありません。")
    return out


def _create_sql(src: Path, table: str) -> tuple[str, list[tuple[str, str]]]:
    """元の表の `CREATE TABLE` と、その表の索引(名前, `CREATE INDEX`)。

    型・主キー・既定値を元のとおりに作るため、定義文をそのまま使う。
    """
    rows = source_db.read_query(
        src, "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = ?"
             " AND sql IS NOT NULL", [table])
    create = next((r["sql"] for r in rows if r["type"] == "table"), "")
    indexes = [(r["name"], r["sql"]) for r in rows if r["type"] == "index"]
    if not create:
        raise source_db.SourceError(f"{table} の定義を読めません")
    return create, indexes


def _backup(dest: Path) -> Path:
    """書く前に梱包資材マスタを手元へ写す。戻したいときの頼り。"""
    from . import app_config
    folder = app_config.local_dir("backup")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{dest.stem}_表を持ってくる前_{datetime.now():%Y%m%d_%H%M%S}{dest.suffix}"
    shutil.copyfile(dest, target)
    return target


def bring(source_path: str, tables: list[str]) -> BringResult:
    """選んだ表を梱包資材マスタへ作り、中身を写す。**無い表だけ。**"""
    src = Path(str(source_path or "").strip())
    if not src.is_file():
        return BringResult(False, f"ファイルが見つかりません: {src}", REFUSE_NO_FILE)
    dest = _dest()
    if dest is None:
        return BringResult(False, "梱包資材マスタが見つかりません。", REFUSE_NO_DEST)
    if _same(src, dest):
        return BringResult(False, "選んだのは、いま使っている梱包資材マスタそのものです。",
                           REFUSE_SAME_FILE)
    wanted = [t for t in dict.fromkeys(str(t) for t in tables) if t]
    if not wanted:
        return BringResult(False, "持ってくる表を選んでください。", REFUSE_NOTHING)

    available = set(source_db.list_tables(src))
    missing = [t for t in wanted if t not in available]
    if missing:
        return BringResult(False, f"選んだファイルに無い表です: {', '.join(missing)}",
                           REFUSE_NOTHING)
    existing = set(source_db.list_tables(dest))
    already = [t for t in wanted if t in existing]
    if already:
        # **上書きも合流もしない。** 今ある表には、このツールが書いた行が
        # 入っているかもしれない
        return BringResult(False, f"梱包資材マスタにもうある表は持ってきません: "
                                  f"{', '.join(already)}", REFUSE_EXISTS)

    try:
        backup = _backup(dest)
    except OSError as exc:
        return BringResult(False, f"書く前の控えを取れませんでした({exc})。"
                                  "何も変えていません。", REFUSE_WRITE_FAILED)

    import_diag.write("=" * 70)
    import_diag.write(f"■ 表を持ってくる  {src} → {dest}")
    import_diag.write(f"  書く前の控え: {backup}")
    brought: list[tuple[str, int]] = []
    failed: list[str] = []
    with source_db.connect(dest) as dst:
        for table in wanted:
            try:
                create, indexes = _create_sql(src, table)
                rows = source_db.read_table(src, table)
                skipped_idx: list[str] = []
                with dst.transaction() as tx:
                    if tx.query("SELECT 1 FROM sqlite_master WHERE type = 'table'"
                                " AND name = ?", [table]):
                        raise source_db.SourceError("ほかの端末が先に作りました")
                    tx.execute(create)
                    for row in rows:
                        tx.insert(table, row)
                    # 索引の名前はファイルの中で1つしか使えない。梱包資材マスタに
                    # 同じ名前があるものは作らない(表は持ってくる)
                    for name, sql in indexes:
                        if tx.query("SELECT 1 FROM sqlite_master WHERE name = ?", [name]):
                            skipped_idx.append(name)
                            continue
                        tx.execute(sql)
                brought.append((table, len(rows)))
                made = len(indexes) - len(skipped_idx)
                import_diag.write(f"  持ってきた: {table} ({len(rows):,}行"
                                  + (f"・索引{made}" if made else "") + ")")
                if skipped_idx:
                    import_diag.write(f"    同じ名前があるので作らなかった索引: "
                                      f"{', '.join(skipped_idx)}")
                log.info("表を持ってきました: %s %s行 (%s → %s)", table, len(rows), src, dest)
            except (source_db.SourceError, sqlite3.Error) as exc:
                failed.append(f"{table}({exc})")
                import_diag.write(f"  ✕ 持ってこられませんでした: {table} ── {exc}")
                log.warning("表を持ってこられませんでした: %s: %s", table, exc)

    done = "、".join(f"{t}({n:,}行)" for t, n in brought)
    if failed and not brought:
        return BringResult(False, "持ってこられませんでした: " + "、".join(failed)
                           + "。梱包資材マスタは変えていません。",
                           REFUSE_WRITE_FAILED, backup=str(backup))
    message = f"梱包資材マスタに {done} を足しました。今ある表には触っていません。"
    if failed:
        message += " ただし次は持ってこられませんでした: " + "、".join(failed)
    return BringResult(not failed, message, "" if not failed else REFUSE_WRITE_FAILED,
                       brought=brought, backup=str(backup))


def plan_dict(p: Plan) -> dict[str, Any]:
    return {
        "source": p.source, "dest": p.dest, "ok": p.ok, "message": p.message,
        "tables": [{"name": c.name, "rows": c.rows, "columns": c.columns,
                    "exists": c.exists} for c in p.candidates],
    }
