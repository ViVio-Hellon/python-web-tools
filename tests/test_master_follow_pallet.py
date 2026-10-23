"""マスタ反映もれ #1: 選んだパレット・蓋は、マスタを直したらいまの中身で引き直す(VER4.8.95)。

現場の声:「アクセス権限に限った話ではなく、他のマスタを変更後にその内容が反映されていない
動作描写をするのはおかしい」。

選んだパレットの行は選んだときの写しで、パレットマスタの発注コード・単位を直しても、倉庫送信・
倉庫連携の下書き・Lot印刷は古いままだった(選び直すまで)。いまはパレットマスタが変わったら
(取り込み・計算し直し)、次に資材選択のセッションに触れたときに引き直す。管理番号は取り込みのたびに
振り直されるので、発注コード(無ければ 幅・丈・記号・業界・単位)で引く。
"""
from __future__ import annotations

import unittest

from packaging_tool import master_state, selection_session, work_context
from packaging_tool.pallet_list import refind_pallet_row
from tests.test_web_selection import SelectionWebTestCase, insert_pallet


def touch() -> None:
    """パレットマスタが変わった(マスタ管理で直す → 取り込み直す、と同じ合図)。"""
    master_state.touch(["PalletMaster"])


class RefindTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1100, length=2000, code="P1", unit="台", industry="タイト")
        insert_pallet(self.conn, width=1100, length=2000, code="P1K", unit="組", industry="タイト")
        self.row = selection_session.svc.find_pallet_row(self.conn, 1100, 2000, "", row_id=1)

    def test_コードで引く_管理番号が変わっても同じ行(self) -> None:
        self.conn.execute("UPDATE PalletMaster SET 管理番号 = 管理番号 + 100, 単位 = '台 ' WHERE コード = 'P1'")
        self.conn.execute("UPDATE PalletMaster SET 備考 = '直した' WHERE コード = 'P1'")
        got = refind_pallet_row(self.conn, self.row)
        self.assertEqual((got.code, got.id, got.remark), ("P1", 101, "直した"))

    def test_コードを直したら寸法と単位で引く(self) -> None:
        self.conn.execute("UPDATE PalletMaster SET コード = 'P1NEW' WHERE コード = 'P1'")
        self.assertEqual(refind_pallet_row(self.conn, self.row).code, "P1NEW")   # 組の行(P1K)にはならない

    def test_消したら無い(self) -> None:
        self.conn.execute("DELETE FROM PalletMaster WHERE コード = 'P1'")
        self.assertIsNone(refind_pallet_row(self.conn, self.row))


class SessionFollowTests(SelectionWebTestCase):
    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1100, length=2000, code="P1", unit="台", industry="タイト")
        self.post("/api/selection/pallet/pick", {"width": 1100, "length": 2000, "symbol": "", "id": 1})
        # 倉庫連携の下書きがある状態(パレットを決めたときに作るもの)
        work_context.get_context().set_pending_orders([{"hatchu_code": "P1", "tani": "台", "hinmei": "x"}])

    def test_コードを直したら選んだ行も下書きも新しいコード(self) -> None:
        self.conn.execute("UPDATE PalletMaster SET コード = 'P9', 単位 = '台' WHERE コード = 'P1'")
        touch()
        state = self.get()
        session = self.session()
        self.assertEqual(session.pallet_row.code, "P9")
        self.assertIn("コード P1→P9", state["master_note"])
        # 下書きも新しいコードで作り直す(ロット検索なしの下書き。`_rebuild_pending_orders`)
        drafts = work_context.get_context().peek_pending_orders()
        self.assertEqual([d["hatchu_code"] for d in drafts], ["P9"])

    def test_変わっていなければ引き直さない(self) -> None:
        self.conn.execute("UPDATE PalletMaster SET コード = 'P9' WHERE コード = 'P1'")   # 合図なし
        self.get()
        self.assertEqual(self.session().pallet_row.code, "P1")
        self.assertEqual(self.session().master_note, "")

    def test_消したら外してそう言う_倉庫送信は止まる(self) -> None:
        self.conn.execute("DELETE FROM PalletMaster WHERE コード = 'P1'")
        touch()
        state = self.get()
        self.assertIsNone(self.session().pallet_row)
        self.assertIn("見つからなくなったので外しました", state["master_note"])
        self.assertFalse(state["outputs"]["can_send"])

    def test_選び直したら一言は消える(self) -> None:
        self.conn.execute("UPDATE PalletMaster SET コード = 'P9' WHERE コード = 'P1'")
        touch()
        self.get()
        self.post("/api/selection/pallet/pick", {"width": 1100, "length": 2000, "symbol": ""})
        self.assertEqual(self.get()["master_note"], "")

    def test_取り込みと計算し直しで合図が出る(self) -> None:
        from packaging_tool import pallet_service
        before = master_state.table_rev("PalletMaster")
        pallet_service.recompute_fit_ranges(self.conn)
        self.assertEqual(master_state.table_rev("PalletMaster"), before + 1)


