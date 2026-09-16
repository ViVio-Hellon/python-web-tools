"""起動をいちばん先に見せる / 終わるときは確実に終わる (VER2.5.0)

【何を守りたいか】
1. **待機画面より前に、重いものを置かない。** 起動でいちばん時間を食う
   のは Flask とアプリ本体の import で、そのあとに待機画面を出したら
   「待たせるために見せるもの」を待たせていることになる
2. **1往復で出す。** 外部への要求(CSS・画像・書体)を1つも出さない
3. **止めたら終わる。** 待ち受けの輪は、つないだままの接続が1本でも
   あると回り続ける ── 画面を開いたまま止めるとプロセスが残っていた
4. **タブを閉じたら終わる。** 窓が無いアプリなので、残っていると次の
   起動が「すでに起動しています」と判定し、新しい版が動かない
"""
from __future__ import annotations

import json
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import app_config, boot_screen, idle_exit  # noqa: E402

try:
    import flask  # noqa: F401
    import waitress  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover
    HAS_WEB = False

_SKIP = "Flask / waitress が入っていないためスキップ"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _BuildsAppMixin:
    """`AppServer.build()` を呼ぶ試験の後片付け。

    `build()` は「どうやって止めるか」を `app.routes.health` の
    モジュール変数へ差し込む。**プロセスに1つの状態**なので、
    戻さないと次の試験がこちらの後始末を掴む
    (`test_停止フックが無ければ501` が実際にそうなった)。
    """

    def keep_shutdown_hook(self) -> None:
        from app.routes import health

        original = health._shutdown_hook
        self.addCleanup(health.set_shutdown_hook, original)


# ==================================================================
# 待機画面
# ==================================================================
class BootScreenTests(unittest.TestCase):
    """`packaging_tool/boot_screen.py` — 骨格は1か所。"""

    def page(self, **over) -> str:
        args = dict(display_name="梱包資材総合ツール", version_label="VER9.9.9",
                    token="tok", app_id="nlm.packaging-tool", poll_ms=300,
                    home_url="/lot")
        args.update(over)
        return boot_screen.render(**args)

    def test_外部への要求を1つも出さない(self):
        """**1往復で出す。** CSS を別に取りに行くと、その分だけ遅れる。"""
        page = self.page()
        self.assertNotIn("<link", page)
        self.assertNotIn("script src", page)
        self.assertNotIn("@import", page)
        self.assertNotIn("url(http", page)

    def test_色はtokensから来る(self):
        """色の出どころは `tokens.css` ただ1つ(設計指針 §3.8)。"""
        page = self.page()
        self.assertIn("--bar-a:", page)          # 埋め込まれている
        self.assertIn("var(--bar-ink)", page)

    def test_版が出る(self):
        """帯がまだ無い場面。ここに出さないと確かめる先が無くなる。"""
        self.assertIn("VER9.9.9", self.page())

    def test_段は4つ(self):
        """進捗率を正確に出すことではなく、無反応の時間をなくすのが目的。"""
        self.assertEqual(len(boot_screen.STEPS), 4)
        page = self.page()
        for key, label in boot_screen.STEPS:
            self.assertIn(f'data-step="{key}"', page)
            self.assertIn(label, page)

    def test_待たずに入る道がある(self):
        self.assertIn('href="/lot"', self.page(home_url="/lot"))

    def test_値はJSONとして埋める(self):
        """表示名やトークンに引用符が入っても壊れないこと。"""
        page = self.page(token='a"b\\c', display_name='<script>')
        self.assertIn(json.dumps('a"b\\c'), page)
        self.assertNotIn("<script>梱包", page)
        self.assertIn("&lt;script&gt;", page)

    def test_tokensが読めなくても描ける(self):
        """起動画面が出ないよりは、色が少し違ってでも出るほうがよい。"""
        boot_screen._tokens_cache = None
        self.addCleanup(setattr, boot_screen, "_tokens_cache", None)
        with mock.patch.object(Path, "read_text", side_effect=OSError("無い")):
            page = self.page()
        self.assertIn("--bar-a:", page)
        self.assertIn("梱包資材総合ツール", page)

    def test_動きを止める設定を尊重する(self):
        self.assertIn("prefers-reduced-motion", self.page())


