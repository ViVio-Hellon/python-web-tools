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

# もうある表にすること
ACTION_REFRESH = "refresh"        # 中身を入れ替える(Access にだけある列は足す)
ACTION_REBUILD = "rebuild"        # 作り直す(列が消えた・名前が変わった)

# 足した表の記録(梱包資材マスタの中)。マスタ管理が同じ名前で読む
from .master_common import BROUGHT_REGISTRY as REGISTRY  # noqa: E402

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
    suspect: int = 0              # 文字化けの疑いがある行(置き換え文字 U+FFFD を含む)
    # もうある表について ── 中身を入れ替えられるか(`refresh`)
    current_rows: int = 0         # 梱包資材マスタのいまの行数
    refresh_why: str = ""         # 入れ替えられない理由(入れ替えられるなら空)
    keeps: str = ""               # 入れ替えても残すもの(PalletMaster の在庫の行など)
    # 列の違い。Access にだけある列は足す、梱包資材マスタにだけある列が
    # あれば表を作り直す(`action`)
    added_columns: list[str] = field(default_factory=list)
    removed_columns: list[str] = field(default_factory=list)
    import_loses: list[str] = field(default_factory=list)  # 消える列のうち、取り込みで読む列

    @property
    def action(self) -> str:
        """もうある表にすること。"refresh"(入れ替える)か "rebuild"(作り直す)。"""
        if not self.exists:
            return ""
        return ACTION_REBUILD if self.removed_columns else ACTION_REFRESH

    @property
    def can_bring(self) -> bool:
        return not self.exists

    @property
    def can_refresh(self) -> bool:
        return self.exists and not self.refresh_why


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

    @property
    def refreshable(self) -> list[Candidate]:
        return [c for c in self.candidates if c.can_refresh]


@dataclass
class BringResult:
    ok: bool
    message: str
    reason: str = ""
    brought: list[tuple[str, int]] = field(default_factory=list)
    backup: str = ""
    # 中身を入れ替えた(作り直した)表: (表, 前の行数, 後の行数)
    refreshed: list[tuple[str, int, int]] = field(default_factory=list)
    # 作り直す前の表を残した名前: (表, 残した名前)
    kept_old: list[tuple[str, str]] = field(default_factory=list)


# Access が自分のために持っている表。**持ってこない。**
# 読み取りに access_parser を使うと、pyodbc と違ってこれらも出てくる
# (MSysObjects などのシステム表、添付ファイルの中身を持つ f_<32桁>_Data)
_ACCESS_INTERNAL = re.compile(r"^(MSys|f_[0-9A-Fa-f]{32}_)")


def is_access_internal(name: str) -> bool:
    return bool(_ACCESS_INTERNAL.match(name))


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _fold(name: str) -> str:
    """表の名前の比べ方。**sqlite3 と同じく英字の大文字・小文字だけを区別しない**
    ("Test" と "test" は同じ表。全角やほかの字はそのまま)。"""
    return name.translate(_ASCII_LOWER)


def _by_fold(names) -> dict[str, str]:
    """{比べ方の名前: 実際の名前}。"""
    return {_fold(n): n for n in names}


class ConvertError(Exception):
    """Access を sqlite3 にできなかった。文は画面にそのまま出す。"""


def is_access(path: Path) -> bool:
    return Path(path).suffix.lower() in ACCESS_SUFFIXES


def _looks_like_converter(folder: Path) -> bool:
    return all((folder / name).is_file() for name in CONVERTER_FILES)


# 同梱した変換ツールの読み取り部品(`vendor/accdb_converter/README.txt`)
BUNDLED_CONVERTER = config.BASE_DIR / "vendor" / "accdb_converter"


def converter_dir() -> Optional[Path]:
    """変換に使う部品のフォルダ。**ふだんは同梱のもの。**

    以前は変換ツールの置き場所を画面で指定させていた(現場の声:「変換ツールの
    場所って指定するってことはこのツールにコンバーターが入ったわけではない
    んですか？ 使いにくい」)。部品をこのツールに入れたので、指定は要らない。
    設定に場所が書いてあるときだけ、そちら(変換ツールを直した直後など)を使う。
    """
    from . import user_settings
    saved = str(user_settings.get(config.KEY_CONVERTER_DIR, "") or "").strip()
    if saved and _looks_like_converter(Path(saved)):
        return Path(saved)
    if _looks_like_converter(BUNDLED_CONVERTER):
        return BUNDLED_CONVERTER
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
            "Access を読む部品(vendor\\accdb_converter)が見つかりません。"
            "ツールのフォルダが欠けていないか確かめてください。")
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


