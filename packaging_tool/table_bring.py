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

【Access のままでも選べる】
.accdb / .mdb を選んだときは、現場でいつも使っている変換ツール
(`accdb_converter`)を**そのまま**呼んで sqlite3 にしてから読む。
変換を自前で持たないのは、型や中身の扱いがいつもの変換と食い違うと
「持ってきたら型が違った」が起きるため。変換ツールの読み取り部品
(pyodbc / access_parser)をこのツールの中へ読み込まないよう、別の
プロセスで動かす(`_convert`)。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from . import config, import_diag, source_db, sync_sources
from .logging_utils import get_logger

log = get_logger("table_bring")

REFUSE_NO_FILE = "no_file"
REFUSE_NO_DEST = "no_dest"
REFUSE_SAME_FILE = "same_file"
REFUSE_NOTHING = "nothing"
REFUSE_EXISTS = "exists"
REFUSE_WRITE_FAILED = "write_failed"
REFUSE_CONVERT = "convert_failed"

# Access のファイル。これを選んだら変換ツールで sqlite3 にしてから読む
ACCESS_SUFFIXES = (".accdb", ".mdb")
# 変換ツールに要るファイル(`engine.read_source` と `writers.write_sqlite`)
CONVERTER_FILES = ("engine.py", "writers.py")
# 変換にかける時間の上限。大きい Access でも数十秒で終わる
CONVERT_TIMEOUT_SEC = 600


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
    converted: str = ""           # Access を変換して読んだとき、その旨(読み取り方式)
    converter: str = ""           # 使った(使う)変換ツールのフォルダ

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


# Access が自分のために持っている表。**持ってこない。**
# 読み取りに access_parser を使うと、pyodbc と違ってこれらも出てくる
# (MSysObjects などのシステム表、添付ファイルの中身を持つ f_<32桁>_Data)
_ACCESS_INTERNAL = re.compile(r"^(MSys|f_[0-9A-Fa-f]{32}_)")


def is_access_internal(name: str) -> bool:
    return bool(_ACCESS_INTERNAL.match(name))


class ConvertError(Exception):
    """Access を sqlite3 にできなかった。文は画面にそのまま出す。"""


def is_access(path: Path) -> bool:
    return Path(path).suffix.lower() in ACCESS_SUFFIXES


def _looks_like_converter(folder: Path) -> bool:
    return all((folder / name).is_file() for name in CONVERTER_FILES)


def converter_dir() -> Optional[Path]:
    """変換ツールのフォルダ。設定にあればそれ、無ければよくある置き場所を探す。

    よくある置き場所 = このツールと同じ階層の `accdb_converter`、
    またはこのツールの中の `accdb_converter`。
    """
    from . import user_settings
    saved = str(user_settings.get(config.KEY_CONVERTER_DIR, "") or "").strip()
    if saved:
        folder = Path(saved)
        return folder if _looks_like_converter(folder) else None
    for folder in (config.BASE_DIR.parent / "accdb_converter",
                   config.BASE_DIR / "accdb_converter"):
        if _looks_like_converter(folder):
            return folder
    return None


def set_converter_dir(folder_text: str) -> str:
    """変換ツールの場所を覚える。**違っていれば覚えずに理由を返す。**"""
    from . import user_settings
    folder = Path(str(folder_text or "").strip())
    if not str(folder_text or "").strip():
        user_settings.save(config.KEY_CONVERTER_DIR, "")
        return ""
    if not _looks_like_converter(folder):
        return (f"{folder} は変換ツールのフォルダではないようです"
                f"({'・'.join(CONVERTER_FILES)} が見つかりません)。")
    user_settings.save(config.KEY_CONVERTER_DIR, str(folder))
    return ""


# 変換の中身。変換ツールの部品をそのまま使う(`engine.read_source` →
# `writers.write_sqlite`)。結果は1行の JSON で返す
_CONVERT_SCRIPT = (
    "import sys, json\n"
    "folder, src, out = sys.argv[1:4]\n"
    "sys.path.insert(0, folder)\n"
    "import engine, writers\n"
    "tables, desc = engine.read_source(src)\n"
    "writers.write_sqlite(tables, out)\n"
    "print(json.dumps({'engine': desc, 'tables': len(tables)}, ensure_ascii=False))\n"
)

# 同じ Access を何度も変換しない(中を見る → 持ってくる で2回呼ばれる)。
# 鍵はファイルの場所と姿(大きさ・更新時刻)。変わっていたら変換し直す
_converted_cache: dict[str, tuple[tuple[int, int], Path, str]] = {}


def _pythons() -> list[list[str]]:
    """変換を動かす Python。まずこのツールと同じもの、だめなら PATH の py / python。

    変換ツールの読み取り部品(pyodbc か access_parser)は、変換ツールの
    `start.bat` が使う Python に入っている。たいていこのツールと同じだが、
    違うときのために PATH のものも試す。
    """
    found = [[sys.executable]]
    for cmd in (["py", "-3"], ["python"]):
        exe = shutil.which(cmd[0])
        if exe and not any(Path(exe) == Path(f[0]) for f in found):
            found.append([exe, *cmd[1:]])
    return found


