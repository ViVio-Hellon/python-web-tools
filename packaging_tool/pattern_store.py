"""実績(スナップショット)の保存庫 ── VBA `modPatternStore` の移植

【何を保存するのか】
配置を承認した**画面の状態そのもの**です。以前の実績(`PalletPatterns`)は
ボードの寸法と枚数だけを10枠に持ち、読み込むたびに配置を計算し直して
いました。タグ(幅補填・丈補填・カット前提・Y積み)が残らないので、
読み込んだ配置が保存したときと違うことがありました。

    ヘッダ     … パレット・製品・ボード種別・配置方式・狭幅・保護材・
                 アングル・プロテック確定値・LotNo・拠点
    選定明細   … 上下の選定リスト(タグ込み・行順どおり)
    配置明細   … 置いたボード1枚ずつの座標と寸法
    カット明細 … 幅カット・丈カット長・丈カット枚数

読み込みは**計算し直しません**(`selection_records.load_pattern`)。

【旧版からの変更】VBA `PalletHistoryModule_v2` は削除され、一覧と削除は
ここに作り直されています(`get_pattern_list` / `delete_pattern_by_id`)。
旧版の実績(`PalletPatterns`)は形が違うので、一覧には出ません。

【共有】保存した実績は、取り込み元(梱包資材マスタ)へ書き戻して全端末で
使います(`pattern_sync`)。そのための手元だけの列が3つあります:

    送信ID        … 保存した実績を世界で1つに見分ける印。送り直しても
                    取り込み元に二重に入らない
    取込元実績ID  … 取り込み元での番号。送れたら入る(取り込んだ実績は
                    最初から入っている)。**空なら未送信**
    使用回数未反映 … 読み込んだ回数のうち、まだ取り込み元へ足していない分

表の名前は `config.TBL_PT_*` だけに書きます(仮の名前。VBA側の名前が
分かったらそこだけ直す)。
"""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from . import config, db
from .board_selection_types import ProtecCutResult
from .logging_utils import get_logger

log = get_logger("pattern_store")


class PatternStoreError(RuntimeError):
    """保存・読込・削除が通らなかった。文言はそのまま画面に出せる。"""


# ------------------------------------------------------------------
# 列の定義(手元と取り込み元で共通)
#
# **ここが唯一の出どころ。** 手元の表(`ensure_tables`)も、取り込み元に
# 作る表と送る列(`pattern_sync`)も、ここから作る。
# ------------------------------------------------------------------
HEADER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("保存形式", "INTEGER"),
    ("パレット幅", "INTEGER"), ("パレット丈", "INTEGER"),
    ("製品幅", "INTEGER"), ("製品丈", "INTEGER"),
    ("製品回転", "INTEGER"),
    ("ボード種別", "TEXT"),
    ("配置方式", "TEXT"),
    ("狭幅下", "INTEGER"), ("狭幅上", "INTEGER"),
    ("保護材", "TEXT"),
    ("アングル", "TEXT"),
    ("プロテック確定値", "TEXT"),
    ("LotNo", "TEXT"),
    ("拠点", "TEXT"),
    ("登録日時", "TEXT"), ("更新日時", "TEXT"),
    ("使用回数", "INTEGER"),
    # 取り込み元にも持つ(送り直しても二重に入らないための印)
    ("送信ID", "TEXT"),
)
# 手元にだけある列(取り込み元へは送らない)
HEADER_LOCAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("取込元実績ID", "INTEGER"),
    ("使用回数未反映", "INTEGER NOT NULL DEFAULT 0"),
)
SELECT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("区分", "TEXT"), ("行順", "INTEGER"),
    ("幅", "INTEGER"), ("丈", "INTEGER"), ("枚数", "INTEGER"),
    ("タグ", "TEXT"),
)
PLACE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("区分", "TEXT"), ("順番", "INTEGER"),
    ("板ID", "INTEGER"), ("インスタンスID", "TEXT"),
    ("座標X", "INTEGER"), ("座標Y", "INTEGER"),
    ("幅", "INTEGER"), ("丈", "INTEGER"),
    ("元幅", "INTEGER"), ("元丈", "INTEGER"),
    ("補填", "INTEGER"),
)
CUT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("種別", "TEXT"), ("キー", "TEXT"), ("値", "INTEGER"),
)