def _suspect_rows(path: Path, table: str, columns: list[str]) -> int:
    """文字化けの疑いがある行の数。**置き換え文字(U+FFFD)を含む行**を数える。

    読み取りが access_parser のとき、まれに1行まるごと読み違えることがある
    (実物の梱包資材マスタ.accdb で PalletMaster 4,133行中1行。幅 1310 が
    「131」、丈 1800 が「Ｐㇾ〸」になった)。読み違えた欄には置き換え文字が
    混ざるので、それを目印にする。pyodbc で読んだ場合は起きない。
    """
    if not columns:
        return 0
    cond = " OR ".join(f"instr(CAST({source_db.quote_identifier(c)} AS TEXT),"
                       " char(65533)) > 0" for c in columns)
    try:
        rows = source_db.read_query(
            path, f"SELECT COUNT(*) AS n FROM {source_db.quote_identifier(table)}"
                  f" WHERE {cond}")
    except (source_db.SourceError, sqlite3.Error):
        return 0
    return int(rows[0]["n"]) if rows else 0


# ドラッグ&ドロップで受け取れるファイル
UPLOAD_SUFFIXES = ACCESS_SUFFIXES + source_db.SUFFIXES
# 受け取る大きさの上限。Access の上限(2GB)より手前で止める
UPLOAD_LIMIT_BYTES = 1024 * 1024 * 1024


def save_upload(filename: str, stream: Any) -> tuple[Optional[Path], str]:
    """ドロップされたファイルを手元の作業フォルダへ置く。(置いた場所, 断る理由)。

    名前はファイル名の部分だけを使う(フォルダを含む名前で外へ書かせない)。
    同じ名前が来たら置き換える ── 直して落とし直すのがふつうの使い方なので。
    """
    from . import app_config
    name = Path(str(filename).replace("\\", "/")).name
    if Path(name).suffix.lower() not in UPLOAD_SUFFIXES:
        return None, ("Access(.accdb / .mdb)か sqlite3(.sqlite3 / .db)の"
                      "ファイルを落としてください。")
    folder = app_config.local_dir("work") / "表を持ってくる"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    written = 0
    try:
        with open(target, "wb") as fh:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > UPLOAD_LIMIT_BYTES:
                    raise ValueError("大きすぎます")
                fh.write(chunk)
    except (OSError, ValueError) as exc:
        target.unlink(missing_ok=True)
        return None, f"ファイルを受け取れませんでした({exc})"
    _converted_cache.pop(str(target), None)      # 同じ名前で落とし直したら変換し直す
    log.info("持ってくるファイルを受け取りました: %s (%s バイト)", target, written)
    return target, ""


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
        out.converted = ("Access を変換して読みました"
                         + (f"(読み取り: {engine})" if engine else ""))
    try:
        names = source_db.list_tables(readable)
        existing = _by_fold(source_db.list_tables(dest))
        counts = source_db.table_counts(readable)
    except (source_db.SourceError, sqlite3.Error) as exc:
        out.message = f"ファイルを読めません: {exc}"
        return out
    internal = [n for n in names if is_access_internal(n)]
    names = [n for n in names if not is_access_internal(n) and n != REGISTRY]
    dest_counts = source_db.table_counts(dest)
    for name in names:
        columns = source_db.columns(readable, name)
        # 梱包資材マスタでの名前(大文字・小文字だけ違っても同じ表)
        here = existing.get(_fold(name))
        found = Candidate(
            name=name, rows=max(counts.get(name, 0), 0), columns=columns,
            exists=here is not None, suspect=_suspect_rows(readable, name, columns))
        if here is not None:
            found.current_rows = max(dest_counts.get(here, 0), 0)
            _compare_columns(found, source_db.columns(dest, here), here)
            found.keeps = KEEPS.get(here, "")
        out.candidates.append(found)
    out.ok = True
    new = len(out.new_tables)
    refresh = len([c for c in out.refreshable if c.action == ACTION_REFRESH])
    rebuild = len([c for c in out.refreshable if c.action == ACTION_REBUILD])
    out.message = (f"{len(names)}表のうち、梱包資材マスタに無い表が {new} 個"
                   f"、中身を Access の最新に入れ替えられる表が {refresh} 個")
    if rebuild:
        out.message += f"、列が変わったので作り直す表が {rebuild} 個"
    out.message += "あります。"
    if internal:
        out.message += f"(Access の内部の表 {len(internal)} 個は出していません)"
    suspects = [c for c in out.candidates if c.suspect]
    if suspects:
        out.message += (" ⚠ 文字化けの疑いがある行があります: "
                        + "、".join(f"{c.name} {c.suspect}行" for c in suspects)
                        + "。持ってくる前に Access の中身と見比べてください。")
    if "access_parser" in engine:
        # 予備の読み方。まれに行を読み違える(上の `_suspect_rows`)
        out.converted += (" ※ 予備の読み方(access_parser)で読みました。まれに行を"
                          "読み違えます。Access のドライバ(pyodbc)が使えるPCなら正確です")
    return out


