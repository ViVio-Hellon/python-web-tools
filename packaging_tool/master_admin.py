"""梱包資材マスタの管理 ── 中身を見る / 直す

【なぜ要るのか】
梱包資材マスタは起動のたびに**黙って読まれるだけ**でした。中に何が
入っているかを見る手立ても、間違いを直す手立ても画面にありません。
値が違っていても、現場には「パレットが出ない」「ボードの候補が
足りない」という形でしか現れず、原因に辿り着けません。
確かめる場所と、直す場所をここに作ります。

【どこを直すのか ── 取り込み元**だけ**】
取り込みは総入れ替えです(`data_sync.import_tables`)。手元のDBを
直しても、次の取り込みで消えます。**同じ事実を2か所に持たない**ので、
書き先は取り込み元(共有フォルダの sqlite3)ただ1つにして、書いた
あとにその表だけ取り込み直します。

    画面 → 取り込み元へ書く → その表だけ取り込み直す → 手元が追いつく

【行をどう指すのか】
sqlite3 の暗黙の `rowid` を使います。取り込みのとき管理番号は手元で
振り直している(`import_specs` の冒頭)ので、**手元の管理番号は
取り込み元の行を指しません**。取り込み元の行は取り込み元の言葉で
指す必要があります。

【誰が直せるのか】
資材モードを持つ端末だけです。マスタは資材課のもので、現場から
書き換えられると「何が正しいのか」が分からなくなります。
ただし**まだ誰も登録されていないとき**は塞ぎません ── アクセス権限
マスタの最初の1行をここから入れられなければ、資材モードに入る手立てが
どこにもなくなるからです(登録が1行でもあれば、その時点で閉まります)。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config, data_sync, db, import_specs, source_db
from .logging_utils import get_logger

log = get_logger("master_admin")

# 行を指す隠しの列名。取り込み元の `rowid` をこの名前で持ち回る。
# **業務の列と衝突しない名前**にする(全角を混ぜてあるのはそのため)
ROW_KEY = "__行"

# 一度に出す行数。**全部は出さない。**
# 注文管理は数千行あり、全部描いても読めないし、共有から引くだけで待つ。
# 出さなかった分は必ず数で言う(`Page.note`)── 黙って切ると
# 「これで全部だ」と読めてしまう
ROW_LIMIT = 200

# 空欄なら今の日時を入れる列。手で打たせる意味が無く、打ち間違いだけが増える
STAMP_COLUMNS = ("更新日時", "登録日時")


# ==================================================================
# どの表を扱うか
# ==================================================================
@dataclass(frozen=True)
class Managed:
    """直せる表1つ。"""

    table: str
    label: str
    mark: str
    note: str


# **直せるのは資材課が持っている表だけ。**
# 並びは触る頻度の順(ヒックの法則)。パレットとボードが大半を占める
MANAGED: tuple[Managed, ...] = (
    Managed(config.TBL_PALLET_MASTER, "パレット", "パ",
            "寸法・適合範囲・記号・保管位置・在庫数"),
    Managed(config.TBL_BOARD_MASTER, "ボード", "ボ",
            "ボードの幅丈と種別(ハードボード / IK / プロテック)"),
    Managed(config.TBL_CORNERBOARD_MASTER, "アングル", "ア",
            "アングルの丈"),
    Managed(config.TBL_HOGOZAI, "梱包保護材", "保",
            "板厚・板幅・板丈の範囲と、使う保護材"),
    Managed(config.TBL_MATSUZAI_KAKUZAI, "松板・角材", "松",
            "品名・厚・幅・丈の範囲と発注コード"),
    Managed("アクセス権限", "アクセス権限", "権",
            "誰がどのモードを使えるか"),
)
BY_TABLE: dict[str, Managed] = {m.table: m for m in MANAGED}

# 直せない表と、その理由。**出さないのではなく、理由を出す。**
# 「なぜこの表だけ直せないのか」が分からないと、画面が壊れて見える
VIEW_ONLY_WHY: dict[str, str] = {
    config.TBL_PALLET_PATTERN:
        "実績から貯まる表です。資材選択で「この配置を覚える」を押すと増えます。",
    config.TBL_WAREHOUSE_ORDER:
        "このツールが書き戻す表です。発注の取り消しは倉庫連携の画面から行います。",
    config.TBL_STOCK_HISTORY:
        "このツールが書き戻す表です。受け払いは簡易在庫の画面から行います。",
    "Form状態管理": "上流の設備が書く表です。",
}
DEFAULT_VIEW_ONLY = "このツールが直す表ではありません。中身の確認だけできます。"


def view_only_why(table: str) -> str:
    """その表を直せない理由。直せる表なら空。"""
    if table in BY_TABLE:
        return ""
    return VIEW_ONLY_WHY.get(table, DEFAULT_VIEW_ONLY)


# ==================================================================
# 直せるかどうか
# ==================================================================
# 断りの種類。**文言から推し量らない**(設計.md §1)
REFUSE_NOT_ALLOWED = "not_allowed"      # 権限が無い
REFUSE_NOT_EDITABLE = "not_editable"    # この表は直す表ではない
REFUSE_BAD_VALUE = "bad_value"          # 入れた値の形が違う
REFUSE_NO_SOURCE = "no_source"          # 取り込み元に届かない
REFUSE_NO_ROW = "no_row"                # その行がもう無い
REFUSE_WRITE_FAILED = "write_failed"    # 書けなかった
REFUSE_NOT_CREATABLE = "not_creatable"  # この表は作る表ではない
REFUSE_ALREADY = "already"              # もうある


# ==================================================================
# 取り込み元に作ってよい表
# ==================================================================
# **このツールが後から足した表だけ**を作る。
#
# 判断の出どころは `import_specs.OPTIONAL_TABLES` ── 「取り込み元に
# 無くても失敗にしない表」は、上流(資材課)が持っていない表、つまり
# このツールが持ち込んだ表だからです。同じ事実を2か所に持たないので、
# ここで別の一覧は作りません。
#
# 逆に PalletMaster のような上流の表は作りません。無いのは資材課側の
# 事情(ファイルが違う・移された)で、空の表を作ってしまうと**その事情が
# 「0件」という形に化けて**、原因を探せなくなります。
def creatable_tables() -> frozenset[str]:
    return frozenset(t for t in import_specs.OPTIONAL_TABLES if t in BY_TABLE)


def can_create(table: str) -> bool:
    return table in creatable_tables()


def _create_why(table: str) -> str:
    """その表が取り込み元に無いことの意味と、次にできること。"""
    if table == "アクセス権限":
        # **画面に出る文です。** 強調の記号を混ぜると、そのまま字として出る
        return ("この表がまだ取り込み元にありません。無いあいだは"
                "どの端末も現場モードだけになります"
                "(締め出さないための既定です)。"
                "ここで作ると、誰がどのモードを使えるかを決められます。")
    return "この表がまだ取り込み元にありません。ここで作れます。"


def _ddl_for(conn: sqlite3.Connection, table: str) -> str:
    """取り込み元へ作る `CREATE TABLE` 文。

    **列の定義は手元のスキーマ(`schema.sql`)から引きます。**
    ここに書き写すと、`schema.sql` を直したときに片方だけ古くなり、
    「作った表に取り込めない」という形でしか気づけなくなります。

    【NOT NULL は既定値の無い列にだけ付ける】
    手元では「空欄で入れてよい」列にも `NOT NULL DEFAULT ''` を付けて
    います(空とNULLの2通りを持たないため)。取り込み元は違います ──
    `_clean` は空欄を**NULL**にして書くので、そのまま写すと
    「ログインIDを空にした行」(=どのIDでもよい、という権限の書き方)が
    入らなくなります。

    残すのは `columns()` が必須と見なす列だけ、つまり**画面が空欄を
    断る列だけ**です。同じ判断を2つ持つと、画面は通したのに書き込みが
    落ちる、という食い違いが生まれます。
    """
    rows = list(conn.execute(
        f"PRAGMA table_info({source_db.quote_identifier(table)})"))
    if not rows:
        raise ValueError(f"手元のスキーマに {table} がありません")
    parts: list[str] = []
    for row in rows:
        piece = [source_db.quote_identifier(row["name"]), str(row["type"])]
        if row["pk"]:
            piece.append("PRIMARY KEY")
        if row["notnull"] and row["dflt_value"] is None:
            piece.append("NOT NULL")
        if row["dflt_value"] is not None:
            # PRAGMA が返す既定値は**SQLの字面のまま**(`''` や `1`)
            piece.append(f"DEFAULT {row['dflt_value']}")
        parts.append(" ".join(piece))
    return (f"CREATE TABLE {source_db.quote_identifier(table)} "
            f"({', '.join(parts)})")


def create_table(conn: sqlite3.Connection, table: str, *,
                 path: Optional[Path] = None) -> Result:
    """取り込み元にその表を作る。**中身は空のまま。**

    行は普段どおり「1行足す」で入れます。作ることと入れることを分けて
    あるのは、最初の1行をここで決め打ちすると、その1行が何を意味するか
    (誰にどの権限を与えたか)が画面に現れないためです。
    """
    allowed, why = can_edit(conn, table)
    if not allowed:
        return Result(False, why, REFUSE_NOT_ALLOWED)
    if not can_create(table):
        return Result(False,
                      f"{_label(table)}は、このツールが作る表ではありません。"
                      "取り込み元にあるはずのものなので、"
                      "ファイルの置き場所を確かめてください。",
                      REFUSE_NOT_CREATABLE)
    found = path or data_sync.find_material_db()
    if found is None:
        return Result(False,
                      f"梱包資材マスタが見つかりません。"
                      f"{config.master_db_dir()} を確かめてください。",
                      REFUSE_NO_SOURCE)

    try:
        ddl = _ddl_for(conn, table)
    except ValueError as exc:                    # pragma: no cover - 通常は無い
        return Result(False, str(exc), REFUSE_NOT_CREATABLE)

    try:
        with source_db.connect(found) as src:
            if table in src.table_names():
                # 誰かが先に作った。**押した人には見えていない事実**
                return Result(False,
                              f"{_label(table)}はもう取り込み元にあります。"
                              "一覧を出し直してください。",
                              REFUSE_ALREADY)
            src.execute(ddl)
    except source_db.SourceError as exc:
        return _write_failed(table, exc)

    log.info("取り込み元に表を作りました: %s (%s)", table, found)
    return Result(True, f"{_label(table)}を取り込み元に作りました"
                        f"{_follow(conn, found, table)}")


def can_edit(conn: Optional[sqlite3.Connection], table: str = "") -> tuple[bool, str]:
    """この端末はマスタを直せるか。**直せないなら理由も返す。**

    権限で見る。いま開いているモードでは見ない ── 資材課の人が現場の
    画面を見ているあいだだけ直せなくなるのは、権限の分け方として
    説明が付かない。

    【アクセス権限マスタだけの逃げ道】
    書き間違えて全員から mode:material を消してしまうと、直せる人が
    どこにもいなくなる(現場の指摘: 「アクセス権の書き換えミスっちゃうと
    二度と書きかえれなくなっちゃう」)。この表(`access_control.TABLE`)
    だけ、**管理者パスワードが分かれば、いまのモードに関わらず**開ける。

    最初は「現場モード(mode:field)を持ち、かつパスワード」の両方を
    条件にしていたが、それだと mode:field 側も壊れている(または
    そう判定される)まれなケースで詰む。「パスワードが分かれば直せる」
    を最優先にする、という現場の判断でモード条件を外した ──
    パスワードそのものが唯一の関門になる。
    パスワードの確認は資材選択画面の管理者認証(`selection_session`)を
    そのまま使う ── アプリの起動プロセスに1つなので、別の認証を
    もう1つ持たない。
    """
    from . import access_control, modes

    if conn is None:
        return False, "手元のデータベースを開けませんでした。"
    grant = access_control.resolve(conn)
    if grant.allows_mode(modes.MATERIAL):
        return True, ""
    if not grant.has_master:
        # まだ誰も登録されていない。ここを塞ぐと、資材モードに入るための
        # 最初の1行をどこからも入れられなくなる
        return True, ""

    if table == access_control.TABLE:
        from . import selection_session
        if selection_session.get_session(conn).admin:
            return True, ""
        return False, (
            f"{access_control.TABLE} は{modes.label(modes.MATERIAL)}モードを"
            "持つ端末のほか、管理者パスワードが分かれば直せます。"
            "「資材選択」画面の管理者エリアでパスワードを入れてから、"
            "もう一度この面を開いてください。")

    return False, (
        f"マスタを直せるのは{modes.label(modes.MATERIAL)}モードを持つ端末だけです。"
        f"{access_control.TABLE} に {grant.identity.label()} と "
        f"{access_control.mode_permission(modes.MATERIAL)} の行を足してください。"
        "(いまの権限は「いまの状態」の面で確かめられます)")


# ==================================================================
# 列
# ==================================================================
@dataclass(frozen=True)
class Column:
    """直せる列1つ。"""

    name: str
    kind: str               # "int" | "real" | "text"
    required: bool = False
    stamp: bool = False     # 空欄なら今の日時が入る
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind,
                "required": self.required, "note": self.note}


# 画面に出す型の名前。**サーバが言葉を持つ**(画面側で書き分けない)
KIND_LABEL = {"int": "整数", "real": "小数", "text": "文字"}


def columns(conn: sqlite3.Connection, table: str,
            present: Optional[Iterable[str]] = None) -> list[Column]:
    """その表で直せる列。

    出どころは3つとも既にあるものを読むだけ ── **同じ事実を書き足さない**。

        どの列を扱うか   … `import_specs.IMPORT_SPECS`(取り込みが読む列)
        どんな型か       … 手元のスキーマ(`schema.sql`)
        本当にあるか     … 取り込み元の列(`present`)

    取り込み元にしか無い列(管理番号など)はここに出ません。触らずに
    そのまま残します。逆に**取り込み元に無い列も出しません** ── 出すと
    打ち込めてしまい、保存の瞬間に「そんな列は無い」と断られます。
    上流が列を足していないことは取り込みの結果が言うので、ここでは黙って
    外します。
    """
    spec = import_specs.IMPORT_SPECS.get(table, [])
    if not spec:
        return []
    allowed = None if present is None else set(present)
    info: dict[str, sqlite3.Row] = {}
    try:
        for row in conn.execute(
                f"PRAGMA table_info({source_db.quote_identifier(table)})"):
            info[row["name"]] = row
    except sqlite3.Error as exc:                 # pragma: no cover - 通常は無い
        log.warning("%s の列を引けません: %s", table, exc)
        return []

    out: list[Column] = []
    for local, source, _conv in spec:
        row = info.get(local)
        if row is None:
            continue
        if allowed is not None and source not in allowed:
            continue
        kind = _kind_of(str(row["type"]))
        stamp = local in STAMP_COLUMNS
        # 既定値のある列は空欄で通してよい。NULL も既定も無い列だけが必須
        required = bool(row["notnull"]) and row["dflt_value"] is None and not stamp
        out.append(Column(
            name=local, kind=kind, required=required, stamp=stamp,
            note="空欄なら今の日時が入ります" if stamp else ""))
    return out


def _kind_of(declared: str) -> str:
    upper = declared.upper()
    if "INT" in upper:
        return "int"
    if "REAL" in upper or "FLOA" in upper or "DOUB" in upper:
        return "real"
    return "text"


# ==================================================================
# 見る
# ==================================================================
@dataclass
class TableInfo:
    """一覧に出す表1つ。"""

    table: str
    label: str
    mark: str
    note: str
    rows: int
    editable: bool
    why: str = ""
    # 取り込み元に無い(が、作れる)表。**隠さずに出す**
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "label": self.label, "mark": self.mark,
                "note": self.note, "rows": self.rows,
                "editable": self.editable, "why": self.why,
                "missing": self.missing}


def tables(path: Optional[Path]) -> list[TableInfo]:
    """取り込み元にある表ぜんぶ。**直せないものも出す。**

    直せる表だけを出すと「あるはずの表が無い」に見えます。中身を
    確かめるのは全部の表でできるので、並べたうえで直せるかどうかを
    札にします。並びは `MANAGED` が先(触る頻度の順)、残りは名前順。

    **作れる表は、取り込み元に無くても並びに出します。** 出さないと
    「無い表は画面にも無い」になり、アクセス権限を一度も入れていない
    端末では、モードを決める場所がどこにも見えません。
    """
    if path is None:
        return []
    counts = source_db.table_counts(path)
    if not counts:
        return []
    out: list[TableInfo] = []
    for managed in MANAGED:
        if managed.table not in counts:
            if can_create(managed.table):
                out.append(TableInfo(
                    table=managed.table, label=managed.label,
                    mark=managed.mark, note=managed.note, rows=0,
                    editable=True, missing=True,
                    why=_create_why(managed.table)))
            continue
        out.append(TableInfo(
            table=managed.table, label=managed.label, mark=managed.mark,
            note=managed.note, rows=counts[managed.table], editable=True))
    for name in sorted(n for n in counts if n not in BY_TABLE):
        out.append(TableInfo(
            table=name, label=name, mark="他", note="", rows=counts[name],
            editable=False, why=view_only_why(name)))
    return out


@dataclass
class Page:
    """1つの表の中身(の一部)。"""

    table: str
    label: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    editable: bool = False
    why: str = ""
    note: str = ""
    error: str = ""
    # 取り込み元に無い(が、作れる)表
    missing: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"table": self.table, "label": self.label,
                "columns": self.columns, "rows": self.rows,
                "total": self.total, "shown": len(self.rows),
                "editable": self.editable, "why": self.why,
                "note": self.note, "error": self.error,
                "missing": self.missing, "row_key": ROW_KEY}


def page(path: Optional[Path], table: str, *, query: str = "",
         limit: int = ROW_LIMIT) -> Page:
    """表の中身を読む。**取り込み元から直に読む。**

    手元の写しではなく元を読むのは、直したあと「本当に入ったか」を
    ここで確かめられるようにするためです。写しを見せると、書き込みが
    失敗していても画面上は直ったように見えます。
    """
    managed = BY_TABLE.get(table)
    view = Page(table=table,
                label=managed.label if managed else table,
                editable=managed is not None,
                why=view_only_why(table))
    if path is None:
        view.error = (f"梱包資材マスタが見つかりません。"
                      f"{config.master_db_dir()} を確かめてください。")
        return view

    names = source_db.columns(path, table)
    if not names:
        if can_create(table):
            # **断りではなく、次にできること。** ここで「ありません」と
            # だけ言うと、直しようが無い故障に見える
            view.missing = True
            view.why = _create_why(table)
            return view
        view.error = f"{table} は取り込み元にありません。"
        return view
    view.columns = names

    where, params = _filter(names, query)
    quoted = source_db.quote_identifier(table)
    try:
        count = source_db.read_query(
            path, f"SELECT COUNT(*) AS n FROM {quoted}{where}", params)
        view.total = int(count[0]["n"]) if count else 0
        view.rows = source_db.read_query(
            path,
            f'SELECT rowid AS "{ROW_KEY}", * FROM {quoted}{where}'
            f" ORDER BY rowid LIMIT ?", [*params, max(1, limit)])
    except source_db.SourceError as exc:
        view.rows = []
        view.error = str(exc)
        return view

    hidden = view.total - len(view.rows)
    if hidden > 0:
        # **黙って切らない。** 絞り込みの手があることまで言う
        view.note = (f"{view.total}件のうち {len(view.rows)}件を出しています"
                     f"(ほか {hidden}件)。絞り込むと目当ての行が出ます。")
    return view


def _filter(names: list[str], query: str) -> tuple[str, list[Any]]:
    """絞り込みの条件。**どの列でもいい**ので、全部の列を見る。

    どの列に何が入っているかを覚えていなくても引けるようにする。
    """
    text = (query or "").strip()
    if not text:
        return "", []
    conds = " OR ".join(
        f"CAST({source_db.quote_identifier(n)} AS TEXT) LIKE ?" for n in names)
    return f" WHERE ({conds})", [f"%{text}%"] * len(names)


# ==================================================================
# 直す
# ==================================================================
@dataclass
class Result:
    ok: bool = True
    message: str = ""
    reason: str = ""


def save_row(conn: sqlite3.Connection, table: str, row_key: Any,
             values: dict[str, Any], *,
             path: Optional[Path] = None) -> Result:
    """1行を書き換える。"""
    path, refused = _ready(conn, table, path)
    if refused:
        return refused

    clean, problem = _clean(conn, table, values, filling=False,
                            present=source_db.columns(path, table))
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)
    if not clean:
        return Result(False, "変える値がありません。", REFUSE_BAD_VALUE)

    try:
        with source_db.connect(path) as src:
            changed = src.update(table, clean, {"rowid": row_key})
    except source_db.SourceError as exc:
        return _write_failed(table, exc)
    if not changed:
        # 一覧を出したあとに誰かが消した。押した人には見えていない事実
        return Result(False, "その行はもうありません。一覧を出し直してください。",
                      REFUSE_NO_ROW)

    log.info("マスタを直しました: %s rowid=%s %s", table, row_key, sorted(clean))
    return Result(True, f"{_label(table)}の1行を直しました{_follow(conn, path, table)}")


def add_row(conn: sqlite3.Connection, table: str, values: dict[str, Any], *,
            path: Optional[Path] = None) -> Result:
    """1行足す。"""
    path, refused = _ready(conn, table, path)
    if refused:
        return refused

    clean, problem = _clean(conn, table, values, filling=True,
                            present=source_db.columns(path, table))
    if problem:
        return Result(False, problem, REFUSE_BAD_VALUE)
    if not clean:
        return Result(False, "入れる値がありません。", REFUSE_BAD_VALUE)

    try:
        with source_db.connect(path) as src:
            src.insert(table, clean)
    except source_db.SourceError as exc:
        return _write_failed(table, exc)

    log.info("マスタに足しました: %s %s", table, sorted(clean))
    return Result(True, f"{_label(table)}に1行足しました{_follow(conn, path, table)}")


def delete_row(conn: sqlite3.Connection, table: str, row_key: Any, *,
               path: Optional[Path] = None) -> Result:
    """1行消す。"""
    path, refused = _ready(conn, table, path)
    if refused:
        return refused

    try:
        with source_db.connect(path) as src:
            changed = src.delete(table, {"rowid": row_key})
    except source_db.SourceError as exc:
        return _write_failed(table, exc)
    if not changed:
        return Result(False, "その行はもうありません。一覧を出し直してください。",
                      REFUSE_NO_ROW)

    log.info("マスタから消しました: %s rowid=%s", table, row_key)
    return Result(True, f"{_label(table)}の1行を消しました{_follow(conn, path, table)}")


def _ready(conn: sqlite3.Connection, table: str, path: Optional[Path],
           ) -> tuple[Optional[Path], Optional[Result]]:
    """書く前に通す関門。通れば `(ファイル, None)`、通らなければ理由。

    順番に意味がある ── **権限 → 表 → 届くか**。届かないことを先に
    言うと、権限が無い人に「共有が落ちている」と読ませてしまう。
    """
    allowed, why = can_edit(conn, table)
    if not allowed:
        return None, Result(False, why, REFUSE_NOT_ALLOWED)
    if table not in BY_TABLE:
        return None, Result(False, view_only_why(table) or "直せない表です。",
                            REFUSE_NOT_EDITABLE)
    found = path or data_sync.find_material_db()
    if found is None:
        return None, Result(False,
                            f"梱包資材マスタが見つかりません。"
                            f"{config.master_db_dir()} を確かめてください。",
                            REFUSE_NO_SOURCE)
    return found, None


def _write_failed(table: str, exc: Exception) -> Result:
    log.warning("%s へ書けませんでした: %s", table, exc)
    return Result(False, f"取り込み元へ書けませんでした: {exc}", REFUSE_WRITE_FAILED)


def _label(table: str) -> str:
    managed = BY_TABLE.get(table)
    return managed.label if managed else table


def _clean(conn: sqlite3.Connection, table: str, values: dict[str, Any],
           *, filling: bool,
           present: Optional[Iterable[str]] = None) -> tuple[dict[str, Any], str]:
    """画面から来た値を、取り込み元へ入れられる形にする。

    `filling` が真なら新しい行なので、送られてこなかった必須の列も
    見る。偽なら書き換えなので、**送られてきた列だけ**を触る
    (送っていない列を消さないため)。
    """
    out: dict[str, Any] = {}
    for column in columns(conn, table, present):
        if column.name not in values and not filling:
            continue
        raw = str(values.get(column.name, "")).strip()
        if raw == "":
            if column.stamp:
                out[column.name] = db.now_db_string()
                continue
            if column.required:
                return {}, f"「{column.name}」は空にできません。"
            # 空欄は「無し」。取り込みのときに手元の既定値で埋まる
            out[column.name] = None
            continue
        if column.kind == "int":
            try:
                out[column.name] = int(float(raw))
            except ValueError:
                return {}, f"「{column.name}」は{KIND_LABEL['int']}で入れてください。"
        elif column.kind == "real":
            try:
                out[column.name] = float(raw)
            except ValueError:
                return {}, f"「{column.name}」は{KIND_LABEL['real']}で入れてください。"
        else:
            out[column.name] = raw
    return out, ""


def _follow(conn: sqlite3.Connection, path: Path, table: str) -> str:
    """書いた表だけを取り込み直して、手元を追いつかせる。

    ここを飛ばすと、取り込み元は直っているのに画面の動きは変わらない
    ── **直したのに効かない**が一番たちが悪い。
    """
    try:
        result = data_sync.import_tables(
            conn, path, {table: import_specs.IMPORT_SPECS[table]},
            required=import_specs.REQUIRED_KEY_COLUMNS,
            blank_is_missing=import_specs.BLANK_IS_MISSING,
            optional=import_specs.OPTIONAL_TABLES,
            fallbacks=import_specs.NULL_FALLBACKS)
    except sqlite3.Error as exc:                 # pragma: no cover - 上流で拾う
        log.warning("%s を取り込み直せません: %s", table, exc)
        return "。ただし手元に取り込めなかったので「まとめて取り込み」を押してください"

    # **入ったかどうかは `imported` で見る。** `errors` には「入ったが
    # 言っておくべきこと」(元に無い列があった等)も混ざるので、これを
    # 失敗として読むと、直せているのに「失敗しました」と出る
    if table not in result.imported:
        return "。ただし手元に取り込めませんでした: " + " / ".join(result.errors)

    notes = [f"手元も{result.imported[table]}件に更新しました"]
    if table == config.TBL_PALLET_MASTER:
        # 寸法を直したら適合範囲も変わる。取り込みと同じ後始末をする
        # (`data_sync.import_master` と同じ理由)
        from . import pallet_service
        summary = pallet_service.recompute_fit_ranges(conn)
        notes.append("適合範囲も計算し直しました" if summary.ok
                     else f"ただし適合範囲の再計算に失敗しました({summary.error})")
    if table == "アクセス権限":
        # **足した権限は、開き直すまで全部は効かない。** 使える画面は
        # 起動時の権限で決まっているので、切り替えは通っても画面が出ない
        notes.append("権限を変えたので、アプリを開き直すと全部が効きます")
    if result.errors:
        # 取り込みが言いたいこと。**黙って捨てない**
        notes.append("なお " + " / ".join(result.errors))
    return "。" + "、".join(notes) + "。"
