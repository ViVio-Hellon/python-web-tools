"""資材選択「パレットを決める」: パレット 幅・丈で直接探す の試験

現場の声:「製品 幅丈の入力で適合パレットを探す(現状)に、パレット 幅丈の入力で直接パレットを探すを追加して。
製品 幅丈の入力が優先。製品 幅丈が空白のとき、パレット 幅丈の入力があれば誤差50mmで範囲検索。
幅だけ・丈だけ・両方は製品の入力検索方法を引き継いで」

【ここで守りたいこと】
1. 製品 幅・丈が空で、パレット 幅・丈を入れると、パレット自身の寸法の前後50mmで探す(幅だけ・丈だけ・両方)
2. 製品 幅・丈に何か入っていれば製品で探す(パレットの欄は見ない。見ていないことを一覧の一言で言う)
3. 製品の欄と同じ絞り込み(単位・EXまで表示・EXオンリー)が掛かる
4. 何も確定しない。「やり直す」で欄も空になる
"""
from __future__ import annotations

import unittest

from tests.test_web_selection import SelectionWebTestCase, insert_pallet


class PalletFindTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1150, length=2650, code="A",
                      w_min=900, w_max=1200, l_min=2400, l_max=2700)
        insert_pallet(self.conn, width=1190, length=2690, code="B",    # 前後50mmぎりぎり(+40)
                      w_min=1000, w_max=1190, l_min=2500, l_max=2690)
        insert_pallet(self.conn, width=1300, length=2650, code="C",    # 幅が150違う
                      w_min=1100, w_max=1300, l_min=2400, l_max=2650)
        insert_pallet(self.conn, width=1150, length=3000, code="D",    # 丈が350違う
                      w_min=900, w_max=1150, l_min=2800, l_max=3000)
        insert_pallet(self.conn, width=1150, length=2650, code="M", unit="枚")   # 単位で外れる

    def live(self, **body) -> dict:
        return self.post("/api/selection/pallet/list", body)

    def codes(self, state: dict) -> list[str]:
        codes = []
        for r in state["rows"]:
            row = self.conn.execute("SELECT コード FROM PalletMaster WHERE 管理番号 = ?",
                                    (r["id"],)).fetchone()
            codes.append(row[0])
        return sorted(codes)

    def test_両方入れると幅も丈も前後50mm(self) -> None:
        state = self.live(pallet_width="1150", pallet_length="2650")
        self.assertEqual(self.codes(state), ["A", "B"])
        self.assertEqual(state["list_note"], "パレット寸法 1150×2650 の前後 50mm: 2 件")

    def test_幅だけなら幅だけで探す(self) -> None:
        state = self.live(pallet_width="1150")
        self.assertEqual(self.codes(state), ["A", "B", "D"])
        self.assertIn("1150×— の前後 50mm", state["list_note"])

    def test_丈だけなら丈だけで探す(self) -> None:
        state = self.live(pallet_length="2650")
        self.assertEqual(self.codes(state), ["A", "B", "C"])
        self.assertIn("—×2650 の前後 50mm", state["list_note"])

    def test_製品の欄が優先(self) -> None:
        """製品 幅・丈に何か入っていれば、パレットの欄は見ない。"""
        state = self.live(product_width="1000", product_length="2500",
                          pallet_width="1300", pallet_length="2650")
        self.assertNotIn("C", self.codes(state))                     # パレットの欄なら出る行
        self.assertIn("A", self.codes(state))
        self.assertIn("パレット 幅・丈では探していません", state["list_note"])
        state = self.live(product_width="1000", pallet_width="1300")  # 製品は幅だけでも優先
        self.assertIn("製品 幅に近いパレット", state["list_note"])

    def test_どちらも空なら全件(self) -> None:
        state = self.live(product_width="", product_length="", pallet_width="", pallet_length="")
        self.assertEqual(self.codes(state), ["A", "B", "C", "D"])    # 枚は単位で外れる

    def test_何も確定せずやり直すで欄も空になる(self) -> None:
        state = self.live(pallet_width="1150", pallet_length="2650")
        self.assertFalse(state["pallet_set"])
        self.assertEqual((state["pallet_find_width"], state["pallet_find_length"]), ("1150", "2650"))
        state = self.post("/api/selection/clear")
        self.assertEqual((state["pallet_find_width"], state["pallet_find_length"]), ("", ""))

    def test_画面に欄がある(self) -> None:
        page = self.client.get("/selection", headers=self.auth()).get_data(as_text=True)
        self.assertIn('id="palFindWidth"', page)
        self.assertIn('id="palFindLength"', page)
        self.assertIn("パレットの寸法の前後50mmで探します", page)


class PalletFindExOnlyTests(SelectionWebTestCase):
    """EXオンリーのときは、パレットの欄で探しても EX の行だけ(製品の欄と同じ)。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr
        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))
        insert_pallet(self.conn, width=1150, length=2650, code="N", unit="台")
        insert_pallet(self.conn, width=1150, length=2650, code="E", unit="台", symbol="EXﾀｲﾄ")
        insert_lot(self.conn)
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")
        insert_odr(self.conn, order_no="A100", 納入先名称="A社", EX_輸出区分="EX")
        self.client.get("/api/lot/1234567", headers=self.auth())     # insert_lot の既定のロット

    def test_EXオンリーならEXの行だけ(self) -> None:
        session = self.session()
        self.assertTrue(session.presenter.is_ex_order)
        session.ex_only = True
        state = self.post("/api/selection/pallet/list", {"pallet_width": "1150", "pallet_length": "2650"})
        self.assertEqual([r["symbol"] for r in state["rows"]], ["EXﾀｲﾄ"])


if __name__ == "__main__":
    unittest.main()
