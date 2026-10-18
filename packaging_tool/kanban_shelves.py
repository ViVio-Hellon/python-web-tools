"""看板と置き場の対応 ── 表(看板置き場)で持つ(VER4.8.68)

現場の声:「ライン毎ではなく置き場の関係性なので(拠点と近いか遠いか置き場が複数あるか)。
置き場が複数あって(1030*1520など)看板がライン毎にあったとしても、L-1でだされたとしても
LVCは出していないであればLVCにとりにいくことになる。全ライン合わせて判断すると1つのラインで
1030*1520の看板を出したら全部なくなるという判定になりますよね? それではダメです」

看板の表(看板_L1 など)はライン単位、資材の置き場は棚の番号(lblItem〇〇)。どの看板がどの置き場の
看板かは、どこにも書いていなかったので、この表で持つ(現場で了承した対応):

    看板_L1        → L-1 のまわり(1・2・3・30・31)と細板の棚(4。VER4.8.69)
    看板_LVC       → LVC のまわり(17・18・19・32)。1000×1600 は 23(VER4.8.69)
    看板_HVC       → HVC のまわり(7・8・35)
    看板_大板小板   → 中板・小板の棚(9・11・12・21・22・23)。1000×1200・1000×1700 は 15(VER4.8.69)
    看板_L1 の「(大板棚)」 → 大板の棚(14・15)

「ハードボード(大板棚)は ハードボード です。置き場が大板ですが看板は L-1 でだします
(置き場はあくまで大板)」。資材名の後ろの(…)は**置き場の指定**で、資材は(…)を外した名前
(`split_material`)。表の「資材の注記」欄に(…)の中身を書いた行が、その置き場を決める。

【表の読み方】
1行が「この看板の、この注記の行は、この置き場の看板」。

    看板        … 看板の表の名前(例 看板_L1)
    資材の注記   … 資材名の後ろの(…)の中身(例 大板棚)。空なら(…)の無い行
    置き場      … 置き場の名前(lblItem1。カンマ区切りで複数。BoardMaster のデータラベルと同じ書き方)
    幅・丈      … このサイズの行だけ(向きは問わない。VER4.8.69)。空ならどのサイズも
    有効        … 0 の行は見ない

同じ看板・同じ注記の行が2つあれば、置き場を合わせる(サイズを書いた行は、そのサイズのときだけ足す)。
棚ごと足すと別サイズの看板まで効く(例: LVC の 1030×1520 の看板が 23 番も言う)ので、1つのサイズだけ
別の棚にあるときは幅・丈を書く。

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


def _row(kanban: str, note: str, shelves: str, order: int, remark: str,
         width: Optional[int] = None, length: Optional[int] = None) -> dict[str, Any]:
    return {"看板": kanban, "資材の注記": note, "置き場": shelves, "幅": width, "丈": length,
            "有効": 1, "並び順": order, "備考": remark, "更新日時": ""}


# VER4.8.69 で現場が了承した対応(看板の置き場にその板が無かった行)。VER4.8.68 で作った表にも足しに行く
# (`upgrade_rows`。幅・丈の列を足したときに1回だけ)
L1_SHELVES_4_8_68 = _labels(1, 2, 3, 30, 31)
L1_SHELVES = _labels(1, 2, 3, 4, 30, 31)
ADDED_ROWS: tuple[dict[str, Any], ...] = (
    _row("看板_LVC", "", _labels(23), 25, "LVC の看板の 1000×1600 は 23 番", 1000, 1600),
    _row("看板_大板小板", "", _labels(15), 45, "大板小板の看板の 1000×1200 は 15 番(大板)", 1000, 1200),
    _row("看板_大板小板", "", _labels(15), 46, "大板小板の看板の 1000×1700 は 15 番(大板)", 1000, 1700),
)

# 初期値 = 現場で了承した対応
SEED: tuple[dict[str, Any], ...] = (
    _row("看板_L1", "", L1_SHELVES, 10, "L-1 のまわりと細板の棚(4)"),
    _row("看板_L1", "大板棚", _labels(14, 15), 15, "ハードボード(大板棚)。看板は L-1 で出すが置き場は大板"),
    _row("看板_LVC", "", _labels(17, 18, 19, 32), 20, "LVC のまわり"),
    _row("看板_HVC", "", _labels(7, 8, 35), 30, "HVC のまわり"),
    _row("看板_大板小板", "", _labels(9, 11, 12, 21, 22, 23), 40, "中板・小板の棚"),
) + ADDED_ROWS

# あとから足した列(VER4.8.68 で作った表に足しに行く ── 手元 `db._add_rule_columns`、
# 取り込み元 `master_schema.ensure_rule_tables`)
ADDED_COLUMNS: tuple[tuple[str, str], ...] = (("幅", "INTEGER"), ("丈", "INTEGER"))

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
    # このサイズ(短辺, 長辺)の行だけ。None ならどのサイズも
    size: Optional[tuple[int, int]] = None

    def fits(self, width: int, length: int) -> bool:
        return self.size is None or self.size == (min(width, length), max(width, length))

    def describe(self) -> str:
        head = self.kanban + (f"の({self.note})" if self.note else "")
        if self.size:
            head += f" {self.size[0]}×{self.size[1]}"
        numbers = sorted(s.replace("lblItem", "") for s in self.shelves)
        return f"{head} → {'・'.join(numbers)}"


def _int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _rule_of(row: dict[str, Any]) -> Rule:
    w, l = _int(row.get("幅")), _int(row.get("丈"))
    return Rule(kanban=_same(row.get("看板")), note=_same(row.get("資材の注記")),
                shelves=split_shelves(row.get("置き場")),
                size=(min(w, l), max(w, l)) if w > 0 and l > 0 else None)


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


def shelves_of(rules: Iterable[Rule], kanban: str, note: str,
               width: int = 0, length: int = 0) -> Optional[frozenset[str]]:
    """その看板・注記(・サイズ)の置き場。対応が無ければ None(= どの置き場の看板か分からない)。

    サイズを書いた行は、そのサイズのときだけ足す(`width`/`length` を省くとサイズの無い行だけ)。
    """
    found = [r.shelves for r in rules
             if r.kanban == _same(kanban) and r.note == _same(note)
             and (r.size is None or (width and length and r.fits(width, length)))]
    if not found:
        return None
    return frozenset().union(*found)


def upgrade_rows(query, execute) -> int:
    """VER4.8.68 で作った表に、VER4.8.69 で了承した対応を足す。直した行の数を返す。

    幅・丈の列を足したとき(= VER4.8.68 の表)に1回だけ呼ぶ。看板_L1 の行は、置き場が
    VER4.8.68 の初期値のまま(誰も直していない)ときだけ 4 番を足す。サイズの行は無ければ足す。
    `query(sql)` は行の dict の一覧を、`execute(sql, params)` は書く(手元・取り込み元の両方で使う)。
    """
    changed = 0
    rows = query(f'SELECT * FROM "{TABLE}"')
    for row in rows:
        if (_same(row.get("看板")) == "看板_L1" and not _same(row.get("資材の注記"))
                and split_shelves(row.get("置き場")) == split_shelves(L1_SHELVES_4_8_68)):
            execute(f'UPDATE "{TABLE}" SET "置き場" = ?, "備考" = ? WHERE "ID" = ?',
                    (L1_SHELVES, SEED[0]["備考"], row.get("ID")))
            changed += 1
    have = {(_same(r.get("看板")), _same(r.get("資材の注記")), _int(r.get("幅")), _int(r.get("丈")))
            for r in rows}
    columns = list(ADDED_ROWS[0])
    col_list = ", ".join(f'"{c}"' for c in columns)
    marks = ", ".join("?" for _ in columns)
    for add in ADDED_ROWS:
        if (add["看板"], add["資材の注記"], add["幅"], add["丈"]) in have:
            continue
        execute(f'INSERT INTO "{TABLE}" ({col_list}) VALUES ({marks})', tuple(add[c] for c in columns))
        changed += 1
    if changed:
        log.info("%s に VER4.8.69 の対応を足しました(%s行)", TABLE, changed)
    return changed


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
