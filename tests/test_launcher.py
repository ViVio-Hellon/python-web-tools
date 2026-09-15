"""起動基盤の確認 (基盤仕様書 2.1〜2.9 / §7 必須チェックリスト)

業務機能ではなく「起動・多重起動の防止・安全な停止」だけを見る。
基盤仕様書のステップ3が「業務機能を作り込んでから起動問題を直すのではなく、
先に基盤を成立させる」と定めているので、ここが Phase 2 の完了条件になる。

実サーバを立てる試験は2つに分けてある。`waitress` が無い環境では
どちらもスキップする。

    LiveServerTests … 状態を変えない試験。**サーバは組で1つ**
    ServerStopTests … 止め方そのものを見る。1件ごとに立て直す

分けているのは速さのため ── 起動と停止だけで1件あたり0.3秒かかるので、
読むだけの試験まで立て直すと、この1ファイルで数秒を使ってしまう。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from packaging_tool import access_control, app_config  # noqa: E402

# 権限は明示して渡す。**開発機の `アクセス権限` マスタの中身で
# 試験の結果が変わってはいけない**
_ALL = access_control.grant_of("mode:field", "mode:material")

try:
    import flask  # noqa: F401
    import waitress  # noqa: F401
    HAS_WEB = True
except ImportError:                              # pragma: no cover - 環境依存
    HAS_WEB = False

_SKIP_WEB = "Flask / waitress が入っていないためスキップ (pip install -r requirements.txt)"


class _CaptureErrors(logging.Handler):
    """ERROR以上だけを溜めるハンドラ。"""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class LocalAreaTestCase(unittest.TestCase):
    """ローカル領域を一時フォルダへ逃がす。

    本物の `%LOCALAPPDATA%` を汚さずにロックファイルを試すため。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get("PACKAGING_TOOL_LOCAL_DIR")
        os.environ["PACKAGING_TOOL_LOCAL_DIR"] = self._tmp.name
        app_config.ensure_local_dirs()

    def tearDown(self) -> None:
        if self._orig is None:
            os.environ.pop("PACKAGING_TOOL_LOCAL_DIR", None)
        else:
            os.environ["PACKAGING_TOOL_LOCAL_DIR"] = self._orig
        self._tmp.cleanup()


# ==================================================================
# 2.4 多重起動の防止
# ==================================================================
class LockFileTests(LocalAreaTestCase):
    def setUp(self) -> None:
        super().setUp()
        import launch_guard
        self.guard = launch_guard

    def test_書いて読める(self) -> None:
        info = self.guard.build_lock_info("field", 8713)
        self.guard.write_lock(info)
        read = self.guard.read_lock("field")
        self.assertIsNotNone(read)
        self.assertEqual(read.pid, os.getpid())
        self.assertEqual(read.port, 8713)
        self.assertEqual(read.app_id, app_config.app_id())

    def test_無ければNone(self) -> None:
        self.assertIsNone(self.guard.read_lock("field"))

    def test_壊れていてもNone(self) -> None:
        """読めないことを理由に起動を止めない。"""
        path = self.guard.lock_path("field")
        path.write_text("これはJSONではない", encoding="utf-8")
        self.assertIsNone(self.guard.read_lock("field"))

    def test_モードごとに別のロック(self) -> None:
        """現場と資材は同時に起動してよい。"""
        self.assertNotEqual(self.guard.lock_path("field"),
                            self.guard.lock_path("material"))

    def test_未知のモードは例外(self) -> None:
        with self.assertRaises(ValueError):
            self.guard.lock_path("けんさ")

    def test_消せる(self) -> None:
        self.guard.write_lock(self.guard.build_lock_info("field", 8713))
        self.guard.remove_lock("field")
        self.assertIsNone(self.guard.read_lock("field"))

    def test_無いものを消しても壊れない(self) -> None:
        self.guard.remove_lock("field")           # 例外が出ないこと


class ProcessAliveTests(unittest.TestCase):
    def setUp(self) -> None:
        import launch_guard
        self.guard = launch_guard

    def test_自分は生きている(self) -> None:
        self.assertTrue(self.guard.is_process_alive(os.getpid()))

    def test_0や負数は生きていない(self) -> None:
        self.assertFalse(self.guard.is_process_alive(0))
        self.assertFalse(self.guard.is_process_alive(-1))

    def test_存在しないPIDは生きていない(self) -> None:
        """使われていない大きな番号を探して確かめる。"""
        for pid in range(4_000_000, 4_000_050):
            if not self.guard.is_process_alive(pid):
                return
        self.skipTest("空いているPIDを見つけられませんでした")

    def test_自分のコマンドラインが取れる(self) -> None:
        """停止前の照合に使う。取れない環境では空文字になる。"""
        cmdline = self.guard.process_command_line(os.getpid())
        if cmdline:
            self.assertIn("python", cmdline.lower())


