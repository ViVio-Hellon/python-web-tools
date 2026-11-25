"""外注条件 ── 包装仕様NOを決め打つ条件を表(外注条件・外注条件明細)で持つ(VER4.9.4)

現場の声:「外注条件明細のテーブルを追加しました。1P0113 は強制発火させることは可能ですが、やはり
読み込みで発火することも必要なので、テーブルの条件を読んで発火させてください」。
いままでプログラムに書いていた 1P0122 の判定(VBA の flag(1)〜flag(5)。`lot_service`)も、この表で持つ。

【表の読み方】
`外注条件` の1行が1つの条件。`優先順位` の小さい順に見て、**最初に当たった行の `戻り値`** を
包装仕様NOにする(受注の包装仕様NOを書き換える)。`有効` が 0 の行は見ない。

`外注条件明細` がその条件の中身(`条件管理番号` = 外注条件の `管理番号`)。管理番号の順に読み、

    - **同じ条件番号の行どうしは「または」**(例: 用途ｺｰﾄﾞ = K434 / 用途ｺｰﾄﾞ = K435)
    - **違う条件番号どうしは「かつ」**(材質 かつ 調質 かつ 用途 かつ 厚 かつ コース)
    - **条件番号が前の行より小さく戻ったら、そこから別の組**(組どうしは「または」)。
      例: 外注梱包_52S裸 は 1〜5(用途 H19S・厚 322.5以上)と、もう一度 1〜5(用途 H197・厚 100以上)の2組。
      戻りを見ないと「H19S で厚 100」も当たってしまう

対象項目(半角カナ・全角どちらでも): 材質(製造材質)・調質(製造調質)・用途ｺｰﾄﾞ・厚(製造板厚)・
幅(製造板幅)・丈(製造板丈)・ｵｰﾀﾞｰ厚/幅/丈・ｺｰｽ(設計_設備ｺｰｽ)。
判定方法: = / <> / Like(* と ? を使う。Access と同じ)/ 含む / 含まない / >= / <= / > / <。
知らない対象項目・判定方法の行は**当たらない**(勝手に読み替えない)。

【表が無いとき】
手元の表が空なら初期値(`SEED`。現場の表と同じ中身)を入れる。取り込み元に表が無くても前と同じに動く。
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

from .logging_utils import get_logger

log = get_logger("outsource_rules")

TABLE = "外注条件"
DETAIL_TABLE = "外注条件明細"

# 初期値(現場の梱包資材マスタの表と同じ中身。2026-11)
SEED: tuple[dict[str, Any], ...] = (
    {"管理番号": 1, "条件名": "外注梱包_75S系", "戻り値": "1P0122", "優先順位": 10, "有効": 1,
     "備考": "引当通りでないため強制表示(2023.07.07)"},
    {"管理番号": 2, "条件名": "外注梱包_52S裸", "戻り値": "1P0113", "優先順位": 20, "有効": 1,
     "備考": "裸梱包(2024.12.10)"},
)
_D = ("管理番号", "条件管理番号", "条件番号", "対象項目", "判定方法", "判定値", "有効", "備考")
DETAIL_SEED: tuple[dict[str, Any], ...] = tuple(dict(zip(_D, row)) for row in (
    (1, 1, 1, "材質", "Like", "*75S", 1, ""),
    (2, 1, 2, "調質", "Like", "T6*", 1, ""),
    (3, 1, 3, "用途ｺｰﾄﾞ", "=", "K434", 1, ""),
    (4, 1, 3, "用途ｺｰﾄﾞ", "=", "K435", 1, "2024.5.23_用途コード追加"),
    (5, 1, 4, "厚", "=", "6.75", 1, ""),
    (6, 1, 5, "ｺｰｽ", "含む", "GCT", 1, ""),
    (7, 1, 5, "ｺｰｽ", "含む", "GFS", 1, ""),
    (8, 1, 5, "ｺｰｽ", "含む", "GSS", 1, "2024.9.17追加"),
    (9, 2, 1, "材質", "Like", "*52S", 1, ""),
    (10, 2, 2, "調質", "=", "R", 1, ""),
    (11, 2, 3, "用途ｺｰﾄﾞ", "=", "H19S", 1, ""),
    (12, 2, 4, "厚", ">=", "322.5", 1, "2026.09.18_350から変更"),
    (13, 2, 5, "ｺｰｽ", "含む", "GCT", 1, ""),
    (14, 2, 5, "ｺｰｽ", "含む", "GFS", 1, ""),
    (15, 2, 5, "ｺｰｽ", "含む", "GSS", 1, ""),
    (16, 2, 1, "材質", "Like", "*52S", 1, ""),
    (17, 2, 2, "調質", "=", "R", 1, ""),
    (18, 2, 3, "用途ｺｰﾄﾞ", "=", "H197", 1, ""),
    (19, 2, 4, "厚", ">=", "100.0", 1, "2026.10.01"),
    (20, 2, 5, "ｺｰｽ", "含む", "GFS", 1, ""),
))


def _norm(text: Any) -> str:
    """半角カナ・全角英数をそろえ、空白を除く(ｺｰｽ と コース を同じに)。"""
    return "".join(unicodedata.normalize("NFKC", str(text or "")).split())


# 対象項目 → ロットの値を取り出す関数の名前(`LotInfo` の属性)
ITEMS: dict[str, str] = {
    "材質": "zaishitsu", "製造材質": "zaishitsu",
    "調質": "choshitsu", "製造調質": "choshitsu",
    "用途コード": "yoto_code",
    "厚": "manufactured_thickness", "製造板厚": "manufactured_thickness",
    "幅": "manufactured_width", "製造板幅": "manufactured_width",
    "丈": "manufactured_length", "製造板丈": "manufactured_length",
    "オーダー厚": "order_thickness", "オーダー板厚": "order_thickness",
    "オーダー幅": "order_width", "オーダー板幅": "order_width",
    "オーダー丈": "order_length", "オーダー板丈": "order_length",
    "コース": "design_course", "設計_設備コース": "design_course",
}
OPERATORS = ("=", "<>", "LIKE", "含む", "含まない", ">=", "<=", ">", "<")
EPS = 1e-6


def _number(value: Any) -> Optional[float]:
    try:
        text = str(value).strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _like(value: str, pattern: str) -> bool:
    """Access の Like(* と ?。# は数字1つ)。大文字小文字は見ない。"""
    regex = "".join(".*" if c == "*" else "." if c == "?" else r"\d" if c == "#" else re.escape(c)
                    for c in pattern.upper())
    return re.fullmatch(regex, value.upper()) is not None


def compare(value: Any, op: str, target: Any) -> bool:
    """1行の判定。知らない判定方法は当てない。"""
    op = _norm(op).upper()
    text, want = _norm(value).upper(), _norm(target).upper()
    if op == "LIKE":
        return _like(text, want)
    if op == "含む":
        return bool(want) and want in text
    if op == "含まない":
        return not (want and want in text)
    a, b = _number(text), _number(want)
    if op in ("=", "<>"):
        same = abs(a - b) < EPS if a is not None and b is not None else text == want
        return same if op == "=" else not same
    if op in (">=", "<=", ">", "<"):
        if a is None or b is None:
            return False
        return {">=": a >= b - EPS, "<=": a <= b + EPS, ">": a > b + EPS, "<": a < b - EPS}[op]
    return False


@dataclass(frozen=True)
class Detail:
    """明細の1行。"""

    number: int
    item: str
    op: str
    target: str

    def known(self) -> bool:
        return _norm(self.item) in ITEMS and _norm(self.op).upper() in OPERATORS

    def matches(self, lot: Any) -> bool:
        attr = ITEMS.get(_norm(self.item))
        if attr is None or not self.known():
            return False
        return compare(getattr(lot, attr, ""), self.op, self.target)

    def describe(self) -> str:
        return f"{self.item} {self.op} {self.target}"


@dataclass(frozen=True)
class Rule:
    """条件1つ(外注条件の1行と、その明細)。"""

    key: int
    name: str
    spec: str
    priority: int = 0
    # 組(「または」)→ 条件番号ごとのまとまり(「かつ」)→ 明細(「または」)
    groups: tuple[tuple[tuple[Detail, ...], ...], ...] = field(default_factory=tuple)

    def matches(self, lot: Any) -> bool:
        return any(all(any(d.matches(lot) for d in same) for same in group)
                   for group in self.groups if group)


def _sets(details: list[Detail]) -> tuple[tuple[tuple[Detail, ...], ...], ...]:
    """明細(管理番号の順)を組に分ける。条件番号が前の行より小さく戻ったら、そこから次の組。"""
    out: list[list[Detail]] = []
    last = None
    for d in details:
        if last is None or d.number < last:
            out.append([])
        out[-1].append(d)
        last = d.number
    result = []
    for group in out:
        numbers = sorted({d.number for d in group})
        result.append(tuple(tuple(d for d in group if d.number == n) for n in numbers))
    return tuple(result)


def _rows(conn: sqlite3.Connection, table: str, order: str) -> list[dict[str, Any]]:
    cursor = conn.execute(f'SELECT * FROM "{table}" WHERE COALESCE(有効, 1) <> 0 ORDER BY {order}')
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, tuple(r))) for r in cursor.fetchall()]


