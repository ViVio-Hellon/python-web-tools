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


class SettingsTests(unittest.TestCase):
    """マスタ反映もれ #8・#9: 設定画面(拠点の選択肢・いまの状態)も引き直す(VER4.8.102)。"""

    def setUp(self) -> None:
        from app.routes import settings as routes
        self.conn = _web.bind_db(self, routes)
        self.client = _web.make_client()

    def test_マスタで足した拠点が状態の選択肢に出る(self) -> None:
        before = self.client.get("/api/settings/state", headers=_web.auth()).get_json()["positions"]
        self.assertNotIn("大板", before)
        self.conn.execute("INSERT INTO BoardMaster (ボード幅, ボード丈, ボードタイプ, データラベル)"
                          " VALUES (0, 0, '拠点', '拠点大板')")
        self.conn.commit()
        after = self.client.get("/api/settings/state", headers=_web.auth()).get_json()["positions"]
        self.assertIn("大板", after)

    def test_画面が選択肢を描き直し_マスタの変化で引き直す(self) -> None:
        js = (JS / "views" / "settings.js").read_text(encoding="utf-8")
        self.assertIn("renderPositions(state.positions || [], state.position)", js)
        self.assertIn("datasync.onChange(() => refreshStatus())", js)
        # 同じ設定画面のマスタ管理で直したときも、その場で知らせる
        self.assertIn("datasync.changedHere()", (JS / "views" / "master.js").read_text(encoding="utf-8"))


class WiringTests(unittest.TestCase):
    """画面の側の配線。世代が変わったら引き直す(打ちかけの入力は触らない)。"""

    def read(self, *parts: str) -> str:
        return JS.joinpath(*parts).read_text(encoding="utf-8")

    def test_見張りが伝え_マスタ管理がほかの窓へ知らせる(self) -> None:
        self.assertIn("datasync.announce(body.data_epoch)", self.read("health.js"))
        self.assertIn("datasync.changedHere()", self.read("views", "master.js"))
        self.assertIn("BroadcastChannel", self.read("datasync.js"))

    def test_資材選択_倉庫連携_ロット検索が引き直す(self) -> None:
        selection = self.read("views", "selection.js")
        self.assertRegex(selection, r'datasync\.onChange\(async \(\) => \{\s*try \{\s*render\(await api\.get\("/api/selection/state"\)\)')
        warehouse = self.read("views", "warehouse.js")
        self.assertRegex(warehouse, r"datasync\.onChange\(\(\) => \{\s*load\(\);\s*lotPlan\.reload\(\);")
        self.assertIn("applyOrderWhy(view.order_why)", warehouse)
        self.assertIn("datasync.onChange(() => pull())", self.read("lotplans_view.js"))

    def test_マスタ管理もほかの窓の直しで読み直す(self) -> None:
        """マスタ反映もれ #12(VER4.8.105): 別の窓で開いているマスタ管理も、開いている表と
        「直せる/直せない」を読み直す。1行の窓を開いている間は閉じてから。自分で書いた直後は読み直さない。"""
        master = self.read("views", "master.js")
        self.assertRegex(master, r"datasync\.onChange\(\(\) => \{\s*if \(writingHere \|\| !loaded\) return;"
                                 r"\s*if \(dialogOpen\(\)\) \{\s*masterPending = true;")
        self.assertRegex(master, r"writingHere = true;\s*try \{\s*datasync\.changedHere\(\);")
        self.assertIn("if (masterPending && !dialogOpen()) {", master)


if __name__ == "__main__":
    unittest.main()


class RailAndRibbonTests(unittest.TestCase):
    """マスタ反映もれ #13(VER4.8.106): レールの「予N」と帯を、どの画面でも合わせる。

    「予N」はロット検索の画面でしか変わらず、帯は使えるモードが変わったときしか描き直さなかった。
    """

    def setUp(self) -> None:
        from app.routes import settings as routes
        _web.bind_db(self, routes)
        self.client = _web.make_client()
        html = self.client.get("/settings").get_data(as_text=True)
        self.screen = {**_web.auth(),
                       "X-Screen-Id": re.search(r'screenId: "([^"]+)"', html).group(1)}

    def test_心拍で予定ロットを数え_共有は背景で見に行く(self) -> None:
        from unittest import mock
        from packaging_tool import lot_plans
        with mock.patch.object(lot_plans, "unseen_count", return_value=3), \
             mock.patch.object(lot_plans, "refresh_in_background") as background:
            beat = self.client.get("/api/health?beat=1", headers=self.screen).get_json()
            plain = self.client.get("/api/health", headers=self.screen).get_json()
        self.assertEqual(beat["lot_plans_unseen"], 3)
        background.assert_called_once()
        self.assertIsNone(plain["lot_plans_unseen"])                 # 心拍のときだけ数える
        self.assertIsNone(self.client.get("/api/health?beat=1").get_json()["lot_plans_unseen"])

    def test_背景の取り込みは間を空け_重ねない(self) -> None:
        from unittest import mock
        from packaging_tool import lot_plans
        self.addCleanup(setattr, lot_plans, "_background_at", lot_plans._background_at)
        lot_plans._background_at = float("-inf")
        with mock.patch.object(lot_plans.threading, "Thread") as thread:
            self.assertTrue(lot_plans.refresh_in_background(now=1000.0))
            self.assertFalse(lot_plans.refresh_in_background(now=1005.0))       # 15秒たっていない
            with lot_plans._refresh_lock:                                       # 取り込み直しの最中
                self.assertFalse(lot_plans.refresh_in_background(now=2000.0))
                self.assertFalse(lot_plans.refresh_if_changed(None))
        self.assertEqual(thread.call_count, 1)

    def test_画面が印と帯を描き直す(self) -> None:
        health = (JS / "health.js").read_text(encoding="utf-8")
        self.assertIn("nav.planBadge(body.lot_plans_unseen)", health)
        self.assertIn("datasync.always(() => { nav.refreshShell(); });", health)
        nav = (JS / "nav.js").read_text(encoding="utf-8")
        self.assertIn("export function planBadge(count)", nav)
        self.assertIn("if (shellBusy) return shellBusy;", nav)      # 同時に来た描き直しは1回にまとめる
        self.assertIn("nav.planBadge(unseen)", (JS / "lotplans_view.js").read_text(encoding="utf-8"))
