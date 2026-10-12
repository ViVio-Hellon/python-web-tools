"""EX受注でパレットを選んでいないとき、「出す」を開いて「パレットなしで送れる」と案内する試験

現場の声:「『出す』の段は畳まれたままで、案内は『製品の幅・丈を入れてください』が出ます。
パレットなしで送れることが画面から分かりにくい状態です」→「直して下さい」
"""
from __future__ import annotations

import unittest

from packaging_tool.presenters import selection as presenter
from tests.test_web_selection import SelectionWebTestCase, insert_pallet


class ExSignalHintTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from tests.test_lot_service import insert_hiki, insert_lot
        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))
        insert_pallet(self.conn, width=1100, length=2000, code="D1", unit="台", symbol="EXﾀｲﾄ")
        insert_lot(self.conn)
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")

    def open_lot(self, ex: str) -> dict:
        from tests.test_lot_service import insert_odr
        insert_odr(self.conn, order_no="A100", EX_輸出区分=ex)
        self.client.get("/api/lot/1234567", headers=self.auth())
        return self.get()

    @staticmethod
    def step(state: dict, key: str) -> dict:
        return next(s for s in state["steps"] if s["key"] == key)

    def test_EXでパレットなしなら出すを開いて送れると案内する(self) -> None:
        state = self.open_lot("EX")
        self.assertTrue(state["outputs"]["can_send"])
        self.assertEqual(self.step(state, "size")["state"], "current", "パレットを決める段はいまの段のまま")
        self.assertTrue(self.step(state, "outputs")["open"])
        self.assertEqual(state["primary_action"], "sendWarehouse")
        self.assertEqual(state["next_hint"], presenter.EX_SIGNAL_HINT)
        page = self.client.get("/selection", headers=self.auth()).get_data(as_text=True)
        self.assertIn('data-open="true">', page)

    def test_EXでもパレットを選んだら案内しない(self) -> None:
        self.open_lot("EX")
        state = self.post("/api/selection/pallet/pick", {"width": 1100, "length": 2000, "symbol": "EXﾀｲﾄ"})
        self.assertFalse(self.step(state, "outputs")["open"])
        self.assertNotEqual(state["next_hint"], presenter.EX_SIGNAL_HINT)

    def test_一覧の行を押してパレット決定を待っているときはそちらを先に言う(self) -> None:
        state = self.open_lot("EX")
        self.assertNotEqual(state["primary_action"], "decide")
        # 「パレット決定」が押せる(行を押した)ときの案内は今までどおり(`can_decide` は画面の行の選択)
        view = presenter.SelectionViewModel()
        view.can_decide = True
        view.ex_signal_only = True
        view.outputs.can_send = True
        presenter._apply_steps(view)
        self.assertEqual(view.primary_action, "decide")

    def test_EXでなければ今までどおり(self) -> None:
        state = self.open_lot("")
        self.assertFalse(self.step(state, "outputs")["open"])
        self.assertFalse(state["outputs"]["can_send"])
        self.assertNotEqual(state["next_hint"], presenter.EX_SIGNAL_HINT)
        page = self.client.get("/selection", headers=self.auth()).get_data(as_text=True)
        self.assertNotIn('data-open="true">', page)


if __name__ == "__main__":
    unittest.main()