# 明細の表と列。ヘッダを消す・送るときは**この順に**明細を扱う
DETAIL_TABLES: tuple[tuple[str, tuple[tuple[str, str], ...], str], ...] = (
    (config.TBL_PT_SELECT, SELECT_COLUMNS, "行順"),
    (config.TBL_PT_PLACE, PLACE_COLUMNS, "順番"),
    (config.TBL_PT_CUT, CUT_COLUMNS, "rowid"),
)

# 削除を取り込み元へ伝えるまで覚えておく表(手元だけ)
TBL_PT_DELETED = "実績削除待ち"

# カット明細の種別(VBA `CollectDictRows` の kind)
CUT_WIDTH = "幅カット"
CUT_LENGTH = "丈カット長"
CUT_LENGTH_COUNT = "丈カット枚数"

# 配置方式(VBA `mPlacementMethod`)
METHOD_NORMAL = "通常"
METHOD_TILING_PREFIX = "別案"

# 区分
CATEGORY_LOWER = "下用"
CATEGORY_UPPER = "上用"


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def create_sql(table: str, columns: Iterable[tuple[str, str]], *,
               header: bool) -> list[str]:
    """表と索引を作る文。手元(`ensure_tables`)と取り込み元の両方で使う。"""
    cols = list(columns)
    if header:
        body = ["実績ID INTEGER PRIMARY KEY AUTOINCREMENT"]
    else:
        body = ["明細ID INTEGER PRIMARY KEY AUTOINCREMENT",
                "実績ID INTEGER NOT NULL"]
    body += [f"{_q(name)} {kind}" for name, kind in cols]
    sqls = [f"CREATE TABLE IF NOT EXISTS {_q(table)} ({', '.join(body)})"]
    if header:
        sqls.append(f"CREATE INDEX IF NOT EXISTS {_q('idx_' + table + '_pallet')} "
                    f"ON {_q(table)} (パレット幅, パレット丈)")
        sqls.append(f"CREATE UNIQUE INDEX IF NOT EXISTS {_q('idx_' + table + '_sendid')} "
                    f"ON {_q(table)} (送信ID)")
    else:
        sqls.append(f"CREATE INDEX IF NOT EXISTS {_q('idx_' + table + '_id')} "
                    f"ON {_q(table)} (実績ID)")
    return sqls


def ensure_tables(conn: sqlite3.Connection) -> None:
    """手元の実績の表を作る(`db.apply_schema` から呼ばれる。冪等)。"""
    sqls = create_sql(config.TBL_PT_HEADER,
                      HEADER_COLUMNS + HEADER_LOCAL_COLUMNS, header=True)
    for table, columns, _order in DETAIL_TABLES:
        sqls += create_sql(table, columns, header=False)
    sqls.append(f"CREATE TABLE IF NOT EXISTS {_q(TBL_PT_DELETED)} ("
                "取込元実績ID INTEGER, 送信ID TEXT, 登録日時 TEXT)")
    for sql in sqls:
        conn.execute(sql)
    conn.commit()


# ------------------------------------------------------------------
# 値の扱い(VBA `DbValue` / `PtLong` / `B2L` / `TagMark`)
# ------------------------------------------------------------------
def db_value(value: Any) -> Any:
    """空文字は NULL で書く(VBA `DbValue`。空文字の扱いに依存しないため)。"""
    if isinstance(value, str) and value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    return value


def pt_long(value: Any) -> int:
    """NULL・空・数でないものは 0(VBA `PtLong`)。"""
    if value is None or isinstance(value, bool):
        return int(bool(value)) if isinstance(value, bool) else 0
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def nv_str(value: Any) -> str:
    """NULL は空文字(VBA `NvStr`)。"""
    return "" if value is None else str(value)


def b2l(flag: bool) -> int:
    return 1 if flag else 0


_TAG_MARKS = {"幅補填": "[幅]", "丈補填": "[丈]", "カット前提": "[切]", "Y積み": "[Y]"}


def tag_mark(tag: str) -> str:
    """一覧のボード構成に添える印(VBA `TagMark`)。"""
    return _TAG_MARKS.get((tag or "").strip(), "")