@unittest.skipUnless(HAS_WEB, _SKIP)
class BootAppTests(unittest.TestCase):
    """`boot_server.BootApp` — Flask より前に答える最小のWSGI。"""

    def setUp(self) -> None:
        from boot_server import BootApp

        self.app = BootApp("field", 8713, "tok")

    def call(self, path: str):
        captured = {}

        def start(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        body = b"".join(self.app({"PATH_INFO": path, "REQUEST_METHOD": "GET"}, start))
        return captured["status"], captured["headers"], body.decode("utf-8")

    def test_待機画面を返す(self):
        status, headers, body = self.call("/")
        self.assertEqual(status, "200 OK")
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("起動しています", body)

    def test_待機画面は残さない(self):
        """次に開いたときは本体が出る。控えを残すと待機画面が居座る。"""
        _, headers, _ = self.call("/")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_healthは本体と同じ形(self):
        """形が違うと、待機画面と `launch_guard` が場合分けを持つ。"""
        _, _, body = self.call("/api/health")
        health = json.loads(body)
        for key in ("app_id", "display_name", "version", "mode", "port",
                    "pid", "ready", "stage", "stage_key", "startup_error", "job"):
            self.assertIn(key, health, key)
        self.assertFalse(health["ready"])
        self.assertEqual(health["app_id"], app_config.app_id())

    def test_多重起動の判定に使える(self):
        """後から起動したプロセスが「自分と同じアプリか」を知れること。"""
        import launch_guard

        _, _, body = self.call("/api/health")
        self.assertTrue(launch_guard.is_our_app(json.loads(body), "field"))

    def test_業務の要求にはまだと答える(self):
        """404 にすると「無い」に見える。**まだであることを言う**。"""
        status, _, body = self.call("/api/lot/search")
        self.assertTrue(status.startswith("503"))
        self.assertEqual(json.loads(body)["error"]["code"], "starting")

    def test_段は外から差し替えられる(self):
        self.app.mark_stage("取り込み中", "import")
        health = json.loads(self.call("/api/health")[2])
        self.assertEqual(health["stage"], "取り込み中")
        self.assertEqual(health["stage_key"], "import")

    def test_起動の失敗を伝えられる(self):
        self.app.mark_error("試験用のエラー")
        self.assertEqual(json.loads(self.call("/api/health")[2])["startup_error"],
                         "試験用のエラー")


@unittest.skipUnless(HAS_WEB, _SKIP)
class HandoverTests(unittest.TestCase):
    """待ち受けを開き直さずに本体へ移る。"""

    def test_差し替えると呼び先が変わる(self):
        from boot_server import Handover

        first = lambda e, s: [b"first"]           # noqa: E731
        second = lambda e, s: [b"second"]         # noqa: E731
        hand = Handover(first)
        self.assertFalse(hand.handed_over)
        self.assertEqual(hand({}, None), [b"first"])
        hand.install(second)
        self.assertTrue(hand.handed_over)
        self.assertEqual(hand({}, None), [b"second"])


@unittest.skipUnless(HAS_WEB, _SKIP)
class BuildOrderTests(_BuildsAppMixin, unittest.TestCase):
    """`AppServer` は**本体を後から**組み立てる。"""

    def setUp(self) -> None:
        self.keep_shutdown_hook()

    def test_組み立てる前は本体を持たない(self):
        import server as server_module

        srv = server_module.AppServer("field", _free_port(), token="t")
        self.assertIsNone(srv.app)
        self.assertIsNotNone(srv.boot)

    def test_トークンは差し替えの前後で変わらない(self):
        """変わると、待機画面が持っているトークンが通らなくなる。"""
        import server as server_module

        srv = server_module.AppServer("field", _free_port(), token="t")
        before = srv.token
        srv.build()
        self.assertEqual(srv.token, before)
        self.assertEqual(srv.app.config["TOKEN"], before)

    def test_段を引き継ぐ(self):
        """差し替えた瞬間に「準備中」へ巻き戻って見えないこと。"""
        import server as server_module

        srv = server_module.AppServer("field", _free_port(), token="t")
        srv.mark_stage("取り込み中", "import")
        srv.build()
        self.assertEqual(srv.app.config["STAGE"], "取り込み中")


# ==================================================================
# 終わり方
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class StopWithOpenConnectionTests(_BuildsAppMixin, unittest.TestCase):
    """**これが「Pythonが終わらない」の正体だった。**

    waitress の受付の輪は `while map:` で回る。`server.close()` が閉じる
    のは待ち受けのソケットだけで、ブラウザが張っている keep-alive の
    接続は map に残る ── 画面を開いたまま止めると輪が回り続け、
    プロセスがいつまでも残っていた。
    """

    def setUp(self) -> None:
        self.keep_shutdown_hook()

    def test_接続を張ったままでも止まる(self):
        import server as server_module

        port = _free_port()
        srv = server_module.AppServer("field", port, token="t")
        srv.build()
        thread = server_module.run_in_background(srv)
        self.assertTrue(server_module.wait_until_listening(port, timeout=10))

        # ブラウザのように keep-alive を張ったままにする
        held = []
        for _ in range(3):
            sock = socket.create_connection(("127.0.0.1", port), timeout=3)
            sock.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                         b"Connection: keep-alive\r\n\r\n")
            sock.recv(4096)
            held.append(sock)
        self.addCleanup(lambda: [s.close() for s in held])

        srv.stop()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(),
                         "接続が残っていると受付の輪が終わらない(プロセスが残る)")

    def test_2回止めても安全(self):
        """画面の「終了」と stop.bat が続けて来ることがある。"""
        import server as server_module

        port = _free_port()
        srv = server_module.AppServer("field", port, token="t")
        srv.build()
        thread = server_module.run_in_background(srv)
        self.assertTrue(server_module.wait_until_listening(port, timeout=10))
        srv.stop()
        srv.stop()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())

    def test_止めを頼んだことが外から分かる(self):
        """起動側が「待ちに上限を置く」判断に使う。"""
        import server as server_module

        srv = server_module.AppServer("field", _free_port(), token="t")
        self.assertFalse(srv.stop_requested)
        srv.stop()
        self.assertTrue(srv.stop_requested)


