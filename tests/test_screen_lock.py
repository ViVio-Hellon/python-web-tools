"""同じアプリを2枚開いたとき、操作できるのは1枚だけ

【何が起きていたか】
ブラウザは同じアドレスを何枚でも開けます。ところがこのツールは
**作業状態をプロセスに1つ**しか持っていません(`work_context` /
`selection_session`)── 1台のPCを1人が使う前提だからです。

    タブA: ロット 1234567 を引く
    タブB: ロット 9999999 を引く      ← 状態が入れ替わる
    タブA: そのまま「倉庫へ送る」      ← **Bのロットで送られる**

タブAには 1234567 が出たままなので、押した人は気づけません。発注の
取り消しには人手が要るので、静かに間違うたぐいの中でも重いほうです。

【どう塞いだか】
`packaging_tool/screen_lock.py`。画面を**開いた**ものが操作できる
画面になり、古いタブからの要求は 409 で断ります。断られたことは
押す前に分かるようにしてあります(`/api/health` の `screen_ok`)。

**番号を送ってこない相手は素通しします。** `curl`・試験、そして
ヘッダを付けられない読み込み(帳票の `<iframe>`)まで巻き込む理由は
ありません ── 守りたいのはタブどうしの取り合いです。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import _isolation  # noqa: E402

_isolation.ensure_isolated()

from tests import _web  # noqa: E402

from packaging_tool import screen_lock  # noqa: E402

try:
    import flask  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask が入っていないためスキップ"

# 画面に埋め込んだ番号(`base.html` の `window.APP.screenId`)
_ID_IN_HTML = re.compile(r'screenId:\s*"([0-9a-f]+)"')

# 守りの効き目を確かめる先。**業務の口ならどれでも同じ**なので、
# 手元のDBもモードも要らないものを選ぶ(見ているのは断る仕組みだけ)
GUARDED = "/api/jobs"


class LockTests(unittest.TestCase):
    """番号の付け替えだけを見る(Flask 抜き)。"""

    def setUp(self) -> None:
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)

    def test_番号は毎回ちがう(self) -> None:
        self.assertNotEqual(screen_lock.new_id(), screen_lock.new_id())

    def test_誰も開いていなければ通す(self) -> None:
        self.assertTrue(screen_lock.is_active(screen_lock.new_id()))

    def test_最後に開いた画面が使う画面(self) -> None:
        a, b = screen_lock.new_id(), screen_lock.new_id()
        screen_lock.claim(a)
        self.assertTrue(screen_lock.is_active(a))
        screen_lock.claim(b)
        self.assertFalse(screen_lock.is_active(a))
        self.assertTrue(screen_lock.is_active(b))

    def test_番号が無ければ素通し(self) -> None:
        """`curl`・試験・帳票の読み込み。取り合いの相手ではない。"""
        screen_lock.claim(screen_lock.new_id())
        self.assertTrue(screen_lock.is_active(""))

    def test_閉じれば手放す(self) -> None:
        a, b = screen_lock.new_id(), screen_lock.new_id()
        screen_lock.claim(a)
        screen_lock.claim(b)
        screen_lock.release(b)
        # 誰も使っていないので、残ったタブは開き直さずに戻れる
        self.assertTrue(screen_lock.is_active(a))

    def test_自分の番でなければ手放さない(self) -> None:
        """古いタブが閉じても、**使っているタブは取り上げられない。**

        開いたままのタブを 8 枚閉じたら操作できなくなった、では
        直しようがない。
        """
        a, b = screen_lock.new_id(), screen_lock.new_id()
        screen_lock.claim(a)
        screen_lock.claim(b)
        screen_lock.release(a)
        self.assertTrue(screen_lock.is_active(b))

    def test_番号を送ってきたら同じ番号を使い続ける(self) -> None:
        """同じタブの中で画面を移ったとき(`nav.js` の差し替え)。"""
        a = screen_lock.new_id()
        self.assertEqual(screen_lock.for_request(a), a)

    def test_送ってこなければ新しい番号(self) -> None:
        """ブラウザがアドレスを開いた ── 新しいタブか、読み込み直し。"""
        first = screen_lock.for_request("")
        second = screen_lock.for_request("")
        self.assertNotEqual(first, second)
        self.assertTrue(screen_lock.is_active(second))
        self.assertFalse(screen_lock.is_active(first))


@unittest.skipUnless(HAS_WEB, _SKIP)
class TwoTabsTests(unittest.TestCase):
    """2枚開いたところを、本物の経路で通して確かめる。"""

    def setUp(self) -> None:
        from app.routes import lot as routes
        screen_lock.reset()
        self.addCleanup(screen_lock.reset)
        _web.bind_db(self, routes)
        self.client = _web.make_client()

    # -- 道具 ------------------------------------------------------
    def open_tab(self) -> str:
        """新しいタブでアプリを開く。振られた番号を返す。"""
        res = self.client.get("/lot", headers=_web.auth())
        self.assertEqual(res.status_code, 200)
        found = _ID_IN_HTML.search(res.get_data(as_text=True))
        self.assertIsNotNone(found, "画面に番号が入っていない")
        return found.group(1)

    def call(self, screen_id: str, path: str = GUARDED):
        headers = dict(_web.auth())
        if screen_id:
            headers[screen_lock.HEADER] = screen_id
        return self.client.get(path, headers=headers)

    # -- 本題 ------------------------------------------------------
    def test_開いた画面は操作できる(self) -> None:
        self.assertEqual(self.call(self.open_tab()).status_code, 200)

    def test_あとから開いたタブが操作する画面になる(self) -> None:
        a = self.open_tab()
        b = self.open_tab()
        self.assertNotEqual(a, b)

        refused = self.call(a)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(refused.get_json()["error"]["code"],
                         screen_lock.REFUSE_TAKEN)
        self.assertEqual(self.call(b).status_code, 200)

    def test_断りの文面に続け方が書いてある(self) -> None:
        """**行き止まりにしない。** 閉じ忘れただけのこともある。"""
        a = self.open_tab()
        self.open_tab()
        message = self.call(a).get_json()["error"]["message"]
        self.assertIn("読み込み直", message)

    def test_番号を送らなければ断られない(self) -> None:
        self.open_tab()
        self.open_tab()
        self.assertEqual(self.call("").status_code, 200)

    def test_同じタブで画面を移っても番号は替わらない(self) -> None:
        """`nav.js` は中身だけ差し替えるので、番号を付けて取りに来る。

        ここで新しい番号を振ってしまうと、**1枚しか開いていないのに
        画面を移るたびに番号が変わる**ことになる。
        """
        a = self.open_tab()
        res = self.client.get("/lot", headers={**_web.auth(),
                                               screen_lock.HEADER: a})
        self.assertEqual(_ID_IN_HTML.search(res.get_data(as_text=True)).group(1), a)
        self.assertEqual(self.call(a).status_code, 200)

    def test_読み込み直せば取り戻せる(self) -> None:
        a = self.open_tab()
        self.open_tab()
        self.assertEqual(self.call(a).status_code, 409)
        # 「このタブで続ける」= 読み込み直し。番号が振り直される
        again = self.open_tab()
        self.assertEqual(self.call(again).status_code, 200)

    def test_押す前に譲ったことが分かる(self) -> None:
        """`/api/health` は素通しで、**そのタブが操作できるか**を返す。

        押したときにも断りは返るが、それだけだと押すまで分からない。
        古いタブは古いLotを出したまま待っている。
        """
        a = self.open_tab()
        ok = self.client.get("/api/health",
                             headers={screen_lock.HEADER: a}).get_json()
        self.assertTrue(ok["screen_ok"])

        b = self.open_tab()
        self.assertFalse(self.client.get(
            "/api/health", headers={screen_lock.HEADER: a}).get_json()["screen_ok"])
        self.assertTrue(self.client.get(
            "/api/health", headers={screen_lock.HEADER: b}).get_json()["screen_ok"])

    def test_心拍は断らない(self) -> None:
        """断ると、**開いているタブが死んだ扱いになって自動終了が誤る。**"""
        a = self.open_tab()
        self.open_tab()
        self.assertEqual(
            self.client.post("/api/alive", json={},
                             headers={screen_lock.HEADER: a}).status_code, 200)

    def test_タブを閉じれば残ったタブが操作へ戻る(self) -> None:
        """閉じる合図(`sendBeacon`)はヘッダを付けられないので本文で渡す。"""
        a = self.open_tab()
        b = self.open_tab()
        self.assertEqual(self.call(a).status_code, 409)

        self.client.post("/api/alive", json={"leaving": True, "screen_id": b})
        self.assertEqual(self.call(a).status_code, 200)

    def test_他人の番号では手放せない(self) -> None:
        a = self.open_tab()
        b = self.open_tab()
        self.client.post("/api/alive", json={"leaving": True, "screen_id": a})
        self.assertEqual(self.call(a).status_code, 409)
        self.assertEqual(self.call(b).status_code, 200)


@unittest.skipUnless(HAS_WEB, _SKIP)
class WiringTests(unittest.TestCase):
    """画面の側が繋がっているか。**外すと黙って無防備になる**ところ。"""

    def js(self, name: str) -> str:
        return (_ROOT / "app" / "static" / "js" / name).read_text(encoding="utf-8")

    def test_全部の要求に番号が付く(self) -> None:
        """`api.js` は1か所で送る。ここが唯一の出口。"""
        self.assertIn("screen.HEADER", self.js("api.js"))

    def test_画面の移動にも番号が付く(self) -> None:
        """付け忘れると、画面を移るたびに番号が変わる。"""
        self.assertIn("screen.HEADER", self.js("nav.js"))

    def test_断られたら覆いを出す(self) -> None:
        self.assertIn("screen.TAKEN", self.js("api.js"))
        self.assertIn("showTaken", self.js("api.js"))

    def test_押す前にも気づく(self) -> None:
        self.assertIn("screen_ok", self.js("health.js"))

    def test_閉じるときに番号を返す(self) -> None:
        self.assertIn("screen_id", self.js("health.js"))

    def test_見出しの名前が画面とサーバで同じ(self) -> None:
        """別々に書くと、**直したつもりで片方だけ**になる。"""
        self.assertIn(f'"{screen_lock.HEADER}"', self.js("screen.js"))
        self.assertIn(f'"{screen_lock.REFUSE_TAKEN}"', self.js("screen.js"))


if __name__ == "__main__":                       # pragma: no cover
    unittest.main(verbosity=2)