def _create_sql(src: Path, table: str) -> tuple[str, list[tuple[str, str]]]:
    """元の表の `CREATE TABLE` と、その表の索引(名前, `CREATE INDEX`)。

    型・主キー・既定値を元のとおりに作るため、定義文をそのまま使う。
    """
    rows = source_db.read_query(
        src, "SELECT type, name, sql FROM sqlite_master WHERE tbl_name = ? COLLATE NOCASE"
             " AND sql IS NOT NULL", [table])
    create = next((r["sql"] for r in rows if r["type"] == "table"), "")
    indexes = [(r["name"], r["sql"]) for r in rows if r["type"] == "index"]
    if not create:
        raise source_db.SourceError(f"{table} の定義を読めません")
    return create, indexes


def _backup(dest: Path, why: str = "表を持ってくる前") -> Path:
    """書く前に梱包資材マスタを手元へ写す。戻したいときの頼り。"""
    from . import app_config
    folder = app_config.local_dir("backup")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{dest.stem}_{why}_{datetime.now():%Y%m%d_%H%M%S}{dest.suffix}"
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
    existing = _by_fold(source_db.list_tables(dest))
    already = [t for t in wanted if _fold(t) in existing]
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
                    # 足したことを梱包資材マスタの中に書く。マスタ管理はこれを
                    # 見て「足した表」として直せるようにする(どの端末からでも)
                    tx.execute(
                        f"CREATE TABLE IF NOT EXISTS {source_db.quote_identifier(REGISTRY)}"
                        " (表 TEXT PRIMARY KEY, 足した日時 TEXT, 元のファイル TEXT)")
                    tx.execute(
                        f"INSERT OR REPLACE INTO {source_db.quote_identifier(REGISTRY)}"
                        " (表, 足した日時, 元のファイル) VALUES (?, ?, ?)",
                        [table, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         str(original)])
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
    message = (f"梱包資材マスタに {done} を足しました。今ある表には触っていません。"
               "マスタ管理の一覧に「足」の印で出ていて、そのまま直せます。")
    if failed:
        message += " ただし次は持ってこられませんでした: " + "、".join(failed)
    return BringResult(not failed, message, "" if not failed else REFUSE_WRITE_FAILED,
                       brought=brought, backup=str(backup))


