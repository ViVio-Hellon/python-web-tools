"""作業ラインの呼び名 ── アクセス権限の「権限」の列に書かれたライン(VER4.8.86)

現場の声:「ラインの読み方は vba-production-board-python-migration を参照してください。L1 やら LS やら
当時の設定が生きてしまっていて申し訳ないが、L1 ではなく L-1 なんです」
「(アクセス権限に)列を足さなくてよくね? ほかのツールものこれで読んでるからいけるはず」

**列は足さない。** ほかのツール(看板・日報)と同じく、アクセス権限の **権限** の列に、ラインの
**正規の呼び名**で書く(1行に1つ。並べるなら「,」「、」「/」「・」で区切る)。

    L-1 / LVC / HVC / 機側 / AIM / コイル / トット / バランサー / NS1

読み方はほかのツールと同じ(`kanban/line_names.py`):
- **正規の呼び名だけを読む**。``L1`` ``LS`` ``大板小板``(古い設定・ほかのツールの表の名前)や、記号・
  大文字小文字・空白の違い(``l-1`` ``L 1``)は読まない
- 揃えるのは**文字の幅(全角/半角)と前後の空白だけ**(``ＬＶＣ`` = ``LVC``)
- 作業長・mode:field などライン以外の値は読み飛ばす
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

#: 正規の呼び名(マスタ・現場の呼び名)。並びは画面の選択肢の並び
OFFICIAL: tuple[str, ...] = ("L-1", "LVC", "HVC", "機側", "AIM", "コイル", "トット", "バランサー", "NS1")

#: 1つの欄に並べるときの区切り(**空白では区切らない** ── 「L 1」を割らない。ほかのツールと同じ)
_SEPARATORS = re.compile(r"[,、/／・;；]+")


def same(text: Any) -> str:
    """比べる形。**文字の幅と前後の空白だけ**を揃える(大文字小文字・記号はそのまま)。"""
    return unicodedata.normalize("NFKC", "" if text is None else str(text)).strip()


_BY_SAME = {same(name): name for name in OFFICIAL}


def tokens(value: Any) -> list[str]:
    """1つの欄に並んだ値を1つずつ(``L-1、mode:field`` → 2つ)。"""
    return [t.strip() for t in _SEPARATORS.split(str(value or "")) if t.strip()]


def official(text: Any) -> str:
    """正規の呼び名なら、その名前(幅を揃えた形)。正規でなければ空(``L1`` ``LS`` ``l-1`` も空)。"""
    return _BY_SAME.get(same(text), "")


def lines_in(values: Iterable[Any]) -> list[str]:
    """欄の値の中のライン(正規の呼び名)。書かれた順、重複なし。"""
    found: list[str] = []
    for value in values:
        for token in tokens(value):
            name = official(token)
            if name and name not in found:
                found.append(name)
    return found
