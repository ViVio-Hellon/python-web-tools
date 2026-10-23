"""マスタを直したら、開いている画面がすぐ読み直す(VER4.8.94)。

現場の声:「作業長でない端末でも送れるままですよ ── と思ったらタブ変えたら送れなくなった。
毎回いうけどマスタ編集したらリアルタイムで読み直してください」

サーバはマスタを直す・取り込むたびに世代を進め(`master_state`)、`/api/health` の見張りが画面へ
伝える。画面(資材選択・倉庫連携・ロット検索)は世代が変わったらサーバから引き直す。
マスタ管理で直した窓は、ほかのタブ・窓へもすぐ知らせる(BroadcastChannel)。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from packaging_tool import master_admin, master_state
from tests import _web
from tests.test_master_admin import MasterTestCase

JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js"


class EpochTests(MasterTestCase):
    def test_マスタ管理で直すと世代が進む(self) -> None:
        before = master_state.epoch()
        result = master_admin.add_row(
            self.conn, "PalletMaster",
            {"幅": "1150", "丈": "2650", "位置": "", "業界": "タイト", "単位": "台", "コード": "099999"},
            path=self.src)
        self.assertTrue(result.ok, result.message)
        # 取り込み直しでも進む(VER4.8.95 `touch`)ので、進んだことだけを見る
        self.assertGreater(master_state.epoch(), before)

    def test_断られたら進まない(self) -> None:
        from packaging_tool import selection_session
        selection_session.get_session(self.conn).admin = False          # 認証していない
        before = master_state.epoch()
        result = master_admin.add_row(self.conn, "PalletMaster", {"幅": "1"}, path=self.src)
        self.assertFalse(result.ok)
        self.assertEqual(master_state.epoch(), before)


class HealthTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import settings as routes
        _web.bind_db(self, routes)
        self.client = _web.make_client()
        html = self.client.get("/settings").get_data(as_text=True)
        self.screen = {**_web.auth(),
                       "X-Screen-Id": re.search(r'screenId: "([^"]+)"', html).group(1)}
        self.html = html

    def test_画面と見張りに世代が載る(self) -> None:
        self.assertIn(f"dataEpoch: {master_state.epoch()}", self.html)
        now = master_state.changed()
        health = self.client.get("/api/health", headers=self.screen).get_json()
        self.assertEqual(health["data_epoch"], now)
        # 画面以外(多重起動の判定など)には出さない
        self.assertIsNone(self.client.get("/api/health").get_json()["data_epoch"])


class WiringTests(unittest.TestCase):
    """画面の側の配線。世代が変わったら引き直す(打ちかけの入力は触らない)。"""

    def read(self, *parts: str) -> str:
        return JS.joinpath(*parts).read_text(encoding="utf-8")

    def test_見張りが伝え_マスタ管理がほかの窓へ知らせる(self) -> None:
        self.assertIn("datasync.announce(body.data_epoch)", self.read("health.js"))
        self.assertIn("datasync.tellOthers()", self.read("views", "master.js"))
        self.assertIn("BroadcastChannel", self.read("datasync.js"))

    def test_資材選択_倉庫連携_ロット検索が引き直す(self) -> None:
        selection = self.read("views", "selection.js")
        self.assertRegex(selection, r'datasync\.onChange\(async \(\) => \{\s*try \{\s*render\(await api\.get\("/api/selection/state"\)\)')
        warehouse = self.read("views", "warehouse.js")
        self.assertRegex(warehouse, r"datasync\.onChange\(\(\) => \{\s*load\(\);\s*lotPlan\.reload\(\);")
        self.assertIn("applyOrderWhy(view.order_why)", warehouse)
        self.assertIn("datasync.onChange(() => pull())", self.read("lotplans_view.js"))


if __name__ == "__main__":
    unittest.main()