class HardExitTests(unittest.TestCase):
    """止まらなかったときの最後の手当て。"""

    def test_上限を過ぎたら落とす(self):
        """残ったプロセスは、次の起動で新しい版を動かなくする。"""
        import start_app

        srv = mock.Mock(stop_requested=True)
        thread = mock.Mock()
        thread.is_alive.return_value = True
        with mock.patch.object(start_app, "_hard_exit") as hard, \
             mock.patch.object(start_app, "EXIT_WAIT_SEC", 0.01):
            start_app._hold_until_stopped(srv, thread)
        hard.assert_called_once()

    def test_終わったなら落とさない(self):
        import start_app

        srv = mock.Mock(stop_requested=True)
        thread = mock.Mock()
        thread.is_alive.side_effect = [True, False]
        with mock.patch.object(start_app, "_hard_exit") as hard, \
             mock.patch.object(start_app, "EXIT_WAIT_SEC", 0.01):
            start_app._hold_until_stopped(srv, thread)
        hard.assert_not_called()


# ==================================================================
# タブを閉じたら終わる
# ==================================================================
class RequestIsPresenceTests(unittest.TestCase):
    """**要求が来ている = 誰かが見ている。**

    心拍(`/api/alive`)だけを見ていると、画面を移ったあと新しいページの
    心拍が届くまでのあいだ、猶予が走り続けて落ちる(現場で実際に起きた)。
    要求そのものを在席の合図にして塞ぐ。
    """

    def setUp(self) -> None:
        from app import create_app

        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        self.stopped = []
        self.watch = idle_exit.install(lambda: self.stopped.append(1),
                                       lambda: False)
        app = create_app("field", token="t", port=8715)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_画面を開くと在席の合図になる(self) -> None:
        self.watch.beat()
        self.watch.leaving()                  # 画面を移った
        self.assertIsNotNone(self.watch._leaving_at)

        self.client.get("/selection")         # 移った先が読み込まれた
        self.assertIsNone(self.watch._leaving_at,
                          "画面を読み込んだのに「閉じた」が残っています")

    def test_APIでも在席の合図になる(self) -> None:
        self.watch.beat()
        self.watch.leaving()
        self.client.get("/api/health")
        self.assertIsNone(self.watch._leaving_at)

    def test_心拍の口だけは取り消さない(self) -> None:
        """`/api/alive` は「閉じました」も同じ口で受ける。
        ここで先に取り消すと、閉じたことが伝わらなくなる。"""
        self.watch.beat()
        self.client.post("/api/alive", json={"leaving": True})
        self.assertIsNotNone(self.watch._leaving_at,
                             "閉じた合図が取り消されています")


