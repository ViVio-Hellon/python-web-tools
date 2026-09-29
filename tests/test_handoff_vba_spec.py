"""倉庫と現場の受け渡し ── 更新後の VBA で直した不具合をツールでも起こさない。

VBA(2026-09 の更新)で直した9項目のうち、ツールにも同じ形で残っていたもの:

1. 送信が途中で失敗すると一部だけ登録される(1P0113 の角材・松板を1行ずつ送っていた)
3. 見つからないロット番号を開いたあとも、前のロットで送信できた
4. 納入先の違う引当があっても知らせなかった
5. 同じロットを二度送っても警告が無かった
6. 1P0113 で資材マスタに発注コードが無くても「---」で送れた
7. 取消済みを選んでも、いつ取り消されたかが分からなかった
9. 自動検索の候補選びが単位を見ていなかった(`test_board_selection_service` 側)
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from packaging_tool import warehouse_service as svc
from packaging_tool import work_context
from tests.test_web_selection import SelectionWebTestCase, insert_pallet
from tests.test_web_warehouse import ORDER, WarehouseWebTestCase, make_db

ORDER_TABLE = "資材パレット注文管理"


def count(conn) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{ORDER_TABLE}"').fetchone()[0]


class CreateOrdersTests(unittest.TestCase):
    """全行を1回で登録する(VBA `WriteWarehouseRows`)。"""

    def setUp(self) -> None:
        self.conn = make_db()
        self.row = {**ORDER}

    def test_全行が入る(self) -> None:
        got = svc.create_orders(self.conn, [self.row, {**self.row, "hinmei": "松板"}],
                                terminal="GENBA-1")
        self.assertTrue(got.ok, got.message)
        self.assertEqual(len(got.mgr_nos), 2)
        self.assertEqual(count(self.conn), 2)

    def test_1行でもおかしければ1行も入らない(self) -> None:
        got = svc.create_orders(self.conn, [self.row, {**self.row, "hatchu_suu": "0"}],
                                terminal="GENBA-1")
        self.assertFalse(got.ok)
        self.assertIn("2行目", got.message)
        self.assertEqual(count(self.conn), 0)

    def test_書く途中で落ちたら先に書いた行も残らない(self) -> None:
        """1行目を書いたあとに2行目で落ちる(VBA ではここで1行目だけが残った)。"""
        self.conn.execute(
            f'CREATE TRIGGER stop BEFORE INSERT ON "{ORDER_TABLE}"'
            " WHEN NEW.品名 = '落ちる' BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END")
        got = svc.create_orders(self.conn, [self.row, {**self.row, "hinmei": "落ちる"}],
                                terminal="GENBA-1")
        self.assertFalse(got.ok)
        self.assertIn("1行も登録していません", got.message)
        self.assertEqual(count(self.conn), 0)

    def test_ロックに当たったら全行をやり直す(self) -> None:
        """やり直しは**開き直した状態で全行を最初から**(VBA は閉じた接続のまま
        やり直していて必ず失敗した)。"""
        real = self.conn
        calls = {"n": 0}

        class Flaky:
            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, *args):
                if sql.startswith("BEGIN") and calls["n"] < 2:
                    calls["n"] += 1
                    raise sqlite3.OperationalError("database is locked")
                return real.execute(sql, *args)

        got = svc.create_orders(Flaky(), [self.row, {**self.row, "hinmei": "松板"}],
                                terminal="GENBA-1")
        self.assertTrue(got.ok, got.message)
        self.assertEqual(calls["n"], 2)
        self.assertEqual(count(real), 2)

    def test_発注コードがハイフン3つなら送らない(self) -> None:
        got = svc.create_orders(self.conn, [{**self.row, "hatchu_code": "---"}],
                                terminal="GENBA-1")
        self.assertFalse(got.ok)
        self.assertEqual(count(self.conn), 0)

    def test_EXは発注コードが空でも送れる(self) -> None:
        got = svc.create_orders(self.conn, [{**self.row, "hinmei": "EX", "hatchu_code": "",
                                             "tani": "", "hatchu_suu": "",
                                             "is_ex_order": True}], terminal="GENBA-1")
        self.assertTrue(got.ok, got.message)


class SendApiTests(WarehouseWebTestCase):
    """`/api/warehouse/send`。"""

    def post(self, body: dict, expect: int = 200) -> dict:
        res = self.clients["field"].post("/api/warehouse/send", json=body,
                                         headers=self.auth())
        self.assertEqual(res.status_code, expect, res.get_json())
        return res.get_json()

    def test_下書き2行はまとめて1回で登録する(self) -> None:
        work_context.get_context().lot_no = ORDER["lot_no"]
        self.addCleanup(setattr, work_context.get_context(), "lot_no", "")
        body = self.post({"from_draft": True, "rows": [
            {**ORDER, "hinmei": "溝切角材"}, {**ORDER, "hinmei": "松板"}]})
        self.assertEqual(len(body["mgr_nos"]), 2)
        self.assertIn("2行まとめて", body["message"])
        self.assertEqual(count(self.conn), 2)

    def test_2行目がおかしければ1行も登録せず何行目かを言う(self) -> None:
        body = self.post({"rows": [ORDER, {**ORDER, "hatchu_suu": ""}]}, expect=400)
        self.assertEqual(body["error"]["row"], 1)
        self.assertIn("2行目", body["error"]["message"])
        self.assertEqual(count(self.conn), 0)

    def test_同じロットを送ってあれば確かめる(self) -> None:
        self.post(ORDER)
        body = self.post(ORDER, expect=409)
        self.assertEqual(body["error"]["code"], "already_sent")
        ask = body["ask"]
        self.assertEqual(ask["title"], "二重送信の確認")
        self.assertIn("二重発注", ask["lead"])
        self.assertIn("1件", ask["why"])
        self.assertEqual(len(ask["fields"]), 1)
        self.assertIn("発注数:4", ask["fields"][0]["value"])
        self.assertEqual(count(self.conn), 1)
        # 「それでも送信する」
        self.post({**ORDER, "confirm_duplicate": True})
        self.assertEqual(count(self.conn), 2)

    def test_取り消した発注は二重送信に数えない(self) -> None:
        mgr = self.post(ORDER)["mgr_no"]
        svc.cancel_order(self.conn, mgr, terminal=svc.this_terminal())
        self.post(ORDER)

    def test_倉庫が確認済みなら印を付けて見せる(self) -> None:
        mgr = self.post(ORDER)["mgr_no"]
        svc.confirm_order(self.conn, mgr)
        ask = self.post(ORDER, expect=409)["ask"]
        self.assertIn("[倉庫確認済]", ask["fields"][0]["value"])

    def test_送信済みが多ければ5件までと残りの件数(self) -> None:
        for _ in range(7):
            self.post({**ORDER, "confirm_duplicate": True})
        fields = self.post(ORDER, expect=409)["ask"]["fields"]
        self.assertEqual(len(fields), 6)
        self.assertEqual(fields[-1], {"label": "ほか", "value": "2件"})


class CancelledRowTests(WarehouseWebTestCase):
    def test_取消済みの行はいつ取り消されたかを言う(self) -> None:
        res = self.send()
        mgr = res.get_json()["mgr_no"]
        svc.cancel_order(self.conn, mgr, terminal=svc.this_terminal())
        when = self.conn.execute(f'SELECT 取り消し日時 FROM "{ORDER_TABLE}"').fetchone()[0]
        state = self.clients["material"].get(
            "/api/warehouse/orders?cancelled=1", headers=self.auth()).get_json()
        row = state["rows"][0]
        self.assertFalse(row["can_confirm"])
        self.assertIn("現場で取り消されています", row["why"])
        self.assertIn(when, row["why"])

    def test_取消済みは確認済みにできない(self) -> None:
        mgr = self.send().get_json()["mgr_no"]
        svc.cancel_order(self.conn, mgr, terminal=svc.this_terminal())
        got = svc.confirm_order(self.conn, mgr)
        self.assertFalse(got.ok)
        self.assertIn("取り消し済み", got.message)
        self.assertIsNone(self.conn.execute(
            f'SELECT 確認日時 FROM "{ORDER_TABLE}"').fetchone()[0])


class SelectionSendTests(SelectionWebTestCase):
    """資材選択の「倉庫送信」(下書きを作るところ)。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from app.routes import warehouse as wh_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr

        for module in (lot_routes, wh_routes):
            original = module.get_db
            module.get_db = lambda: self.conn
            self.addCleanup(lambda m=module, o=original: setattr(m, "get_db", o))
        self.insert_hiki, self.insert_odr = insert_hiki, insert_odr
        insert_pallet(self.conn, width=1100, length=2000, code="P9", unit="台")
        insert_lot(self.conn)

    def open_lot(self, lot_no: str = "1234567") -> dict:
        return self.client.get(f"/api/lot/{lot_no}", headers=self.auth()).get_json()

    def pick(self) -> None:
        self.post("/api/selection/pallet/pick", {"width": 1100, "length": 2000, "symbol": ""})
        self.post("/api/selection/pallet/apply", {"width": "1100", "length": "2000"})

    # --- 3. 見つからないロット番号 ---------------------------------------
    def test_見つからないロットを開いたら前のロットで送れない(self) -> None:
        self.insert_hiki(self.conn)
        self.insert_odr(self.conn)
        self.open_lot()
        self.pick()
        self.assertTrue(self.get()["outputs"]["can_send"])

        body = self.open_lot("9999999")
        self.assertFalse(body["found"])
        self.assertEqual(work_context.get_context().lot_no, "")
        self.assertIsNone(self.session().presenter.lot_result)
        outputs = self.get()["outputs"]
        self.assertFalse(outputs["can_send"])
        self.assertIn("ロット", outputs["send_why"])
        self.post("/api/selection/send", expect=422)

    # --- 4. 納入先の違う引当 ---------------------------------------------
    def test_納入先の違う引当があれば下書きに知らせを添える(self) -> None:
        self.insert_hiki(self.conn, order_no="O1", no="00000001")
        self.insert_hiki(self.conn, order_no="O2", no="00000002")
        self.insert_odr(self.conn, order_no="O1", 納入先名称="納入先A")
        self.insert_odr(self.conn, order_no="O2", 納入先名称="納入先B")
        self.open_lot()
        self.pick()
        self.post("/api/selection/send")
        order = work_context.get_context().pending_orders[0]
        self.assertEqual(order["nounyusaki"], "納入先A")
        self.assertIn("納入先の違う引当があります(納入先A・納入先B)", order["notice"])
        self.assertIn("納入先: 納入先A(先頭の引当)", order["notice"])

    def test_納入先が1つなら知らせない(self) -> None:
        self.insert_hiki(self.conn, order_no="O1", no="00000001")
        self.insert_hiki(self.conn, order_no="O2", no="00000002")
        self.insert_odr(self.conn, order_no="O1", 納入先名称="納入先A")
        self.insert_odr(self.conn, order_no="O2", 納入先名称="納入先A")
        self.open_lot()
        self.pick()
        self.post("/api/selection/send")
        self.assertNotIn("notice", work_context.get_context().pending_orders[0])

    # --- 6. 1P0113 で発注コードが無い資材 --------------------------------
    def test_1P0113で発注コードの無い資材があれば送らない(self) -> None:
        from packaging_tool import special_packaging as spk
        self.insert_hiki(self.conn)
        self.insert_odr(self.conn)
        self.open_lot()
        for name, atsu, haba, code in (
            (spk.KAKUZAI_NAME, spk.KAKUZAI_ATSU, spk.KAKUZAI_HABA, "K001"),
            (spk.MATSUITA_NAME, spk.MATSUITA_ATSU, spk.MATSUITA_HABA, ""),
        ):
            self.conn.execute(
                "INSERT INTO 松板角材 (品名, 厚, 幅, 丈min, 丈max, コード, 単位, 備考)"
                " VALUES (?,?,?,?,?,?,?,?)", (name, atsu, haba, 1, 9999, code, "本", ""))
        self.conn.commit()
        session = self.session()
        session.presenter.force_1p0113 = True
        session.presenter.apply_1p0113_mode("", product_width=1000, product_length=1800)
        work_context.get_context().product_width = 1000
        work_context.get_context().product_length = 1800
        session.reload_1p0113_materials()

        outputs = self.get()["outputs"]
        self.assertFalse(outputs["can_send"])
        self.assertIn("資材マスタに該当がない資材がある", outputs["send_why"])
        self.assertIn(spk.MATSUITA_SIZE_LABEL, outputs["send_why"])
        self.assertNotIn(spk.KAKUZAI_SIZE_LABEL, outputs["send_why"])
        self.post("/api/selection/send", expect=422)
        self.assertEqual(work_context.get_context().pending_orders, [])


if __name__ == "__main__":
    unittest.main()
