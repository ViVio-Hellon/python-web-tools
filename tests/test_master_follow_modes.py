"""マスタ反映もれ #4〜#6: 開いているロットのモードを、条件の表が変わったら判定し直す(VER4.8.98〜)。

現場の声:「マスタを変更後にその内容が反映されていない動作描写をするのはおかしい」。
プロテック・保護材(上下共用・アングル)・1P1185 はロットを開いたときだけ判定していたので、
条件の表を直しても開き直すまで古い判定のまま動いていた。
"""
from __future__ import annotations

from packaging_tool import master_state
from tests.test_protec_rules import TABLE as PROTEC_TABLE, add_rule
from tests.test_web_selection import SelectionWebTestCase, insert_board


class _LotCase(SelectionWebTestCase):
    spec = "1P0001"

    def setUp(self) -> None:
        super().setUp()
        from app.routes import lot as lot_routes
        from tests.test_lot_service import insert_hiki, insert_lot, insert_odr
        original = lot_routes.get_db
        lot_routes.get_db = lambda: self.conn
        self.addCleanup(lambda: setattr(lot_routes, "get_db", original))
        insert_board(self.conn, width=900, length=1800, board_type="ハードボード")
        insert_board(self.conn, width=900, length=1800, board_type="プロテックボード")
        insert_lot(self.conn)                                       # ｵｰﾀﾞｰ厚 3.2
        insert_hiki(self.conn, order_no="A100", qty=22.0, no="00000001")
        insert_odr(self.conn, order_no="A100", 包装仕様NO=self.spec)
        self.client.get("/api/lot/1234567", headers=self.auth())
        self.get()                                                  # いまの回数を覚えさせる


class ProtecTests(_LotCase):
    """#4 プロテック条件。"""

    def test_条件を足したら開いているロットがプロテックになる_ボードは外す(self) -> None:
        session = self.session()
        self.assertFalse(session.presenter.protec.is_protec)
        session.selected.lower.append(object())                     # 前の判定で選んだボード
        add_rule(self.conn, self.spec)
        master_state.touch([PROTEC_TABLE])
        state = self.get()
        self.assertTrue(session.presenter.protec.is_protec)
        self.assertEqual(session.board_type, "プロテックボード")
        self.assertEqual(session.selected.lower, [])
        self.assertIn("プロテックになりました", state["master_note"])
        self.assertIn("選定し直してください", state["master_note"])

    def test_判定が変わらなければ何もしない(self) -> None:
        session = self.session()
        session.selected.lower.append(object())
        add_rule(self.conn, "1P9999")                                # ほかの包装仕様NO
        master_state.touch([PROTEC_TABLE])
        from packaging_tool import selection_session
        selection_session.get_session(self.conn)                    # 画面を出さずにセッションに触れる
        self.assertEqual(len(session.selected.lower), 1)
        self.assertEqual(session.master_note, "")

    def test_同じロットを開き直して判定が変わったらボードを外す(self) -> None:
        session = self.session()
        session.selected.lower.append(object())
        add_rule(self.conn, self.spec)                               # 合図なしで直した(ほかの端末の取り込み前など)
        self.client.get("/api/lot/1234567", headers=self.auth())
        self.assertTrue(session.presenter.protec.is_protec)
        self.assertEqual(session.selected.lower, [])


class HosozaiTests(_LotCase):
    """#5 梱包保護材(使用保護材 → アングル要否・上下共用・蓋)。"""

    def add(self, material: str) -> None:
        self.conn.execute("INSERT INTO 梱包保護材 (包装仕様書, 材質, 調質, 用途コード, 使用保護材)"
                          " VALUES (?,?,?,?,?)", (self.spec, "A5052", "H32", "", material))
        self.conn.commit()

    def test_保護材を足したら開いているロットが上下共用になる_ボードは外す(self) -> None:
        session = self.session()
        self.assertFalse(session.presenter.is_shared_board_mode)
        session.selected.lower.append(object())
        self.add("上蓋")
        master_state.touch(["梱包保護材"])
        state = self.get()
        self.assertEqual(session.presenter.last_hosozai, "上蓋")
        self.assertTrue(session.presenter.is_shared_board_mode)
        self.assertTrue(session.presenter.lid_mode)                  # 蓋を選べる
        self.assertEqual(session.selected.lower, [])
        self.assertIn("なし → 上蓋", state["master_note"])
        self.assertIn("選定し直してください", state["master_note"])

    def test_変わらなければ何もしない(self) -> None:
        master_state.touch(["梱包保護材"])
        from packaging_tool import selection_session
        session = selection_session.get_session(self.conn)
        self.assertEqual(session.master_note, "")
