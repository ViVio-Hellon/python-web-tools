"""パレット適合設定の画面の口(`/pallet-fit`・`/api/pallet-fit/*`)。

中身の計算は `tests/test_fit_setting.py` が確かめる。ここは
- 画面を開いても**作業中のタブを取り上げない**(「いま使っているタブ」を名乗らない)
- 探す → 見本 → 反映 → 元に戻す が口を通して動く
- 断りの種別が HTTP に写る(許されていない 403・先を越された 409・入力の誤り 400)
- 見本づくりは書く錠を通さない(読むのに時間がかかっても、ほかの書く操作を待たせない)
"""
from __future__ import annotations

from unittest import mock

from packaging_tool import access_control, fit_setting, screen_lock, selection_session
from tests import _web
from tests.test_fit_setting import FitSettingTestCase


class PalletFitWebTests(FitSettingTestCase):
    def setUp(self) -> None:
        super().setUp()
        from app.routes import pallet_fit as routes
        self.routes = routes
        _web.bind_db(self, routes, self.conn)
        self.client = _web.make_client("material", grant=access_control.grant_of("mode:field", "mode:material"))
        self.auth = _web.auth()
        patcher = mock.patch.object(fit_setting, "refresh_in_background", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pallet(1035, 3150, code="P-1")
        self.pallet(1035, 2000, code="P-2")
        self.sync()

    def post(self, path: str, body: dict):
        return self.client.post(path, json=body, headers=self.auth)

    def fit_body(self) -> dict:
        found = self.post("/api/pallet-fit/find", {"width": 1035, "length": 3150}).get_json()
        return {"kind": "fit", "width": 1035, "length": 3150, "group": found["groups"][0]["key"],
                "w_min": 1000, "w_max": 1020, "l_min": 2641, "l_max": 3140}

    # ------------------------------------------------------------------
    def test_画面を開いても作業中のタブを取り上げない(self) -> None:
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        mine = screen_lock.claim("作業中のタブ")
        res = self.client.get("/pallet-fit")
        self.assertEqual(res.status_code, 200)
        page = res.get_data(as_text=True)
        self.assertIn("パレット適合設定", page)
        self.assertIn('screenId: ""', page)
        self.assertEqual(screen_lock.active(), mine)

    def test_資材選択の一覧から開いた寸法を渡す(self) -> None:
        page = self.client.get("/pallet-fit?w=1035&l=3150").get_data(as_text=True)
        self.assertIn('startWidth: "1035"', page)

    def test_状態は直せるかと書く先を言う(self) -> None:
        body = self.client.get("/api/pallet-fit/state", headers=self.auth).get_json()
        self.assertTrue(body["admin"])
        self.assertIn(str(self.threshold), body["where"]["threshold"])
        selection_session.get_session(self.conn).admin = False
        body = self.client.get("/api/pallet-fit/state", headers=self.auth).get_json()
        self.assertFalse(body["can_edit"])
        self.assertIn("パスワード", body["why"])

    def test_探して見本を作って反映して元に戻す(self) -> None:
        plan = self.post("/api/pallet-fit/plan", self.fit_body()).get_json()["plan"]
        self.assertEqual({a["code"] for a in plan["affected"]}, {"P-1", "P-2"})
        self.assertTrue(plan["can_apply"])
        res = self.post("/api/pallet-fit/apply", {"plan_id": plan["id"]})
        self.assertEqual(res.status_code, 200, res.get_json())
        body = res.get_json()
        self.assertEqual(body["history"][0]["種類"], "適合")
        self.assertEqual(self.local("P-1")["巾適合min"], 1000)
        res = self.post("/api/pallet-fit/undo", {"log_no": body["log_no"]})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(self.local("P-1")["巾適合min"], 991)

    def test_製品で確かめる(self) -> None:
        plan = self.post("/api/pallet-fit/plan", self.fit_body()).get_json()["plan"]
        got = self.post("/api/pallet-fit/check", {"plan_id": plan["id"], "width": 990, "length": 3000}).get_json()
        self.assertEqual((got["before"], got["after"]), (1, 0))

    def test_入力の誤りは400(self) -> None:
        res = self.post("/api/pallet-fit/plan", {**self.fit_body(), "w_min": 1030, "w_max": 1000})
        self.assertEqual(res.status_code, 400)
        self.assertIn("最小", res.get_json()["error"]["message"])

    def test_認証が無ければ403で書かない(self) -> None:
        plan = self.post("/api/pallet-fit/plan", self.fit_body()).get_json()["plan"]
        selection_session.get_session(self.conn).admin = False
        res = self.post("/api/pallet-fit/apply", {"plan_id": plan["id"]})
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.bands(lo=1001, hi=1050), [(1001, 1050, 991, 1040)])

    def test_古い見本は409(self) -> None:
        res = self.post("/api/pallet-fit/apply", {"plan_id": "ない見本"})
        self.assertEqual(res.status_code, 409)

    def test_見本づくりは書く錠を通さない(self) -> None:
        import app as app_module
        for path in self.routes.READ_ONLY_PATHS:
            with self.subTest(path=path):
                self.assertTrue(path.startswith(app_module._NO_LOCK_PREFIXES))
        self.assertFalse("/api/pallet-fit/apply".startswith(app_module._NO_LOCK_PREFIXES))


class MasterAdminLockTests(FitSettingTestCase):
    """マスタ管理では、適合範囲を決める表は見るだけ。記号表は桁数・脚数・備考だけ直せる。"""

    def test_帯の表は見るだけでパレット適合設定へ案内する(self) -> None:
        from packaging_tool import master_admin
        page = master_admin.page(self.threshold, "PalletHabaThreshold")
        self.assertFalse(page.editable)
        self.assertIn("パレット適合設定", page.why)
        result = master_admin.save_row(self.conn, "PalletHabaThreshold", 1, {"適合最小値": "1"},
                                       path=self.threshold)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, master_admin.REFUSE_NOT_EDITABLE)

    def test_脚数と桁数の帯は今までどおり直せる(self) -> None:
        from packaging_tool import master_admin
        self.assertTrue(master_admin.page(self.threshold, "PalletAshiThreshold").editable)

    def test_記号表は桁数脚数だけ直せて_行は足せない(self) -> None:
        from packaging_tool import master_admin
        page = master_admin.page(self.threshold, "PalletSymbolMaster")
        self.assertTrue(page.editable)
        self.assertFalse(page.to_dict()["can_add_row"])
        ok = master_admin.save_row(self.conn, "PalletSymbolMaster", 1, {"桁数": "5"}, path=self.threshold)
        self.assertTrue(ok.ok, ok.message)
        refused = master_admin.save_row(self.conn, "PalletSymbolMaster", 1, {"巾適合最小値": "1"},
                                        path=self.threshold)
        self.assertFalse(refused.ok)
        self.assertIn("パレット適合設定", refused.message)
        added = master_admin.add_row(self.conn, "PalletSymbolMaster", {"記号": "Q1"}, path=self.threshold)
        self.assertFalse(added.ok)
        gone = master_admin.delete_row(self.conn, "PalletSymbolMaster", 1, path=self.threshold)
        self.assertFalse(gone.ok)