def _build(heads: list[dict[str, Any]], details: list[dict[str, Any]]) -> tuple[Rule, ...]:
    by_head: dict[int, list[Detail]] = {}
    for row in details:
        try:
            head = int(row.get("条件管理番号"))
            number = int(row.get("条件番号") or 0)
        except (TypeError, ValueError):
            continue
        by_head.setdefault(head, []).append(Detail(number, str(row.get("対象項目") or ""),
                                                   str(row.get("判定方法") or ""),
                                                   str(row.get("判定値") or "")))
    rules = []
    for row in heads:
        try:
            key = int(row.get("管理番号"))
        except (TypeError, ValueError):
            continue
        spec = _norm(row.get("戻り値")).upper()
        found = by_head.get(key, [])
        if not spec or not found:
            continue                          # 戻り値か明細が無い条件は何にも当てない
        for d in found:
            if not d.known():
                log.warning("%s(%s)の明細「%s」は読めません(この行は当たりません)",
                            row.get("条件名"), key, d.describe())
        rules.append(Rule(key=key, name=str(row.get("条件名") or ""), spec=spec,
                          priority=int(row.get("優先順位") or 0), groups=_sets(found)))
    return tuple(rules)


DEFAULT_RULES: tuple[Rule, ...] = _build([dict(r) for r in SEED], [dict(r) for r in DETAIL_SEED])


