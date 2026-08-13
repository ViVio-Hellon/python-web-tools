"""選定ボードの使用実績(board_usage)のユニットテスト。

【ここで守りたいこと】
配置しただけでは積まない ── 印刷したときだけ積む、という運用がそのまま
コードに現れているか。集計単位は幅×丈×ボードタイプで、上用/下用は
分けない。
"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import board_usage as bu
from packaging_tool import db
from packaging_tool.models import PlacedBoardModel


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    return conn


def board(width, length, category="下用") -> PlacedBoardModel:
    return PlacedBoardModel(width=width, length=length, board_category=category)


class RecordUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_no_boards_records_nothing(self):
        self.assertEqual(bu.record_usage(self.conn, [], "ハードボード"), 0)
        self.assertEqual(bu.list_usage(self.conn), [])

    def test_counts_each_physical_sheet(self):
        placed = [board(1000, 2000), board(1000, 2000), board(500, 1000)]
        total = bu.record_usage(self.conn, placed, "ハードボード")
        self.assertEqual(total, 3)
        rows = {(r.width, r.length): r.usage_count for r in bu.list_usage(self.conn)}
        self.assertEqual(rows[(1000, 2000)], 2)
        self.assertEqual(rows[(500, 1000)], 1)

    def test_upper_and_lower_are_not_split(self):
        """集計単位は幅×丈×ボードタイプ。上用/下用は同じ板として合算する。"""
        placed = [board(1000, 2000, "上用"), board(1000, 2000, "下用")]
        bu.record_usage(self.conn, placed, "ハードボード")
        rows = bu.list_usage(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].usage_count, 2)

    def test_board_type_is_a_separate_key(self):
        """幅・丈が同じでもボードタイプが違えば別集計。"""
        bu.record_usage(self.conn, [board(1000, 2000)], "ハードボード")
        bu.record_usage(self.conn, [board(1000, 2000)], "IKボード")
        rows = {r.board_type: r.usage_count for r in bu.list_usage(self.conn)}
        self.assertEqual(rows, {"ハードボード": 1, "IKボード": 1})

    def test_accumulates_across_multiple_prints(self):
        bu.record_usage(self.conn, [board(1000, 2000)], "ハードボード")
        bu.record_usage(self.conn, [board(1000, 2000), board(1000, 2000)], "ハードボード")
        rows = bu.list_usage(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].usage_count, 3)

    def test_list_usage_orders_by_count_descending(self):
        bu.record_usage(self.conn, [board(100, 100)], "ハードボード")
        bu.record_usage(self.conn, [board(200, 200)] * 5, "ハードボード")
        rows = bu.list_usage(self.conn)
        self.assertEqual([(r.width, r.length) for r in rows],
                         [(200, 200), (100, 100)])


if __name__ == "__main__":
    unittest.main()