# ------------------------------------------------------------------
# もうある表の中身を、Access の最新に入れ替える
# ------------------------------------------------------------------
def _tool_owned() -> dict[str, str]:
    """**このツールが書き込む表。** Access から入れ替えない。

    これらは取り込み元(sqlite3)のほうが新しい ── 現場・倉庫の端末が
    発注・受払・実績・使用実績・コメントを書き足していて、Access には
    届いていない。Access の中身で入れ替えると、それが消える(しかも
    ツールは「送り済み」と覚えているので送り直さない)。
    """
    from . import access_control, sync_writeback
    owned = {spec.access_table: "このツールが書き込む表です(発注・受払・使用実績・コメント)"
             for spec in sync_writeback.WRITEBACK_SPECS}
    for table in (config.TBL_PT_HEADER, config.TBL_PT_SELECT,
                  config.TBL_PT_PLACE, config.TBL_PT_CUT):
        owned[table] = "このツールが書き込む表です(パレット実績)"
    owned[access_control.TABLE] = "マスタ管理で直す表です(書き間違えると誰も入れなくなるため)"
    owned[REGISTRY] = "このツールの控えの表です"
    return owned


# 入れ替えても**残すもの**。表ごとに決める(画面にも出す)
KEYS_PALLET_STOCK = ("幅", "丈", "位置")
KEEPS: dict[str, str] = {
    "PalletMaster": ("このツールの受入・払出で入った在庫の行(位置のある行)は残します"
                     "(Access には届いていないため)"),
}


def _import_columns(table: str) -> list[str]:
    """このツールが取り込みでその表から読む列(取り込み元の列名)。"""
    from . import import_specs
    return [source for _local, source, _conv in import_specs.IMPORT_SPECS.get(table, [])]


def _compare_columns(found: "Candidate", have: list[str], table: str) -> None:
    """Access の列と梱包資材マスタの列を比べて、足す列・消える列・できない理由を入れる。

    `table` は梱包資材マスタでの表の名前。
    """
    found.added_columns = [c for c in found.columns if c not in have]
    found.removed_columns = [c for c in have if c not in found.columns]
    reads = _import_columns(table)
    found.import_loses = [c for c in found.removed_columns if c in reads]
    found.refresh_why = refresh_why(table, found)


def refresh_why(table: str, found: "Candidate") -> str:
    """その表を入れ替え(作り直し)られない理由。できるなら空。"""
    owned = _tool_owned().get(table)
    if owned:
        return owned + "。Access の中身では入れ替えません"
    if found.suspect:
        return (f"文字化けの疑いがある行が {found.suspect}行 あります。"
                "Access のドライバ(pyodbc)が使えるPCで読み直してください")
    if not found.columns:
        return "Access の表に列がありません"
    return ""


