"""発注ごとの現場⇔倉庫のやり取り(`order_comments`)。

現場の端末 A(PC名 GENBA-1)と倉庫の端末 B(SOUKO)、共有の梱包資材マスタ
(実物と同じ、Access から変換した形。コメントの表はまだ無い)で確かめる。
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from packaging_tool import config, data_sync, order_comments as oc, sync_writeback
from packaging_tool import warehouse_service as wh
from tests.test_warehouse_handoff import HandoffBase, ORDER

COMMENTS = config.TBL_ORDER_COMMENT


class CommentTestBase(HandoffBase):
    def order(self, lot: str = "NEW") -> int:
        made = wh.create_order(self.a, lot_no=lot, hinmei="テスト品", hatchu_code="X1",
                               tani="台", atu=1, haba=1000, take=2000,
                               hatchu_suu=1, terminal="GENBA-1")
        self.assertTrue(made.ok, made.message)
        return made.mgr_no

    def write(self, conn, lot: str, text: str, who: str, side: str) -> oc.CommentResult:
        return oc.add(conn, self.local_no(conn, lot), text, terminal=who, side=side)

    def texts(self, conn, lot: str, who: str) -> list[tuple]:
        return [(c.side, c.text, c.unread)
                for c in oc.comments_for(conn, self.local_no(conn, lot), terminal=who)]

    def unread(self, conn, lot: str, who: str) -> int:
        row = conn.execute(f'SELECT * FROM "{ORDER}" WHERE LotNo = ?', (lot,)).fetchone()
        got = oc.summaries(conn, terminal=who).get(oc.order_key(row))
        return got.unread if got else 0


class ExchangeTests(CommentTestBase):
    def test_現場と倉庫で行き来し未読が付いて消える(self):
        self.order("NEW")
        self.assertTrue(self.write(self.a, "NEW", "至急でお願いします", "GENBA-1", oc.SIDE_FIELD).ok)
        self.send(self.a)
        # 共有に表と発注キーが届く
        self.assertEqual(self.shared(f'SELECT 本文, 書いた端末 FROM "{COMMENTS}"'),
                         [("至急でお願いします", "GENBA-1")])
        self.assertTrue(self.shared(f'SELECT 発注キー FROM "{ORDER}" WHERE LotNo="NEW"')[0][0])

        self.refresh(self.b)
        self.assertEqual(self.unread(self.b, "NEW", "SOUKO"), 1)
        self.assertEqual(self.texts(self.b, "NEW", "SOUKO"),
                         [("現場", "至急でお願いします", True)])
        oc.mark_read(self.b, self.local_no(self.b, "NEW"))
        self.assertEqual(self.unread(self.b, "NEW", "SOUKO"), 0)

        self.write(self.b, "NEW", "午後に出します", "SOUKO", oc.SIDE_MATERIAL)
        self.send(self.b)
        self.refresh(self.a)
        self.assertEqual(self.unread(self.a, "NEW", "GENBA-1"), 1)   # 自分の分は数えない
        self.assertEqual([t[:2] for t in self.texts(self.a, "NEW", "GENBA-1")],
                         [("現場", "至急でお願いします"), ("倉庫", "午後に出します")])

    def test_この版より前の発注にも書ける(self):
        """発注キーの無い発注は、共有の管理番号で結ぶ。"""
        self.write(self.a, "L1", "寸法を確認してください", "GENBA-1", oc.SIDE_FIELD)
        self.send(self.a)
        self.refresh(self.b)
        self.assertEqual(self.texts(self.b, "L1", "SOUKO"),
                         [("現場", "寸法を確認してください", True)])

    def test_取り込みをまたいでも既読は残る(self):
        self.order("NEW")
        self.write(self.a, "NEW", "よろしく", "GENBA-1", oc.SIDE_FIELD)
        self.send(self.a)
        self.refresh(self.b)
        oc.mark_read(self.b, self.local_no(self.b, "NEW"))
        data_sync.import_master(self.b, self.src)       # 総入れ替え
        self.assertEqual(self.unread(self.b, "NEW", "SOUKO"), 0)


class ClosedTests(CommentTestBase):
    def test_確認済みの発注には書けないが読める(self):
        self.write(self.a, "L1", "前のひとこと", "GENBA-1", oc.SIDE_FIELD)
        wh.confirm_order(self.b, self.local_no(self.b, "L1"))
        got = self.write(self.b, "L1", "あとから", "SOUKO", oc.SIDE_MATERIAL)
        self.assertFalse(got.ok)
        self.assertIn("確認済み", got.message)
        self.assertEqual(self.texts(self.a, "L1", "GENBA-1")[0][1], "前のひとこと")

    def test_取り消し済みの発注には書けない(self):
        wh.cancel_order(self.a, self.local_no(self.a, "L2"), terminal="GENBA-1")
        got = self.write(self.a, "L2", "あとから", "GENBA-1", oc.SIDE_FIELD)
        self.assertFalse(got.ok)
        self.assertIn("取り消し済み", got.message)

    def test_空や長すぎるものは断る(self):
        self.assertFalse(self.write(self.a, "L1", "  ", "GENBA-1", oc.SIDE_FIELD).ok)
        long = self.write(self.a, "L1", "あ" * (oc.MAX_LENGTH + 1), "GENBA-1", oc.SIDE_FIELD)
        self.assertFalse(long.ok)
        self.assertIn(str(oc.MAX_LENGTH), long.message)

    def test_送れていないコメントは取り込みで消えない(self):
        self.write(self.a, "L1", "まだ送れていない", "GENBA-1", oc.SIDE_FIELD)
        with mock.patch.object(sync_writeback, "ensure_shared_tables", lambda *a, **k: []):
            result = data_sync.import_master(self.a, self.src)
        self.assertNotIn(COMMENTS, result.imported)
        self.assertEqual(self.texts(self.a, "L1", "GENBA-1")[0][1], "まだ送れていない")


class RouteTests(CommentTestBase):
    """画面から(現場の端末で開いている)。"""

    def setUp(self) -> None:
        super().setUp()
        from app.routes import warehouse as routes
        from packaging_tool import access_control, sync_sources
        from tests import _web

        _web.bind_db(self, routes, self.a)
        for target, name, value in (
                (sync_sources, "find_material_db", lambda *a, **k: self.src),
                (data_sync, "write_back_in_background", lambda *a, **k: None),
                (wh, "this_terminal", lambda: "GENBA-1")):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = _web.make_client(
            "field", port=8718, grant=access_control.grant_of("mode:field"))
        self.headers = _web.auth()

    def test_送るときに添えたコメントが発注に付く(self):
        res = self.client.post("/api/warehouse/send", headers=self.headers, json={
            "lot_no": "SENDX", "hinmei": "品", "hatchu_code": "C1", "tani": "台",
            "atu": "1", "haba": "1000", "take": "2000", "hatchu_suu": "1",
            "comment": "急ぎです"})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.texts(self.a, "SENDX", "GENBA-1"), [("現場", "急ぎです", False)])

    def test_開くと既読になり一覧の印が消える(self):
        self.write(self.b, "L1", "倉庫から", "SOUKO", oc.SIDE_MATERIAL)
        self.send(self.b)
        self.refresh(self.a)
        state = self.client.get("/api/warehouse/orders", headers=self.headers).get_json()
        self.assertEqual(state["unread_comments"], 1)
        no = self.local_no(self.a, "L1")
        body = self.client.get(f"/api/warehouse/comments?mgr_no={no}",
                               headers=self.headers).get_json()
        self.assertEqual([c["unread"] for c in body["comments"]], [True])   # 開いた時点の印
        state = self.client.get("/api/warehouse/orders", headers=self.headers).get_json()
        self.assertEqual(state["unread_comments"], 0)

    def test_ほかの端末が先に確認していたら書けない理由を言う(self):
        stale = self.local_no(self.a, "L1")
        wh.confirm_order(self.b, self.local_no(self.b, "L1"))
        self.send(self.b)
        res = self.client.post("/api/warehouse/comment", headers=self.headers,
                               json={"mgr_no": stale, "text": "あとから"})
        self.assertEqual(res.status_code, 422)
        body = res.get_json()
        self.assertIn("確認済み", body["error"]["message"])
        self.assertIn("確認済み", body["write_why"])


if __name__ == "__main__":
    unittest.main()