class GuardDecisionTests(LocalAreaTestCase):
    """既存インスタンスの判定(基盤仕様書 2.4)。"""

    def setUp(self) -> None:
        super().setUp()
        import launch_guard
        self.guard = launch_guard

    def test_ロックが無ければ起動する(self) -> None:
        result = self.guard.check_existing("field")
        self.assertTrue(result.should_start)

    def test_死んだロックは掃除して起動する(self) -> None:
        info = self.guard.build_lock_info("field", 8713)
        info.pid = 4_000_001                      # 居ないPID
        self.guard.write_lock(info)
        result = self.guard.check_existing("field")
        self.assertTrue(result.should_start)
        self.assertIsNone(self.guard.read_lock("field"), "ロックが残っています")

    def test_プロセスは生きているがアプリでないなら起動する(self) -> None:
        """PIDは使い回される。自分のPIDを書いても、そのポートに
        アプリが居なければ「別物」と判断して起動する。"""
        info = self.guard.build_lock_info("field", _free_port())
        self.guard.write_lock(info)
        result = self.guard.check_existing("field")
        self.assertTrue(result.should_start)


class AcquireLockTests(LocalAreaTestCase):
    """**多重起動を本当に止める。**

    以前は「ロックがあるか見る」→(ポートを決める・待ち受けを確かめる:
    最大15秒)→「ロックを書く」の順だった。そのあいだに始まった2つ目は
    「ロックなし」を見て一緒に立ち上がる ── 現場から届いた多重起動は
    これで、その日の最初なら取り込みも走るので、利用者が「反応が無い」と
    もう一度押す時間は十分にあった。

    直したあとは、**作れたかどうか**でロックを取る(`O_CREAT | O_EXCL`)。
    見てから作るのではないので、2つ目は必ず外れる。
    """

    def setUp(self) -> None:
        super().setUp()
        import launch_guard
        self.guard = launch_guard

    def test_取れるのは1つだけ(self) -> None:
        self.assertTrue(self.guard.try_acquire("field"))
        self.assertFalse(self.guard.try_acquire("field"))

    def test_ポートを決める前でも2つ目は入れない(self) -> None:
        """**隙間そのものを塞ぐ。** 取った直後はまだポートも決まって
        いないが、その状態でも2つ目は取れない。"""
        self.assertTrue(self.guard.try_acquire("field"))
        self.assertEqual(self.guard.read_lock("field").port,
                         self.guard.STARTING_PORT)
        self.assertFalse(self.guard.try_acquire("field"))

    def test_手放せば取り直せる(self) -> None:
        self.guard.try_acquire("field")
        self.guard.remove_lock("field")
        self.assertTrue(self.guard.try_acquire("field"))

    def test_モードが違えば両方取れる(self) -> None:
        """現場と資材は同時に起動してよい。"""
        self.assertTrue(self.guard.try_acquire("field"))
        self.assertTrue(self.guard.try_acquire("material"))

    def test_同時に走らせても1つだけ(self) -> None:
        """**本当に競争させて確かめる。** 順番に呼ぶだけでは、
        たまたま通っているのか防いでいるのか分からない。"""
        import multiprocessing

        with multiprocessing.Pool(16) as pool:
            got = pool.map(_try_acquire_field, range(16))
        self.assertEqual(sum(got), 1, f"取れた数={sum(got)}")


class StartingLockTests(LocalAreaTestCase):
    """立ち上がり中のロックを見たときの振る舞い。"""

    def setUp(self) -> None:
        super().setUp()
        import launch_guard
        self.guard = launch_guard

    def test_立ち上がったら合流する(self) -> None:
        """**待って同じ画面へ入れる。** 起動には時間がかかるので、
        もう一度押した人を追い返さない。"""
        import threading

        self.guard.try_acquire("field")

        def finish():
            time.sleep(0.5)
            self.guard.write_lock(
                self.guard.build_lock_info("field", 8713, "tok"))

        thread = threading.Thread(target=finish, daemon=True)
        thread.start()
        self.addCleanup(thread.join)

        result = self.guard.check_existing("field")
        self.assertFalse(result.should_start)
        self.assertIn("8713", result.url)

    def test_立ち上がらないまま居なくなったら引き継ぐ(self) -> None:
        """失敗したロックが残るかぎり二度と起動できない、を作らない。"""
        self.guard.try_acquire("field")
        info = self.guard.read_lock("field")
        info.pid = 4_000_001                      # 居ないPID
        self.guard.write_lock(info)

        result = self.guard.check_existing("field")
        self.assertTrue(result.should_start)
        self.assertIsNone(self.guard.read_lock("field"))


def _try_acquire_field(_n):
    """別プロセスから呼ぶので、モジュールの外に置く。"""
    import launch_guard
    return launch_guard.try_acquire("field")


