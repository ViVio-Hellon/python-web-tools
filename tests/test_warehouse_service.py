"""倉庫連携(warehouse_service)のユニットテスト。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packaging_tool import db, warehouse_service as svc


class WarehouseServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        db.apply_schema(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    def _create(self, **overrides):
        params = dict(
            lot_no="1234567", hinmei="テスト品", hatchu_code="C001", tani="台",
            atu=1.0, haba=100, take=200, hatchu_suu=5,
        )
        params.update(overrides)
        return svc.create_order(self.conn, **params)


class CreateOrderTests(WarehouseServiceTestCase):
    def test_success(self):
        result = self._create()
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.mgr_no)

        row = self.conn.execute("SELECT * FROM 資材パレット注文管理 WHERE 管理番号=?", (result.mgr_no,)).fetchone()
        self.assertEqual(row["LotNo"], "1234567")
        self.assertIsNone(row["取り消し済"])
        self.assertIsNone(row["確認済み"])

    def test_requires_lot_no(self):
        result = self._create(lot_no="")
        self.assertFalse(result.ok)

    def test_requires_positive_qty(self):
        result = self._create(hatchu_suu=0)
        self.assertFalse(result.ok)

    def test_requires_numeric_dims(self):
        result = self._create(haba="abc")
        self.assertFalse(result.ok)


class ListOrdersTests(WarehouseServiceTestCase):
    def test_excludes_cancelled_by_default(self):
        r1 = self._create(lot_no="1111111")
        self._create(lot_no="2222222")
        svc.cancel_order(self.conn, r1.mgr_no)

        active = svc.list_orders(self.conn)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["LotNo"], "2222222")

        all_orders = svc.list_orders(self.conn, include_cancelled=True)
        self.assertEqual(len(all_orders), 2)

    def test_keyword_filter(self):
        self._create(lot_no="1111111", hinmei="アルミ板")
        self._create(lot_no="2222222", hinmei="ステンレス板")
        results = svc.list_orders(self.conn, keyword="アルミ")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["LotNo"], "1111111")

    def test_status_field(self):
        r = self._create()
        [row] = svc.list_orders(self.conn)
        self.assertEqual(row["状態"], svc.STATUS_PENDING)

        svc.confirm_order(self.conn, r.mgr_no)
        [row] = svc.list_orders(self.conn)
        self.assertEqual(row["状態"], svc.STATUS_CONFIRMED)


class ConfirmCancelTests(WarehouseServiceTestCase):
    def test_confirm_then_cancel_blocked(self):
        r = self._create()
        result = svc.confirm_order(self.conn, r.mgr_no)
        self.assertTrue(result.ok)

        cancel_result = svc.cancel_order(self.conn, r.mgr_no)
        self.assertFalse(cancel_result.ok)  # 確認済みは取り消せない

    def test_cancel_then_confirm_blocked(self):
        r = self._create()
        result = svc.cancel_order(self.conn, r.mgr_no)
        self.assertTrue(result.ok)

        confirm_result = svc.confirm_order(self.conn, r.mgr_no)
        self.assertFalse(confirm_result.ok)  # 取消済みは確認できない

    def test_confirm_missing_order(self):
        result = svc.confirm_order(self.conn, 999)
        self.assertFalse(result.ok)

    def test_double_confirm_second_fails(self):
        r = self._create()
        first = svc.confirm_order(self.conn, r.mgr_no)
        second = svc.confirm_order(self.conn, r.mgr_no)
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)


if __name__ == "__main__":
    unittest.main()
