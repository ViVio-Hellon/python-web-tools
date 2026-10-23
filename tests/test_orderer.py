"""倉庫送信は作業長の端末だけ / 作業者は蓋を選ばずにボードへ / 予定ロットを開く手順(VER4.8.93)。

現場の声:
- 「作業長以外の 倉庫連携 倉庫送信をできないようにしてください(作業長以外見えなくてもいいかも)」
- 「上下共用ボードオーダー選択中にて作業長以外は蓋の選定なしでボード選定できるようにしてください。
   発注がらみは枚・台セットになってしまいますが、作業者には関係ないです」
- 「予定ロットを開くとき、ボタン内に秒数が出るが、同時にプログレスも出して進度表示してほしい」

表に作業長(Administrator)が1人もいなければ、今までどおり誰でも送れる(締め出さない)。
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from packaging_tool import access_control, selection_session, work_context
from tests import _web
from tests.test_web_selection import SelectionWebTestCase, insert_pallet

ROOT = Path(__file__).resolve().parent.parent
LOT = "1234567"
PC = "LINE-PC-01"
ME = {access_control.ENV_LOGIN: "worker", access_control.ENV_HOST: PC}


def access_row(conn, *, pc=PC, permission="mode:field", enabled=1) -> None:
    conn.execute('INSERT INTO "アクセス権限" (ログインID, PC名, 権限, 有効) VALUES (?,?,?,?)',
                 ("", pc, permission, enabled))
    conn.commit()


class CanOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)
        self.me = access_control.Identity(login_id="worker", pc_name=PC)

    def test_作業長が1人もいなければ誰でも(self) -> None:
        self.assertTrue(access_control.can_order(self.conn, self.me))
        access_row(self.conn, permission="L-1")
        access_row(self.conn, pc="OTHER", permission="作業長", enabled=0)   # 無効の作業長は数えない
        self.assertTrue(access_control.can_order(self.conn, self.me))
        self.assertTrue(access_control.can_order(None, self.me))

    def test_作業長がいれば作業長だけ(self) -> None:
        access_row(self.conn, permission="L-1")
        access_row(self.conn, pc="LEADER-PC", permission="作業長")
        self.assertFalse(access_control.can_order(self.conn, self.me))
        self.assertTrue(access_control.can_order(
            self.conn, access_control.Identity(login_id="x", pc_name="LEADER-PC")))
        access_row(self.conn, permission="Administrator")                   # Administrator も作業長
        self.assertTrue(access_control.can_order(self.conn, self.me))


class _LidCase(SelectionWebTestCase):
    """上下共用(EX)のロットを開いた状態。蓋(枚)とパレット(台)がある。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from app.routes import warehouse as wh_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr
        for module in (lot_routes, wh_routes):
            original = module.get_db
            module.get_db = lambda: self.conn
            self.addCleanup(lambda m=module, o=original: setattr(m, "get_db", o))
        env = mock.patch.dict(os.environ, ME)
        env.start()
        self.addCleanup(env.stop)
        insert_pallet(self.conn, width=1300, length=2500, code="D1", unit="台", industry="タイト", symbol="EXﾀｲﾄ")
        insert_pallet(self.conn, width=1300, length=2500, code="M1", unit="枚", industry="タイト", symbol="EXﾀｲﾄ")
        insert_lot(self.conn, 設計_設備コース="AAA", 製造板幅=1200.0, 製造板丈=2400.0)
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")
        insert_odr(self.conn, order_no="A100", 納入先名称="A社", EX_輸出区分="EX")
        self.client.get(f"/api/lot/{LOT}", headers=self.auth())
        self.client.post("/api/lot/expand", json={}, headers=self.auth())

    def decide(self) -> dict:
        r = self.conn.execute("SELECT 管理番号, 幅, 丈, 記号 FROM PalletMaster WHERE コード = 'D1'").fetchone()
        return self.post("/api/selection/pallet/pick",
                         {"id": r[0], "width": r[1], "length": r[2], "symbol": r[3]})

    @staticmethod
    def step(state: dict, key: str) -> dict:
        return next(s for s in state["steps"] if s["key"] == key)


