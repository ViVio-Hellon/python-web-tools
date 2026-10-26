"""exe起動・vbs起動を混ぜたときの直し(VER4.8.113)。通しの点検は `scripts/mixed_launch_check.py`。

現場の声:「倉庫モードはexe起動、現場はvbs起動をしてた場合や、違うラインでvbs起動、exe起動していた場合でのテスト」。
通しで回して見つかった2つ:
- 初めて起動した(手元にアクセス権限がまだ無い)倉庫の端末が、取り込んだあとも現場モードのままだった
- ラインで押した作業済みが、作業長の倉庫連携の「作業予定ライン」に出なかった(欄を引き直していなかった)
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import start_app  # noqa: E402
from packaging_tool import access_control as ac  # noqa: E402
from packaging_tool import config, master_state, modes  # noqa: E402
from tests import _web  # noqa: E402


def grant(*codes: str, master: bool = True) -> ac.Grant:
    found = ac.grant_of(*codes)
    found.has_master = master
    return found


class FollowImportedGrantTests(unittest.TestCase):
    def srv(self, mode: str, *, auto: bool, note_mode: str = "") -> SimpleNamespace:
        app = SimpleNamespace(config={"MODE": mode, "MODE_NOTE": "x" if note_mode else "",
                                      "NOTE_MODE": note_mode})
        return SimpleNamespace(app=app, auto_mode=auto)

    def follow(self, srv, got: ac.Grant):
        with mock.patch.object(ac, "startup_grant", return_value=got):
            return start_app._follow_imported_grant(srv)

    def test_既定の起動は取り込んだ権限の強いほうへ(self) -> None:
        srv = self.srv(modes.FIELD, auto=True)                  # 取り込む前は権限が無く現場で開いた
        self.assertEqual(self.follow(srv, grant("mode:material")), modes.MATERIAL)
        self.assertEqual(srv.app.config["MODE"], modes.MATERIAL)

    def test_指定して落としたモードは権限があれば戻す(self) -> None:
        srv = self.srv(modes.FIELD, auto=False, note_mode=modes.MATERIAL)
        self.assertEqual(self.follow(srv, grant("mode:field", "mode:material")), modes.MATERIAL)
        self.assertEqual(srv.app.config["MODE_NOTE"], "")

    def test_指定して開いたモードは変えない(self) -> None:
        srv = self.srv(modes.FIELD, auto=False)                 # vbs を --mode field で開いた
        self.assertIsNone(self.follow(srv, grant("mode:field", "mode:material")))
        self.assertEqual(srv.app.config["MODE"], modes.FIELD)

    def test_アクセス権限がまだ無ければ変えない(self) -> None:
        srv = self.srv(modes.FIELD, auto=True)
        self.assertIsNone(self.follow(srv, grant("mode:material", master=False)))

    def test_取り込みを待ちきれなかったときは決め直さない(self) -> None:
        srv = mock.Mock()
        job = SimpleNamespace(is_running=True)
        with mock.patch.object(start_app, "IMPORT_WAIT_LIMIT_SEC", 0), \
             mock.patch.object(start_app, "_follow_imported_grant") as follow:
            start_app._ready_when_imported(srv, job)
        follow.assert_not_called()
        srv.mark_ready.assert_called_once_with(True)

    def test_起動の入口が既定かどうかを覚える(self) -> None:
        src = (_ROOT / "start_app.py").read_text(encoding="utf-8")
        self.assertEqual(src.count('auto = mode in ("", AUTO)'), 2)     # vbs(start)と exe(start_bridge)
        self.assertEqual(src.count("srv.auto_mode = auto"), 2)


class PlansRevTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import settings as routes
        _web.bind_db(self, routes)
        self.client = _web.make_client()
        html = self.client.get("/settings").get_data(as_text=True)
        self.screen = {**_web.auth(), "X-Screen-Id": re.search(r'screenId: "([^"]+)"', html).group(1)}

    def test_見張りが予定ロットの変わった回数を返す(self) -> None:
        before = self.client.get("/api/health", headers=self.screen).get_json()["lot_plans_rev"]
        master_state.touch([config.TBL_LOT_PLAN])
        after = self.client.get("/api/health", headers=self.screen).get_json()["lot_plans_rev"]
        self.assertEqual(after, before + 1)
        self.assertIsNone(self.client.get("/api/health").get_json()["lot_plans_rev"])   # 画面からだけ

    def test_取り込み直したら回数が進む_倉庫連携の欄が引き直す(self) -> None:
        src = (_ROOT / "packaging_tool" / "lot_plans.py").read_text(encoding="utf-8")
        self.assertIn("master_state.touch([TABLE])", src)
        health = (_ROOT / "app/static/js/health.js").read_text(encoding="utf-8")
        self.assertIn('window.dispatchEvent(new CustomEvent("tool:plans-changed"))', health)
        card = (_ROOT / "app/static/js/lotplan_send.js").read_text(encoding="utf-8")
        self.assertIn('window.addEventListener("tool:plans-changed", onPlans);', card)
        self.assertIn('nav.onLeave(() => window.removeEventListener("tool:plans-changed", onPlans));', card)


if __name__ == "__main__":
    unittest.main()
