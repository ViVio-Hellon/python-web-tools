"""予定ロットを開くと、発注したパレットを決めてボード選定へ移る(VER4.8.92)。

現場の声:「予定に届いたロットを開いた場合に、倉庫連携で発注したパレットサイズを検索して
パレットが決定した状態でボード選定まで移行させてください」

発注(資材パレット注文管理)の 幅・丈 はロットの板の寸法なので、パレットは 発注コード で引く
(PalletMaster の コード は1行に1つ)。コードで引けなければ品名「業界　記号　幅x丈」で引き、
マスタに無い寸法なら寸法だけ決める。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packaging_tool import lot_plans, selection_session, work_context  # noqa: E402
from tests import _web  # noqa: E402
from tests.test_lot_service import insert_lot  # noqa: E402
from tests.test_web_lot import make_db  # noqa: E402
from tests.test_web_selection import insert_pallet  # noqa: E402

LOT = "1234567"


def insert_order(conn, *, lot=LOT, hinmei="一般　　1300x2500", code="P1300", unit="台",
                 at="2026-10-23 08:00:00", cancelled=None) -> None:
    # 幅・丈はロットの板の寸法(パレットの寸法ではない)。わざと違う値にしておく
    conn.execute('INSERT INTO "資材パレット注文管理" (登録日時, LotNo, 品名, 発注コード, 発注数, 単位,'
                 " 取り消し済, 厚, 幅, 丈) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (at, lot, hinmei, code, 3, unit, cancelled, 40.0, 1200, 2400))
    conn.commit()


def pallet_id(conn, code: str) -> int:
    return conn.execute("SELECT 管理番号 FROM PalletMaster WHERE コード = ?", (code,)).fetchone()[0]


class OrderedPalletTests(unittest.TestCase):
    """このロットに発注したパレットを引く(`lot_plans.ordered_pallet`)。"""

    def setUp(self) -> None:
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        insert_pallet(self.conn, width=1300, length=2500, code="P1300")
        insert_pallet(self.conn, width=1300, length=2500, code="P1300K", unit="組")   # 同じ寸法の別の行
        insert_pallet(self.conn, width=1100, length=2000, symbol="EX2方向", code="P1100")

    def test_発注コードで発注した行そのものを引く(self) -> None:
        insert_order(self.conn, code="P1300K", unit="組")
        got, why = lot_plans.ordered_pallet(self.conn, LOT)
        self.assertEqual(why, "")
        self.assertEqual((got.width, got.length, got.row_id), (1300, 2500, pallet_id(self.conn, "P1300K")))
        self.assertFalse(got.by_name)

    def test_新しい発注から_取り消し_蓋_EXは見ない(self) -> None:
        insert_order(self.conn, code="P1300", at="2026-10-20 08:00:00")
        insert_order(self.conn, code="P1100", hinmei="一般　EX2方向　1100x2000",
                     at="2026-10-22 08:00:00", cancelled="1")                    # 取り消した
        insert_order(self.conn, code="LID", unit="枚", at="2026-10-23 08:00:00")   # 蓋
        insert_order(self.conn, code="", hinmei="EX", unit="", at="2026-10-23 09:00:00")  # EX の合図
        got, _why = lot_plans.ordered_pallet(self.conn, "  1234567 ")
        self.assertEqual(got.code, "P1300")

    def test_コードで引けなければ品名で引く(self) -> None:
        insert_order(self.conn, code="単価表のマトリックス", hinmei="一般　EX2方向　1100x2000")
        got, _why = lot_plans.ordered_pallet(self.conn, LOT)
        self.assertEqual((got.row_id, got.by_name, got.size_only), (pallet_id(self.conn, "P1100"), True, False))

    def test_マスタに無い寸法は寸法だけ(self) -> None:
        insert_order(self.conn, code="単価表のマトリックス", hinmei="タイト　　1100x2250")
        got, _why = lot_plans.ordered_pallet(self.conn, LOT)
        self.assertEqual((got.width, got.length, got.row_id, got.size_only), (1100, 2250, 0, True))

    def test_見つからない理由(self) -> None:
        self.assertIn("発注がありません", lot_plans.ordered_pallet(self.conn, LOT)[1])
        insert_order(self.conn, code="", hinmei="EX", unit="")
        insert_order(self.conn, code="LID", unit="枚")
        got, why = lot_plans.ordered_pallet(self.conn, LOT)
        self.assertIsNone(got)
        self.assertIn("EX の合図", why)
        self.assertIn("蓋(枚)", why)


class PlanStartTests(unittest.TestCase):
    """POST /api/lot/plans/start。"""

    mode = "field"

    def setUp(self) -> None:
        from app.routes import lot as lot_routes
        from app.routes import selection as selection_routes
        work_context.reset_context()
        self.addCleanup(work_context.reset_context)
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        self.conn = _web.bind_db(self, lot_routes, make_db())
        _web.bind_db(self, selection_routes, self.conn)
        # 共有から取り込み直すところは試験では見に行かない(手元の分で引く)
        refresh = mock.patch("packaging_tool.data_sync.refresh_orders",
                             return_value=mock.Mock(errors=[]))
        self.refresh = refresh.start()
        self.addCleanup(refresh.stop)
        from packaging_tool import access_control
        grant = access_control.grant_of(f"mode:{self.mode}")
        self.client = _web.make_client(self.mode, ready=False, grant=grant)
        insert_lot(self.conn, 設計_設備コース="AAA", 製造板幅=1200.0, 製造板丈=2400.0)
        insert_pallet(self.conn, width=1300, length=2500, code="P1300")

    def start(self, lot=LOT) -> dict:
        res = self.client.post("/api/lot/plans/start", json={"lot_no": lot}, headers=_web.auth())
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        return res.get_json()

    def test_発注したパレットを決めてボード選定へ移る(self) -> None:
        insert_order(self.conn, code="P1300")
        body = self.start()
        self.assertEqual(body["next"], "/selection")
        self.assertIn("P1300", body["message"])
        self.refresh.assert_called_once()                       # 作業長の発注を見るため取り込み直す
        session = selection_session.get_session(self.conn)
        self.assertEqual((session.palette.width, session.palette.length), (1300, 2500))
        self.assertEqual(session.pallet_row.code, "P1300")      # 発注した行そのもの
        self.assertTrue(session.product.is_set)                 # 製品サイズ(1200×2400)も載ると確かめて確定
        state = self.client.get("/api/selection/state", headers=_web.auth()).get_json()
        current = next(s for s in state["steps"] if s["state"] == "current")
        self.assertEqual(current["key"], "boards")              # ボードの段が開く
        self.assertEqual(self.client.get("/selection").status_code, 200)

    def test_載らないパレットでも移って_そう言う(self) -> None:
        insert_pallet(self.conn, width=900, length=900, code="P900")
        insert_order(self.conn, code="P900")
        body = self.start()
        self.assertEqual(body["next"], "/selection")
        self.assertTrue(body["warn"])
        self.assertIn("製品サイズは載るか確かめられなかった", body["message"])
        self.assertFalse(selection_session.get_session(self.conn).product.is_set)

    def test_マスタに無い寸法は寸法だけ決める(self) -> None:
        insert_order(self.conn, code="単価表のマトリックス", hinmei="タイト　　1250x2450")
        body = self.start()
        self.assertEqual(body["next"], "/selection")
        self.assertIn("寸法だけ決めました", body["message"])
        session = selection_session.get_session(self.conn)
        self.assertEqual((session.palette.width, session.palette.length), (1250, 2450))
        self.assertIsNone(session.pallet_row)
        self.assertTrue(session.product.is_set)

    def test_発注が無ければロット情報を開く(self) -> None:
        body = self.start()
        self.assertEqual(body["next"], "")
        self.assertTrue(body["found"])                           # ロット情報(今までの「開く」と同じ中身)
        self.assertIn("発注がありません", body["message"])
        self.assertEqual(work_context.get_context().lot_no, LOT)  # 作業中のロットにはなる

    def test_無いロットはそのまま(self) -> None:
        body = self.start("9999999")
        self.assertEqual(body["next"], "")
        self.assertFalse(body["found"])


class PlanStartMaterialTests(PlanStartTests):
    """資材モードでは資材選択へ移らない(資材展開は現場の操作)。"""

    mode = "material"

    def test_発注したパレットを決めてボード選定へ移る(self) -> None:
        insert_order(self.conn, code="P1300")
        body = self.start()
        self.assertEqual(body["next"], "")
        self.assertTrue(body["found"])

    def test_載らないパレットでも移って_そう言う(self) -> None:
        self.skipTest("資材モードでは移らない")

    def test_マスタに無い寸法は寸法だけ決める(self) -> None:
        self.skipTest("資材モードでは移らない")

    def test_発注が無ければロット情報を開く(self) -> None:
        body = self.start()
        self.assertEqual(body["next"], "")
        self.assertEqual(body["message"], "")


class PlanStartScreenTests(unittest.TestCase):
    def test_予定ロットの開くは新しい入口を通る(self) -> None:
        lot_js = (ROOT / "app/static/js/views/lot.js").read_text(encoding="utf-8")
        self.assertIn('api.post("/api/lot/plans/start"', lot_js)
        self.assertIn("startPlans(options.plans, openPlanned)", lot_js)
        # 知らせは移る前に消す(移った先のレールの印に残らないように)
        view = (ROOT / "app/static/js/lotplans_view.js").read_text(encoding="utf-8")
        body = view.split("export async function openPlan(item)")[1].split("}")[0]
        self.assertLess(body.index("markSeen"), body.index("onOpen"))


if __name__ == "__main__":
    unittest.main()