# ------------------------------------------------------------------
# プロテック確定値(VBA `ProtecToText` / `ProtecFromText`)
# ------------------------------------------------------------------
# VBA と同じ16個を同じ順に書く。**17個目は Python版だけの値**
# (`eff_width_before_cut`)で、VBA の `ProtecFromText` は16個目までしか
# 読まないので、足しても VBA 側の読み込みは壊れない。逆に VBA が書いた
# 16個の文字列を読んだときは、カット前の実効幅をカット後の値で埋める
_PROTEC_FIELDS: tuple[tuple[str, str], ...] = (
    ("valid", "bool"),
    ("orig_width", "int"), ("orig_length", "int"),
    ("is_rotated", "bool"),
    ("cut_eff_width", "int"), ("eff_length", "int"),
    ("need_cut", "bool"),
    ("count", "int"),
    ("need_length_cut", "bool"),
    ("len_normal_cnt", "int"), ("len_cut_cnt", "int"),
    ("length_cut_eff", "int"),
    ("len_cut_optional", "bool"),
    ("len_opt_normal_cnt", "int"), ("len_opt_cut_cnt", "int"),
    ("len_opt_cut_eff", "int"),
    ("eff_width_before_cut", "int"),          # Python版だけ(17個目)
)
_PROTEC_VBA_COUNT = 16


def protec_to_text(result: Optional[ProtecCutResult]) -> str:
    """確定値をカンマ区切りの文字列にする。**有効でなければ空文字。**"""
    if result is None or not result.valid:
        return ""
    values = []
    for name, kind in _PROTEC_FIELDS:
        value = getattr(result, name)
        values.append(str(b2l(value) if kind == "bool" else int(value)))
    return ",".join(values)


def protec_from_text(text: str) -> ProtecCutResult:
    """文字列から確定値を戻す。読めなければ空(`valid=False`)。"""
    result = ProtecCutResult()
    parts = [p.strip() for p in (text or "").split(",")]
    if not (text or "").strip() or len(parts) < _PROTEC_VBA_COUNT:
        return result
    for (name, kind), raw in zip(_PROTEC_FIELDS, parts):
        value = pt_long(raw)
        setattr(result, name, value != 0 if kind == "bool" else value)
    if len(parts) <= _PROTEC_VBA_COUNT:
        # VBA が書いた文字列。カット前の実効幅は持っていない
        result.eff_width_before_cut = result.cut_eff_width
    return result


# ------------------------------------------------------------------
# 保存(VBA `SavePatternSnapshot`)
# ------------------------------------------------------------------
_HEADER_NAMES = frozenset(n for n, _ in HEADER_COLUMNS)
_DETAIL_NAMES = {table: frozenset(n for n, _ in cols)
                 for table, cols, _order in DETAIL_TABLES}


def _check_names(table: str, row: dict[str, Any], allowed: frozenset[str]) -> None:
    """知らない列名は断る(VBA は `rs.Fields(k)` がエラーになる)。

    列名は SQL に埋め込むので、ここで表の定義にある名前だけに絞る。
    """
    unknown = [k for k in row if k not in allowed]
    if unknown:
        raise PatternStoreError(f"{table} に無い列です: {', '.join(unknown)}")


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
    cols = ", ".join(_q(k) for k in values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {_q(table)} ({cols}) VALUES ({marks})",
                       [db_value(v) for v in values.values()])
    return int(cur.lastrowid or 0)


