"""1P1185モード(パレットを決め打つ)の発火条件と、決め打つパレット ── 表(パレット限定条件)で持つ

現場の声:「1P1185モードの 包装仕様NO=1P1185・取引先名称に ﾅﾒｶﾜｱﾙﾐ を含む・製造板幅 1242〜1249・
製造板丈 1242〜1249 も、マスタ編集で条件行を足すことで発火条件を外から足せるように。
こっちは発火後のパレットサイズを固定値で決め打つサイズも決めさせて」

【表の読み方】
1行が1つの条件と、当たったときに使うパレット。**どれか1行に当たれば発火**(上から順に、最初に当たった行)。
1行の中は「かつ」:

    包装仕様NO          … 受注の包装仕様NOと同じなら(必須)
    取引先名称          … 受注の取引先名称にこの文字を**含む**なら(空なら見ない)
    製造板幅_最小/最大   … SIKALOT の製造板幅がこの範囲なら(空・0 なら見ない)
    製造板丈_最小/最大   … SIKALOT の製造板丈がこの範囲なら(空・0 なら見ない)
    ----- 発火したら -----
    業界               … この業界のパレットだけ(空なら業界は見ない)
    パレット幅・パレット丈 … この寸法のパレットだけ(必須)

製造板幅・丈は**整数に切り捨ててから**比べる(VBA `CheckAndSet1P1185Mode` の `CLng` 相当。
もとのプログラムも `int()` で比べていた)。寸法の欄を書いたのにロットの寸法が無い(0)ときは当たらない。

【表が無いとき】
起動のたびに手元の表が空なら初期値(`SEED` = もとの 1P1185 の決め打ち)を入れる。取り込み元に表が
まだ無くても、手元の初期値で前と同じに動く。マスタ管理の「取り込み元に作る」で作ると、取り込み元にも
初期値が入る。**最後の1行は消せない**(止めるときは有効を 0 に。`master_admin.KEEP_LAST_ROW`)。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .logging_utils import get_logger
from .protec_rules import _bound, normalize_spec

log = get_logger("pallet_limit_rules")

TABLE = "パレット限定条件"

RANGE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("製造板幅_最小", "製造板幅_最大", "製造板幅"),
    ("製造板丈_最小", "製造板丈_最大", "製造板丈"),
)

# 初期値 = もとのプログラムの決め打ち(`special_packaging` の P1185_* と同じ値)
SEED: tuple[dict[str, Any], ...] = (
    {"包装仕様NO": "1P1185", "取引先名称": "ﾅﾒｶﾜｱﾙﾐ",
     "製造板幅_最小": 1242.0, "製造板幅_最大": 1249.0,
     "製造板丈_最小": 1242.0, "製造板丈_最大": 1249.0,
     "業界": "タイト", "パレット幅": 1300, "パレット丈": 1300,
     "有効": 1, "並び順": 10, "備考": "", "更新日時": ""},
)


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class Rule:
    """条件の1行と、当たったときのパレット。"""

    spec: str
    customer: str = ""
    ranges: tuple[tuple[Optional[float], Optional[float]], ...] = ((None, None),) * 2
    industry: str = ""
    pallet_width: int = 0
    pallet_length: int = 0

    def matches(self, spec: Any, customer: Any, width: Any, length: Any) -> bool:
        if not self.spec or normalize_spec(spec) != self.spec:
            return False
        if self.customer and self.customer not in str(customer or ""):
            return False
        for (low, high), value in zip(self.ranges, (width, length)):
            if low is None and high is None:
                continue
            number = _int(value)                  # もとの判定と同じく整数に切り捨てて比べる
            if number <= 0:
                return False
            if low is not None and number < low:
                return False
            if high is not None and number > high:
                return False
        return True

    @property
    def usable(self) -> bool:
        """当たったときに決め打つパレットが書いてあるか(無い行は発火させない)。"""
        return self.pallet_width > 0 and self.pallet_length > 0

    def pallet_text(self) -> str:
        """「タイト 1300×1300」。"""
        return f"{self.industry + ' ' if self.industry else ''}{self.pallet_width}×{self.pallet_length}"

    def describe(self) -> str:
        """「1P1185 取引先 ﾅﾒｶﾜｱﾙﾐ / 製造板幅 1242〜1249 / 製造板丈 1242〜1249 → タイト 1300×1300」。"""
        parts = []
        if self.customer:
            parts.append(f"取引先 {self.customer}")
        for (low, high), (_, _, name) in zip(self.ranges, RANGE_COLUMNS):
            if low is None and high is None:
                continue
            fmt = lambda v: f"{v:g}"              # noqa: E731
            if low is not None and high is not None:
                text = fmt(low) if low == high else f"{fmt(low)}〜{fmt(high)}"
            else:
                text = f"{fmt(low)}以上" if low is not None else f"{fmt(high)}以下"
            parts.append(f"{name} {text}")
        head = self.spec + (f" {' / '.join(parts)}" if parts else "")
        return f"{head} → {self.pallet_text()}"


def _rule_of(row: dict[str, Any]) -> Rule:
    return Rule(spec=normalize_spec(row.get("包装仕様NO")),
                customer=str(row.get("取引先名称") or "").strip(),
                ranges=tuple((_bound(row.get(lo)), _bound(row.get(hi)))
                             for lo, hi, _ in RANGE_COLUMNS),
                industry=str(row.get("業界") or "").strip(),
                pallet_width=_int(row.get("パレット幅")),
                pallet_length=_int(row.get("パレット丈")))


DEFAULT_RULES: tuple[Rule, ...] = tuple(_rule_of(r) for r in SEED)


def load(conn: Optional[sqlite3.Connection]) -> tuple[Rule, ...]:
    """手元の表の有効な行。**表を読めなければ初期値**(前と同じに動くように)。"""
    if conn is None:
        return DEFAULT_RULES
    try:
        cursor = conn.execute(
            f'SELECT * FROM "{TABLE}" WHERE COALESCE(有効, 1) <> 0 ORDER BY 並び順, rowid')
        names = [d[0] for d in cursor.description]
        rows = [dict(zip(names, tuple(r))) for r in cursor.fetchall()]
    except sqlite3.Error as exc:
        log.warning("%s を読めません(初期値で判定します): %s", TABLE, exc)
        return DEFAULT_RULES
    rules = []
    for rule in map(_rule_of, rows):
        if not rule.spec:
            continue
        if not rule.usable:
            log.warning("%s: パレット幅・丈が無い行は使いません(%s)", TABLE, rule.spec)
            continue
        rules.append(rule)
    return tuple(rules)


def find(rules: Iterable[Rule], spec: Any, customer: Any, width: Any,
         length: Any) -> Optional[Rule]:
    """当たった最初の行。当たらなければ None。"""
    for rule in rules:
        if rule.matches(spec, customer, width, length):
            return rule
    return None


def seed(conn: sqlite3.Connection) -> int:
    """手元の表が**空なら**初期値を入れる。入っている表には触らない(直した値を書き戻さない)。"""
    try:
        count = conn.execute(f'SELECT COUNT(*) FROM "{TABLE}"').fetchone()[0]
    except sqlite3.Error as exc:                 # pragma: no cover - 通常は無い
        log.warning("%s を数えられません: %s", TABLE, exc)
        return 0
    if count:
        return 0
    columns = list(SEED[0])
    col_list = ", ".join(f'"{c}"' for c in columns)
    marks = ", ".join("?" for _ in columns)
    with conn:
        conn.executemany(f'INSERT INTO "{TABLE}" ({col_list}) VALUES ({marks})',
                         [tuple(row[c] for c in columns) for row in SEED])
    log.info("%s に初期値を%s件入れました", TABLE, len(SEED))
    return len(SEED)
