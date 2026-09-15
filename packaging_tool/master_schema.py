"""表そのものを作る・作り直す

取り込み元にまだ無い表を作る段。**閾値の表**(パレット適合基準)は
プログラムに直接書かれていた数値を表へ出したもので、取り込み元の
ファイルにはまだ無いことがある。

`rebuild_table` は、表はあるが列名が想定と違って1つも打ち込めないとき
に作り直す。**中身は移さない** ── 列名が違う以上どの列がどれに当たるか
決められないので、種として基準表の初期値を入れ直す。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# **他の段は名前ではなくモジュールで呼ぶ。** 差し替え(試験の stub)の
# 当て先が持ち主の1か所で済む
from . import config, data_sync, db, import_specs, source_db
from . import master_common
from .logging_utils import get_logger
from .master_common import (BY_TABLE, MANAGED, REFUSE_ALREADY,
                            REFUSE_BAD_VALUE, REFUSE_NOT_ALLOWED,
                            REFUSE_NOT_CREATABLE, REFUSE_NOT_EDITABLE,
                            REFUSE_NO_ROW, REFUSE_NO_SOURCE,
                            REFUSE_WRITE_FAILED, ROW_KEY, ROW_LIMIT,
                            STAMP_COLUMNS, Managed, Result, _follow, _label,
                            _write_failed, can_edit, source_dir, source_for,
                            source_label, view_only_why)
from . import master_columns

log = get_logger("master_admin.schema")


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

    残すのは `master_columns.columns()` が必須と見なす列だけ、つまり**画面が空欄を
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
    """取り込み元にその表を作る。**中身は空のまま** ── 閾値の表を除く。

    行は普段どおり「1行足す」で入れます。作ることと入れることを分けて
    あるのは、最初の1行をここで決め打ちすると、その1行が何を意味するか
    (誰にどの権限を与えたか)が画面に現れないためです。

    **パレット適合閾値の7表だけは中身も入れます。** ここは「誰かが
    決める1行」ではなく社内の選定基準表そのもので、しかも空だと
    適合範囲の再計算がまるごと止まります(帯が0〜999999を覆えない)。
    空の表を作って渡すと、押した人には作れたように見えて、次に再計算を
    押したときに初めて止まります。作るなら使える状態で作ります。
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
    found = path or source_for(table)
    if found is None:
        return Result(False,
                      f"{source_label(table)}が見つかりません。"
                      f"{source_dir(table)} を確かめてください。",
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
            seeded = _seed_source(src, table)
    except source_db.SourceError as exc:
        return _write_failed(table, exc)

    log.info("取り込み元に表を作りました: %s (%s, 初期値%s件)",
             table, found, seeded)
    made = f"{_label(table)}を取り込み元に作りました"
    if seeded:
        made += f"(基準表の初期値{seeded}件を入れてあります)"
    return Result(True, made + _follow(conn, found, table))


def _seed_source(src: source_db.SourceConnection, table: str) -> int:
    """作ったばかりの表に初期値を入れる。**入れる表だけ入れる。**

    対象は `pallet_threshold.SEED` を持つ表(パレット適合閾値の7表)
    だけです。アクセス権限のように「最初の1行が決めごと」の表は、
    ここでは何もしません ── 理由は `create_table` の説明にあります。
    """
    from . import pallet_threshold
    seed_rows = pallet_threshold.SEED.get(table)
    if not seed_rows:
        return 0
    columns = list(seed_rows[0])
    col_list = ", ".join(source_db.quote_identifier(c) for c in columns)
    marks = ", ".join("?" for _ in columns)
    sql = (f"INSERT INTO {source_db.quote_identifier(table)} "
           f"({col_list}) VALUES ({marks})")
    for row in seed_rows:
        src.execute(sql, [row[c] for c in columns])
    return len(seed_rows)


def can_rebuild(conn: sqlite3.Connection, table: str,
                path: Optional[Path] = None) -> bool:
    """列名が想定と違うだけで、作り直せば直る状態か。

    `create_table()` が使える(=表が無い)条件とは逆で、**表はあるのに
    列が1つも一致しない**ときだけ真になる。両方は同時に真にならない
    (無ければ作る、あれば作り直す、で always exactly one)。
    """
    if not can_create(table):
        return False
    found = path or source_for(table)
    if found is None:
        return False
    present = source_db.columns(found, table)
    if not present:
        return False                              # 表が無い(create_tableの領分)
    return bool(master_columns.column_mismatch_why(table, present))

def rebuild_table(conn: sqlite3.Connection, table: str, *,
                  path: Optional[Path] = None) -> Result:
    """列名が想定と違う表を、**中身を捨てずに**正しい列名へ作り直す。

    【なぜ RENAME であって DROP でないか】
    列名が違うだけで、中に意味のあるデータが入っている可能性を
    否定できない(誰かが手作業で作った表かもしれない)。黙って消すと
    取り返しがつかないので、`{table}_旧_YYYYMMDDHHMMSS` のような名前へ
    退避してから、正しい列名で新しく作る。退避したことは結果の文言で
    必ず言う ── 「直ったように見えて、実は前のデータがどこにあるか
    誰も分からない」を作らない。

    【なぜここにしかできないのか】
    sqlite3 ファイルはテキストエディタでは編集できない(バイナリ形式)。
    列名を直す手段が他に無い環境を前提に、この画面から完結できるように
    してある。
    """
    allowed, why = can_edit(conn, table)
    if not allowed:
        return Result(False, why, REFUSE_NOT_ALLOWED)
    if not can_create(table):
        return Result(False,
                      f"{_label(table)}は、このツールが作り直す表ではありません。",
                      REFUSE_NOT_CREATABLE)
    found = path or source_for(table)
    if found is None:
        return Result(False,
                      f"{source_label(table)}が見つかりません。"
                      f"{source_dir(table)} を確かめてください。",
                      REFUSE_NO_SOURCE)

    present = source_db.columns(found, table)
    if not present:
        # 表そのものが無い ── これは create_table の仕事
        return Result(False, f"{_label(table)}は取り込み元にまだありません。"
                             "「取り込み元へ作る」を使ってください。",
                      REFUSE_NOT_CREATABLE)
    if not master_columns.column_mismatch_why(table, present):
        # 1列でも一致していれば、作り直しの対象ではない(誤って
        # 一致している列まで巻き込んで消さない)
        return Result(False, f"{_label(table)}は列名が一部一致しているため、"
                             "作り直しの対象ではありません。"
                             "一致していない列だけ、取り込み元で直してください。",
                      REFUSE_NOT_CREATABLE)

    try:
        ddl = _ddl_for(conn, table)
    except ValueError as exc:                    # pragma: no cover - 通常は無い
        return Result(False, str(exc), REFUSE_NOT_CREATABLE)

    from datetime import datetime
    backup_name = f"{table}_旧_{datetime.now():%Y%m%d%H%M%S}"

    try:
        with source_db.connect(found) as src:
            if table not in src.table_names():
                # 一覧を出したあとに誰かが消した/作り直した
                return Result(False, f"{_label(table)}はもうありません。"
                                     "一覧を出し直してください。",
                              REFUSE_NO_ROW)
            if backup_name in src.table_names():  # pragma: no cover - 秒単位で衝突は稀
                return Result(False, "退避先の表名が衝突しました。"
                                     "もう一度押してください。",
                              REFUSE_ALREADY)
            src.execute(
                f"ALTER TABLE {source_db.quote_identifier(table)} "
                f"RENAME TO {source_db.quote_identifier(backup_name)}")
            src.execute(ddl)
    except source_db.SourceError as exc:
        return _write_failed(table, exc)

    log.info("取り込み元で作り直しました: %s -> 退避 %s、新規作成 (%s)",
             table, backup_name, found)
    return Result(True,
                  f"{_label(table)}を正しい列名で作り直しました。"
                  f"元の表は {backup_name} という名前で残しています"
                  "(中身が要らないと分かれば、あとで消してください)"
                  f"{_follow(conn, found, table)}")
