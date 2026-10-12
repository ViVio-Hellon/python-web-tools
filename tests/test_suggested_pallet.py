"""製品サイズで出した候補のうち、面積がいちばん小さい行を選んでおく試験

現場の声:「一覧でリストに載った後、一番小さいサイズをデフォルト選択しておく(小さい行ではなく一番小さいサイズ)。
EXと上下共用以外は選んでおいてもよい」

【ここで守りたいこと】
1. 製品サイズの幅・丈の両方で絞った候補のうち、面積(幅×丈)最小の行に印(`suggested`)。同じ大きさなら一覧で上の行
2. 選んでおくだけで決めない(パレットは決まっていない)
3. EX・上下共用・1P0113・パレットを決めたあと・全件の一覧・片方だけ入力中は選んでおかない
"""
from __future__ import annotations

import unittest

from tests.test_web_selection import SelectionWebTestCase, insert_pallet


class SuggestedPalletTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))
        # どれも製品 1000×2000 が載る。面積は 1100×2400=264万 / 1200×2100=252万 / 1100×2300=253万
        insert_pallet(self.conn, width=1100, length=2400, code="A", w_min=900, w_max=1100,
                      l_min=1900, l_max=2400)
        insert_pallet(self.conn, width=1200, length=2100, code="B", w_min=900, w_max=1200,
                      l_min=1900, l_max=2100)
        insert_pallet(self.conn, width=1100, length=2300, code="C", w_min=900, w_max=1100,
                      l_min=1900, l_max=2300)
        insert_pallet(self.conn, width=1200, length=2100, code="B2", w_min=900, w_max=1200,
                      l_min=1900, l_max=2100)            # B と同じ大きさ(一覧で下)

    def live(self, width="1000", length="2000") -> dict:
        return self.post("/api/selection/pallet/list", {"product_width": width, "product_length": length})

    @staticmethod
    def suggested(state: dict) -> list:
        return [r["note"].split("コード: ")[1].split()[0] for r in state["rows"] if r["suggested"]]

    def test_面積がいちばん小さい行を選んでおく(self) -> None:
        state = self.live()
        self.assertEqual(len(state["rows"]), 4)
        self.assertEqual(self.suggested(state), ["B"], "1200×2100(幅は大きいが面積が最小)。同じ大きさの B2 より上")
        self.assertIsNone(self.session().pallet_row, "選んでおくだけで決めない")
        self.assertFalse(self.session().palette.is_set)

    def test_片方だけ入力中と全件の一覧では選んでおかない(self) -> None:
        self.assertEqual(self.suggested(self.live(length="")), [])
        self.assertEqual(self.suggested(self.get()), [])

    def test_パレットを決めたあとは選んでおかない(self) -> None:
        self.live()
        state = self.post("/api/selection/pallet/pick", {"width": 1100, "length": 2400, "symbol": ""})
        self.assertEqual(self.suggested(state), [])

    def test_EXと上下共用と1P0113では選んでおかない(self) -> None:
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr
        insert_lot(self.conn)
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")
        insert_odr(self.conn, order_no="A100")
        self.client.get("/api/lot/1234567", headers=self.auth())
        presenter = self.session().presenter
        self.assertEqual(self.suggested(self.live()), ["B"], "普通のロットなら選んでおく")
        presenter.apply_hosozai("ザラ板")                       # 上下共用
        self.assertEqual(self.suggested(self.live()), [])
        presenter.apply_hosozai("一致なし")
        presenter.is_ex_order = True                             # EX
        self.assertEqual(self.suggested(self.live()), [])
        presenter.is_ex_order = False
        presenter.mode_1p0113 = True                             # 1P0113
        self.assertEqual(self.suggested(self.live()), [])


if __name__ == "__main__":
    unittest.main()