def refresh(conn: Optional[sqlite3.Connection], source_path: str,
            tables: list[str]) -> BringResult:
    """もうある表を Access(変換したもの)の最新にする。表ごとに2通り。

    - **入れ替える**(列が同じか、Access で列を足しただけ): Access にだけある
      列を梱包資材マスタの表に足してから、中身を入れ替える
    - **作り直す**(梱包資材マスタにだけある列がある ── Access で列を消した・
      名前を変えた): いまの表を `表_作り直す前_日時` という名前で残し、
      Access の定義で作り直して中身を写す

    【なぜ要るか】
    Access のほうが新しいとき、以前は Access をまるごと変換して差し替えて
    いた。すると、このツールが共有に足した表・列・行(発注・コメント・
    在庫・送信ID の索引など)が消える。**変えるのは選んだ表だけ**にして、
    ほかには触らない。

    【守っていること】
    - このツールが書き込む表は入れ替えない(`_tool_owned`)
    - PalletMaster は、ツールが足した在庫の行を残す(`KEEPS`)。作り直しでも同じ
    - 1つの表は **列を足す+消す+入れる(作り直しは 名前を変える+作る+入れる)
      を1回で確定**。途中で落ちたら元のまま
    - 書く前に梱包資材マスタを手元の `backup` へ写す
    - 書いたら手元も取り込み直す(マスタ管理と同じ)
    """
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
        return BringResult(False, "入れ替える表を選んでください。", REFUSE_NOTHING)
    original = src
    try:
        src, _engine = _readable(src)
    except ConvertError as exc:
        return BringResult(False, str(exc), REFUSE_CONVERT)

    available = _by_fold(source_db.list_tables(src))
    existing = _by_fold(source_db.list_tables(dest))
    refusals = []
    actions: dict[str, str] = {}
    # ここから先は**梱包資材マスタでの名前**で扱う(大文字・小文字だけ違う
    # "palletmaster" が来ても PalletMaster として在庫の行を残すため)。
    # sqlite3 の文は名前の大文字・小文字を区別しないので、Access 側もこれで読める
    named = []
    for table in wanted:
        if _fold(table) not in available:
            refusals.append(f"{table}(選んだファイルに無い)")
        elif _fold(table) not in existing:
            refusals.append(f"{table}(梱包資材マスタに無い。「持ってくる」を使ってください)")
        else:
            table = existing[_fold(table)]
            named.append(table)
            columns = source_db.columns(src, table)
            found = Candidate(name=table, columns=columns, exists=True,
                              suspect=_suspect_rows(src, table, columns))
            _compare_columns(found, source_db.columns(dest, table), table)
            if found.refresh_why:
                refusals.append(f"{table}({found.refresh_why})")
            actions[table] = found.action
    if refusals:
        return BringResult(False, "入れ替えられない表があります: " + "、".join(refusals)
                           + "。何も変えていません。", REFUSE_NOTHING)
    wanted = list(dict.fromkeys(named))

    try:
        backup = _backup(dest, "中身を入れ替える前")
    except OSError as exc:
        return BringResult(False, f"書く前の控えを取れませんでした({exc})。"
                                  "何も変えていません。", REFUSE_WRITE_FAILED)
    import_diag.write("=" * 70)
    import_diag.write(f"■ 表を Access の最新にする  {original} → {dest}")
    import_diag.write(f"  書く前の控え: {backup}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    done: list[tuple[str, int, int]] = []
    kept_old: list[tuple[str, str]] = []
    failed: list[str] = []
    notes: list[str] = []
    with source_db.connect(dest) as dst:
        for table in wanted:
            rebuild = actions[table] == ACTION_REBUILD
            verb = "作り直した" if rebuild else "入れ替えた"
            try:
                if rebuild:
                    before, after, note, old = _rebuild_one(dst, src, table, stamp)
                    kept_old.append((table, old))
                else:
                    before, after, note = _refresh_one(dst, src, table)
                done.append((table, before, after))
                if note:
                    notes.append(f"{table}: {note}")
                import_diag.write(f"  {verb}: {table} {before:,}行 → {after:,}行"
                                  + (f"({note})" if note else ""))
                log.info("表を%s: %s %s→%s行 (%s)", verb, table, before, after, src)
            except (source_db.SourceError, sqlite3.Error) as exc:
                failed.append(f"{table}({exc})")
                import_diag.write(f"  ✕ {'作り直せ' if rebuild else '入れ替えられ'}"
                                  f"ませんでした: {table} ── {exc}")
                log.warning("表を Access の最新にできませんでした: %s: %s", table, exc)

    follow = []
    if conn is not None:
        from .master_common import _follow
        for table, _b, _a in done:
            said = _follow(conn, dest, table).lstrip("。")
            if said:
                follow.append(f"{table}: {said}")
    if failed and not done:
        return BringResult(False, "Access の最新にできませんでした: " + "、".join(failed)
                           + "。梱包資材マスタは変えていません。",
                           REFUSE_WRITE_FAILED, backup=str(backup))
    rebuilt = {t for t, _o in kept_old}
    parts = [f"{t}({b:,}行 → {a:,}行{'・作り直し' if t in rebuilt else ''})"
             for t, b, a in done]
    message = "Access の最新にしました: " + "、".join(parts) + "。"
    if kept_old:
        message += (" 作り直す前の表は "
                    + "、".join(f"{old}" for _t, old in kept_old)
                    + " という名前で残しています(要らなければ消してください)。")
    if notes:
        message += " " + "。".join(notes) + "。"
    if follow:
        message += " " + " / ".join(follow)
    if failed:
        message += " ただし次はできませんでした: " + "、".join(failed)
    return BringResult(not failed, message, "" if not failed else REFUSE_WRITE_FAILED,
                       backup=str(backup), refreshed=done, kept_old=kept_old)


def _column_types(path: Path, table: str) -> dict[str, str]:
    """列の宣言した型(`PRAGMA table_info` の type)。型の無い列は空。"""
    try:
        with source_db._connect(Path(path), read_only=True) as conn:
            with source_db.identifiers_as_utf8(conn):
                rows = conn.execute(
                    f"PRAGMA table_info({source_db.quote_identifier(table)})").fetchall()
        return {r["name"]: str(r["type"] or "") for r in rows}
    except (source_db.SourceError, sqlite3.Error):
        return {}


def _stock_rows(tx: "source_db.SourceTransaction", table: str) -> list[dict[str, Any]]:
    """PalletMaster の、このツールの在庫の行(位置のある行)。"""
    return tx.query(f"SELECT * FROM {source_db.quote_identifier(table)}"
                    " WHERE TRIM(COALESCE(位置, '')) <> ''")


def _stock_key(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(k) or "").strip() for k in KEYS_PALLET_STOCK)


