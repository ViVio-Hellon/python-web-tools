"""プロテックモードの発火条件 ── 表(プロテック条件)で持つ

現場の声:「プロテックモードに条件の追加。包装仕様NO 1P1125 は SIKALOT のｵｰﾀﾞｰ板厚 3.0〜15.0、
1P1255 はｵｰﾀﾞｰ厚 12.0 かつ(幅 1075〜1081・丈 2332〜2338 / 幅 1072〜1078・丈 2262〜2268)。
プロテックモードのテーブルを足し、マスタ編集で条件行を足すことで発火条件を外から足せるように」

【表の読み方】
1行が1つの条件。**どれか1行に当たればプロテック**(行どうしは「または」)。
1行の中は「かつ」:

    包装仕様NO      … 受注の包装仕様NOと同じなら(必須)
    オーダー厚_最小/最大 … SIKALOT の ｵｰﾀﾞｰ板厚 がこの範囲なら
    オーダー幅_最小/最大 … SIKALOT の ｵｰﾀﾞｰ板幅 がこの範囲なら
    オーダー丈_最小/最大 … SIKALOT の ｵｰﾀﾞｰ板丈 がこの範囲なら
    有効            … 0 の行は見ない

**範囲の欄が空(または0)なら、その寸法は見ない。** 1P1216 のように包装仕様NOだけで
決まるものは、寸法の欄を全部空にする。厚 12.0 ちょうどは 最小=最大=12 と書く。
寸法の欄を書いたのにロットのオーダー寸法が無い(0)ときは、当たらない(確かめられないので)。

1P1216(コーミ金属)の上用の幅許容は表ではなくプログラムで決まっている
(`special_packaging.PROTEC_SPEC_1P1216` → `board_selection_protec.PROTEC_1P1216_TOLERANCE`)。

【表が無いとき】
起動のたびに手元の表が空なら初期値(`SEED`)を入れる(`db.apply_schema` → `seed`)。
取り込み元に表がまだ無くても、手元の初期値で前と同じに動く。マスタ管理の「取り込み元に作る」で
作ると、取り込み元にも同じ初期値が入る(`master_schema._seed_source`)。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .logging_utils import get_logger

log = get_logger("protec_rules")

TABLE = "プロテック条件"

# (列名, 何の寸法か)
RANGE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("オーダー厚_最小", "オーダー厚_最大", "厚"),
    ("オーダー幅_最小", "オーダー幅_最大", "幅"),
    ("オーダー丈_最小", "オーダー丈_最大", "丈"),
)

# 寸法を比べるときの遊び(小数の誤差だけ。12.0 と 11.9999999 を同じに)
EPS = 1e-6

# 初期値。いままでプログラムに書いてあった3つ(1P1216・1P1211・1P1125)に、
# 現場の声の条件(1P1125 の厚・1P1255 の2行)を入れたもの
_BLANK = {"オーダー厚_最小": None, "オーダー厚_最大": None,
          "オーダー幅_最小": None, "オーダー幅_最大": None,
          "オーダー丈_最小": None, "オーダー丈_最大": None}
SEED: tuple[dict[str, Any], ...] = (
    {**_BLANK, "包装仕様NO": "1P1216", "有効": 1, "並び順": 10,
     "備考": "ｺｰﾐ金属。上用の幅許容がほかと違う(許容はプログラムで決まっている)", "更新日時": ""},
    {**_BLANK, "包装仕様NO": "1P1211", "有効": 1, "並び順": 20, "備考": "", "更新日時": ""},
    {**_BLANK, "包装仕様NO": "1P1125", "オーダー厚_最小": 3.0, "オーダー厚_最大": 15.0,
     "有効": 1, "並び順": 30, "備考": "ｵｰﾀﾞｰ厚 3.0〜15.0", "更新日時": ""},
    {**_BLANK, "包装仕様NO": "1P1255", "オーダー厚_最小": 12.0, "オーダー厚_最大": 12.0,
     "オーダー幅_最小": 1075.0, "オーダー幅_最大": 1081.0,
     "オーダー丈_最小": 2332.0, "オーダー丈_最大": 2338.0,
     "有効": 1, "並び順": 40, "備考": "", "更新日時": ""},
    {**_BLANK, "包装仕様NO": "1P1255", "オーダー厚_最小": 12.0, "オーダー厚_最大": 12.0,
     "オーダー幅_最小": 1072.0, "オーダー幅_最大": 1078.0,
     "オーダー丈_最小": 2262.0, "オーダー丈_最大": 2268.0,
     "有効": 1, "並び順": 41, "備考": "", "更新日時": ""},
)


def normalize_spec(spec: Any) -> str:
    """包装仕様NOをそろえる(前後・途中の空白を除き、大文字に)。"""
    return "".join(str(spec or "").split()).upper()


def _bound(value: Any) -> Optional[float]:
    """範囲の端。空・0・読めない値は「見ない」(None)。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number != 0 else None


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True)
class Rule:
    """条件の1行。"""

    spec: str
    ranges: tuple[tuple[Optional[float], Optional[float]], ...] = ((None, None),) * 3
    note: str = ""

    def matches(self, spec: str, thickness: Any = 0, width: Any = 0, length: Any = 0) -> bool:
        if normalize_spec(spec) != self.spec or not self.spec:
            return False
        for (low, high), value in zip(self.ranges, (thickness, width, length)):
            if low is None and high is None:
                continue
            number = _num(value)
            if number <= 0:
                return False                    # 寸法が無い。確かめられないので当てない
            if low is not None and number < low - EPS:
                return False
            if high is not None and number > high + EPS:
                return False
        return True

    def describe(self) -> str:
        """「1P1255 ｵｰﾀﾞｰ厚 12 / 幅 1075〜1081 / 丈 2332〜2338」。寸法を見ない行は包装仕様NOだけ。"""
        parts = []
        for (low, high), (_, _, name) in zip(self.ranges, RANGE_COLUMNS):
            if low is None and high is None:
                continue
            if low is not None and high is not None:
                text = _fmt(low) if abs(low - high) < EPS else f"{_fmt(low)}〜{_fmt(high)}"
            elif low is not None:
                text = f"{_fmt(low)}以上"
            else:
                text = f"{_fmt(high)}以下"
            parts.append(f"{name} {text}")
        return self.spec + (f" ｵｰﾀﾞｰ{' / '.join(parts)}" if parts else "")


def _rule_of(row: dict[str, Any]) -> Rule:
    return Rule(spec=normalize_spec(row.get("包装仕様NO")),
                ranges=tuple((_bound(row.get(lo)), _bound(row.get(hi)))
                             for lo, hi, _ in RANGE_COLUMNS),
                note=str(row.get("備考") or ""))


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
    return tuple(rule for rule in map(_rule_of, rows) if rule.spec)


def find(rules: Iterable[Rule], spec: str, thickness: Any = 0, width: Any = 0,
         length: Any = 0) -> Optional[Rule]:
    """当たった最初の行。当たらなければ None。"""
    for rule in rules:
        if rule.matches(spec, thickness, width, length):
            return rule
    return None


def specs_with_size(rules: Iterable[Rule], spec: str) -> list[Rule]:
    """その包装仕様NOの行のうち、寸法を見る行(当たらなかった理由を言うため)。"""
    key = normalize_spec(spec)
    return [r for r in rules if r.spec == key
            and any(low is not None or high is not None for low, high in r.ranges)]


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
