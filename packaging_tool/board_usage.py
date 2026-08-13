"""選定ボードの使用実績

配置図を印刷したときだけ、その配置(何枚使ったか)を積む。**配置しただけ
では積まない** ── 配置は何度でも試せる操作で、置いてみただけの下書きまで
数えると「よく使われるサイズ」が実際の使用実態とずれる(現場の指示:
印刷=実施に使用した)。

集計単位は 幅×丈×ボードタイプ。上用/下用は物理的には同じ板なので分けない
(現場の指示)。
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass

from . import db
from .logging_utils import get_logger
from .models import PlacedBoardModel

log = get_logger("board_usage")

TABLE = "ボード使用実績"


@dataclass
class UsageRow:
    width: int
    length: int
    board_type: str
    usage_count: int
    last_used_at: str


def record_usage(conn: sqlite3.Connection, placed: list[PlacedBoardModel],
                 board_type: str) -> int:
    """配置図の印刷1回ぶんを積む。戻り値は積んだ枚数(通知用)。

    `placed` は物理的な板1枚につき1件(`PlacedBoardModel`)なので、
    同じ幅×丈が複数あればそのまま複数枚として数える。
    """
    counts = Counter((board.width, board.length) for board in placed)
    if not counts:
        return 0

    now = db.now_db_string()
    for (width, length), qty in counts.items():
        row = db.fetch_one(
            conn,
            f"SELECT 管理番号 FROM {TABLE} "
            "WHERE ボード幅 = ? AND ボード丈 = ? AND ボードタイプ = ?",
            (width, length, board_type), caller_name="board_usage.record_usage")
        if row is not None:
            db.execute_with_retry(
                conn,
                f"UPDATE {TABLE} SET 使用回数 = 使用回数 + ?, 最終使用日時 = ? "
                "WHERE 管理番号 = ?",
                (qty, now, row["管理番号"]),
                caller_name="board_usage.record_usage.update")
        else:
            db.insert_record(
                conn, TABLE,
                {"ボード幅": width, "ボード丈": length, "ボードタイプ": board_type,
                 "使用回数": qty, "最終使用日時": now},
                caller_name="board_usage.record_usage.insert")

    conn.commit()
    total = sum(counts.values())
    log.info("board_usage.record_usage: %s種類 計%s枚 (タイプ=%s)",
             len(counts), total, board_type)
    return total


def list_usage(conn: sqlite3.Connection) -> list[UsageRow]:
    """使用回数の多い順。管理者エリアの一覧に出す。"""
    rows = db.fetch_all(
        conn,
        f"SELECT ボード幅, ボード丈, ボードタイプ, 使用回数, 最終使用日時 FROM {TABLE} "
        "ORDER BY 使用回数 DESC, 最終使用日時 DESC",
        caller_name="board_usage.list_usage") or []
    return [UsageRow(width=r["ボード幅"], length=r["ボード丈"],
                     board_type=r["ボードタイプ"], usage_count=r["使用回数"],
                     last_used_at=r["最終使用日時"])
            for r in rows]