def save_pattern_snapshot(conn: sqlite3.Connection, header: dict[str, Any],
                          select_rows: list[dict[str, Any]],
                          place_rows: list[dict[str, Any]],
                          cut_rows: list[dict[str, Any]]) -> int:
    """ヘッダ+明細3種を**1つのトランザクションで**保存し、実績IDを返す。

    途中で失敗したら全部戻す(ヘッダだけ残る・明細が半分だけ、を作らない)。
    保存形式・登録日時・更新日時・使用回数・送信IDはここで決める。
    """
    _check_names(config.TBL_PT_HEADER, header, _HEADER_NAMES)
    for table, rows in ((config.TBL_PT_SELECT, select_rows),
                        (config.TBL_PT_PLACE, place_rows),
                        (config.TBL_PT_CUT, cut_rows)):
        for row in rows:
            _check_names(table, row, _DETAIL_NAMES[table])

    now = db.now_db_string()
    values = dict(header)
    values.update({"保存形式": config.PT_FORMAT_VER, "登録日時": now,
                   "更新日時": now, "使用回数": 0,
                   "送信ID": uuid.uuid4().hex})
    try:
        with conn:
            new_id = _insert(conn, config.TBL_PT_HEADER, values)
            if new_id <= 0:
                raise PatternStoreError("実績IDの採番に失敗しました")
            for table, rows in ((config.TBL_PT_SELECT, select_rows),
                                (config.TBL_PT_PLACE, place_rows),
                                (config.TBL_PT_CUT, cut_rows)):
                for row in rows:
                    _insert(conn, table, {"実績ID": new_id, **row})
    except sqlite3.Error as exc:
        log.exception("save_pattern_snapshot エラー(ロールバック済)")
        raise PatternStoreError(str(exc)) from exc
    log.info("save_pattern_snapshot: 保存完了 実績ID=%s 選定=%s 配置=%s カット=%s",
             new_id, len(select_rows), len(place_rows), len(cut_rows))
    return new_id


# ------------------------------------------------------------------
# 読込(VBA `LoadPatternSnapshot`)
# ------------------------------------------------------------------
@dataclass
class Snapshot:
    header: dict[str, Any]
    select_rows: list[dict[str, Any]] = field(default_factory=list)
    place_rows: list[dict[str, Any]] = field(default_factory=list)
    cut_rows: list[dict[str, Any]] = field(default_factory=list)


