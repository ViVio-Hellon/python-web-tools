"""仕掛ロットの一覧を、条件で絞って引く

【なぜ要るのか】
ロット番号を打って1件引く方式は、**番号が分かっているとき**しか使えません。
現場は「あの厚みの、あの用途のやつ」から探すこともあるので、一覧を出して
絞り込めるようにします。

【判断はここが持つ】
どの列で絞れるか・どの演算子が使えるか・どう並べるかは全部ここが決めます。
画面は組み立てた条件を表示して押すだけで、**一覧に無い列や演算子は
指定できません**(設計書 §1 の「一覧に無いものは選べない」)。
画面で弾くだけでなく、ここでも確かめます ── 要求は直接投げられます。

【SQLの作り方】
値は必ずプレースホルダで渡します。列名は**この表にあるものしか使わない**
ので、SQL に埋め込んでも外から差し込まれる余地はありません。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from . import db
from .logging_utils import get_logger

log = get_logger("lot_query")

TABLE = "仕掛ロット"
# 引当の有無フラグ(下の `HIKI_EXISTS`)を出すためだけに参照する
TABLE_HIKI = "仕掛引当"

# 断りの種類。**文言から推し量らない**(設計書 §1)
REFUSE_BAD_INPUT = "bad_input"      # 数値でない・空 → 400
REFUSE_NOT_LISTED = "not_listed"    # 一覧に無い列/演算子を指した → 400

TYPE_TEXT = "text"
TYPE_NUMBER = "number"

# 演算子。記号は画面にそのまま出す(現場が読める形)
OP_EQ = "="
OP_NE = "≠"
OP_GT = ">"
OP_LT = "<"
OP_GE = "≥"
OP_LE = "≤"
OP_CONTAINS = "含む"

_SQL_BY_OP = {OP_EQ: "=", OP_NE: "<>", OP_GT: ">", OP_LT: "<",
              OP_GE: ">=", OP_LE: "<="}

OPS_TEXT = (OP_EQ, OP_NE, OP_CONTAINS)
OPS_NUMBER = (OP_EQ, OP_NE, OP_GE, OP_LE, OP_GT, OP_LT)


@dataclass(frozen=True)
class Column:
    """一覧に出す列。**絞り込みと並べ替えの対象もこれだけ**。

    見えていない列で絞れると、なぜその件数になったのかが読めなくなる。
    列を増やすときはここに1行足せば、一覧・絞り込み・並べ替えの
    すべてに同時に効く。
    """

    key: str          # 画面とAPIで使う識別子(ASCII)
    label: str        # 見出し。現場が使う語
    source: str       # 行から値を取り出すときのキー(実列名、計算列なら別名)
    kind: str = TYPE_TEXT
    decimals: int = 0   # 数値の表示桁
    # SQL 上の読み方。既定は素の列名。仕掛ロットに実体が無い**計算列**
    # (引当有無)だけがここに式を持ち、`SELECT` では `source` の別名を付ける。
    # WHERE には別名が使えないので、絞り込みと横断検索はこの式を使う
    expr: str = ""
    # その列が取りうる値がこれだけと決まっているなら並べる(引当有無の 0/1)。
    # 「候補は実際にデータにある値から作る」という `suggest` の約束を守るため
    # ── 空なら値の候補はデータから引く
    choices: tuple[str, ...] = ()

    @property
    def sql(self) -> str:
        """WHERE / DISTINCT で使う読み方(実列も計算列もこれ1つで済む)。"""
        return self.expr or f'"{self.source}"'

    @property
    def computed(self) -> bool:
        return bool(self.expr)

    @property
    def ops(self) -> tuple[str, ...]:
        return OPS_NUMBER if self.kind == TYPE_NUMBER else OPS_TEXT


# 同じロット番号が仕掛引当(SIKAHIKI)にあるか。有=1 / 無=0。
# `EXISTS` は SQLite ではそのまま 1/0 を返すので CASE は要らない。
# 仕掛引当(ロット番号)には索引があるので、行ごとに引いても速い
HIKI_EXISTS = (f'(EXISTS (SELECT 1 FROM {TABLE_HIKI} h '
               f'WHERE h."ロット番号" = {TABLE}."ロット番号"))')


# 一覧の列。並びがそのまま表の左からの順になる
# 一覧の列。並びがそのまま表の左からの順になる。
#
# **取り込んだ列は全部出す。** 以前は8列に絞っていたが、絞った側の列
# (オーダー寸法・設備コース・BOX実績)を見たい場面で、一覧からは
# 分からず1件ずつ詳細を開くことになっていた。取り込みが持っている事実は
# 一覧に出しておいて、**目で探せる**ようにする。
# 出す列を増やせば、絞り込みと並べ替えにも同時に効く。
COLUMNS: tuple[Column, ...] = (
    Column("lot_no", "ロット番号", "ロット番号"),
    # 引当があるか(有=1 / 無=0)。仕掛ロットには無い**計算列**で、
    # 同じロット番号が仕掛引当(SIKAHIKI)にあるかどうかだけを見る。
    # ロット番号のすぐ隣に置く ── 何番のロットの話かと切り離すと読めない
    # (この列だけ見ても意味を成さない)。絞り込み・並べ替えにも効くので、
    # 「引当のあるロットだけ」を一覧で作れる
    Column("hiki", "引当有無", "引当有無", TYPE_NUMBER,
           expr=HIKI_EXISTS, choices=("0", "1")),
    Column("yoto_code", "用途コード", "用途コード"),
    Column("yoto_name", "用途名", "用途名"),
    Column("zaishitsu", "製造材質", "製造材質"),
    Column("choshitsu", "製造調質", "製造調質"),
    Column("thickness", "製造板厚", "製造板厚", TYPE_NUMBER, decimals=3),
    Column("width", "製造板幅", "製造板幅", TYPE_NUMBER, decimals=1),
    Column("length", "製造板丈", "製造板丈", TYPE_NUMBER, decimals=1),
    # 指図の寸法。製造の寸法と並べて見ると、削り代が読める
    Column("order_thickness", "オーダー板厚", "オーダー板厚", TYPE_NUMBER, decimals=3),
    Column("order_width", "オーダー板幅", "オーダー板幅", TYPE_NUMBER, decimals=1),
    Column("order_length", "オーダー板丈", "オーダー板丈", TYPE_NUMBER, decimals=1),
    # どの設備を通る/通ったか
    Column("course_plan", "設計_設備コース", "設計_設備コース"),
    Column("course_actual", "実績_設備コース", "実績_設備コース"),
    # BOX工程の実績。ロット番号は工程ごとに行が分かれるので、
    # ここが違えば「同じロット番号の別の行」だと分かる
    Column("box_thickness", "BOX実績_板厚", "BOX実績_板厚", TYPE_NUMBER, decimals=3),
    Column("box_width", "BOX実績_板幅", "BOX実績_板幅", TYPE_NUMBER, decimals=1),
    Column("box_length", "BOX実績_板丈", "BOX実績_板丈", TYPE_NUMBER, decimals=1),
    Column("box_count", "BOX実績_枚本数", "BOX実績_枚本数", TYPE_NUMBER),
    # 試験指示票(先行データ)の要否判定に使う値(VBA `AdvanceCheck`)
    Column("grade_surface", "品質グレード_表面処理", "品質グレード_表面処理"),
)

BY_KEY = {c.key: c for c in COLUMNS}

# 表示件数。**青天井にしない** ── 2,000件超を一度に描くと画面が固まる
PAGE_SIZES = (50, 100, 200, 500)
DEFAULT_PAGE_SIZE = 200

# 既定の並び。ロット番号の昇順(現場が番号で覚えているため)
DEFAULT_SORT = "lot_no"

# 「検索して条件を追加」の候補をいくつまで出すか。
# 多すぎると読む手間のほうが大きくなる
SUGGEST_PER_COLUMN = 5
SUGGEST_TOTAL = 12


@dataclass(frozen=True)
class Condition:
    """絞り込み1つ。画面ではチップ1つに対応する。"""

    column: str
    op: str
    value: str

    @property
    def col(self) -> Column:
        return BY_KEY[self.column]

    @property
    def label(self) -> str:
        """チップに出す文言。例「製造板厚 ≥ 3」"""
        return f"{self.col.label} {self.op} {self.value}"

    def to_dict(self) -> dict[str, str]:
        return {"column": self.column, "op": self.op, "value": self.value,
                "label": self.label}


@dataclass
class Refusal:
    message: str
    reason: str


def make_condition(column: str, op: str, value: Any) -> tuple[
        Optional[Condition], Optional[Refusal]]:
    """条件を1つ作る。**作れないものは作らせない**。

    列も演算子も一覧にあるものだけ。数値の列に数字でないものを渡したら
    その場で断る ── SQL に渡してから0件になると、条件が悪いのか
    データが無いのか区別できない。
    """
    col = BY_KEY.get(column)
    if col is None:
        return None, Refusal(f"「{column}」は一覧にない列です。", REFUSE_NOT_LISTED)
    if op not in col.ops:
        return None, Refusal(
            f"{col.label} に「{op}」は使えません。"
            f"使えるのは {' / '.join(col.ops)} です。", REFUSE_NOT_LISTED)

    text = str(value).strip()
    if not text:
        return None, Refusal(f"{col.label} の値を入れてください。", REFUSE_BAD_INPUT)
    if col.kind == TYPE_NUMBER:
        try:
            float(text)
        except ValueError:
            return None, Refusal(
                f"{col.label} には数値を入れてください。", REFUSE_BAD_INPUT)
    return Condition(column=col.key, op=op, value=text), None


# ==================================================================
# SQL の組み立て
# ==================================================================
# ロット番号は BOX工程ごとに複数行ありうる。1件検索(`lot_service`)は
# **取り込み順の先頭だけ**を見るので、一覧も同じ1行しか出さない。
# 全部出すと、押しても同じ内容が出る行が並び、押し分けたつもりの人が
# 「押した行と違うものが出た」と受け取る
_REPRESENTATIVE = (
    f'"管理番号" = (SELECT MIN(s."管理番号") FROM {TABLE} s '
    f'WHERE s."ロット番号" = {TABLE}."ロット番号")')

# 計算列(`Column.expr` を持つもの)は実体が無いので、`SELECT *` に
# 別名付きで足す。これで一覧・並べ替えとも実列と同じ扱いにできる
_SELECT = ", ".join(
    ["*"] + [f'{c.expr} AS "{c.source}"' for c in COLUMNS if c.computed])


def _where(conditions: list[Condition], text: str) -> tuple[str, list[Any]]:
    clauses: list[str] = [_REPRESENTATIVE]
    params: list[Any] = []

    for cond in conditions:
        col = cond.col
        name = col.sql
        if cond.op == OP_CONTAINS:
            clauses.append(f"{name} LIKE ?")
            params.append(f"%{cond.value}%")
        elif col.kind == TYPE_NUMBER:
            clauses.append(f"{name} {_SQL_BY_OP[cond.op]} ?")
            params.append(float(cond.value))
        else:
            clauses.append(f"{name} {_SQL_BY_OP[cond.op]} ?")
            params.append(cond.value)

    text = (text or "").strip()
    if text:
        # 一覧検索は**出ている列すべて**を横断する。どの列に入っているか
        # 分からないから打つので、列を指定させるのは筋が違う
        ors = " OR ".join(f"CAST({c.sql} AS TEXT) LIKE ?" for c in COLUMNS)
        clauses.append(f"({ors})")
        params.extend([f"%{text}%"] * len(COLUMNS))

    return " WHERE " + " AND ".join(clauses), params


def count(conn: sqlite3.Connection, conditions: list[Condition],
          text: str = "") -> int:
    """条件に合う総数。**表示件数で切る前の数**。

    「200件表示」と出ているとき、それが全部なのか切られたのかが
    分からないと、探し漏れに気づけない。
    """
    where, params = _where(conditions, text)
    row = db.fetch_one(conn, f"SELECT COUNT(*) AS c FROM {TABLE}{where}",
                       tuple(params), caller_name="lot_query.count")
    return int(row["c"]) if row else 0


def fetch(conn: sqlite3.Connection, conditions: list[Condition], *,
          text: str = "", sort: str = DEFAULT_SORT, descending: bool = False,
          limit: int = DEFAULT_PAGE_SIZE) -> list[sqlite3.Row]:
    """条件に合う行を引く。"""
    col = BY_KEY.get(sort) or BY_KEY[DEFAULT_SORT]
    where, params = _where(conditions, text)
    # 計算列は `SELECT` で別名を付けてあるので、並べ替えは実列と同じく
    # 別名で書ける(WHERE と違って ORDER BY は別名を使える)
    order = f'ORDER BY "{col.source}" {"DESC" if descending else "ASC"}'
    # 同値のときの並びを固定する。安定しないと、同じ条件で開き直すたびに
    # 行が入れ替わって「さっき見た行」が見つからなくなる
    order += ', "ロット番号" ASC, "管理番号" ASC'
    return db.fetch_all(
        conn, f"SELECT {_SELECT} FROM {TABLE}{where} {order} LIMIT ?",
        tuple(params) + (int(limit),), caller_name="lot_query.fetch") or []


# ==================================================================
# 「検索して条件を追加」の候補
# ==================================================================
@dataclass(frozen=True)
class Suggestion:
    """打った文字から作れる条件の候補。"""

    kind: str          # "condition" | "saved"
    label: str
    column: str = ""
    op: str = ""
    value: str = ""
    name: str = ""     # 保存フィルタの名前

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "label": self.label, "column": self.column,
                "op": self.op, "value": self.value, "name": self.name}


def suggest(conn: sqlite3.Connection, text: str,
            saved: Optional[dict[str, Any]] = None) -> list[Suggestion]:
    """打った文字から「足せる条件」を組み立てる。

    候補は**実際にデータにある値**から作ります ── 0件になる条件を
    勧めても意味がありません。数値の列は帯(以上/以下)も出します。
    """
    text = (text or "").strip()
    out: list[Suggestion] = []

    # 保存フィルタ(名前が部分一致するもの)
    for name in sorted(saved or {}):
        if not text or text in name:
            out.append(Suggestion(kind="saved", label=name, name=name))

    if not text:
        return out[:SUGGEST_TOTAL]

    # 数値として読めるなら、数値の列に「= / ≥ / ≤」を出す
    numeric = None
    try:
        numeric = float(text)
    except ValueError:
        pass

    def _rank(c: Column) -> int:
        # 打った文字がその列の取りうる値そのものなら真っ先に出す。
        # 引当有無は値が 0/1 しか無いので、ちょうど「1」と打ったのなら
        # それを探している見込みが高い。候補は12件で打ち切られるため、
        # ここで前に出しておかないと数値列3件×4列に埋もれて一度も出ない
        if c.choices:
            return -1 if text in c.choices else 1
        # **数値を打ったなら数値の列を先に出す。** 「3」と打つ人が探して
        # いるのはたいてい板厚で、「3を含むロット番号」ではない
        return 0 if (numeric is not None and c.kind == TYPE_NUMBER) else 1

    ordered = sorted(COLUMNS, key=_rank)
    for col in ordered:
        if len(out) >= SUGGEST_TOTAL:
            break
        if col.choices:
            # 取りうる値が決まっている列(引当有無の 0/1)。打った文字が
            # その値でなければ勧めない ── 「引当有無 = 3」のような、
            # 絶対に0件になる条件を候補に出さないため。
            # 帯(≥ / ≤)も 0/1 の2値には意味が無いので `=` だけにする
            if text in col.choices:
                out.append(Suggestion(
                    kind="condition", column=col.key, op=OP_EQ, value=text,
                    label=f"{col.label} = {text}"))
            continue
        if col.kind == TYPE_NUMBER:
            if numeric is None:
                continue
            for op in (OP_EQ, OP_GE, OP_LE):
                out.append(Suggestion(
                    kind="condition", column=col.key, op=op, value=text,
                    label=f"{col.label} {op} {text}"))
            continue

        rows = db.fetch_all(
            conn,
            f"SELECT DISTINCT {col.sql} AS v FROM {TABLE} "
            f"WHERE {_REPRESENTATIVE} AND {col.sql} LIKE ? "
            f"AND {col.sql} <> '' "
            f"ORDER BY {col.sql} LIMIT ?",
            (f"%{text}%", SUGGEST_PER_COLUMN),
            caller_name="lot_query.suggest") or []
        for row in rows:
            value = str(row["v"])
            out.append(Suggestion(
                kind="condition", column=col.key, op=OP_EQ, value=value,
                label=f"{col.label} = {value}"))

    return out[:SUGGEST_TOTAL]


def format_value(col: Column, value: Any) -> str:
    """一覧のセルに出す文字。"""
    if value is None or value == "":
        return ""
    if col.kind != TYPE_NUMBER:
        return str(value)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not col.decimals:
        return str(int(number))
    # 末尾の0は落とす。1.000 より 1 のほうが読みやすい
    return f"{number:.{col.decimals}f}".rstrip("0").rstrip(".") or "0"