class WorkerTests(_LidCase):
    """作業長のいる職場の、作業長でない端末(ラインの作業者)。"""

    def setUp(self) -> None:
        super().setUp()
        access_row(self.conn, permission="L-1")
        access_row(self.conn, pc="LEADER-PC", permission="作業長")

    def test_蓋を選ばずにボードへ進む(self) -> None:
        state = self.decide()
        self.assertTrue(state["lid_enabled"])
        self.assertFalse(state["lid_set"])
        self.assertFalse(self.step(state, "size")["open"])          # 蓋を待たない(畳む)
        self.assertEqual(self.step(state, "boards")["state"], "current")

    def test_倉庫送信できない_見出しのボタンも出ない(self) -> None:
        state = self.decide()
        self.assertFalse(state["outputs"]["can_send"])
        self.assertIn("作業長", state["outputs"]["send_why"])
        res = self.client.post("/api/selection/send", json={}, headers=self.auth())
        self.assertEqual(res.status_code, 422)
        self.assertIn("作業長", res.get_json()["message"])
        self.assertEqual(work_context.get_context().peek_pending_orders(), [])   # 下書きも作らない

    def test_倉庫連携の倉庫へ送信も断る(self) -> None:
        page = self.client.get("/warehouse").get_data(as_text=True)
        self.assertIn('id="send" disabled', page)
        self.assertIn('id="orderWhy"', page)
        res = self.client.post("/api/warehouse/send", json={"lot_no": LOT}, headers=self.auth())
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "not_orderer")


class LeaderTests(_LidCase):
    """作業長の端末は今までどおり(蓋が決まるまで段を開いておく・送れる)。"""

    def setUp(self) -> None:
        super().setUp()
        access_row(self.conn, permission="作業長")

    def test_蓋が決まるまで開いておく_送れる(self) -> None:
        state = self.decide()
        self.assertTrue(self.step(state, "size")["open"])
        self.assertTrue(state["outputs"]["can_send"])
        page = self.client.get("/warehouse").get_data(as_text=True)
        self.assertNotIn('id="orderWhy"', page)
        self.assertNotIn('id="send" disabled', page)


class PlanStepsTests(unittest.TestCase):
    """予定ロットを開く手順(open → orders → pallet)。画面は1つずつ呼んで進み具合を出す。"""

    def setUp(self) -> None:
        from app.routes import lot as lot_routes
        from app.routes import selection as selection_routes
        from tests.test_lot_service import insert_lot
        from tests.test_plan_start import insert_order
        from tests.test_web_lot import make_db
        work_context.reset_context()
        self.addCleanup(work_context.reset_context)
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        self.conn = _web.bind_db(self, lot_routes, make_db())
        _web.bind_db(self, selection_routes, self.conn)
        refresh = mock.patch("packaging_tool.data_sync.refresh_orders", return_value=mock.Mock(errors=[]))
        self.refresh = refresh.start()
        self.addCleanup(refresh.stop)
        self.client = _web.make_client("field", ready=False, grant=access_control.grant_of("mode:field"))
        insert_lot(self.conn, 設計_設備コース="AAA", 製造板幅=1200.0, 製造板丈=2400.0)
        insert_pallet(self.conn, width=1300, length=2500, code="P1300")
        insert_order(self.conn, code="P1300")

    def post(self, step: str) -> dict:
        res = self.client.post("/api/lot/plans/start", json={"lot_no": LOT, "step": step}, headers=_web.auth())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()

    def test_1つずつ進む(self) -> None:
        self.assertEqual(self.post("open")["next_step"], "orders")
        self.refresh.assert_not_called()
        self.assertEqual(self.post("orders")["next_step"], "pallet")
        self.refresh.assert_called_once()
        body = self.post("pallet")
        self.assertEqual(body["next"], "/selection")
        self.assertNotIn("next_step", body)
        self.assertEqual(selection_session.get_session(self.conn).pallet_row.code, "P1300")

    def test_途中で別のロットになっていたら開き直す(self) -> None:
        self.post("open")
        work_context.reset_context()                       # ほかで作業中のロットが変わった
        body = self.post("pallet")
        self.assertEqual(body["next"], "/selection")
        self.assertEqual(work_context.get_context().lot_no, LOT)

    def test_知らない手順は断る(self) -> None:
        res = self.client.post("/api/lot/plans/start", json={"lot_no": LOT, "step": "x"}, headers=_web.auth())
        self.assertEqual(res.status_code, 400)

    def test_画面は手順ごとに進み具合を出す(self) -> None:
        lot_js = (ROOT / "app/static/js/views/lot.js").read_text(encoding="utf-8")
        for step in ('"open"', '"orders"', '"pallet"'):
            self.assertIn(step, lot_js)
        self.assertIn("planProgress(index, total", lot_js)
        self.assertIn("planProgress(null)", lot_js)
        page = (ROOT / "app/templates/lot.html").read_text(encoding="utf-8")
        self.assertIn('id="planProgressBar"', page)


if __name__ == "__main__":
    unittest.main()
