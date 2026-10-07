"""操作説明書(`/manual`)と、画面の色(ライト / ダーク)の切り替え。"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import config, user_settings  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False


@unittest.skipUnless(HAS_WEB, "Flask が無い")
class ManualAndThemeTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests import _web
        saved = user_settings.get(user_settings.KEY_THEME)
        self.addCleanup(user_settings.save, user_settings.KEY_THEME, saved or "")
        user_settings.set_theme("")
        self.client = _web.make_client("field", port=8713)
        self.auth = _web.auth()

    def page(self, path: str = "/lot") -> str:
        res = self.client.get(path, headers=self.auth)
        self.assertEqual(res.status_code, 200, path)
        return res.get_data(as_text=True)

    def test_帯に説明書と色の切り替えがある(self) -> None:
        html = self.page()
        self.assertIn('id="openManual"', html)
        self.assertIn('id="themeToggle"', html)

    def test_選んだ色は最初の描画から付いていて端末に残る(self) -> None:
        self.assertIn('<html lang="ja">', self.page())         # 選んでいなければ OS に合わせる
        res = self.client.post("/api/theme", json={"theme": "dark"}, headers=self.auth)
        self.assertEqual(res.get_json(), {"theme": "dark"})
        self.assertIn('<html lang="ja" data-theme="dark">', self.page())
        self.assertIn('<html lang="ja" data-theme="dark">', self.page("/manual"))
        # 知らない値は「OS に合わせる」
        res = self.client.post("/api/theme", json={"theme": "pink"}, headers=self.auth)
        self.assertEqual(res.get_json(), {"theme": ""})
        self.assertIn('<html lang="ja">', self.page())

    def test_説明書の写真はすべてある(self) -> None:
        html = self.page("/manual")
        self.assertIn("操作説明書", html)
        images = re.findall(r'src="/static/manual/img/([^"?]+)', html)
        self.assertGreaterEqual(len(images), 15, images)
        for name in images:
            with self.subTest(name=name):
                self.assertTrue((_ROOT / "app/static/manual/img" / name).is_file())
                res = self.client.get(f"/static/manual/img/{name}")
                self.assertEqual(res.status_code, 200)
                res.close()

    def test_目次の行き先がすべてある(self) -> None:
        html = self.page("/manual")
        targets = re.findall(r'<a href="#([\w-]+)">', html)
        for target in targets:
            with self.subTest(target=target):
                self.assertIn(f'id="{target}"', html)

    def test_資材モードでも開ける(self) -> None:
        from packaging_tool import access_control
        from tests import _web
        client = _web.make_client("material", port=8723,
                                  grant=access_control.grant_of("mode:material"))
        res = client.get("/manual")
        self.assertEqual(res.status_code, 200)

    def test_説明書は別の窓で開く(self) -> None:
        js = (_ROOT / "app/static/js/app.js").read_text("utf-8")
        self.assertIn('desktop.openWindow("/manual", "操作説明書")', js)


if __name__ == "__main__":
    unittest.main()
