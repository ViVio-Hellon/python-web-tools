"""ロット検索なしで、資材選択から倉庫連携へ送る試験

現場の声:「LOT検索なしで資材選択しても資材連携できるようにしてほしい。パレット自体は
製品サイズを入力して検索しパレット決定はする。LOTデータなしでもここで資材連携に
品名と発注コードは展開できるはず。残りの必須欄は自分で埋める方式になる」

【ここで守りたいこと】
1. ロットを開かずにパレットを一覧から選べば、倉庫送信できる。下書きには品名・発注コード・単位だけが入る
2. その下書きは LotNo などを打ち込んで送れる(作業中のロットと照らさない)。必須欄が空なら断る
3. パレットを選んでいなければ、今までどおりロットが要ると断る
4. ロットのある下書きは今までどおり、作業中のロットと違う LotNo を断る
"""
from __future__ import annotations

import re
import unittest

from packaging_tool import work_context
from packaging_tool.presenters import outputs
from tests.test_web_selection import SelectionWebTestCase, insert_pallet


class NoLotSendTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from app.routes import warehouse as wh_routes
        original = wh_routes.get_db
        wh_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(wh_routes, "get_db", original))
        insert_pallet(self.conn, width=1100, length=2000, code="P9", unit="枚", symbol="SP-18")

    def pick_pallet(self) -> None:
        self.post("/api/selection/pallet/pick", {"width": 1100, "length": 2000, "symbol": "SP-18"})
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})

    def draft(self) -> dict:
        return work_context.get_context().peek_pending_orders()[0]

    def send(self, **values):
        body = {k: v for k, v in self.draft().items()
                if not k.startswith("pallet_") and k not in ("no_lot", "notice")}
        body.update(from_draft=True, **values)
        return self.client.post("/api/warehouse/send", json=body, headers=self.auth())

    def test_ロットを開かずにパレットを選ぶと品名と発注コードの下書きができる(self) -> None:
        self.assertEqual(work_context.get_context().lot_no, "")
        self.pick_pallet()
        state = self.post("/api/selection/send")
        self.assertEqual(state.get("next_url"), "/warehouse")
        draft = self.draft()
        self.assertEqual((draft["hinmei"], draft["hatchu_code"], draft["tani"]),
                         ("一般　SP-18　1100x2000", "P9", "枚"))
        for key in ("lot_no", "hatchu_suu", "zaisitu", "atu", "haba", "take", "nounyusaki"):
            self.assertEqual(draft[key], "", key)
        self.assertTrue(draft["no_lot"])
        self.assertEqual(draft["notice"], outputs.NO_LOT_NOTICE)

    def test_打ち込んだLotNoで送れる(self) -> None:
        self.pick_pallet()
        self.post("/api/selection/send")
        res = self.send(lot_no="7654321", hatchu_suu="4", nounyusaki="手入力の納入先")
        self.assertEqual(res.status_code, 200, res.get_json())
        row = self.conn.execute(
            "SELECT LotNo, 品名, 発注コード, 単位, 発注数, 納入先 FROM 資材パレット注文管理").fetchone()
        self.assertEqual(tuple(row), ("7654321", "一般　SP-18　1100x2000", "P9", "枚", 4, "手入力の納入先"))
        self.assertEqual(work_context.get_context().peek_pending_orders(), [], "送った下書きは手放す")

    def test_必須欄が空なら断る(self) -> None:
        self.pick_pallet()
        self.post("/api/selection/send")
        res = self.send(hatchu_suu="4")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["field"], "lot_no")
        res = self.send(lot_no="7654321")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["field"], "hatchu_suu")

    def test_パレットを選んでいなければロットが要ると断る(self) -> None:
        res = self.client.post("/api/selection/send", json={}, headers=self.auth())
        self.assertEqual(res.status_code, 422)
        self.assertIn("ロット検索", res.get_json()["message"])

    def test_倉庫連携の画面に下書きが出る(self) -> None:
        self.pick_pallet()
        self.post("/api/selection/send")
        page = self.client.get("/warehouse", headers=self.auth()).get_data(as_text=True)
        found = re.search(r'data-orders="([^"]*)"', page)
        self.assertIsNotNone(found)
        self.assertIn("P9", found.group(1))

    def test_画面から印を送ってもロットのある下書きは照らす(self) -> None:
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr
        from app.routes import lot as lot_routes
        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))
        insert_lot(self.conn)
        insert_hiki(self.conn, order_no="A100", qty=10.0, no="00000001")
        insert_odr(self.conn, order_no="A100")
        self.client.get("/api/lot/1234567", headers=self.auth())
        self.pick_pallet()
        self.post("/api/selection/send")
        self.assertNotIn("no_lot", self.draft())
        res = self.send(lot_no="7654321", no_lot=True)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.get_json()["error"]["code"], "lot_mismatch")


if __name__ == "__main__":
    unittest.main()
