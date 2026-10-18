"""看板と置き場の対応 ── 表(看板置き場)で持つ(VER4.8.68)

現場の声:「ライン毎ではなく置き場の関係性なので(拠点と近いか遠いか置き場が複数あるか)。
置き場が複数あって(1030*1520など)看板がライン毎にあったとしても、L-1でだされたとしても
LVCは出していないであればLVCにとりにいくことになる。全ライン合わせて判断すると1つのラインで
1030*1520の看板を出したら全部なくなるという判定になりますよね? それではダメです」

看板の表(看板_L1 など)はライン単位、資材の置き場は棚の番号(lblItem〇〇)。どの看板がどの置き場の
看板かは、どこにも書いていなかったので、この表で持つ(現場で了承した対応):

    看板_L1        → L-1 のまわり(1・2・3・30・31)
    看板_LVC       → LVC のまわり(17・18・19・32)
    看板_HVC       → HVC のまわり(7・8・35)
    看板_大板小板   → 中板・小板の棚(9・11・12・21・22・23)
    看板_L1 の「(大板棚)」 → 大板の棚(14・15)

「ハードボード(大板棚)は ハードボード です。置き場が大板ですが看板は L-1 でだします
(置き場はあくまで大板)」。資材名の後ろの(…)は**置き場の指定**で、資材は(…)を外した名前
(`split_material`)。表の「資材の注記」欄に(…)の中身を書いた行が、その置き場を決める。

【表の読み方】
1行が「この看板の、この注記の行は、この置き場の看板」。

    看板        … 看板の表の名前(例 看板_L1)
    資材の注記   … 資材名の後ろの(…)の中身(例 大板棚)。空なら(…)の無い行
    置き場      … 置き場の名前(lblItem1。カンマ区切りで複数。BoardMaster のデータラベルと同じ書き方)
    有効        … 0 の行は見ない

同じ看板・同じ注記の行が2つあれば、置き場を合わせる。

【どう使うか】(`material_service.build_board_stock_map`)
看板の1行は、その資材が**その置き場に置いてあるとき**だけ、その置き場の在庫を言う
(置き場はマスタのデータラベル)。欲 = 〇 ならその置き場は切れている。詳しくは
`docs/疲労度の計算.md` の「看板の位置と疲労度」。

【表が無いとき】
起動のたびに手元の表が空なら初期値(`SEED`)を入れる。取り込み元にまだ無ければ、取り込みのときに
初期値入りで作る(プロテック条件と同じ)。**最後の1行は消せない**(止めるときは有効を 0 に)。
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .logging_utils import get_logger

log = get_logger("kanban_shelves")

TABLE = "看板置き場"


def _labels(*numbers: int) -> str:
    return ",".join(f"lblItem{n}" for n in numbers)


# 初期値 = 現場で了承した対応
SEED: tuple[dict[str, Any], ...] = (
    {"看板": "看板_L1", "資材の注記": "", "置き場": _labels(1, 2, 3, 30, 31),
     "有効": 1, "並び順": 10, "備考": "L-1 のまわり", "更新日時": ""},
    {"看板": "看板_L1", "資材の注記": "大板棚", "置き場": _labels(14, 15),
     "有効": 1, "並び順": 15, "備考": "ハードボード(大板棚)。看板は L-1 で出すが置き場は大板", "更新日時": ""},
    {"看板": "看板_LVC", "資材の注記": "", "置き場": _labels(17, 18, 19, 32),
     "有効": 1, "並び順": 20, "備考": "LVC のまわり", "更新日時": ""},
    {"看板": "看板_HVC", "資材の注記": "", "置き場": _labels(7, 8, 35),
     "有効": 1, "並び順": 30, "備考": "HVC のまわり", "更新日時": ""},
    {"看板": "看板_大板小板", "資材の注記": "", "置き場": _labels(9, 11, 12, 21, 22, 23),
     "有効": 1, "並び順": 40, "備考": "中板・小板の棚", "更新日時": ""},
)

# 資材名の後ろの(…)。全角の括弧も
_NOTE = re.compile(r"^(.*?)\s*[(（]([^()（）]*)[)）]\s*$")


def _same(text: Any) -> str:
    """比べる形。全角半角と前後の空白だけそろえる。"""
    return unicodedata.normalize("NFKC", "" if text is None else str(text)).strip()


def split_material(name: Any) -> tuple[str, str]:
    """「ハードボード(大板棚)」→(「ハードボード」, 「大板棚」)。(…)が無ければ注記は空。"""
    text = "" if name is None else str(name).strip()
    found = _NOTE.match(text)
    if not found or not found.group(1).strip():
        return text, ""
    return found.group(1).strip(), _same(found.group(2))


def split_shelves(value: Any) -> frozenset[str]:
    """置き場の欄 → 置き場の名前の集まり(データラベルと同じ読み方。拠点の印は数えない)。"""
    from .location_service import _split_labels
    return frozenset(_split_labels(value))


@dataclass(frozen=True)
class Rule:
    """表の1行。"""

    kanban: str
    note: str
    shelves: frozenset[str]

    def describe(self) -> str:
        head = self.kanban + (f"の({self.note})" if self.note else "")
        numbers = sorted(s.replace("lblItem", "") for s in self.shelves)
        return f"{head} → {'・'.join(numbers)}"


def _rule_of(row: dict[str, Any]) -> Rule:
    return Rule(kanban=_same(row.get("看板")), note=_same(row.get("資材の注記")),
                shelves=split_shelves(row.get("置き場")))


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
    return tuple(rule for rule in map(_rule_of, rows) if rule.kanban and rule.shelves)


def shelves_of(rules: Iterable[Rule], kanban: str, note: str) -> Optional[frozenset[str]]:
    """その看板・注記の置き場。対応が無ければ None(= どの置き場の看板か分からない)。"""
    found = [r.shelves for r in rules if r.kanban == _same(kanban) and r.note == _same(note)]
    if not found:
        return None
    return frozenset().union(*found)


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