def _convert(src: Path) -> tuple[Path, str]:
    """Access を sqlite3 にする。返すのは (作ったファイル, 読み取り方式)。"""
    stat = src.stat()
    stamp = (stat.st_size, stat.st_mtime_ns)
    cached = _converted_cache.get(str(src))
    if cached and cached[0] == stamp and cached[1].is_file():
        return cached[1], cached[2]

    folder = converter_dir()
    if folder is None:
        raise ConvertError(
            "Access のまま読むには、変換ツール(accdb_converter)の場所が要ります。"
            "下の「変換ツールの場所」に入れてください。")
    from . import app_config
    work = app_config.local_dir("work")
    work.mkdir(parents=True, exist_ok=True)
    out = work / f"表を持ってくる_{src.stem}_{datetime.now():%Y%m%d_%H%M%S}.sqlite3"

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    tried: list[str] = []
    no_engine = False
    for python in _pythons():
        try:
            done = subprocess.run(
                [*python, "-c", _CONVERT_SCRIPT, str(folder), str(src), str(out)],
                cwd=str(folder), env=env, capture_output=True,
                timeout=CONVERT_TIMEOUT_SEC)
        except (OSError, subprocess.TimeoutExpired) as exc:
            tried.append(f"{' '.join(python)}: {exc}")
            continue
        stdout = done.stdout.decode("utf-8", "replace").strip()
        stderr = done.stderr.decode("utf-8", "replace").strip()
        if done.returncode == 0 and out.is_file():
            info: dict[str, Any] = {}
            try:
                info = json.loads(stdout.splitlines()[-1]) if stdout else {}
            except ValueError:
                pass
            engine = str(info.get("engine", ""))
            if cached:
                Path(cached[1]).unlink(missing_ok=True)
            _converted_cache[str(src)] = (stamp, out, engine)
            log.info("Access を変換しました: %s → %s (%s)", src, out, engine)
            return out, engine
        last = stderr.splitlines()[-1] if stderr else f"終了コード {done.returncode}"
        tried.append(f"{' '.join(python)}: {last}")
        # 読み取り部品が入っていない Python なら次を試す。それ以外は中身の問題
        no_engine = "読み取りエンジン" in stderr or "ModuleNotFoundError" in stderr
        if not no_engine:
            break
    out.unlink(missing_ok=True)
    if no_engine:
        raise ConvertError(
            "Access を読む部品がこのPCの Python に入っていません"
            "(pyodbc + Access のドライバ、または access_parser のどちらかが要ります)。"
            "変換ツールの start_debug.bat で確かめられます。"
            f"詳しく: {' / '.join(tried[-2:])}")
    raise ConvertError("Access を変換できませんでした。" + " / ".join(tried[-2:]))


def _readable(src: Path) -> tuple[Path, str]:
    """読む実体。Access なら変換したもの、sqlite3 ならそのもの。"""
    if is_access(src):
        return _convert(src)
    return src, ""


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
    folder = converter_dir()
    out.converter = str(folder) if folder else ""
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
        readable, engine = _readable(src)
    except ConvertError as exc:
        out.message = str(exc)
        return out
    if is_access(src):
        out.converted = (f"Access を変換して読みました(変換ツール: {folder}"
                         + (f"、読み取り: {engine}" if engine else "") + ")")
    try:
        names = source_db.list_tables(readable)
        existing = set(source_db.list_tables(dest))
        counts = source_db.table_counts(readable)
    except (source_db.SourceError, sqlite3.Error) as exc:
        out.message = f"ファイルを読めません: {exc}"
        return out
    internal = [n for n in names if is_access_internal(n)]
    names = [n for n in names if not is_access_internal(n)]
    for name in names:
        out.candidates.append(Candidate(
            name=name, rows=max(counts.get(name, 0), 0),
            columns=source_db.columns(readable, name), exists=name in existing))
    out.ok = True
    new = len(out.new_tables)
    out.message = (f"{len(names)}表のうち、梱包資材マスタに無い表が {new} 個あります。"
                   if new else
                   f"{len(names)}表とも、梱包資材マスタにもうあります。持ってくる表はありません。")
    if internal:
        out.message += f"(Access の内部の表 {len(internal)} 個は出していません)"
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

    original = src
    try:
        src, _engine = _readable(src)
    except ConvertError as exc:
        return BringResult(False, str(exc), REFUSE_CONVERT)

    internal = [t for t in wanted if is_access_internal(t)]
    if internal:
        return BringResult(False, f"Access の内部の表は持ってきません: {', '.join(internal)}",
                           REFUSE_NOTHING)
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
    import_diag.write(f"■ 表を持ってくる  {original} → {dest}")
    if original != src:
        import_diag.write(f"  Access を変換して読みました: {src}")
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
        "converted": p.converted, "converter": p.converter,
        "access": is_access(Path(p.source)) if p.source else False,
        "tables": [{"name": c.name, "rows": c.rows, "columns": c.columns,
                    "exists": c.exists} for c in p.candidates],
    }
