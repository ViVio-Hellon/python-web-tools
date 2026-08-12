"""資材配置パターンの保存・読込

VBA `PalletHistoryModule_v2` の `SaveNewPattern_v2`/`GetPatternList`/
`LoadPatternByID_v2`/`DeletePatternByID` の移植。対象テーブルは
`PalletPatterns`(A〜Jの10枠、各枠に幅/丈/枚数/用途を持つ)。

資材選択画面の管理者エリアにある「保存」「実績検索」から呼ばれる。

【移植で変更した点】
    - 管理番号の採番はVBA版が`MAX(管理番号)+1`を手計算していた
      (複数端末同時実行での重複リスクがあった)のに対し、
      SQLite版はAUTOINCREMENTに任せる。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from . import db
from .logging_utils import get_logger

log = get_logger("pattern_service")

_SLOT_LETTERS = "ABCDEFGHIJ"

# `用途`列に入る値(VBA `SaveNewPattern_v2` のリテラル)
USAGE_LOWER = "下用"
USAGE_UPPER = "上用"


@dataclass
class PatternBoard:
    width: int
    length: int
    count: int
    usage: str = ""  # '下用' / '上用' / ''


@dataclass
class PatternSummary:
    id: int
    product_width: int
    product_length: int
    registered_at: str
    usage_count: int
    board_summary: str


@dataclass
class PatternDetail:
    id: int
    pallet_width: int
    pallet_length: int
    product_width: int
    product_length: int
    boards_lower: list[PatternBoard] = field(default_factory=list)
    boards_upper: list[PatternBoard] = field(default_factory=list)


def save_new_pattern(
    conn: sqlite3.Connection,
    *,
    pallet_width: int,
    pallet_length: int,
    boards_lower: list[PatternBoard],
    boards_upper: list[PatternBoard],
    product_width: int,
    product_length: int,
) -> int:
    """VBA `SaveNewPattern_v2` の移植。

    下用ボードを先に、続けて上用ボードをA〜Jの最大10枠に詰める
    (10枠を超える分は切り捨てる。元VBA仕様を踏襲)。

    `用途`列は「どちらのリストから来たか」で決まる。VBA `SaveNewPattern_v2`
    も `'下用'` / `'上用'` をリテラルで埋めており、呼び出し側が
    `PatternBoard.usage` に何を入れていても無視される。
    (読込時の下用/上用の振り分けはこの列だけが頼りなので、
    呼び出し側の設定漏れで全部が下用に化けないようここで確定させる)
    """
    slots = [(b, USAGE_LOWER) for b in boards_lower]
    slots += [(b, USAGE_UPPER) for b in boards_upper]
    slots = slots[:10]
    now = db.now_db_string()
    values: dict[str, object] = {
        "パレット幅": pallet_width, "パレット丈": pallet_length,
        "製品幅": product_width, "製品丈": product_length,
        "登録日時": now, "更新日時": now, "使用回数": 0,
    }
    for i, letter in enumerate(_SLOT_LETTERS):
        if i < len(slots):
            b, usage = slots[i]
            values[f"{letter}幅"], values[f"{letter}丈"] = b.width, b.length
            values[f"{letter}枚数"], values[f"{letter}用途"] = b.count, usage
        else:
            values[f"{letter}幅"] = values[f"{letter}丈"] = values[f"{letter}枚数"] = 0
            values[f"{letter}用途"] = ""

    result = db.insert_record(conn, "PalletPatterns", values, caller_name="save_new_pattern")
    if not result.ok:
        raise RuntimeError(f"パターン保存に失敗しました: {result.error}")
    log.info("save_new_pattern: 管理番号=%s", result.lastrowid)
    return result.lastrowid


def get_pattern_list(conn: sqlite3.Connection, pallet_width: int = 0, pallet_length: int = 0) -> list[PatternSummary]:
    """VBA `GetPatternList` の移植。パレット幅・丈が両方>0のときのみ絞り込む。"""
    if pallet_width > 0 and pallet_length > 0:
        rows = db.fetch_all(
            conn, "SELECT * FROM PalletPatterns WHERE パレット幅 = ? AND パレット丈 = ? ORDER BY 管理番号 DESC",
            (pallet_width, pallet_length), caller_name="get_pattern_list",
        ) or []
    else:
        rows = db.fetch_all(
            conn, "SELECT * FROM PalletPatterns ORDER BY 管理番号 DESC", caller_name="get_pattern_list",
        ) or []

    summaries = []
    for row in rows:
        parts = []
        for letter in _SLOT_LETTERS:
            w, l, c = row[f"{letter}幅"], row[f"{letter}丈"], row[f"{letter}枚数"]
            if w and l and c:
                parts.append(f"{w}×{l}({c}枚)")
        summaries.append(PatternSummary(
            id=row["管理番号"], product_width=row["製品幅"], product_length=row["製品丈"],
            registered_at=row["登録日時"], usage_count=row["使用回数"],
            board_summary=", ".join(parts),
        ))
    return summaries


def load_pattern_by_id(conn: sqlite3.Connection, pattern_id: int) -> Optional[PatternDetail]:
    """VBA `LoadPatternByID_v2` の移植。

    読み込みと同時に使用回数をインクリメントし更新日時を更新する
    (VBA版と同じ副作用)。見つからなければ `None` を返す
    (VBA版は `vbObjectError+5` を送出していたが、Python版は
    呼び出し側でNoneを判定させる方が自然なため変更している)。
    """
    row = db.fetch_one(conn, "SELECT * FROM PalletPatterns WHERE 管理番号 = ?", (pattern_id,), caller_name="load_pattern_by_id")
    if row is None:
        return None

    boards_lower: list[PatternBoard] = []
    boards_upper: list[PatternBoard] = []
    for letter in _SLOT_LETTERS:
        w, l, c, usage = row[f"{letter}幅"], row[f"{letter}丈"], row[f"{letter}枚数"], row[f"{letter}用途"]
        if not w or not l or not c:
            continue
        board = PatternBoard(width=w, length=l, count=c, usage=usage or "")
        (boards_upper if usage == USAGE_UPPER else boards_lower).append(board)  # 未設定は下用扱い(元VBA仕様)

    db.execute_with_retry(
        conn, "UPDATE PalletPatterns SET 使用回数 = 使用回数 + 1, 更新日時 = ? WHERE 管理番号 = ?",
        (db.now_db_string(), pattern_id), caller_name="load_pattern_by_id.touch",
    )

    return PatternDetail(
        id=row["管理番号"], pallet_width=row["パレット幅"], pallet_length=row["パレット丈"],
        product_width=row["製品幅"], product_length=row["製品丈"],
        boards_lower=boards_lower, boards_upper=boards_upper,
    )


def delete_pattern_by_id(conn: sqlite3.Connection, pattern_id: int) -> bool:
    """VBA `DeletePatternByID` の移植。"""
    result = db.execute_with_retry(
        conn, "DELETE FROM PalletPatterns WHERE 管理番号 = ?", (pattern_id,), caller_name="delete_pattern_by_id",
    )
    return result.ok and result.rowcount > 0