class IdleWatchTests(unittest.TestCase):
    """`packaging_tool/idle_exit.py` — 見張りの判断だけを見る。"""

    def setUp(self) -> None:
        self.stopped = []
        self.busy = False
        idle_exit.reset()
        self.addCleanup(idle_exit.reset)

    def watch(self, **kw) -> idle_exit.IdleWatch:
        return idle_exit.IdleWatch(lambda: self.stopped.append(1),
                                   lambda: self.busy, **kw)

    def test_1度も繋がっていなければ落とさない(self):
        """`--no-browser` で立てておく使い方を巻き添えにしない。"""
        w = self.watch(idle_sec=0.0)
        self.assertIsNone(w.overdue())

    def test_心拍があるうちは落とさない(self):
        w = self.watch(idle_sec=10.0)
        w.beat()
        self.assertIsNone(w.overdue())

    def test_心拍が途切れたら落とす(self):
        w = self.watch(idle_sec=0.0)
        w.beat()
        self.assertIsNotNone(w.overdue())

    def test_画面を移っただけで落とさない(self):
        """**現場で実際に起きた「使っている最中に閉じた」の再現。**

        画面を移ると、古いページの `pagehide` が「閉じました」を送る。
        新しいページの最初の心拍が届くまでのあいだ猶予が走り続けるので、
        頁とESモジュールの読み込みが続くと、そのあいだに切れる。

            08:41:15 画面が閉じました。8秒 待って終了します
            08:41:20 資材選択のセッションを開始しました   ← 使っている
            08:41:25 誰も見ていないので終了します

        真ん中の行が「見ている」証拠なのに、心拍だけを見ていたので
        届かなかった。**要求そのものを在席の合図にする**(`app/__init__`
        の `_note_someone_is_here`)ので、新しいページを読み込んだ時点で
        取り消される。
        """
        w = self.watch(idle_sec=90.0, grace_sec=0.0)
        w.beat()              # 開いている
        w.leaving()           # 画面を移った(古いページの pagehide)
        self.assertIsNotNone(w.overdue(), "前提: このままだと落ちる")

        w.beat()              # 新しいページの要求が届いた
        self.assertIsNone(w.overdue(), "移っただけで落としてはいけない")

    def test_閉じたら猶予のあとで落とす(self):
        w = self.watch(idle_sec=999.0, grace_sec=0.0)
        w.beat()
        w.leaving()
        self.assertIn("閉じ", w.overdue())

    def test_猶予のあいだは落とさない(self):
        w = self.watch(idle_sec=999.0, grace_sec=999.0)
        w.beat()
        w.leaving()
        self.assertIsNone(w.overdue())

    def test_再読込は取り消される(self):
        """`pagehide` は再読込でも飛ぶ。戻ってきたら取り消す。"""
        w = self.watch(idle_sec=999.0, grace_sec=0.0)
        w.beat()
        w.leaving()
        self.assertIsNotNone(w.overdue())
        w.beat()                                   # 戻ってきた
        self.assertIsNone(w.overdue())

    def test_処理中は落とさない(self):
        """取り込みの途中で終わると、DBが中途半端な状態で残る。"""
        self.busy = True
        w = self.watch(idle_sec=0.0, grace_sec=0.0, tick_sec=0.01)
        w.beat()
        w.start()
        self.addCleanup(w.cancel)
        time.sleep(0.15)
        self.assertEqual(self.stopped, [])

    def test_処理が終われば落とす(self):
        self.busy = True
        w = self.watch(idle_sec=0.0, grace_sec=0.0, tick_sec=0.01)
        w.beat()
        w.start()
        self.addCleanup(w.cancel)
        time.sleep(0.1)
        self.busy = False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not self.stopped:
            time.sleep(0.02)
        self.assertEqual(self.stopped, [1])

    def test_見張りは1つ(self):
        first = idle_exit.install(lambda: None, lambda: False)
        second = idle_exit.install(lambda: None, lambda: False)
        self.assertIs(first, second)


