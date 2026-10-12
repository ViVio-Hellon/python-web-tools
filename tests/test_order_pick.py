"""ロット検索で選んだオーダーを倉庫連携に紐づける試験

【ここで守りたいこと】
1. ロット検索で引当の行(オーダー)を押すと、倉庫連携の**納入先がそのオーダーのもの**になる
   (以前は受注番号のいちばん小さいオーダーのまま変わらなかった)
2. 同じロットでもオーダーごとに資材が違う。**一緒に送るオーダー**を選び、発注数はその梱包数の合計
3. 倉庫の一覧に、含めたオーダーと含めなかったオーダーが出る(「なぜ同じロットで違う資材を」と迷わない)
4. 見るだけの窓(発注一覧の「Lotを開く」)で押しても、選んだことにしない
"""
from __future__ import annotations

import html as html_lib
import json
import re
import sqlite3
import unittest

from packaging_tool import db, import_specs, lot_service, sync_writeback, work_context
from packaging_tool import config
from packaging_tool.presenters import warehouse as wh_presenter
from tests.test_web_selection import SelectionWebTestCase, insert_pallet

LOT = "1234567"


class OrderPickTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from app.routes import warehouse as wh_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        for module in (lot_routes, wh_routes):
            original = module.get_db
            module.get_db = lambda: self.conn
            self.addCleanup(lambda m=module, o=original: setattr(m, "get_db", o))
        insert_pallet(self.conn, width=1100, length=2000, code="P9", unit="台")
        insert_lot(self.conn)
        # オーダー3つ。受注番号の小さい順(A…)と引当の順(Z…が先)を**わざと逆に**する。
        # 梱包単位は重量(500kg)で数える。引当 10・10・22 → 1・1・1梱包(ロット全体 3)
        insert_hiki(self.conn, order_no="Z300", qty=10.0, no="00000001")
        insert_hiki(self.conn, order_no="M200", qty=10.0, no="00000002")
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000003")
        insert_odr(self.conn, order_no="Z300", 納入先名称="Z社")
        insert_odr(self.conn, order_no="M200", 納入先名称="M社")
        insert_odr(self.conn, order_no="A100", 納入先名称="A社")

    def open_lot(self) -> dict:
        return self.client.get(f"/api/lot/{LOT}", headers=self.auth()).get_json()

    def click(self, hiki_no: str, pick: bool = True) -> dict:
        query = "?pick=1" if pick else ""
        return self.client.get(f"/api/lot/{LOT}/hiki/{hiki_no}{query}", headers=self.auth()).get_json()

    def pick_pallet(self) -> None:
        self.post("/api/selection/pallet/pick", {"width": 1100, "length": 2000, "symbol": ""})
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})
        self.post("/api/selection/send")

    def drafts_on_page(self) -> list:
        page = self.client.get("/warehouse", headers=self.auth()).get_data(as_text=True)
        found = re.search(r'data-orders="([^"]*)"', page)
        return json.loads(html_lib.unescape(found.group(1))) if found else []

    def send(self, **kw):
        draft = work_context.get_context().peek_pending_orders()[0]
        body = {k: v for k, v in draft.items() if not k.startswith("pallet_")}
        body.update(from_draft=True, **kw)
        return self.client.post("/api/warehouse/send", json=body, headers=self.auth())

    # --- 梱包数 -------------------------------------------------------------
    def test_オーダーごとの梱包数を足すとロット全体と同じ(self) -> None:
        result = lot_service.search_lot(self.conn, LOT)
        counts, reason = lot_service.packages_by_order(result)
        self.assertEqual(reason, "")
        self.assertEqual(counts, {"Z300": 1, "M200": 1, "A100": 1})
        self.assertEqual(sum(counts.values()), lot_service.calc_total_packages(result))

    # --- 納入先 -------------------------------------------------------------
    def test_選ばなければ今までと同じ納入先と発注数(self) -> None:
        self.open_lot()
        self.pick_pallet()
        draft = work_context.get_context().peek_pending_orders()[0]
        # 受注情報に出ていたオーダー(受注番号のいちばん小さい A100)。以前と同じ
        self.assertEqual((draft["nounyusaki"], draft["hatchu_suu"]), ("A社", "3"))
        self.assertEqual(draft["juchu_no"], "A100・Z300・M200")

    def test_ロット検索で選んだオーダーの納入先と梱包数になる(self) -> None:
        self.open_lot()
        body = self.click("00000002")                   # M200
        self.assertTrue(body["picked"])
        self.assertEqual(work_context.get_context().picked_order, "M200")
        self.pick_pallet()
        draft = work_context.get_context().peek_pending_orders()[0]
        self.assertEqual((draft["nounyusaki"], draft["hatchu_suu"], draft["juchu_no"]),
                         ("M社", "1", "M200"))

    def test_パレットを決めたあとで選び直しても倉庫連携を開くと合わせ直す(self) -> None:
        self.open_lot()
        self.pick_pallet()
        self.click("00000003")                          # A100(1梱包)
        drafts = self.drafts_on_page()
        self.assertEqual((drafts[0]["nounyusaki"], drafts[0]["hatchu_suu"]), ("A社", "1"))
        block = drafts[0]["orders"]
        self.assertIn("オーダーが 3件", block["lead"])
        self.assertEqual([(i["order_no"], i["packages"], i["picked"], i["checked"]) for i in block["items"]],
                         [("Z300", 1, False, False), ("M200", 1, False, False), ("A100", 1, True, True)])

    def test_見るだけの窓で押しても選んだことにしない(self) -> None:
        self.open_lot()
        body = self.click("00000002", pick=False)
        self.assertFalse(body["picked"])
        self.assertEqual(work_context.get_context().picked_order, "")

    def test_開き直すと選んだオーダーの行が選ばれていて受注情報もそれ(self) -> None:
        self.open_lot()
        self.click("00000002")
        body = self.open_lot()
        self.assertEqual([h["order_no"] for h in body["hiki"] if h["selected"]], ["M200"])
        self.assertEqual(body["order_no"], "M200")

    def test_別のロットに移ると選んだオーダーは外れる(self) -> None:
        self.open_lot()
        self.click("00000002")
        work_context.get_context().clear()
        self.assertEqual(work_context.get_context().picked_order, "")

    # --- 送る --------------------------------------------------------------
    def test_一緒に送るオーダーを選んで送ると一覧に含めたものと含めないものが出る(self) -> None:
        self.open_lot()
        self.click("00000001")                          # Z300 を納入先に
        self.pick_pallet()
        res = self.send(orders=["Z300", "M200"], hatchu_suu="2")
        self.assertEqual(res.status_code, 200, res.get_json())
        row = self.conn.execute("SELECT 納入先, 発注数, 受注番号, ロットの受注 FROM 資材パレット注文管理").fetchone()
        self.assertEqual(tuple(row), ("Z社", 2, "Z300・M200", "Z300・M200・A100"))
        values = self.get("/api/warehouse/orders")["rows"][0]["values"]
        self.assertEqual(values["オーダー"], "Z300・M200(3件中2件)")       # 一覧の欄(狭い)
        self.assertEqual(values["オーダーの内訳"],                            # 行を開いた欄
                         "Z300・M200(このロットのほかのオーダー A100 はこの発注に含まない)")

    def test_このロットに無いオーダーは断る(self) -> None:
        self.open_lot()
        self.pick_pallet()
        res = self.send(orders=["Z300", "X999"])
        self.assertEqual(res.status_code, 400)
        self.assertIn("X999", res.get_json()["error"]["message"])

    def test_納入先のオーダーは外しても必ず含める_納入先は画面の値を使わない(self) -> None:
        self.open_lot()
        self.click("00000003")                          # A100
        self.pick_pallet()
        res = self.send(orders=["M200"], nounyusaki="書き換えた納入先")
        self.assertEqual(res.status_code, 200, res.get_json())
        row = self.conn.execute("SELECT 納入先, 受注番号 FROM 資材パレット注文管理").fetchone()
        self.assertEqual(tuple(row), ("A社", "A100・M200"))


