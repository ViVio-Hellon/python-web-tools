"""外注条件(VER4.9.4)── 表で包装仕様NOを決め打つ。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import db, outsource_rules as rules  # noqa: E402


def lot(**kw):
    base = dict(zaishitsu="F52S", choshitsu="R", yoto_code="H19S", manufactured_thickness=352.5,
                manufactured_width=1300, manufactured_length=2100, order_thickness=350,
                order_width=1300, order_length=2100, design_course="HOT SSW GFS KEN")
    base.update(kw)
    return SimpleNamespace(**base)


class CompareTests(unittest.TestCase):
    def test_like_は_access_と同じ(self):
        self.assertTrue(rules.compare("A7075S", "Like", "*75S"))
        self.assertTrue(rules.compare("T6511", "Like", "T6*"))
        self.assertFalse(rules.compare("A5052", "Like", "*75S"))

    def test_数は数として比べる(self):
        self.assertTrue(rules.compare("6.750", "=", "6.75"))
        self.assertTrue(rules.compare(322.5, ">=", "322.5"))
        self.assertFalse(rules.compare(322.4, ">=", "322.5"))

    def test_含む(self):
        self.assertTrue(rules.compare("HOT PSW GFS KEN", "含む", "GFS"))
        self.assertFalse(rules.compare("HOT PSW KEN", "含む", "GFS"))

    def test_知らない判定方法は当てない(self):
        self.assertFalse(rules.compare("A", "≒", "A"))


class DefaultRuleTests(unittest.TestCase):
    """初期値(現場の表と同じ中身)の判定。"""

    def find(self, **kw):
        found = rules.find(rules.DEFAULT_RULES, lot(**kw))
        return found.spec if found else ""

    def test_52S裸_H19S_は厚322_5以上(self):
        self.assertEqual(self.find(), "1P0113")
        self.assertEqual(self.find(manufactured_thickness=300), "")

    def test_52S裸_H197_は厚100以上かつGFS(self):
        self.assertEqual(self.find(yoto_code="H197", manufactured_thickness=100), "1P0113")
        self.assertEqual(self.find(yoto_code="H197", manufactured_thickness=85), "")
        # H197 の組は GFS だけ(GCT は当たらない)
        self.assertEqual(self.find(yoto_code="H197", manufactured_thickness=100,
                                   design_course="HOT GCT"), "")

    def test_組をまたいで混ぜない(self):
        """H19S で厚 100 は、H197 の組の厚(100以上)では当てない。"""
        self.assertEqual(self.find(manufactured_thickness=100), "")

    def test_75S系は1P0122(self):
        self.assertEqual(self.find(zaishitsu="A7075S", choshitsu="T6511", yoto_code="K435",
                                   manufactured_thickness=6.75, design_course="GSS"), "1P0122")

    def test_半角カナの対象項目も読める(self):
        self.assertEqual(rules._norm("用途ｺｰﾄﾞ"), "用途コード")


class TableTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_空の表には初期値が入る(self):
        self.assertEqual(len(rules.load(self.conn)), 2)

    def test_無効にした条件は見ない(self):
        self.conn.execute("UPDATE 外注条件 SET 有効 = 0 WHERE 戻り値 = '1P0113'")
        self.assertIsNone(rules.find(rules.load(self.conn), lot()))

    def test_優先順位の小さい方が先(self):
        self.conn.execute("INSERT INTO 外注条件 (管理番号, 条件名, 戻り値, 優先順位, 有効) VALUES (9, '先', '1P9999', 1, 1)")
        self.conn.execute("INSERT INTO 外注条件明細 (管理番号, 条件管理番号, 条件番号, 対象項目, 判定方法, 判定値, 有効)"
                          " VALUES (99, 9, 1, '材質', 'Like', '*52S', 1)")
        self.assertEqual(rules.find(rules.load(self.conn), lot()).spec, "1P9999")


if __name__ == "__main__":
    unittest.main()