@unittest.skipUnless(HAS_WEB, _SKIP)
class AliveApiTests(unittest.TestCase):
    """`POST /api/alive` — 画面からの心拍。"""

    def setUp(self) -> None:
        from tests import _web

        idle_exit.reset()
        self.addCleanup(idle_exit.reset)
        self.client = _web.make_client(port=8751)

    def test_トークンなしで通る(self):
        """トークンが切れた画面が、黙って死んだ扱いになると困る。"""
        from app import TOKEN_EXEMPT_PATHS

        self.assertIn("/api/alive", TOKEN_EXEMPT_PATHS)
        res = self.client.post("/api/alive", json={})
        self.assertEqual(res.status_code, 200)

    def test_見張りが居なければ何もしない(self):
        res = self.client.post("/api/alive", json={})
        self.assertFalse(res.get_json()["watching"])

    def test_心拍が届く(self):
        watch = idle_exit.install(lambda: None, lambda: False)
        watch.cancel()
        res = self.client.post("/api/alive", json={},
                               headers={"X-Tool-Token": "test-token-abc123"})
        self.assertTrue(res.get_json()["watching"])
        self.assertIsNone(watch.overdue())

    def test_閉じた合図が届く(self):
        watch = idle_exit.install(lambda: None, lambda: False, grace_sec=0.0)
        watch.cancel()
        self.client.post("/api/alive", json={})
        self.client.post("/api/alive", json={"leaving": True})
        self.assertIsNotNone(watch.overdue())

    def test_心拍は書き込みの錠を取らない(self):
        """20秒ごとに来る。待たせると自動終了が誤る。"""
        import app as app_module

        request = mock.Mock(method="POST", path="/api/alive")
        self.assertFalse(app_module._is_write(request))


# ==================================================================
# 版でキャッシュを外す
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class StaticVersionTests(unittest.TestCase):
    """入れ替えたら CSS/JS を取り直させる。"""

    def test_静的ファイルのURLに版が付く(self):
        from flask import url_for

        from app import create_app

        app = create_app("field", token="t", port=8752)
        with app.test_request_context():
            url = url_for("static", filename="css/tokens.css")
        self.assertIn(f"v={app_config.version()}", url)

    def test_画面のCSSにも付く(self):
        from tests import _web

        client = _web.make_client(port=8753)
        html = client.get("/lot", headers=_web.auth()).get_data(as_text=True)
        self.assertIn(f"tokens.css?v={app_config.version()}", html)

    def test_版を上げれば変わる(self):
        """上げ忘れなければ必ず変わる(出どころは config/app.json)。"""
        from flask import url_for

        from app import create_app

        with mock.patch.object(app_config, "version", return_value="9.9.9"):
            app = create_app("field", token="t", port=8754)
        with app.test_request_context():
            url = url_for("static", filename="js/app.js")
        self.assertIn("v=9.9.9", url)
        self.assertNotIn(f"v={app_config.version()}", url)


