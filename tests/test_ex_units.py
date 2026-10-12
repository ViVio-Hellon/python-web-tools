"""EXモード(EXオンリーを含む)のとき、パレット一覧に「台」と「枚」を出す

現場の声:「EXモードまたはEXオンリー選択時、台 と 枚 をリストに入れてください。今は台だけです」。
EXでないときは今までどおり(台だけ。保護材が確定していれば台と枚)。
"""
from __future__ import annotations

import sqlite3
import unittest

from packaging_tool import db
from packaging_tool import pallet_auto_select, pallet_list
from packaging_tool.pallet_common import allowed_units, unit_allowed
from tests.test_web_selection import insert_pallet


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.apply_schema(conn)
    insert_pallet(conn, width=1100, length=2000, unit="台", code="P1")
    insert_pallet(conn, width=1100, length=2000, unit="枚", code="P2", symbol="EX2方向")
    insert_pallet(conn, width=1100, length=2000, unit="組", code="P3")
    return conn


class ExUnitTests(unittest.TestCase):
    def test_単位の決まり(self) -> None:
        self.assertEqual(allowed_units(""), ("台",))
        self.assertEqual(allowed_units("", ex=True), ("台", "枚"))
        self.assertTrue(unit_allowed("枚", "", True))
        self.assertFalse(unit_allowed("組", "", True), "組は今までどおり出さない")

    def test_全件の一覧(self) -> None:
        conn = make_db()
        units = lambda **kw: sorted(r.unit for r in pallet_list.list_pallet_sizes(conn, show_all=True, **kw))
        self.assertEqual(units(), ["台"])
        self.assertEqual(units(is_ex_order=True), ["台", "枚"])
        self.assertEqual(units(is_ex_order=True, ex_only=True), ["枚"], "EXオンリーは記号にEXを含む行だけ")

    def test_直接検索(self) -> None:
        conn = make_db()
        rows = pallet_list.search_pallet_direct(conn, pallet_width_text="1100", pallet_length_text="2000",
                                                show_all=True, is_ex_order=True)
        self.assertEqual(sorted(r.unit for r in rows), ["台", "枚"])

    def test_製品サイズの一覧と自動検索(self) -> None:
        conn = make_db()
        rows = pallet_list.list_pallets_for_product(conn, product_width=1050, product_length=1950,
                                                    show_all=True, is_ex_order=True)
        self.assertIn("枚", {r.unit for r in rows})
        row = conn.execute("SELECT * FROM PalletMaster WHERE 単位 = '枚'").fetchone()
        self.assertTrue(pallet_auto_select._listable(row, ex_only_mode=False, show_all=True,
                                                     last_hosozai="", is_ex_order=True))
        self.assertFalse(pallet_auto_select._listable(row, ex_only_mode=False, show_all=True,
                                                      last_hosozai=""))

    def test_押した行を引くときも枚を落とさない(self) -> None:
        conn = make_db()
        row = pallet_list.find_pallet_row(conn, 1100, 2000, "EX2方向", is_ex_order=True)
        self.assertEqual((row.code, row.unit), ("P2", "枚"))


if __name__ == "__main__":
    unittest.main()
