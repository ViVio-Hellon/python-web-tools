"""保存していない図を、黙って捨てない (VER2.82.0)

【何を守りたいか】
現場の声:「配置編集保存されてないよ」。

配置編集は「配置を保存」を押すまでファイルに書かない(間違えても開き
直せば戻る、という約束)。ところがタブを閉じると8秒でアプリが自動終了
し、「終了」ボタンも何も言わずに終わっていた ── **保存していない編集が
黙って消えていた。**

1. **見張りが未保存を返す。** タブを閉じるときにブラウザが聞くため
2. **「終了」は捨てる前に聞く。** 実行中の処理もあれば同じ1問で聞く
   (「はい」は force で呼び直すので、別々に聞くと2つ目を飛ばす)
3. **勝手に保存はしない。** 約束が壊れる
4. **聞いただけで作業状態を作らない。** 図のファイルを読みに行かせない
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _isolation  # noqa: E402,F401

from packaging_tool import layout_session, pallet_map_session  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ"
TOKEN = "test-token"


def _reset() -> None:
    layout_session.reset_session()
    pallet_map_session.reset_session()


class HasUnsavedTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset()
        self.addCleanup(_reset)

    def test_開いていなければ無い_作業状態も作らない(self) -> None:
        self.assertFalse(layout_session.has_unsaved())
        self.assertFalse(pallet_map_session.has_unsaved())
        self.assertIsNone(layout_session._session)
        self.assertIsNone(pallet_map_session._session)

    def test_動かしたら有る(self) -> None:
        pallet_map_session.get_session().dirty = True
        self.assertTrue(pallet_map_session.has_unsaved())
        self.assertFalse(layout_session.has_unsaved())


@unittest.skipUnless(HAS_WEB, _SKIP)
class UnsavedWebTests(unittest.TestCase):
    def setUp(self) -> None:
        from app import create_app
        from app.routes import health
        from packaging_tool import jobs, screen_lock

        _reset()
        self.addCleanup(_reset)
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        # 停止フックは入れない(入れると本当に止めに行く)。通れば 501
        original = health._shutdown_hook
        health.set_shutdown_hook(None)
        self.addCleanup(health.set_shutdown_hook, original)
        self.registry = jobs.get_registry()
        self.app = create_app("field", token=TOKEN, port=8795)
        self.app.config["TESTING"] = True
        self.app.config["READY"] = True
        self.client = self.app.test_client()

    def auth(self) -> dict:
        return {"X-Tool-Token": TOKEN}

    def health(self, **headers) -> dict:
        return self.client.get("/api/health", headers=headers).get_json()

    def shutdown(self, **body):
        return self.client.post("/api/shutdown", json=body, headers=self.auth())

    # --- 見張り -----------------------------------------------------
    def test_画面からの問い合わせに未保存を返す(self) -> None:
        from packaging_tool import screen_lock
        head = {screen_lock.HEADER: "abc"}
        self.assertEqual(self.health(**head)["unsaved"], {})
        layout_session.get_session().dirty = True
        self.assertEqual(self.health(**head)["unsaved"],
                         {"layout": layout_session.UNSAVED_LABEL})

    def test_画面以外には返さない(self) -> None:
        layout_session.get_session().dirty = True
        self.assertIsNone(self.health()["unsaved"])

    def test_保存したら消える(self) -> None:
        from packaging_tool import screen_lock
        session = pallet_map_session.get_session()
        session.dirty = True
        session.dirty = False           # save() が最後にすること
        self.assertEqual(self.health(**{screen_lock.HEADER: "abc"})["unsaved"], {})

    # --- 終了 -------------------------------------------------------
    def test_未保存があれば止めずに聞く(self) -> None:
        pallet_map_session.get_session().dirty = True
        res = self.shutdown()
        self.assertEqual(res.status_code, 409)
        body = res.get_json()
        self.assertFalse(body["stopped"])
        self.assertEqual(body["reason"], "unsaved")
        self.assertIn(pallet_map_session.UNSAVED_LABEL, body["message"])
        self.assertIn("保存せずに終了しますか", body["message"])
        # ランチャー・stop.bat は `running` だけを読んで理由を出す。空だと「(不明)」になる
        self.assertEqual(body["running"],
                         [f"保存していない変更({pallet_map_session.UNSAVED_LABEL})"])

    def test_両方の図の名前を出す(self) -> None:
        layout_session.get_session().dirty = True
        pallet_map_session.get_session().dirty = True
        message = self.shutdown().get_json()["message"]
        self.assertIn(layout_session.UNSAVED_LABEL, message)
        self.assertIn(pallet_map_session.UNSAVED_LABEL, message)

    def test_はいと答えたら止める_勝手に保存しない(self) -> None:
        session = layout_session.get_session()
        session.dirty = True
        res = self.shutdown(force=True)
        # 停止フックが無いので 501 = 未保存の断りを越えて止めに行った
        self.assertEqual(res.status_code, 501)
        self.assertTrue(session.dirty, "勝手に保存してはいけない")

    def test_未保存が無ければ今までどおり(self) -> None:
        self.assertEqual(self.shutdown().status_code, 501)

    def test_実行中の処理もあれば同じ1問で聞く(self) -> None:
        """「はい」は force で呼び直す。**処理の中断も同じ問いに入れる。**"""
        layout_session.get_session().dirty = True
        release = threading.Event()
        self.registry.start("import", "まとめて取り込み",
                            lambda _p: release.wait(3))
        try:
            body = self.shutdown().get_json()
            self.assertEqual(body["reason"], "unsaved")
            self.assertEqual(body["running"], ["まとめて取り込み",
                                               f"保存していない変更({layout_session.UNSAVED_LABEL})"])
            self.assertIn("実行中の処理", body["message"])
            self.assertIn("中断", body["message"])
        finally:
            release.set()
        for _ in range(100):
            if not self.registry.busy_labels():
                break
            threading.Event().wait(0.05)


class UnsavedScriptWiringTests(unittest.TestCase):
    """ブラウザ側の配線。**消える前に聞く・中の移動では聞かない。**"""

    JS = _ROOT / "app" / "static" / "js"

    def read(self, name: str) -> str:
        return (self.JS / name).read_text(encoding="utf-8")

    def test_閉じるときに聞く(self) -> None:
        text = self.read("unsaved.js")
        self.assertIn('addEventListener("beforeunload"', text)
        self.assertIn("preventDefault", text)

    def test_見張りの答えを入れる(self) -> None:
        self.assertIn("unsaved.fromServer(body.unsaved)", self.read("health.js"))

    def test_図の画面が描き直すたびに覚える(self) -> None:
        self.assertIn('unsaved.mark("layout"', self.read("views/layout.js"))
        self.assertIn('unsaved.mark("inventory"', self.read("views/inventory.js"))

    def test_中の移動では聞かない(self) -> None:
        """モード切替・「このタブで続ける」は編集が消えないので聞かない。"""
        app = self.read("app.js")
        before_nav = app.index("unsaved.allowLeave();\n      location.href")
        self.assertGreater(before_nav, 0)
        self.assertRegex(self.read("screen.js"),
                         r"unsaved\.allowLeave\(\);[^\n]*\n\s*location\.reload\(\)")

    def test_使うファイルは必ず読み込む(self) -> None:
        """読み込み忘れは構文として正しい(`node --check` を通る)。

        作っている途中で、図の画面2つが `unsaved is not defined` で**丸ごと動かなく
        なった**(配置編集のボタンすら効かない)。使う側は全部読み込む。
        """
        for path in self.JS.rglob("*.js"):
            text = path.read_text(encoding="utf-8")
            if path.name == "unsaved.js" or "unsaved." not in text:
                continue
            with self.subTest(path.relative_to(self.JS).as_posix()):
                self.assertRegex(
                    text, r'import \* as unsaved from "(\./|\.\./)unsaved\.js";')

    def test_終了の問いはサーバの文を出す(self) -> None:
        # 窓はアプリの中に出す(デスクトップ版でもブラウザの confirm は出ない)
        self.assertIn("confirmBox(err.message", self.read("app.js"))


if __name__ == "__main__":
    unittest.main()