if __name__ == "__main__":                        # pragma: no cover
    unittest.main()


# ==================================================================
# 版が古いまま出るのを防ぐ
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP)
class CachePolicyTests(unittest.TestCase):
    """**「入れ替えたのに古いまま」の正体はここだった。**

    以前は `/api/` にだけ `no-store` を付けていて、**画面のHTMLには
    キャッシュの指示が1つも無かった**。指示が無いHTMLはブラウザが
    自分の判断で控えるので、版のバッジが古いまま出続ける。
    """

    def setUp(self) -> None:
        from tests import _web

        self.client = _web.make_client(port=8761)
        self.auth = _web.auth()

    def test_画面のHTMLは控えさせない(self):
        res = self.client.get("/lot", headers=self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["Cache-Control"], "no-store")

    def test_APIも控えさせない(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.headers["Cache-Control"], "no-store")

    def test_古いブラウザ向けの指示も付ける(self):
        res = self.client.get("/lot", headers=self.auth)
        self.assertEqual(res.headers.get("Pragma"), "no-cache")
        self.assertEqual(res.headers.get("Expires"), "0")

    def test_静的ファイルは長く控えてよい(self):
        """URLに版が入っているので、入れ替えれば必ず取り直される。"""
        res = self.client.get(f"/static/css/tokens.css?v={app_config.version()}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("max-age=", res.headers["Cache-Control"])
        self.assertIn("immutable", res.headers["Cache-Control"])

    def test_版の付かない静的ファイルは必ず確かめさせる(self):
        """**入口だけが新しくて中身が古い、を作らない。**

        版を付けるのは `url_for('static', ...)` で、効くのはテンプレートが
        名指しするファイルだけ。その中の

            import * as mapedit from "../mapedit.js";

        は版の付かない素のURLで取りに行く。ここに `immutable` を付けると
        ブラウザは再確認すらせず、入れ替えても共有モジュールだけが
        何日も古いまま残る ── 現場で実際に

            dragger?.clearSelection is not a function

        という形で壊れた(入口の inventory.js は v=2.27.0、中の
        mapedit.js は古いまま)。
        """
        res = self.client.get("/static/js/mapedit.js")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("immutable", res.headers["Cache-Control"])
        self.assertIn("no-cache", res.headers["Cache-Control"])

    def test_共有モジュールは版なしで読まれている(self):
        """上の試験が守っているものが、実際にその形で読まれていること。"""
        root = Path(__file__).resolve().parent.parent
        views = root / "app" / "static" / "js" / "views"
        found = []
        for path in views.glob("*.js"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("import") and '"../' in line:
                    found.append(line)
        self.assertTrue(found, "共有モジュールの import が見当たりません")
        # 版が付いていないことを確かめる(付ける方式へ変えたらこの試験を直す)
        self.assertFalse([line for line in found if "?v=" in line])

    def test_版のバッジは毎回作り直される(self):
        """控えられないので、入れ替えれば次に開いたときに変わる。"""
        html = self.client.get("/lot", headers=self.auth).get_data(as_text=True)
        self.assertIn(app_config.version_label(), html)


class VersionSourceTests(unittest.TestCase):
    """版の出どころは `config/app.json` ただ1つ。"""

    def test_画面に出す版を書き写さない(self):
        """**出す場所に数字を書かない。** 上げたときに片方だけ古くなる。

        文中の言及(「〜VER2.2.0 では」)は履歴なので構いません。
        見ているのは**画面に出る経路**、つまりテンプレートと JS です。
        """
        import re

        found = []
        for folder in (_ROOT / "app" / "templates", _ROOT / "app" / "static"):
            for path in folder.rglob("*"):
                if path.suffix not in (".html", ".js", ".css"):
                    continue
                text = path.read_text(encoding="utf-8")
                for hit in re.findall(r"VER\s?\d+\.\d+\.\d+", text):
                    found.append(f"{path.relative_to(_ROOT)}: {hit}")
        self.assertEqual(found, [], "版を画面に直接書いています")

    def test_版を出すのはapp_configだけ(self):
        """出どころが2つあると、片方だけ直した状態を作れる。"""
        import re

        callers = set()
        for path in (_ROOT / "packaging_tool").rglob("*.py"):
            if path.name == "app_config.py":
                continue
            text = path.read_text(encoding="utf-8")
            if re.search(r"\bVERSION_PREFIX\b", text):
                callers.add(path.name)
        self.assertEqual(callers, set(),
                         "版の組み立ては app_config.version_label() だけ")

    def test_READMEの版はapp_jsonと一致する(self):
        readme = (_ROOT / "README.md").read_text(encoding="utf-8").splitlines()[0]
        self.assertIn(app_config.version_label(), readme)


class StaleInstanceTests(unittest.TestCase):
    """**古い版が動いていたら、合流せずに立て直す。**

    入れ替えたのに古いプロセスが残っていると、多重起動の判定が
    「すでに起動しています」と答えて古いほうのブラウザを開く ──
    新しい版がいつまでも動かず、版のバッジも古いまま。
    """

    def setUp(self) -> None:
        import launch_guard

        self.guard = launch_guard
        self.info = launch_guard.LockInfo(
            app_id=app_config.app_id(), mode="field", pid=4242, port=8713,
            url="http://127.0.0.1:8713/", started_at=0.0,
            app_root=str(_ROOT), token="tok")

    def _patch(self, health: dict):
        """ロックも生存もあることにして、`/api/health` だけ差し替える。"""
        return (
            mock.patch.object(self.guard, "read_lock", return_value=self.info),
            mock.patch.object(self.guard, "is_process_alive", return_value=True),
            mock.patch.object(self.guard, "probe_health", return_value=health),
        )

    def health(self, version: str) -> dict:
        return {"app_id": app_config.app_id(), "mode": "field",
                "version": version}

    def test_同じ版なら合流する(self):
        patches = self._patch(self.health(app_config.version()))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = self.guard.check_existing("field")
        self.assertFalse(result.should_start)
        self.assertEqual(result.stale_version, "")

    def test_古い版なら終わらせて立て直す(self):
        patches = self._patch(self.health("2.0.1"))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(self.guard, "request_shutdown", return_value=True), \
             mock.patch.object(self.guard, "probe_health", return_value=None), \
             mock.patch.object(self.guard, "remove_lock") as removed:
            # probe_health を2度使う(版の確認 → 消えたかの確認)ので、
            # 版の確認だけ元に戻す
            with mock.patch.object(self.guard, "probe_health",
                                   side_effect=[self.health("2.0.1"), None]):
                result = self.guard.check_existing("field")
        self.assertTrue(result.should_start, "古い版に合流してはいけない")
        self.assertEqual(result.stale_version, "2.0.1")
        removed.assert_called_once()

    def test_止められなければ合流して知らせる(self):
        """取り込みの途中(409)なら止めない。中途半端に残すほうが害が大きい。"""
        patches = self._patch(self.health("2.0.1"))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        with mock.patch.object(self.guard, "request_shutdown", return_value=False):
            result = self.guard.check_existing("field")
        self.assertFalse(result.should_start)
        self.assertEqual(result.stale_version, "2.0.1")
        self.assertIn("2.0.1", result.reason)

    def test_版が分からなければ今までどおり合流する(self):
        """古い版は `version` を返さないことがある。締め出さない。"""
        patches = self._patch({"app_id": app_config.app_id(), "mode": "field"})
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        result = self.guard.check_existing("field")
        self.assertFalse(result.should_start)


@unittest.skipUnless(HAS_WEB, _SKIP)
class WhichCopyTests(unittest.TestCase):
    """どのフォルダの、どの版が動いているか。"""

    def test_healthが置き場所を返す(self):
        from tests import _web

        client = _web.make_client(port=8762)
        health = client.get("/api/health").get_json()
        self.assertEqual(health["version"], app_config.version())
        self.assertEqual(health["app_root"], str(app_config.APP_ROOT))