def _refresh_one(dst: "source_db.SourceConnection", src: Path,
                 table: str) -> tuple[int, int, str]:
    """1つの表を入れ替える。(前の行数, 後の行数, 添える一言)。

    Access にだけある列は、先に梱包資材マスタの表へ足す(型は Access の定義のまま)。
    """
    q = source_db.quote_identifier
    have = source_db.columns(dst.path, table)
    columns = source_db.columns(src, table)
    added = [c for c in columns if c not in have]
    kinds = _column_types(src, table)
    rows = source_db.read_table(src, table)
    notes = []
    with dst.transaction() as tx:
        before = int(tx.query(f"SELECT COUNT(*) AS n FROM {q(table)}")[0]["n"])
        for column in added:
            kind = kinds.get(column, "")
            tx.execute(f"ALTER TABLE {q(table)} ADD COLUMN {q(column)}"
                       + (f" {kind}" if kind else ""))
        if added:
            notes.append(f"列 {'・'.join(added)} を足しました")
        have = have + added
        if table == "PalletMaster" and all(k in have for k in KEYS_PALLET_STOCK):
            # 在庫の行(位置のある行)は残す。Access の行で同じ棚のものは
            # 入れない(ツールの在庫のほうが新しい)
            kept = {_stock_key(r) for r in _stock_rows(tx, table)}
            tx.execute(f"DELETE FROM {q(table)} WHERE TRIM(COALESCE(位置, '')) = ''")
            rows = [r for r in rows if _stock_key(r) not in kept]
            if kept:
                notes.append(f"在庫の行 {len(kept)}行 は残しました")
        else:
            tx.execute(f"DELETE FROM {q(table)}")
        for row in rows:
            tx.insert(table, {c: row.get(c) for c in columns})
        if table == "PalletMaster" and "管理番号" in have:
            # 残した在庫の行と Access の行で管理番号が重ならないよう、
            # 重なったものだけ続きを振り直す
            dup = tx.query(f"SELECT rowid AS r FROM {q(table)} WHERE 管理番号 IN ("
                           f" SELECT 管理番号 FROM {q(table)} GROUP BY 管理番号"
                           " HAVING COUNT(*) > 1) AND TRIM(COALESCE(位置, '')) <> ''")
            top = int(tx.query(f"SELECT COALESCE(MAX(CAST(管理番号 AS INTEGER)), 0) AS n"
                               f" FROM {q(table)}")[0]["n"])
            for i, r in enumerate(dup, start=1):
                tx.execute(f"UPDATE {q(table)} SET 管理番号 = ? WHERE rowid = ?", (top + i, r["r"]))
        after = int(tx.query(f"SELECT COUNT(*) AS n FROM {q(table)}")[0]["n"])
    return before, after, "、".join(notes)