class AppIdMatchTests(unittest.TestCase):
    """`app_id` の照合(基盤仕様書 2.3)。

    同じポートに別のアプリが居るとき、HTTPが返るだけで
    「自分が起動している」と判断してはいけない。
    """

    def setUp(self) -> None:
        import launch_guard
        self.guard = launch_guard

    def test_応答が無ければ偽(self) -> None:
        self.assertFalse(self.guard.is_our_app(None, "field"))

    def test_app_idが違えば偽(self) -> None:
        self.assertFalse(self.guard.is_our_app(
            {"app_id": "other.app", "mode": "field"}, "field"))

    def test_モードが違えば偽(self) -> None:
        """現場と倉庫を取り違えると権限の分離が崩れる。"""
        self.assertFalse(self.guard.is_our_app(
            {"app_id": app_config.app_id(), "mode": "material"}, "field"))

    def test_両方一致すれば真(self) -> None:
        self.assertTrue(self.guard.is_our_app(
            {"app_id": app_config.app_id(), "mode": "field"}, "field"))


class PortTests(unittest.TestCase):
    def setUp(self) -> None:
        import launch_guard
        self.guard = launch_guard

    def test_使用中のポートは空きでない(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            self.assertFalse(self.guard.is_port_free(port))

    def test_空きポートは空き(self) -> None:
        self.assertTrue(self.guard.is_port_free(_free_port()))

    def test_候補から選ぶ(self) -> None:
        port = self.guard.pick_port("field")
        self.assertIn(port, app_config.port_candidates("field"))


# ==================================================================
# Flask アプリ本体
# ==================================================================
@unittest.skipUnless(HAS_WEB, _SKIP_WEB)
class StartupModeTests(unittest.TestCase):
    """**押したショートカットではなく、端末の権限が開くモードを決める。**

    起動ファイルを1つに絞れるのはこの規則があるからで、逆に言えば
    ここが崩れると資材の端末が現場モードで開き、「発注一覧が出ない」を
    権限の問題として調べることになる。
    """

    def test_資材だけの端末は資材で開く(self) -> None:
        grant = access_control.grant_of("mode:material")
        self.assertEqual(grant.startup_mode(), "material")

    def test_現場だけの端末は現場で開く(self) -> None:
        grant = access_control.grant_of("mode:field")
        self.assertEqual(grant.startup_mode(), "field")

    def test_両方あるなら狭いほうから開く(self) -> None:
        """帯の切替でいつでも移れるので、権限の広いほうを既定にしない。"""
        self.assertEqual(_ALL.startup_mode(), "field")

    def test_権限が無くても開ける(self) -> None:
        """開けないと、権限を直す画面(設定)にも辿り着けない。"""
        self.assertEqual(access_control.grant_of().startup_mode(), "field")

    def test_明示した指定はそのまま使う(self) -> None:
        """診断と、2つ並べて開く運用のために残してある。"""
        import start_app
        self.assertEqual(start_app.resolve_mode("material"), "material")
        # 旧名も入口で1度だけそろえる
        self.assertEqual(start_app.resolve_mode("warehouse"), "material")

    def test_既定は権限に任せる(self) -> None:
        import start_app
        with mock.patch("packaging_tool.access_control.startup_grant",
                        return_value=access_control.grant_of("mode:material")):
            self.assertEqual(start_app.resolve_mode(start_app.AUTO), "material")

    def test_モードを決めるのにFlaskを読まない(self) -> None:
        """**待機画面より前**にやることなので、重い import を持ち込まない。

        以前は `from app import startup_grant` だったので、開くモードを
        決めるためだけに Flask とアプリ本体ぜんぶを読んでいた ──
        起動でいちばん重い処理が、待機画面より前に居た。
        """
        source = (_ROOT / "start_app.py").read_text(encoding="utf-8")
        head = source.split("def start(")[0]
        self.assertNotIn("from app import", head)
        self.assertIn("access_control.startup_grant", head)


class AppFactoryTests(unittest.TestCase):
    def _client(self, mode: str = "field", token: str = "テスト用トークン"):
        from app import create_app
        app = create_app(mode, token=token, port=8713, grant=_ALL)
        app.config["TESTING"] = True
        return app, app.test_client()

    def test_未知のroleは例外(self) -> None:
        from app import create_app
        with self.assertRaises(ValueError):
            create_app("けんさ")

    def test_起動確認APIはトークン不要(self) -> None:
        """多重起動の判定と起動待機画面が、トークンを知らずに叩くため。"""
        _, client = self._client()
        res = client.get("/api/health")
        self.assertEqual(res.status_code, 200)

    def test_起動確認APIが必要な項目を返す(self) -> None:
        _, client = self._client()
        body = client.get("/api/health").get_json()
        for key in ("app_id", "ready", "version", "pid", "mode", "port"):
            self.assertIn(key, body)
        self.assertEqual(body["app_id"], app_config.app_id())
        self.assertEqual(body["mode"], "field")

    def test_起動直後は準備中(self) -> None:
        _, client = self._client()
        self.assertFalse(client.get("/api/health").get_json()["ready"])

    def test_準備中は起動待機画面を出す(self) -> None:
        """ブラウザだけが先に開いて接続エラーになるのを防ぐ(2.3)。"""
        _, client = self._client()
        html = client.get("/").get_data(as_text=True)
        self.assertIn("起動しています", html)

    def test_準備が終われば業務画面へ送る(self) -> None:
        """`/` で止まる画面を出さない。

        `Start.vbs` が開くのは `/` なので、ここで止まる画面を出すと
        利用者は**アプリに入れない**(レールが無いので行き先が分からない)。
        実際にそうなっていた。
        """
        app, client = self._client()
        app.config["READY"] = True
        res = client.get("/")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers["Location"], "/lot")

    def test_資材は資材で最初に開ける画面へ送る(self) -> None:
        """まだ作っていない画面へ送ると、起動した瞬間に404になる。"""
        app, client = self._client(mode="material")
        app.config["READY"] = True
        res = client.get("/")
        self.assertEqual(res.status_code, 302)
        location = res.headers["Location"]
        self.assertEqual(client.get(location).status_code, 200,
                         f"{location} が開けません")

    def test_準備中は業務画面へ送らない(self) -> None:
        """先に送ると、まだ動かない画面を触らせることになる。"""
        _, client = self._client()
        self.assertEqual(client.get("/").status_code, 200)

    def test_トークンが無ければ401(self) -> None:
        _, client = self._client()
        self.assertEqual(client.get("/api/jobs").status_code, 401)

    def test_トークンが違えば401(self) -> None:
        _, client = self._client()
        res = client.get("/api/jobs", headers={"X-Tool-Token": "ちがう"})
        self.assertEqual(res.status_code, 401)

    def test_正しいトークンなら通る(self) -> None:
        _, client = self._client()
        res = client.get("/api/jobs", headers={"X-Tool-Token": "テスト用トークン"})
        self.assertEqual(res.status_code, 200)

    def test_Host偽装を拒否する(self) -> None:
        """DNSリバインディング対策。"""
        _, client = self._client()
        res = client.get("/api/health", headers={"Host": "evil.example.com"})
        self.assertEqual(res.status_code, 400)

    def test_localhostは通す(self) -> None:
        _, client = self._client()
        res = client.get("/api/health", headers={"Host": "localhost:8713"})
        self.assertEqual(res.status_code, 200)

    def test_別オリジンからの要求を拒否する(self) -> None:
        _, client = self._client()
        res = client.get("/api/jobs", headers={
            "X-Tool-Token": "テスト用トークン", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(res.status_code, 403)

    def test_CORSヘッダを返さない(self) -> None:
        _, client = self._client()
        res = client.get("/api/health")
        self.assertNotIn("Access-Control-Allow-Origin", res.headers)

    def test_APIは保存させない(self) -> None:
        _, client = self._client()
        res = client.get("/api/health")
        self.assertEqual(res.headers.get("Cache-Control"), "no-store")

    def test_停止フックが無ければ501(self) -> None:
        """サーバ抜きで組み立てた場合。黙って成功したことにしない。"""
        _, client = self._client()
        res = client.post("/api/shutdown", json={},
                          headers={"X-Tool-Token": "テスト用トークン"})
        self.assertEqual(res.status_code, 501)


@unittest.skipUnless(HAS_WEB, _SKIP_WEB)
class RoleSeparationTests(unittest.TestCase):
    """権限の分離。**現場モードには倉庫の窓口が存在しない。**

    tkinter版が「現場アプリにボタンをそもそも作らない」ことで保っていた
    保証を、HTTPの層で再現する。現場側の都合で確認済みにできてしまうと、
    確認という工程自体が意味を失う。
    """

    def _client(self, mode: str, grant=None):
        from app import create_app
        app = create_app(mode, token="t", port=8713, grant=grant or _ALL)
        app.config["TESTING"] = True
        return app.test_client()

    def test_権限が無ければ資材の窓口は無い(self) -> None:
        """資材の権限が無ければ「確認」は存在しない(404)。

        **取り消しはここに含めない。** 出した本人が引っ込める操作なので
        現場にも要る(VBA `frmSendConfirm` の取り消し)。止めるのは
        状態だけで、権限やモードでは止めない。
        """
        from packaging_tool import access_control
        client = self._client("field", access_control.grant_of("mode:field"))
        res = client.post("/api/warehouse/confirm", json={},
                          headers={"X-Tool-Token": "t"})
        self.assertEqual(res.status_code, 404, "確認が存在しています")

        # 取り消しは存在する(対象が無いので400になるが、404ではない)
        res = client.post("/api/warehouse/cancel", json={},
                          headers={"X-Tool-Token": "t"})
        self.assertNotEqual(res.status_code, 404, "取り消しが存在しません")

    def test_モードが応答に出る(self) -> None:
        for mode in ("field", "material"):
            with self.subTest(mode=mode):
                body = self._client(mode).get("/api/health").get_json()
                self.assertEqual(body["mode"], mode)

    def test_モードごとにポートが違う(self) -> None:
        self.assertNotEqual(app_config.port("field"),
                            app_config.port("material"))


# ==================================================================
# 実サーバ
# ==================================================================
class _LiveServerMixin:
    """立てたサーバを叩くための小道具。"""

    def _get(self, path: str, **kw) -> tuple[int, str]:
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", **kw)
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status, res.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")


def _boot(mode: str = "field", *, build: bool = True):
    """待ち受けが始まるまで待って、(サーバ, スレッド, ポート) を返す。

    `build=False` にすると**本体を組み立てない**。起動直後だけの
    時間帯(待機画面だけが答える)を見る試験がそれを使う。
    """
    import server as server_module
    port = _free_port()
    srv = server_module.AppServer(mode, port, token="t")
    thread = server_module.run_in_background(srv)
    if not server_module.wait_until_listening(port, timeout=10):
        raise AssertionError("待ち受けが始まりませんでした")
    if build:
        srv.build()
    return srv, thread, port


@unittest.skipUnless(HAS_WEB, _SKIP_WEB)
class LiveServerTests(_LiveServerMixin, unittest.TestCase):
    """実際に待ち受けて、起動確認までを通す。

    **サーバはこの組で1つだけ立てる。** 1件ごとに立て直すと、
    起動と停止だけで数秒かかる。ここに置くのは**サーバの状態を
    変えない試験**か、変えても後片付けで戻せる試験だけで、
    止め方そのものを見るものは `ServerStopTests` に分けてある。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls._orig = os.environ.get("PACKAGING_TOOL_LOCAL_DIR")
        os.environ["PACKAGING_TOOL_LOCAL_DIR"] = cls._tmp.name
        app_config.ensure_local_dirs()
        # 停止のときに waitress が内部エラーを吐かないことを見る。
        # 処理中の接続が残ったままソケットを閉じると
        # 「Bad file descriptor」が積まれる
        cls.noise = _CaptureErrors()
        cls.waitress_log = logging.getLogger("waitress")
        cls.waitress_log.addHandler(cls.noise)
        cls.srv, cls.thread, cls.port = _boot()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.srv.stop()
        cls.thread.join(timeout=5)
        time.sleep(0.2)                 # 閉じ際の取りこぼしは少し遅れて出る
        cls.waitress_log.removeHandler(cls.noise)
        messages = list(cls.noise.messages)
        if cls._orig is None:
            os.environ.pop("PACKAGING_TOOL_LOCAL_DIR", None)
        else:
            os.environ["PACKAGING_TOOL_LOCAL_DIR"] = cls._orig
        cls._tmp.cleanup()
        assert messages == [], f"停止のときに waitress が内部エラー: {messages}"

    def tearDown(self) -> None:
        # 状態を触った試験のあとを戻す。**組で1つのサーバを使い回す**ので、
        # 戻さないと次の試験が前の状態を見る
        self.srv.mark_ready(False)
        self.srv.mark_error("")

    def test_起動確認APIが応答する(self) -> None:
        status, body = self._get("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["app_id"], app_config.app_id())

    def test_日本語の表示名がヘッダを壊さない(self) -> None:
        """HTTPヘッダは latin-1 で符号化される。表示名を `Server:` に
        入れると全ての応答が UnicodeEncodeError で落ちる。実際に踏んだ。"""
        status, _ = self._get("/")
        self.assertEqual(status, 200)

    def test_準備が終わるまで待機画面(self) -> None:
        _, html = self._get("/")
        self.assertIn("起動しています", html)
        self.srv.mark_ready(True)
        _, html = self._get("/")
        self.assertNotIn("起動しています", html)

    def test_起動エラーは画面に伝わる(self) -> None:
        """失敗してもサーバは落とさない。落とすと理由が伝わらない。"""
        self.srv.mark_error("試験用のエラー")
        _, body = self._get("/api/health")
        self.assertEqual(json.loads(body)["startup_error"], "試験用のエラー")

    def test_probe_healthで見つけられる(self) -> None:
        import launch_guard
        health = launch_guard.probe_health(self.port)
        self.assertIsNotNone(health)
        self.assertTrue(launch_guard.is_our_app(health, "field"))

    def test_プロキシ設定があっても自分自身には届く(self) -> None:
        """社内PCではプロキシが入っている。除外一覧に 127.0.0.1 が無いと、
        **自分自身への通信までプロキシへ送られて失敗する**。

        現場で実際に踏んだ: サーバは待ち受けを始めているのに
        `/api/health` が返らず、15秒待って「サーバを起動できませんでした」
        になった。ループバックにプロキシを挟む理由は無い。
        """
        import urllib.request

        import launch_guard

        # 何も居ないところへ向けたプロキシを設定する
        saved = {k: os.environ.get(k) for k in
                 ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")}
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
        try:
            # この試験が意味を持つことの確認 ── 既定のやり方なら失敗する
            default = urllib.request.build_opener(
                urllib.request.ProxyHandler(urllib.request.getproxies()))
            with self.assertRaises(Exception):
                default.open(f"http://127.0.0.1:{self.port}/api/health", timeout=2)

            # 送信口を分けてあるので、こちらは通る
            self.assertIsNotNone(launch_guard.probe_health(self.port, timeout=2))
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_TCPで繋がることを別に見られる(self) -> None:
        """「繋がらない」と「繋がるが応答しない」は原因が違う。"""
        import launch_guard
        self.assertTrue(launch_guard.is_port_accepting(self.port))
        self.assertFalse(launch_guard.is_port_accepting(_free_port()))

    def test_待ち受けの確認が成功する(self) -> None:
        import server as server_module
        check = server_module.diagnose_listening(self.port, timeout=5)
        self.assertTrue(check.ok)
        self.assertEqual(check.hint, "")

@unittest.skipUnless(HAS_WEB, _SKIP_WEB)
class ServerStopTests(_LiveServerMixin, LocalAreaTestCase):
    """止め方そのものを見る。**1件ごとに立て直す** ── 止めてしまうので。"""

    def setUp(self) -> None:
        super().setUp()
        self.srv, self.thread, self.port = _boot()

    def tearDown(self) -> None:
        self.srv.stop()
        self.thread.join(timeout=5)
        super().tearDown()

    def test_停止できる(self) -> None:
        import launch_guard
        status, body = self._get(
            "/api/shutdown", method="POST", data=b"{}",
            headers={"X-Tool-Token": "t", "Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["stopped"])
        self.assertTrue(self.srv.wait_stopped(timeout=5), "止まりませんでした")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if launch_guard.probe_health(self.port, timeout=0.3) is None:
                return
            time.sleep(0.1)
        self.fail("停止後も応答しています")

    def test_処理スレッドを畳んでからソケットを閉じる(self) -> None:
        """順序そのものを固定する。

        先にソケットを閉じると、返しかけの応答を書こうとした
        処理スレッドが閉じた記述子に触れて例外を残す。症状(ログの
        トレースバック)は競合次第で出たり出なかったりするので、
        原因側の順序をここで押さえる。
        """
        order: list[str] = []
        waitress_server = self.srv._server
        dispatcher = waitress_server.task_dispatcher
        real_shutdown, real_close = dispatcher.shutdown, waitress_server.close

        def shutdown(*args, **kw):
            order.append("dispatcher")
            return real_shutdown(*args, **kw)

        def close(*args, **kw):
            order.append("socket")
            return real_close(*args, **kw)

        dispatcher.shutdown = shutdown
        waitress_server.close = close
        self.srv.stop()
        self.assertEqual(order, ["dispatcher", "socket"])


class ListenDiagnosisTests(unittest.TestCase):
    """待ち受けを確かめられなかったときに、**どこで**失敗したかを出す。

    「サーバを起動できませんでした」とだけ出しても、現場では手の打ちようがない。
    """

    def test_繋がらないなら遮断を疑わせる(self) -> None:
        import server as server_module
        check = server_module.diagnose_listening(_free_port(), timeout=0.3)
        self.assertFalse(check.ok)
        self.assertFalse(check.tcp_ok)
        self.assertIn("接続できません", check.hint)
        self.assertIn("セキュリティ", check.hint)

    @unittest.skipUnless(HAS_WEB, _SKIP_WEB)
    def test_繋がるのに応答しないならプロキシを疑わせる(self) -> None:
        """TCPは通るがHTTPが返らない状態を作る(応答を返さないソケット)。"""
        import server as server_module

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]

            saved = os.environ.get("http_proxy")
            os.environ["http_proxy"] = "http://proxy.example:8080"
            try:
                check = server_module.diagnose_listening(port, timeout=0.5)
            finally:
                if saved is None:
                    os.environ.pop("http_proxy", None)
                else:
                    os.environ["http_proxy"] = saved

        self.assertFalse(check.ok)
        self.assertTrue(check.tcp_ok, "TCPは通っているはずです")
        self.assertIn("応答が返りません", check.hint)
        self.assertIn("127.0.0.1", check.hint)
        self.assertIn("プロキシ", check.hint)

    def test_繋がっているなら起動を止めない(self) -> None:
        """待ち受けているサーバを、確認が取れないだけで落とさない。

        以前はここを区別せず中止しており、**動いているサーバごと**
        終わらせていた。応答が取れない原因はこちら側の確認経路
        (プロキシ等)にあることが多く、ブラウザなら開けることが多い。
        """
        import server as server_module
        import start_app

        # 応答は取れないが、待ち受けてはいる
        self.assertFalse(start_app.should_abort(
            server_module.ListenCheck(False, tcp_ok=True, hint="…")))

    def test_繋がらないなら起動を止める(self) -> None:
        import server as server_module
        import start_app
        self.assertTrue(start_app.should_abort(
            server_module.ListenCheck(False, tcp_ok=False, hint="…")))

    def test_確認が取れれば当然止めない(self) -> None:
        import server as server_module
        import start_app
        self.assertFalse(start_app.should_abort(
            server_module.ListenCheck(True, tcp_ok=True)))


# ==================================================================
# 起動時の自動取り込み
# ==================================================================
class AutoImportTests(LocalAreaTestCase):
    """準備完了にしてから走らせる(基盤仕様書 監視レベル2)。

    数十秒かかることがあるので、終わるまで起動待機画面に留めると
    「立ち上がらない」ように見える。
    """

    def setUp(self) -> None:
        super().setUp()
        import start_app
        from packaging_tool import config, data_sync, jobs, user_settings
        self.start_app = start_app
        self.jobs = jobs
        self.registry = jobs.reset_registry(
            jobs.JobRegistry(store_path=Path(self._tmp.name) / "jobs.json"))
        self.addCleanup(jobs.reset_registry,
                        jobs.JobRegistry(store_path=Path(self._tmp.name) / "x.json"))

        # 実物のAccessを読みに行かせない
        original = data_sync.auto_import
        data_sync.auto_import = lambda conn, **kw: None
        self.addCleanup(setattr, data_sync, "auto_import", original)

        self.saved = user_settings.get(config.KEY_AUTO_IMPORT)
        self.config, self.user_settings = config, user_settings
        self.addCleanup(user_settings.save, config.KEY_AUTO_IMPORT,
                        self.saved if self.saved is not None else True)

    def fake_srv(self):
        """`mark_ready` / `mark_stage` を受け取るだけの器。

        **準備完了になったかどうか**まで見たいので、記録しておく。
        """
        class Srv:
            def __init__(self) -> None:
                self.ready = False
                self.stage = ""

            def mark_ready(self, value: bool) -> None:
                self.ready = value

            def mark_stage(self, text: str) -> None:
                self.stage = text
        return Srv()

    def wait_idle(self, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.registry.is_busy():
                return
            time.sleep(0.01)
        self.fail("自動取り込みが終わりませんでした")

    def test_設定がONなら走る(self) -> None:
        self.user_settings.save(self.config.KEY_AUTO_IMPORT, True)
        srv = self.fake_srv()
        self.assertTrue(self.start_app._start_auto_import(srv))
        self.wait_idle()
        self.assertEqual(self.registry.recent()[0].label, "起動時の自動取り込み")
        # **待たせているあいだ、何をしているかを出す**
        self.assertEqual(srv.stage, "取り込み中")

    def test_取り込みが終わってから準備完了にする(self) -> None:
        """先に完了にすると、マスタが空のまま業務画面へ入ってしまう。

        起動待機画面が出る前にブラウザが業務画面へ着き、現場は
        「仕掛台帳が未取り込みです」を読むことになる。
        """
        self.user_settings.save(self.config.KEY_AUTO_IMPORT, True)
        srv = self.fake_srv()
        self.start_app._start_auto_import(srv)
        self.assertFalse(srv.ready)              # 始めた時点ではまだ
        self.wait_idle()
        deadline = time.monotonic() + 5.0
        while not srv.ready and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(srv.ready)

    def test_設定がOFFなら走らない(self) -> None:
        self.user_settings.save(self.config.KEY_AUTO_IMPORT, False)
        srv = self.fake_srv()
        # 始めないので、呼び手がその場で準備完了にする
        self.assertFalse(self.start_app._start_auto_import(srv))
        self.assertFalse(self.registry.is_busy())
        self.assertEqual(self.registry.recent(), [])

    def test_取り込めなくても起動は続く(self) -> None:
        """取り込めないことと、アプリが使えないことは別。"""
        from packaging_tool import data_sync

        def boom(_conn, **_kw):
            raise RuntimeError("共有フォルダに届きません")

        data_sync.auto_import = boom
        self.user_settings.save(self.config.KEY_AUTO_IMPORT, True)
        srv = self.fake_srv()
        self.start_app._start_auto_import(srv)   # 例外を投げないこと
        self.wait_idle()
        self.assertEqual(self.registry.recent()[0].state, self.jobs.STATE_FAILED)
        # **取り込めなくても入れる。** 失敗しても準備完了にする
        deadline = time.monotonic() + 5.0
        while not srv.ready and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(srv.ready)


# ==================================================================
# 実行環境の確認
# ==================================================================
class PipHintTests(unittest.TestCase):
    """入れ方の案内は、実行して結果が見えるものでなければ意味がない。"""

    def test_pythonwはpythonに読み替える(self) -> None:
        """`Start.vbs` は画面を出さないために pythonw で起動する。

        そのまま案内すると `pythonw.exe -m pip install ...` になるが、
        pythonw には画面が無いので**実行しても何も表示されない**
        (成否すら分からない)。実際にこの案内が出て詰まった。
        """
        import start_app
        saved = sys.executable
        try:
            for path, expected in (
                    (r"C:\Python\pythonw.exe", "python.exe"),
                    (r"C:\Python\pythonw3.14.exe", "python3.14.exe"),
                    (r"C:\Python\python.exe", "python.exe"),
                    ("/usr/bin/python3", "python3")):
                with self.subTest(path=path):
                    sys.executable = path
                    self.assertEqual(start_app.console_python(), expected)
        finally:
            sys.executable = saved

    def test_案内にpythonwを出さない(self) -> None:
        """実際の `check_packages()` が出す案内で確かめる。"""
        import start_app
        saved_exe, saved_pkgs = sys.executable, start_app.REQUIRED_PACKAGES
        sys.executable = r"C:\Python\pythonw.exe"
        start_app.REQUIRED_PACKAGES = (("packaging_tool_not_installed", "waitress"),)
        try:
            with self.assertRaises(start_app.StartupError) as caught:
                start_app.check_packages()
        finally:
            sys.executable, start_app.REQUIRED_PACKAGES = saved_exe, saved_pkgs

        hint = caught.exception.hint
        self.assertNotIn("pythonw", hint, "pythonw では実行しても何も出ません")
        self.assertIn("python.exe -m pip", hint)
        self.assertIn("requirements.txt", hint)


class EnvironmentCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        import start_app
        self.start_app = start_app

    def test_いまのPythonは条件を満たす(self) -> None:
        self.start_app.check_python_version()      # 例外が出ないこと

    @unittest.skipUnless(HAS_WEB, _SKIP_WEB)
    def test_必須パッケージがそろっている(self) -> None:
        self.start_app.check_packages()

    def test_足りないパッケージは入れ方まで示す(self) -> None:
        """基盤仕様書 ステップ5「次に何を確認すればよいか表示される」。"""
        original = self.start_app.REQUIRED_PACKAGES
        self.start_app.REQUIRED_PACKAGES = (("存在しないはずのモジュール", "NoSuchPkg"),)
        try:
            with self.assertRaises(self.start_app.StartupError) as ctx:
                self.start_app.check_packages()
            self.assertIn("NoSuchPkg", str(ctx.exception))
            self.assertIn("pip install", ctx.exception.hint)
        finally:
            self.start_app.REQUIRED_PACKAGES = original

    def test_エラー画面を書ける(self) -> None:
        path = self.start_app._write_error_page("試験", "ヒント", "/tmp/logs")
        self.assertTrue(path.exists())
        html = path.read_text(encoding="utf-8")
        self.assertIn("試験", html)
        self.assertIn("/tmp/logs", html)

    def test_エラー画面はHTMLを打ち消す(self) -> None:
        """例外メッセージに `<` が入っても画面が壊れないこと。"""
        path = self.start_app._write_error_page("<script>x</script>", "", "/tmp")
        self.assertNotIn("<script>x", path.read_text(encoding="utf-8"))


def _free_port() -> int:
    """いま空いているポートを1つ借りる。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(HAS_WEB, _SKIP_WEB)
class CatalogTests(unittest.TestCase):
    """部品カタログ (`/__catalog`)。

    画面を作る前に部品を確かめる見本帳。壊れたまま気づかないと、
    デザインシステムを直す拠り所が無くなる。
    """

    def setUp(self) -> None:
        from app import create_app
        app = create_app("field", token="t", port=8713)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_開ける(self) -> None:
        self.assertEqual(self.client.get("/__catalog").status_code, 200)

    def test_コントラストの検査結果が載る(self) -> None:
        html = self.client.get("/__catalog").get_data(as_text=True)
        self.assertIn("コントラストの検査", html)
        self.assertIn("--act-run", html)

    def test_配置図の見本が描かれる(self) -> None:
        """`presenters.render_json` の出力をそのままSVGにできること。"""
        html = self.client.get("/__catalog").get_data(as_text=True)
        self.assertIn("<svg", html)
        self.assertIn("幅1130", html)

    def test_見本にカット注記が出ない(self) -> None:
        """元寸と実寸を食い違わせると意味のないカット注記が出る。

        見本としておかしいので、回転だけの板にしてある。
        """
        from app.routes.catalog import _sample_plan
        for rect in _sample_plan()["boards"]:
            self.assertNotIn("カット", rect["caption"])

    def test_業務のURLとぶつからない(self) -> None:
        """カタログのURLは `__` 付きにして業務のURL空間と分けてある。

        あとで `/lot` `/selection` などを足したときに衝突しないため。
        """
        from app import create_app
        app = create_app("field", token="t", port=8713)
        catalog_rules = [str(r) for r in app.url_map.iter_rules()
                         if r.endpoint.startswith("catalog.")]
        self.assertTrue(catalog_rules, "カタログのURLが登録されていません")
        for rule in catalog_rules:
            self.assertTrue(rule.startswith("/__"), f"{rule} が __ で始まっていません")