def _read_rows(conn: sqlite3.Connection, sql: str,
               params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    """SELECT の結果を「列名→値」の辞書の並びにする(VBA `ReadRows`)。"""
    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def _details(conn: sqlite3.Connection, pattern_id: int) -> list[list[dict[str, Any]]]:
    out = []
    for table, _cols, order in DETAIL_TABLES:
        out.append(_read_rows(
            conn, f"SELECT * FROM {_q(table)} WHERE 実績ID = ? ORDER BY {order}",
            (pattern_id,)))
    return out


def load_pattern_snapshot(conn: sqlite3.Connection, pattern_id: int) -> Snapshot:
    """ヘッダ+明細3種を読み、**使用回数を+1する**。

    見つからない・保存形式が違うときは `PatternStoreError`。使用回数は
    手元で足し、取り込み元へは `使用回数未反映` として後から足す
    (`pattern_sync`)── 別の端末で同時に読んでも数が消えない。
    """
    headers = _read_rows(
        conn, f"SELECT * FROM {_q(config.TBL_PT_HEADER)} WHERE 実績ID = ?",
        (pattern_id,))
    if not headers:
        raise PatternStoreError(f"実績が見つかりません(ID: {pattern_id})")
    header = headers[0]
    if pt_long(header.get("保存形式")) != config.PT_FORMAT_VER:
        raise PatternStoreError(
            f"保存形式が異なるため読み込めません(形式: {pt_long(header.get('保存形式'))})")

    select_rows, place_rows, cut_rows = _details(conn, pattern_id)
    try:
        with conn:
            conn.execute(
                f"UPDATE {_q(config.TBL_PT_HEADER)} SET "
                "使用回数 = COALESCE(使用回数, 0) + 1, "
                "使用回数未反映 = COALESCE(使用回数未反映, 0) + 1, "
                "更新日時 = ? WHERE 実績ID = ?",
                (db.now_db_string(), pattern_id))
    except sqlite3.Error as exc:
        raise PatternStoreError(str(exc)) from exc
    log.info("load_pattern_snapshot: 実績ID=%s 選定=%s 配置=%s カット=%s",
             pattern_id, len(select_rows), len(place_rows), len(cut_rows))
    return Snapshot(header=header, select_rows=select_rows,
                    place_rows=place_rows, cut_rows=cut_rows)


# ------------------------------------------------------------------
# 一覧(VBA `GetPatternList`)
# ------------------------------------------------------------------
@dataclass
class PatternSummary:
    """一覧の1行。VBA の辞書キー(ID / 製品幅 / 製品丈 / 登録日時 /
    使用回数 / 配置方式 / ボード構成)と同じものを持つ。"""

    id: int
    product_width: int
    product_length: int
    registered_at: str
    usage_count: int
    method: str
    board_summary: str
    # 取り込み元へまだ届いていない(この端末でしか見えない)
    unsent: bool = False


def _piece(row: dict[str, Any]) -> str:
    return (f"{pt_long(row.get('幅'))}x{pt_long(row.get('丈'))}"
            f"({pt_long(row.get('枚数'))}){tag_mark(nv_str(row.get('タグ')))}")


def get_pattern_list(conn: sqlite3.Connection, pallet_width: int = 0,
                     pallet_length: int = 0) -> list[PatternSummary]:
    """実績の一覧(新しい順)。パレット幅・丈が両方あるときだけ絞り込む。

    **保存形式が違うものは出さない**(読めないものを並べない)。
    """
    sql = (f"SELECT * FROM {_q(config.TBL_PT_HEADER)} WHERE 保存形式 = ?")
    params: list[Any] = [config.PT_FORMAT_VER]
    if pallet_width > 0 and pallet_length > 0:
        sql += " AND パレット幅 = ? AND パレット丈 = ?"
        params += [pallet_width, pallet_length]
    sql += " ORDER BY 実績ID DESC"
    headers = _read_rows(conn, sql, params)
    if not headers:
        return []

    ids = [h["実績ID"] for h in headers]
    marks = ", ".join("?" for _ in ids)
    lower: dict[int, list[str]] = {}
    upper: dict[int, list[str]] = {}
    for row in _read_rows(
            conn, f"SELECT * FROM {_q(config.TBL_PT_SELECT)} "
                  f"WHERE 実績ID IN ({marks}) ORDER BY 実績ID, 行順", ids):
        bucket = upper if nv_str(row.get("区分")) == CATEGORY_UPPER else lower
        bucket.setdefault(row["実績ID"], []).append(_piece(row))

    out = []
    for h in headers:
        pid = h["実績ID"]
        method = nv_str(h.get("配置方式"))
        out.append(PatternSummary(
            id=pt_long(pid),
            product_width=pt_long(h.get("製品幅")),
            product_length=pt_long(h.get("製品丈")),
            registered_at=nv_str(h.get("登録日時")),
            usage_count=pt_long(h.get("使用回数")),
            method=method,
            board_summary=(f"[{method}] 下:{' '.join(lower.get(pid, []))}"
                           f" / 上:{' '.join(upper.get(pid, []))}"),
            unsent=h.get("取込元実績ID") is None,
        ))
    return out


# ------------------------------------------------------------------
# 削除(VBA `DeletePatternByID`)
# ------------------------------------------------------------------
def delete_pattern_by_id(conn: sqlite3.Connection, pattern_id: int) -> bool:
    """ヘッダ+明細3種を**1つのトランザクションで**消す。無ければ False。

    取り込み元にも消してもらうため、**消したことを覚えておく**
    (`実績削除待ち`)。覚えずに手元だけ消すと、次の取り込みで戻ってくる。
    """
    headers = _read_rows(
        conn, f"SELECT 取込元実績ID, 送信ID FROM {_q(config.TBL_PT_HEADER)} "
              "WHERE 実績ID = ?", (pattern_id,))
    if not headers:
        return False
    source_id, send_id = headers[0]["取込元実績ID"], headers[0]["送信ID"]
    try:
        with conn:
            for table, _cols, _order in DETAIL_TABLES:
                conn.execute(f"DELETE FROM {_q(table)} WHERE 実績ID = ?",
                             (pattern_id,))
            conn.execute(f"DELETE FROM {_q(config.TBL_PT_HEADER)} WHERE 実績ID = ?",
                         (pattern_id,))
            # 取り込み元に届いている(かもしれない)ものは、向こうでも消す。
            # 送信IDは「送れたが、送れたと書く前に落ちた」ぶんも拾える
            if source_id is not None or send_id:
                conn.execute(
                    f"INSERT INTO {_q(TBL_PT_DELETED)} (取込元実績ID, 送信ID, 登録日時) "
                    "VALUES (?, ?, ?)", (source_id, send_id, db.now_db_string()))
    except sqlite3.Error as exc:
        raise PatternStoreError(str(exc)) from exc
    log.info("delete_pattern_by_id: 削除完了 実績ID=%s", pattern_id)
    return True
