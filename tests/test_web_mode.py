"""モードの切替と、権限による登録の切り分け

【何を守っているか】
守りは2段で、**目的が違うので両方要る**。

1. 権限が無い端末には、そのモードの画面もAPIも**登録しない**(404)。
   守るのは「誰か」── 以前は「どのショートカットを押したか」でしか
   分かれておらず、同じPCの別の人が資材のポートを開けば通っていた
2. 権限があっても、いま別のモードで見ていれば**断る**(403)。
   こちらは誤操作の防止

**404 は「無い」、403 は「今はできない」。** 混ぜると、権限を足せば
直るのか、モードを切り替えれば直るのかが利用者に分からない。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _isolation  # noqa: E402

_isolation.ensure_isolated()

from packaging_tool import access_control as ac  # noqa: E402
from packaging_tool import modes  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ"
TOKEN = "test-token"

BOTH = ("mode:field", "mode:material")
FIELD_ONLY = ("mode:field",)
MATERIAL_ONLY = ("mode:material",)


@unittest.skipUnless(HAS_WEB, _SKIP)
class ModeTestCase(unittest.TestCase):
    def make(self, mode: str, *codes: str):
        from app import create_app
        app = create_app(mode, token=TOKEN, port=8791,
                         grant=ac.grant_of(*codes))
        app.config["TESTING"] = True
        app.config["READY"] = True
        return app

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}


class StartupTests(ModeTestCase):
    def test_権限があれば指定したモードで開く(self) -> None:
        self.assertEqual(self.make("material", *BOTH).config["MODE"],
                         modes.MATERIAL)

    def test_権限が無ければ現場へ落として理由を残す(self) -> None:
        """**起動そのものは失敗させない。**

        失敗させると、権限を直す画面(設定)にも辿り着けなくなる。
        """
        app = self.make("material", *FIELD_ONLY)
        self.assertEqual(app.config["MODE"], modes.FIELD)
        self.assertIn("権限", app.config["MODE_NOTE"])

    def test_旧名でも起動できる(self) -> None:
        """現場に配ってあるショートカットは `warehouse` で書かれている。"""
        self.assertEqual(self.make("warehouse", *BOTH).config["MODE"],
                         modes.MATERIAL)

    def test_知らないモードは例外(self) -> None:
        with self.assertRaises(ValueError):
            self.make("けんさ", *BOTH)


class RegistrationTests(ModeTestCase):
    def rules(self, app) -> set:
        return {r.rule for r in app.url_map.iter_rules()}

    def test_現場の権限が無ければ現場の画面は無い(self) -> None:
        rules = self.rules(self.make("material", *MATERIAL_ONLY))
        for path in ("/lot", "/selection", "/inventory", "/layout", "/log"):
            with self.subTest(path=path):
                self.assertNotIn(path, rules)

    def test_資材の権限が無ければ確認のAPIは無い(self) -> None:
        rules = self.rules(self.make("field", *FIELD_ONLY))
        self.assertNotIn("/api/warehouse/confirm", rules)

    def test_両方持っていれば両方ある(self) -> None:
        """モードを切り替えられるので、いま資材モードで見ていても
        現場のURLは要る(レールに出すかどうかはモードが決める)。"""
        rules = self.rules(self.make("material", *BOTH))
        self.assertIn("/lot", rules)
        self.assertIn("/api/warehouse/confirm", rules)

    def test_設定はどのモードでも開ける(self) -> None:
        """「なぜこのモードしか選べないのか」を確かめる場所。"""
        for codes in (FIELD_ONLY, MATERIAL_ONLY, BOTH):
            with self.subTest(codes=codes):
                self.assertIn("/settings", self.rules(self.make("field", *codes)))


class SwitchTests(ModeTestCase):
    def test_権限のあるモードへ切り替えられる(self) -> None:
        app = self.make("field", *BOTH)
        body = app.test_client().post("/api/mode", json={"mode": "material"},
                                      headers=self.auth()).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(app.config["MODE"], modes.MATERIAL)

    def test_行き先も返す(self) -> None:
        """切り替えると出す画面が変わる。いまの画面が切替先に
        無いことがある(資材モードに資材選択は無い)。"""
        app = self.make("field", *BOTH)
        body = app.test_client().post("/api/mode", json={"mode": "material"},
                                      headers=self.auth()).get_json()
        self.assertEqual(body["next"], "/warehouse")

    def test_権限の無いモードへは切り替えられない(self) -> None:
        """画面には出さないが、要求を直接投げられても通らないようにする
        (設計書 §1 の6番)。"""
        app = self.make("field", *FIELD_ONLY)
        res = app.test_client().post("/api/mode", json={"mode": "material"},
                                     headers=self.auth())
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.get_json()["error"]["code"], "not_allowed")
        self.assertEqual(app.config["MODE"], modes.FIELD)

    def test_知らないモードは400(self) -> None:
        """入力の形が違う。**サーバの状態は動いていない。**"""
        app = self.make("field", *BOTH)
        res = app.test_client().post("/api/mode", json={"mode": "けんさ"},
                                     headers=self.auth())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(app.config["MODE"], modes.FIELD)

    def test_同じモードなら何もしない(self) -> None:
        app = self.make("field", *BOTH)
        body = app.test_client().post("/api/mode", json={"mode": "field"},
                                      headers=self.auth()).get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["next"], "")

    def test_旧名でも切り替えられる(self) -> None:
        app = self.make("field", *BOTH)
        app.test_client().post("/api/mode", json={"mode": "warehouse"},
                               headers=self.auth())
        self.assertEqual(app.config["MODE"], modes.MATERIAL)

    def test_トークンが要る(self) -> None:
        app = self.make("field", *BOTH)
        self.assertEqual(
            app.test_client().post("/api/mode", json={"mode": "material"}).status_code,
            401)


class RibbonTests(ModeTestCase):
    def html(self, app) -> str:
        return app.test_client().get("/settings").get_data(as_text=True)

    def test_持っているモードが2つなら切替を出す(self) -> None:
        html = self.html(self.make("field", *BOTH))
        self.assertIn('class="modeswitch"', html)
        self.assertIn('data-mode="material"', html)

    def test_1つしか無ければ切替を出さない(self) -> None:
        """押せない選択肢を並べても、できないことが増えたようにしか
        見えない。"""
        html = self.html(self.make("field", *FIELD_ONLY))
        self.assertNotIn('class="modeswitch"', html)
        self.assertIn("現場モード", html)

    def test_いまのモードが分かる(self) -> None:
        """どれが今かの判断もサーバが持つ(画面は写すだけ)。"""
        import re
        html = self.html(self.make("material", *BOTH))
        pressed = {
            m.group(1) for m in re.finditer(
                r'data-mode="(\w+)"[^>]*?aria-pressed="true"', html, re.S)}
        self.assertEqual(pressed, {"material"})

    def test_タブのアイコンがモードで変わる(self) -> None:
        field = self.html(self.make("field", *BOTH))
        material = self.html(self.make("material", *BOTH))
        self.assertNotEqual(
            field[field.index('rel="icon"'):field.index('rel="icon"') + 400],
            material[material.index('rel="icon"'):material.index('rel="icon"') + 400])


class SettingsSectionTests(ModeTestCase):
    def test_設定画面に権限の節が出る(self) -> None:
        """**なぜこのモードしか選べないのか**を利用者が自分で確かめられる。"""
        html = self.html_of(self.make("field", *FIELD_ONLY))
        self.assertIn("この端末の権限", html)
        self.assertIn("使えるモード", html)

    def html_of(self, app) -> str:
        return app.test_client().get("/settings").get_data(as_text=True)


if __name__ == "__main__":
    unittest.main()