class WarehouseDraftTests(unittest.TestCase):
    """倉庫連携: 画面の下書きが古い(マスタで作り直された)なら送らずに読み直させる。"""

    def setUp(self) -> None:
        from app.routes import warehouse as routes
        from packaging_tool import access_control
        from tests import _web
        work_context.reset_context()
        self.addCleanup(work_context.reset_context)
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        self.conn = _web.bind_db(self, routes)
        self.client = _web.make_client("field", ready=False, grant=access_control.grant_of("mode:field"))
        self.auth = _web.auth()

    def test_古い発注コードの下書きは送らない(self) -> None:
        context = work_context.get_context()
        context.set_pending_orders([{"lot_no": "", "hatchu_code": "P9", "tani": "台", "hinmei": "タイト　　1100x2000",
                                     "no_lot": True}])
        sent = {"from_draft": True, "lot_no": "1234567", "hinmei": "タイト　　1100x2000", "hatchu_code": "P1",
                "tani": "台", "hatchu_suu": "3", "zaisitu": "A", "choshitu": "O", "atu": "1", "haba": "1",
                "take": "1", "nounyusaki": "x", "yoto_code": "y"}
        res = self.client.post("/api/warehouse/send", json=sent, headers=self.auth)
        body = res.get_json()
        if res.status_code == 400:                   # 入力の形で先に断られたら、この試験の前提が崩れている
            self.fail(body)
        self.assertEqual(res.status_code, 409, body)
        self.assertEqual(body["error"]["code"], "draft_changed")
        self.assertIn("P1 → P9", body["error"]["message"])

    def test_一覧の応答に下書きの印が載る(self) -> None:
        work_context.get_context().set_pending_orders([{"hatchu_code": "P9", "tani": "台", "hinmei": "h"}])
        body = self.client.get("/api/warehouse/orders", headers=self.auth).get_json()
        self.assertEqual(body["drafts_sig"], "P9|台|h")


if __name__ == "__main__":
    unittest.main()


class RenumberTests(SelectionWebTestCase):
    """マスタ反映もれ #2: 取り込みで管理番号が振り直されても「パレット決定」「蓋選択」が通る(VER4.8.96)。"""

    def setUp(self) -> None:
        super().setUp()
        insert_pallet(self.conn, width=1100, length=2000, code="P1", unit="台", industry="タイト")

    def renumber(self) -> None:
        """取り込み直し(DELETE して入れ直す)と同じく、番号が変わる。"""
        self.conn.execute("UPDATE PalletMaster SET 管理番号 = 管理番号 + 50")

    def test_古い番号でも発注コードで引き直して決まる(self) -> None:
        self.renumber()
        state = self.post("/api/selection/pallet/pick",
                          {"width": 1100, "length": 2000, "symbol": "", "id": 1, "code": "P1"})
        self.assertTrue(state["pallet_set"])
        self.assertEqual((self.session().pallet_row.id, self.session().pallet_row.code), (51, "P1"))

    def test_コードが無ければ今までどおり断る(self) -> None:
        self.renumber()
        self.post("/api/selection/pallet/pick", {"width": 1100, "length": 2000, "symbol": "", "id": 1}, expect=400)

    def test_一覧の行に発注コードが載る_画面が付け直す(self) -> None:
        from pathlib import Path
        rows = self.get()["rows"]
        self.assertEqual(rows[0]["code"], "P1")
        js = (Path(__file__).resolve().parent.parent / "app/static/js/views/selection.js").read_text(encoding="utf-8")
        self.assertIn("tr.dataset.code = item.code", js)
        self.assertIn('id: pickedPalletRow.id, code: pickedPalletRow.code || ""', js)
        self.assertIn("(r.code || \"\") === (pickedPalletRow.code || \"\")", js)


class Materials1P0113Tests(SelectionWebTestCase):
    """マスタ反映もれ #3: 松板角材を直したら、1P0113 の角材・松板を引き直す(VER4.8.97)。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from packaging_tool import special_packaging as spk
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr
        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))
        for name, atsu, haba, code in ((spk.KAKUZAI_NAME, spk.KAKUZAI_ATSU, spk.KAKUZAI_HABA, "K001"),
                                       (spk.MATSUITA_NAME, spk.MATSUITA_ATSU, spk.MATSUITA_HABA, "M001")):
            self.conn.execute("INSERT INTO 松板角材 (品名, 厚, 幅, 丈min, 丈max, コード, 単位, 備考)"
                              " VALUES (?,?,?,?,?,?,?,?)", (name, atsu, haba, 1, 9999, code, "本", ""))
        self.conn.commit()
        insert_lot(self.conn)
        insert_hiki(self.conn)
        insert_odr(self.conn)
        self.client.get("/api/lot/1234567", headers=self.auth())
        session = self.session()
        session.presenter.force_1p0113 = True
        session.presenter.apply_1p0113_mode("", product_width=1000, product_length=1800)
        work_context.get_context().product_width = 1000
        work_context.get_context().product_length = 1800
        session.reload_1p0113_materials()
        self.get()                                        # いまの回数を覚えさせる

    def test_コードを直したら倉庫送信も新しいコード(self) -> None:
        self.conn.execute("UPDATE 松板角材 SET コード = 'K999' WHERE コード = 'K001'")
        master_state.touch(["松板角材"])
        state = self.get()
        self.assertIn("コード K001→K999", state["master_note"])
        self.post("/api/selection/send")
        self.assertEqual([o["hatchu_code"] for o in work_context.get_context().pending_orders], ["K999", "M001"])

    def test_足した行で送れるようになる(self) -> None:
        self.conn.execute("DELETE FROM 松板角材 WHERE コード = 'M001'")
        master_state.touch(["松板角材"])
        self.assertFalse(self.get()["outputs"]["can_send"])          # 該当がないので止まる
        from packaging_tool import special_packaging as spk
        self.conn.execute("INSERT INTO 松板角材 (品名, 厚, 幅, 丈min, 丈max, コード, 単位, 備考)"
                          " VALUES (?,?,?,?,?,?,?,?)",
                          (spk.MATSUITA_NAME, spk.MATSUITA_ATSU, spk.MATSUITA_HABA, 1, 9999, "M777", "枚", ""))
        master_state.touch(["松板角材"])
        self.assertTrue(self.get()["outputs"]["can_send"])
