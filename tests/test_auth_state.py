"""管理者認証が変わったことを、開いている画面へすぐ伝える(VER4.1.0)

現場の声:「パスワード認証してもマスタ編集がすぐできない。タブを切り替えて
戻ってもまだ無い。認証系はすぐにどこにでも反映させないと分からない」。

- 認証の状態が変わると世代が進む(同じ状態のままなら進まない)
- 認証の応答・`/api/health`(画面からの問い合わせ)・画面の `window.APP` に世代が載る
- 認証したあとのマスタ管理は「直せる」で返る
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import _isolation  # noqa: E402,F401
from packaging_tool import auth_state, config, selection_session  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False


class EpochTests(unittest.TestCase):
    def setUp(self) -> None:
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        from tests import _web
        self.conn = _web.memory_db()
        self.addCleanup(self.conn.close)

    def test_変わったときだけ世代が進む(self) -> None:
        session = selection_session.get_session(self.conn)
        before = auth_state.epoch()
        self.assertFalse(session.authenticate("ちがう").ok)      # 未認証のまま
        self.assertEqual(auth_state.epoch(), before)
        self.assertTrue(session.authenticate(config.ADMIN_PASSWORD).ok)
        self.assertEqual(auth_state.epoch(), before + 1)
        self.assertTrue(auth_state.admin_now())
        session.authenticate(config.ADMIN_PASSWORD)                # 認証済みのまま
        self.assertEqual(auth_state.epoch(), before + 1)
        session.authenticate("ちがう")                             # 外れた
        self.assertEqual(auth_state.epoch(), before + 2)
        self.assertFalse(auth_state.admin_now())


@unittest.skipUnless(HAS_WEB, "flask が無い")
class WebTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.routes import settings as routes
        from tests import _web
        selection_session.reset_session()
        self.addCleanup(selection_session.reset_session)
        self._web = _web
        _web.bind_db(self, routes)
        self.client = _web.make_client()
        self.screen = {**_web.auth(), "X-Screen-Id": "s1"}

    def use_page_screen(self) -> str:
        """画面を開き、そのときに振られたタブの番号で問い合わせる(最後に開いたタブだけが通る)。"""
        import re
        html = self.client.get("/settings").get_data(as_text=True)
        self.screen["X-Screen-Id"] = re.search(r'screenId: "([^"]+)"', html).group(1)
        return html

    def test_認証の応答と見張りと画面に世代が載る(self) -> None:
        before = auth_state.epoch()
        html = self.use_page_screen()
        self.assertIn(f"authEpoch: {before}", html)
        res = self.client.post("/api/settings/admin-auth",
                               json={"password": config.ADMIN_PASSWORD}, headers=self.screen)
        self.assertEqual(res.get_json()["auth"], {"admin": True, "epoch": before + 1})
        health = self.client.get("/api/health", headers=self.screen).get_json()
        self.assertEqual(health["auth"], {"admin": True, "epoch": before + 1})
        # 画面以外(多重起動の判定など)には出さない
        self.assertIsNone(self.client.get("/api/health").get_json()["auth"])

    def test_認証したらマスタ管理は直せるで返る(self) -> None:
        from packaging_tool import access_control
        from packaging_tool.presenters import master as presenter
        from unittest import mock
        with mock.patch.object(access_control, "resolve_for_screen",
                               return_value=access_control.grant_of("mode:material")):
            conn = self._web.memory_db()
            self.addCleanup(conn.close)
            before = presenter.frame(conn)
            self.use_page_screen()
            res = self.client.post("/api/settings/admin-auth",
                                   json={"password": config.ADMIN_PASSWORD}, headers=self.screen)
            self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
            after = presenter.frame(conn)
            fast = self.client.get("/api/master/access?table=BoardMaster",
                                   headers=self.screen).get_json()
        self.assertNotEqual((before.can_edit, before.edit_why), (after.can_edit, after.edit_why))
        # 速い口も同じ答え(マスタ管理が認証直後にまず引く)
        self.assertEqual((fast["can_edit"], fast["edit_why"]), (after.can_edit, after.edit_why))


class ScreenWiringTests(unittest.TestCase):
    """画面の側の配線(JS)。動かして確かめるのはブラウザの確認で行う。"""

    ROOT = Path(__file__).resolve().parent.parent / "app" / "static" / "js"

    def test_見張りと認証の応答から伝え_マスタ管理が描き直す(self) -> None:
        health = (self.ROOT / "health.js").read_text(encoding="utf-8")
        settings = (self.ROOT / "views" / "settings.js").read_text(encoding="utf-8")
        master = (self.ROOT / "views" / "master.js").read_text(encoding="utf-8")
        self.assertIn("authsync.announce(body.auth)", health)
        self.assertIn("authsync.announce(res.auth)", settings)
        self.assertIn("master.authChanged()", settings)
        self.assertIn("export async function authChanged()", master)
        # まず「直せる/直せない」だけを引き直す(共有を読まないので一瞬)
        self.assertIn("/api/master/access?table=", master)
        # 1行の窓を閉じたら印を外す(外さないと、以後の読み直しが全部止まっていた)
        self.assertIn('el.mEdit.addEventListener("close"', master)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()