class OrderSummaryTests(unittest.TestCase):
    def test_一覧の言い方(self) -> None:
        s = wh_presenter.order_summary
        self.assertEqual(s({"受注番号": "", "ロットの受注": ""}), "")
        self.assertEqual(s({"受注番号": "O1", "ロットの受注": "O1"}), "O1")
        self.assertEqual(s({"受注番号": "O1・O2", "ロットの受注": "O1・O2"}), "O1・O2(このロットの全オーダー)")


class OrderColumnSyncTests(unittest.TestCase):
    def test_共有へ送り取り込む列に入っている(self) -> None:
        spec = next(s for s in sync_writeback.WRITEBACK_SPECS
                    if s.sqlite_table == config.TBL_WAREHOUSE_ORDER)
        self.assertIn("受注番号", spec.optional_columns)
        self.assertIn("ロットの受注", spec.optional_columns)
        added = dict(sync_writeback.SHARED_ADDED_COLUMNS[config.TBL_WAREHOUSE_ORDER])
        self.assertEqual((added["受注番号"], added["ロットの受注"]), ("TEXT", "TEXT"))
        cols = [src for _c, src, _f in import_specs.IMPORT_SPECS[config.TBL_WAREHOUSE_ORDER]]
        self.assertIn("受注番号", cols)
        self.assertIn("ロットの受注", import_specs.OPTIONAL_COLUMNS[config.TBL_WAREHOUSE_ORDER])

    def test_古い手元DBにも列を足す(self) -> None:
        new = sqlite3.connect(":memory:")
        db.apply_schema(new)
        old_cols = [f"[{r[1]}] {r[2]}" for r in new.execute("PRAGMA table_info(資材パレット注文管理)")
                    if r[1] not in ("受注番号", "ロットの受注")]
        conn = sqlite3.connect(":memory:")
        conn.execute(f"CREATE TABLE 資材パレット注文管理 ({', '.join(old_cols)})")   # 前の版の形
        db.apply_schema(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(資材パレット注文管理)")]
        self.assertIn("受注番号", cols)
        self.assertIn("ロットの受注", cols)


if __name__ == "__main__":
    unittest.main()