def _rebuild_one(dst: "source_db.SourceConnection", src: Path, table: str,
                 stamp: str) -> tuple[int, int, str, str]:
    """1つの表を Access の定義で作り直す。(前の行数, 後の行数, 添える一言, 残した名前)。

    いまの表は `表_作り直す前_日時` という名前に変えて残す(中身もそのまま)。
    その表の索引は外す ── 索引の名前はファイルの中で1つしか使えず、
    作り直した表に同じ名前の索引を作れなくなるため。
    """
    q = source_db.quote_identifier
    create, indexes = _create_sql(src, table)
    columns = source_db.columns(src, table)
    old_columns = source_db.columns(dst.path, table)
    rows = source_db.read_table(src, table)
    old = f"{table}_作り直す前_{stamp}"
    notes = []
    with dst.transaction() as tx:
        if tx.query("SELECT 1 FROM sqlite_master WHERE name = ?", [old]):
            raise source_db.SourceError(f"{old} という表がもうあります")
        before = int(tx.query(f"SELECT COUNT(*) AS n FROM {q(table)}")[0]["n"])
        old_indexes = [r["name"] for r in tx.query(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?"
            " AND sql IS NOT NULL", [table])]
        tx.execute(f"ALTER TABLE {q(table)} RENAME TO {q(old)}")
        for name in old_indexes:
            tx.execute(f"DROP INDEX {q(name)}")
        tx.execute(create)
        made = tx.query("SELECT name FROM sqlite_master WHERE type = 'table'"
                        " AND name = ? COLLATE NOCASE", [table])[0]["name"]
        if made != table:
            # Access では大文字・小文字だけ違う名前だった。梱包資材マスタでの
            # 名前に戻す(sqlite3 は大文字・小文字だけの付け替えを断るので2段で)
            tx.execute(f"ALTER TABLE {q(made)} RENAME TO {q(old + '_作りかけ')}")
            tx.execute(f"ALTER TABLE {q(old + '_作りかけ')} RENAME TO {q(table)}")

        stock: list[dict[str, Any]] = []
        if (table == "PalletMaster" and all(k in old_columns for k in KEYS_PALLET_STOCK)
                and all(k in columns for k in KEYS_PALLET_STOCK)):
            # 在庫の行(位置のある行)は、作り直した表へも持っていく。
            # Access の行で同じ棚のものは入れない(ツールの在庫のほうが新しい)
            stock = _stock_rows(tx, old)
            kept = {_stock_key(r) for r in stock}
            rows = [r for r in rows if _stock_key(r) not in kept]
        for row in rows:
            tx.insert(table, {c: row.get(c) for c in columns})
        if stock:
            common = [c for c in old_columns if c in columns]
            used = set()
            if "管理番号" in common:
                used = {str(r["n"]) for r in tx.query(
                    f"SELECT 管理番号 AS n FROM {q(table)}")}
            top = max([int(n) for n in used if n.lstrip("-").isdigit()] or [0])
            for row in stock:
                values = {c: row.get(c) for c in common}
                if "管理番号" in values and str(values["管理番号"]) in used:
                    top += 1                  # Access の行と重なったら続きを振る
                    values["管理番号"] = top
                if "管理番号" in values:
                    used.add(str(values["管理番号"]))
                tx.insert(table, values)
            notes.append(f"在庫の行 {len(stock)}行 は作り直した表へ移しました")

        skipped = []
        for name, sql in indexes:
            if tx.query("SELECT 1 FROM sqlite_master WHERE name = ?", [name]):
                skipped.append(name)
                continue
            tx.execute(sql)
        if skipped:
            notes.append(f"同じ名前があるので作らなかった索引: {'・'.join(skipped)}")
        gone = [n for n in old_indexes if n not in {name for name, _sql in indexes}]
        if gone:
            notes.append(f"Access に無い索引 {'・'.join(gone)} は付けていません")
        after = int(tx.query(f"SELECT COUNT(*) AS n FROM {q(table)}")[0]["n"])
    return before, after, "、".join(notes), old


def plan_dict(p: Plan) -> dict[str, Any]:
    return {
        "source": p.source, "dest": p.dest, "ok": p.ok, "message": p.message,
        "converted": p.converted, "converter": p.converter,
        # 持ってこられる表に文字化けの疑いがある(画面は注意の色にする)
        "warn": any(c.suspect for c in p.new_tables),
        "access": is_access(Path(p.source)) if p.source else False,
        "tables": [{"name": c.name, "rows": c.rows, "columns": c.columns,
                    "exists": c.exists, "suspect": c.suspect,
                    "current_rows": c.current_rows, "refresh_why": c.refresh_why,
                    "can_refresh": c.can_refresh, "keeps": c.keeps,
                    "action": c.action, "added_columns": c.added_columns,
                    "removed_columns": c.removed_columns,
                    "import_loses": c.import_loses} for c in p.candidates],
    }