def load(conn: Optional[sqlite3.Connection]) -> tuple[Rule, ...]:
    """手元の表の有効な条件。**表を読めなければ初期値**(前と同じに動くように)。"""
    if conn is None:
        return DEFAULT_RULES
    try:
        heads = _rows(conn, TABLE, "優先順位, 管理番号")
        details = _rows(conn, DETAIL_TABLE, "管理番号")
    except sqlite3.Error as exc:
        log.warning("%s を読めません(初期値で判定します): %s", TABLE, exc)
        return DEFAULT_RULES
    return _build(heads, details)


def find(rules: tuple[Rule, ...], lot: Any) -> Optional[Rule]:
    """最初に当たった条件(優先順位の順)。当たらなければ None。"""
    for rule in rules:
        if rule.matches(lot):
            return rule
    return None


def seed(conn: sqlite3.Connection) -> int:
    """手元の表が**空なら**初期値を入れる(外注条件と明細の両方。直した値は書き戻さない)。"""
    made = 0
    for table, rows in ((TABLE, SEED), (DETAIL_TABLE, DETAIL_SEED)):
        try:
            if conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]:
                continue
        except sqlite3.Error as exc:            # pragma: no cover - 通常は無い
            log.warning("%s を数えられません: %s", table, exc)
            continue
        columns = list(rows[0])
        with conn:
            conn.executemany(
                f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in columns)})'
                f' VALUES ({", ".join("?" for _ in columns)})',
                [tuple(r[c] for c in columns) for r in rows])
        made += len(rows)
        log.info("%s に初期値を%s件入れました", table, len(rows))
    return made
